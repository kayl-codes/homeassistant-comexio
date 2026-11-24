"""Webhook ids of their own for every Web-IO command (dg).

HA's webhook endpoint takes no authentication, so the id in its path is the only secret. One id for all
commands would give away everything once it has been read from a single request. Instead every command POSTs
to an id of its own, derived by HMAC from a secret of the entry and the command's key (its class and its
Comexio address). A request that names an id and a payload that do not belong together is dropped, so an id
read from one command lets nobody forge the value of another.

The secret comes from ``webhook.async_generate_id()`` and lives in a store of the entry (never in the config
entry, which diagnostics and backups show). The ids are derived, not stored: a command the sync creates later
needs no bookkeeping, and the store stays small.

The old id (``comexio_<server_id>``) stays registered while commands still use it. ``migrated`` lists the
commands already rewritten to their own path; once all are, and Comexio has restarted since the last write
(Comexio only sends a changed command after a restart), the old id is switched off for good.

Not every write is such a change. A command the sync creates, and a command that only gets another name (its path
and protocol stay), push at once, without a Comexio restart (measured live 2026-11-22: a renamed marker and a new
marker pushed within seconds of the write). ``mark_written`` leaves the stamp of ``last_write`` alone for these two,
so the sync of a rename or a new marker raises no "restart needed" notice. Every other write (the nightly rewrite of a
path or protocol, a type fix, a range correction, an uploaded class) still moves it.

``https_written`` lists the commands this entry last wrote with the protocol HTTPS (#165). Comexio's scrape does
not show a command's protocol, so the ledger is how the integration knows it without reading every command's form:
a command that is not in it was written with HTTP, as every command was before HTTPS was supported. It is the
migration's second criterion, next to the path: a command whose recorded protocol is not the one HA is reached
with today is rewritten, and Comexio has to restart for that, too.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import hmac
import logging
import re
from time import monotonic
from typing import Any

from aiocomexio.const import WebioClass
from aiocomexio.onewire import OneWireSensor
from aiocomexio.webio import WEBIO_PROTOCOL_HTTPS
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, WEBHOOK_REFUSAL_LOG_INTERVAL_SEC

_LOGGER = logging.getLogger(__name__)

WEBHOOK_PATH_PREFIX = "/api/webhook/"
# 128 bit of the HMAC: as strong as HA's own generated ids, and a path HA's router takes without fuss.
_ID_HEX_LENGTH = 32
_SAVE_DELAY_SEC = 2
_KEY_SECRET = "secret"  # nosec B105 - a storage key, not a password
_KEY_MIGRATED = "migrated"
_KEY_LEGACY = "legacy"
_KEY_WRITTEN = "last_write"
_KEY_RESTART_FREE = "restart_free"
_KEY_HOST = "host"
_KEY_HOST_CHANGED = "host_changed"
_KEY_HTTPS = "https_written"
_KEY_PROTOCOL_WRITE = "protocol_write"
_KEY_PROTOCOL_MOVED = "protocol_moved"


def legacy_webhook_id(server_id: str) -> str:
    """The one id every command used before they got their own."""
    return f"comexio_{server_id}"


def _stored_names(stored: Any) -> set[str]:
    """The command names of a stored list; anything that is no list of strings (a hand-edited store) is dropped."""
    return {name for name in stored if isinstance(name, str)} if isinstance(stored, list) else set()


def _stored_stamp(stored: Any) -> datetime | None:
    """A stored time stamp, aware; None when it is missing or unreadable.

    as_utc: a naive stamp (a hand-edited store) would make the comparison with the aware boot time raise.
    """
    try:
        return dt_util.as_utc(datetime.fromisoformat(stored)) if isinstance(stored, str) else None
    except ValueError:
        return None


def _part(value: Any) -> str:
    """One component of a command key: its colons and percent signs escaped, so two addresses never share a key."""
    return str(value).replace("%", "%25").replace(":", "%3A")


def command_key(webio_class: WebioClass, item: Mapping[str, Any] | OneWireSensor) -> str:
    """The key of the Web-IO command of a parsed item: its class plus the address Comexio pushes it under.

    Must agree with payload_key for the payload that command sends (aiocomexio.webio). A 1-Wire sensor's command
    pushes the sensor's address as ``id``, not its config id; the sensor comes as aiocomexio's OneWireSensor (the
    Web-IO builders) or as its data item (``address`` key, see api.onewire_item).
    """
    if isinstance(item, OneWireSensor):
        return f"{WebioClass.ONEWIRE}:{_part(item.address)}"
    if webio_class == WebioClass.ONEWIRE:
        return f"{WebioClass.ONEWIRE}:{_part(item['address'])}"
    if webio_class == WebioClass.IO:
        return f"io:{_part(item['ext_name'])}:{_part(item['identifier'])}"
    return f"{webio_class}:{_part(item['id'])}"


# Payload types whose command key is "<type>:<id>"; any other type with an id is a marker's.
_ID_KEYED_PAYLOAD_TYPES = frozenset({WebioClass.QUICKBUS, WebioClass.KNX, WebioClass.ONEWIRE})
# Payload types whose commands carried their own id from the start: none of them ever posted to the old id, so a
# push of theirs on it is forged even while the old id is on for the commands not migrated yet.
_OWN_ID_ONLY_PAYLOAD_TYPES = frozenset({WebioClass.ONEWIRE})


_IO_ADDRESS_FIELDS = ("ext", "io")


def _has_malformed_field(data: Mapping[str, Any]) -> bool:
    """True for a "type", or an IO's "ext"/"io", that is neither absent (or null) nor a string.

    A JSON array or object as type would not even hash, and the IO update lowercases ext and io.
    """
    kind = data.get("type")
    if kind is None:
        return False
    if not isinstance(kind, str):
        return True
    return kind == WebioClass.IO and not all(isinstance(data.get(field), str | None) for field in _IO_ADDRESS_FIELDS)


def payload_key(data: Mapping[str, Any]) -> str | None:
    """The command key a webhook payload names, or None if it names none (a missing address, a malformed field)."""
    if _has_malformed_field(data):
        return None
    kind = data.get("type")
    if kind == WebioClass.IO:
        ext, io_id = data.get("ext"), data.get("io")
        return f"io:{_part(ext)}:{_part(io_id)}" if ext and io_id else None
    item_id = data.get("id")
    if item_id is None:
        return None
    return f"{kind if kind in _ID_KEYED_PAYLOAD_TYPES else WebioClass.MARKER}:{_part(item_id)}"


def derive_webhook_id(secret: str, key: str) -> str:
    """The webhook id of the command with this key."""
    return hmac.new(secret.encode(), key.encode(), sha256).hexdigest()[:_ID_HEX_LENGTH]


def command_path(secret: str, webio_class: WebioClass, item: Mapping[str, Any] | OneWireSensor) -> str:
    """The URL path a command POSTs to; the callable aiocomexio's builders take as their webhook path."""
    return f"{WEBHOOK_PATH_PREFIX}{derive_webhook_id(secret, command_key(webio_class, item))}"


_ID_IN_PATH_RE = re.compile(rf"({re.escape(WEBHOOK_PATH_PREFIX)})[0-9a-f]{{{_ID_HEX_LENGTH}}}")
WEBHOOK_ID_REDACTED = "<id of this command>"


def redact_webhook_ids(text: str) -> str:
    """The text with the id of every per-command webhook path replaced.

    For output that other HA users can read (a persistent notification): the id is the secret of its command, and
    whoever reads it can forge that command's value.
    """
    return _ID_IN_PATH_RE.sub(rf"\1{WEBHOOK_ID_REDACTED}", text)


def webhook_security_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The store of an entry's secret and migration ledger, keyed by the entry id like the saved config."""
    return Store(hass, 1, f"{DOMAIN}_webhook_security_{entry_id}")


class WebhookSecurity:
    """The secret, the migration ledger and the legacy switch of one entry, persisted in a store."""

    def __init__(self, hass: HomeAssistant, entry_id: str, server_id: str, host: str = "") -> None:
        self._store: Store = webhook_security_store(hass, entry_id)
        self.server_id = server_id
        self.host = host
        self.secret = ""  # nosec B105 - filled by async_load
        self.legacy_enabled = True
        # True once a load found the ledger recorded for another host: the old id is on again for that reason, and
        # no poll of the new host may switch it off on the strength of its own empty device list. Persisted, so a
        # restart before the first scraped poll does not forget it; it stays set, as it only matters while the entry
        # has no saved config (see ComexioCoordinator._async_settle_legacy_webhook_id).
        self.host_changed = False
        self.migrated: set[str] = set()
        # Names of the commands last written with HTTPS (see the module docstring); every other one carries HTTP.
        self.https_written: set[str] = set()
        self.last_write: datetime | None = None
        # When a command that already had its own path was last rewritten to another protocol (#165), until a run has
        # read every command back after a Comexio restart: the old id may be off by then, so this is what keeps the
        # nightly run (restart request, read-back) going.
        self.protocol_write: datetime | None = None
        # The commands this entry rewrote from HTTPS to HTTP while a protocol move waits for its read-back: only for
        # these, a form that shows no protocol proves nothing (protocol_unproven). Ends with protocol_write.
        self.protocol_moved: set[str] = set()
        # kind of refusal -> (time of its last log line, refusals since then that were not logged)
        self._refusal_log: dict[str, tuple[float, int]] = {}

    def warn_refused(self, kind: str, message: str, *args: Any) -> None:
        """Log a refused request, one line per kind and WEBHOOK_REFUSAL_LOG_INTERVAL_SEC.

        The endpoint takes no authentication and the old id is guessable, so anybody who reaches HA can send refused
        requests as fast as they like; the rest is counted and named in the next line (a flood that ends inside the
        interval is named by no line, the skipped ones stay readable at debug level). Never pass the payload.
        """
        now = monotonic()
        last, skipped = self._refusal_log.get(kind, (None, 0))
        if last is not None and now - last < WEBHOOK_REFUSAL_LOG_INTERVAL_SEC:
            self._refusal_log[kind] = (last, skipped + 1)
            _LOGGER.debug(message, *args)
            return
        self._refusal_log[kind] = (now, 0)
        if skipped:
            message += f" ({skipped} more of this kind since the last message were not logged)"
        _LOGGER.warning(message, *args)

    async def async_load(self) -> None:
        """Read the store, creating the secret on first use."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict) or not isinstance(stored.get(_KEY_SECRET), str) or not stored[_KEY_SECRET]:
            if stored is not None:
                # Every command already rewritten POSTs to an id of the lost secret and is refused until the
                # migration (its ledger is reset with the secret) rewrites it again, so this must not pass quietly.
                _LOGGER.error(
                    "[%s] The webhook secret store is unreadable; a new secret was generated. Web-IO commands "
                    "already moved to their own webhook id are refused until the nightly migration rewrites them",
                    self.server_id,
                )
                # The recorded host change is no part of the secret: an entry that has not polled yet must not
                # lose it with the ledger (the new host's empty device list would switch the old id off). A host
                # that differs from the stored one counts the same, as in the intact ledger (_async_adopt_host).
                self.host_changed = self._host_change_in_lost_ledger(stored)
            self.secret = webhook.async_generate_id()
            await self._async_save()
            return
        self.secret = stored[_KEY_SECRET]
        self.legacy_enabled = bool(stored.get(_KEY_LEGACY, True))
        self.host_changed = bool(stored.get(_KEY_HOST_CHANGED, False))
        self.migrated = _stored_names(stored.get(_KEY_MIGRATED))
        self.https_written = _stored_names(stored.get(_KEY_HTTPS))
        self.last_write = _stored_stamp(stored.get(_KEY_WRITTEN))
        self.protocol_write = _stored_stamp(stored.get(_KEY_PROTOCOL_WRITE))
        self.protocol_moved = _stored_names(stored.get(_KEY_PROTOCOL_MOVED))
        if self.migrated and self.last_write is None and stored.get(_KEY_RESTART_FREE) is not True:
            # Commands were rewritten but their stamp is lost: count the write as now, so the old id is only
            # switched off after a Comexio restart that happens from here on. Not when the ledger says that no
            # write of it needed a restart (restart_free): there is no stamp to lose then.
            self.last_write = dt_util.utcnow()
        await self._async_adopt_host(stored.get(_KEY_HOST))

    def _host_change_in_lost_ledger(self, stored: Any) -> bool:
        """Whether a ledger without a usable secret still tells that the host changed (recorded, or another host)."""
        if not isinstance(stored, dict):
            return False
        stored_host = stored.get(_KEY_HOST)
        return stored.get(_KEY_HOST_CHANGED) is True or (
            bool(self.host) and stored_host is not None and stored_host != self.host
        )

    async def _async_adopt_host(self, stored_host: Any) -> None:
        """Bind the ledger to the host it was written for; another host may still hold commands on the old id.

        Without a stored host (a ledger from before it was recorded) the current one is taken over unchanged. After a
        change the old id is on again and counts the write as now: the check reads every migrated command back from
        the new host, sends the deviating ones through the rewrite again and waits for a restart of that server.
        """
        if not self.host:
            # No host to compare (the entry has none): keep what is stored, so the next save does not drop it.
            self.host = stored_host if isinstance(stored_host, str) else ""
            return
        if stored_host == self.host:
            return
        if stored_host is not None:
            # Also with an empty ledger: the new host may hold commands that still POST to the old id.
            _LOGGER.warning(
                "[%s] The Comexio host changed since the webhook ids were recorded: the old webhook id is switched "
                "on again until every command of the new host has its own",
                self.server_id,
            )
            self.legacy_enabled = True
            self.host_changed = True
            if self.migrated:
                self.last_write = dt_util.utcnow()
        await self._async_save()

    def _data(self) -> dict[str, Any]:
        return {
            _KEY_SECRET: self.secret,
            _KEY_LEGACY: self.legacy_enabled,
            _KEY_MIGRATED: sorted(self.migrated),
            _KEY_HTTPS: sorted(self.https_written),
            _KEY_WRITTEN: self.last_write.isoformat() if self.last_write else None,
            # Names are in the ledger but no write of them needs a restart: a missing stamp is meant, not lost.
            _KEY_RESTART_FREE: self.last_write is None and bool(self.migrated),
            _KEY_PROTOCOL_WRITE: self.protocol_write.isoformat() if self.protocol_write else None,
            _KEY_PROTOCOL_MOVED: sorted(self.protocol_moved),
            _KEY_HOST: self.host or None,
            _KEY_HOST_CHANGED: self.host_changed,
        }

    async def _async_save(self) -> None:
        await self._store.async_save(self._data())

    async def async_save(self) -> None:
        """Persist the ledger and the legacy switch."""
        await self._async_save()

    @callback
    def schedule_save(self) -> None:
        """Persist soon (a sync writes commands outside the nightly run, and a lost last_write could switch the old
        id off before Comexio has restarted)."""
        self._store.async_delay_save(self._data, _SAVE_DELAY_SEC)

    def path_for(self, webio_class: WebioClass, item: Mapping[str, Any] | OneWireSensor) -> str:
        return command_path(self.secret, webio_class, item)

    def mark_written(
        self,
        name: str,
        now: datetime,
        *,
        https: bool = False,
        created: bool = False,
        replaces: str | None = None,
        type_changed: bool = False,
        contested: bool = False,
    ) -> bool:
        """A command of this name now carries its own path, and the given protocol, in Comexio's configuration.

        created: the command is new in Comexio. replaces: the name it had before a rename; its protocol state
        (HTTPS record, pending move proof) moves to the new name, whatever kind of rename. A new command and a plain
        rename leave ``last_write`` alone, because Comexio pushes such a command at once: a new command has nothing
        to restart, and a rename keeps the path
        and the protocol the ledger already records for the old name. type_changed: the rename saves a new body
        too, which is not verified to push at once, so it is a rewrite. A rename of a command the ledger does not
        know is a rewrite like any other, and so is every write that changes the protocol of a command the ledger
        knows (also a created one that reuses a name, or a rename: the protocol is compared with the old name's).
        contested: the rename is part of an exchange of names within one sync (two commands swapping, or A to B
        together with B to C): the writes run one after the other, so the state of either name may belong to the
        other command. Nothing is moved or retired then; both names stay known, both owe the proof where either may
        have carried HTTPS, and the write is a rewrite (``_mark_contested``). Returns whether ``last_write`` moved.
        """
        if contested and replaces is not None and replaces != name:
            return self._mark_contested(name, replaces, now, https)
        renamed = replaces is not None and replaces != name and replaces in self.migrated
        origin = replaces if renamed else name
        protocol_changed = self._record_write(name, origin, now, https)
        if replaces is not None and replaces != name:
            self._retire_protocol_state(replaces, name, https=https)
        if not protocol_changed and (created or (renamed and not type_changed)):
            return False
        self.last_write = now
        return True

    def _mark_contested(self, name: str, old: str, now: datetime, https: bool) -> bool:
        """Book a rename within an exchange of names: a superset of what either command may owe, never less.

        A name in ``protocol_moved`` counts as one that may have carried HTTPS: an earlier write of the same exchange
        may already have taken its HTTPS record away.
        """
        names = (name, old)
        if any(n in self.migrated and self.carries_https(n) != https for n in names):
            self.protocol_write = now
        if not https and any(self.carries_https(n) or n in self.protocol_moved for n in names):
            self.protocol_moved.update(names)
            self.protocol_write = now
        self.migrated.update(names)
        # The old name's state stays: in a swap the other command's write has just set it. The head of a chain leaves
        # its name behind in ``migrated`` (and its HTTPS record, if it had one) although no command carries it; a
        # later command of that name meets it, which at worst costs a proof or a restart notice it did not need
        # (a new command whose protocol differs from the head's) or takes it for written with HTTPS (HTTPS target,
        # and only a command created by hand under the head's old name).
        if https:
            self.protocol_moved.discard(name)
            self.https_written.add(name)
        else:
            self.https_written.discard(name)
        self.last_write = now
        return True

    def _record_write(self, name: str, origin: str, now: datetime, https: bool) -> bool:
        """Book a write in the per-command state; True when it changed the protocol of a command the ledger knows.

        origin is the name the ledger knew the command by before the write (the old name of a rename, else name).
        """
        protocol_changed = origin in self.migrated and self.carries_https(origin) != https
        if protocol_changed:
            self.protocol_write = now
        if not https and self.carries_https(origin):
            # It left HTTPS: the read-back has to prove that (protocol_unproven), and a restart has to come first.
            self.protocol_moved.add(name)
            self.protocol_write = now
        self.migrated.add(name)
        if https:
            self.protocol_moved.discard(name)  # back on HTTPS: a form without a protocol is judged by the target again
            self.https_written.add(name)
        else:
            self.https_written.discard(name)
        return protocol_changed

    def _retire_protocol_state(self, old: str, new: str, *, https: bool) -> None:
        """A command gave up its name: its protocol state follows it, and a later command of that name meets none.

        A move to HTTP that still waits for its proof stays due under the new name. The old name leaves ``migrated``
        too: a later command that takes the name over is a new one, not the known command it would be taken for
        (an HTTPS one would otherwise look like a protocol change and ask for a restart it does not need). Renames
        that cross each other in one sync never get here (``_mark_contested`` keeps both names).
        """
        if old in self.protocol_moved:
            self.protocol_moved.discard(old)
            if not https:
                self.protocol_moved.add(new)
        self.https_written.discard(old)
        self.migrated.discard(old)

    def end_protocol_move(self) -> None:
        """Every command is read back right: nothing waits for a restart or a proof any more."""
        self.protocol_write = None
        self.protocol_moved.clear()

    def carries_https(self, name: str) -> bool:
        """Whether the command of this name was last written with HTTPS (any other one carries HTTP)."""
        return name in self.https_written

    def accepts(self, webhook_id: str, data: Mapping[str, Any]) -> bool:
        """Whether a request on webhook_id may deliver this payload.

        The id of a command only carries that command's value; the old id carries any while it is enabled, except
        the kinds whose commands never posted to it. A payload that names another server, or whose type (or IO
        address) is no string, is refused either way.
        """
        if _has_malformed_field(data):
            return False
        server = data.get("server")
        if server is not None and server != self.server_id:
            # Only a cut of the server name, never the payload: the sender is anybody who can reach the endpoint.
            self.warn_refused("foreign-server", "[%s] Webhook payload for server %.40r dropped", self.server_id, server)
            return False
        if _same(webhook_id, legacy_webhook_id(self.server_id)):
            return self.legacy_enabled and data.get("type") not in _OWN_ID_ONLY_PAYLOAD_TYPES
        key = payload_key(data)
        return key is not None and _same(webhook_id, derive_webhook_id(self.secret, key))


_SERVER_FIELD_RE = re.compile(r'server="(?:[^"\\]|\\.)*"')


def command_deviation(target: Mapping[str, Any], parameter: str, data: str) -> str | None:
    """Why a command as Comexio holds it (its path and Lua body) is not the target, or None when it is.

    The path must be the target's own, and the body must carry the target's ``server="..."`` field: only then does
    the command push to the id the handler expects, with a payload the handler accepts. The text is compared the
    way aiocomexio hands it over (surrounding blanks and the form's line-break handling aside).
    """
    if parameter.strip() != target.get("Parameter"):
        # No ids in the reason: it is logged, and a webhook id is the secret of its command.
        return "path is not the command's own webhook path"
    wanted = _SERVER_FIELD_RE.search(str(target.get("Data", "")))
    if wanted and len(re.findall(rf"(?<=[\s,{{]){re.escape(wanted.group(0))}", data)) != 1:
        return f"body without exactly one {wanted.group(0)}"
    return None


def protocol_deviation(target: Mapping[str, Any], protocol: int | None) -> str | None:
    """Why the protocol of a command as Comexio holds it is not the target's, or None when it is (or is unknown)."""
    if protocol is None or protocol == target.get("Protocol"):
        return None
    return "protocol is not the one Home Assistant is reached with"


def protocol_unproven(target: Mapping[str, Any], protocol: int | None, *, moving: bool = False) -> bool:
    """True when a form that shows no protocol leaves the protocol of a command unproven.

    A command that was HTTP before HTTPS was supported is not blocked by a form without the field (that is how it
    always was), but nothing proves a command is HTTPS then: it counts as unreadable, not as deviating, so it is
    not rewritten over and over. The same holds for a move to HTTP (``moving``: a protocol move waits for its
    read-back): without the field nothing proves the command left HTTPS.
    """
    return protocol is None and (moving or target.get("Protocol") == WEBIO_PROTOCOL_HTTPS)


def _same(received: str, expected: str) -> bool:
    """Constant-time comparison; bytes, because compare_digest refuses a str that is not ASCII."""
    return hmac.compare_digest(received.encode(), expected.encode())
