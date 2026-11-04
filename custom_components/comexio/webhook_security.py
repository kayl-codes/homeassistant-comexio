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
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import hmac
import logging
from typing import Any

from aiocomexio.const import WebioClass
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

WEBHOOK_PATH_PREFIX = "/api/webhook/"
# 128 bit of the HMAC: as strong as HA's own generated ids, and a path HA's router takes without fuss.
_ID_HEX_LENGTH = 32
_KEY_SECRET = "secret"  # nosec B105 - a storage key, not a password
_KEY_MIGRATED = "migrated"
_KEY_LEGACY = "legacy"
_KEY_WRITTEN = "last_write"


def legacy_webhook_id(server_id: str) -> str:
    """The one id every command used before they got their own."""
    return f"comexio_{server_id}"


def command_key(webio_class: WebioClass, item: Mapping[str, Any]) -> str:
    """The key of the Web-IO command of a parsed item: its class plus the address Comexio pushes it under.

    Must agree with payload_key for the payload that command sends (aiocomexio.webio).
    """
    if webio_class == WebioClass.IO:
        return f"io:{item['ext_name']}:{item['identifier']}"
    return f"{webio_class}:{item['id']}"


def payload_key(data: Mapping[str, Any]) -> str | None:
    """The command key a webhook payload names, or None if it names none (a missing address field)."""
    kind = data.get("type")
    if kind == WebioClass.IO:
        ext, io_id = data.get("ext"), data.get("io")
        return f"io:{ext}:{io_id}" if ext and io_id else None
    item_id = data.get("id")
    if item_id is None:
        return None
    return f"{kind if kind in (WebioClass.QUICKBUS, WebioClass.KNX) else WebioClass.MARKER}:{item_id}"


def derive_webhook_id(secret: str, key: str) -> str:
    """The webhook id of the command with this key."""
    return hmac.new(secret.encode(), key.encode(), sha256).hexdigest()[:_ID_HEX_LENGTH]


def command_path(secret: str, webio_class: WebioClass, item: Mapping[str, Any]) -> str:
    """The URL path a command POSTs to; the callable aiocomexio's builders take as their webhook path."""
    return f"{WEBHOOK_PATH_PREFIX}{derive_webhook_id(secret, command_key(webio_class, item))}"


def webhook_security_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The store of an entry's secret and migration ledger, keyed by the entry id like the saved config."""
    return Store(hass, 1, f"{DOMAIN}_webhook_security_{entry_id}")


class WebhookSecurity:
    """The secret, the migration ledger and the legacy switch of one entry, persisted in a store."""

    def __init__(self, hass: HomeAssistant, entry_id: str, server_id: str) -> None:
        self._store: Store = webhook_security_store(hass, entry_id)
        self.server_id = server_id
        self.secret = ""  # nosec B105 - filled by async_load
        self.legacy_enabled = True
        self.migrated: set[str] = set()
        self.last_write: datetime | None = None

    async def async_load(self) -> None:
        """Read the store, creating the secret on first use."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict) or not isinstance(stored.get(_KEY_SECRET), str) or not stored[_KEY_SECRET]:
            self.secret = webhook.async_generate_id()
            await self._async_save()
            return
        self.secret = stored[_KEY_SECRET]
        self.legacy_enabled = bool(stored.get(_KEY_LEGACY, True))
        migrated = stored.get(_KEY_MIGRATED)
        self.migrated = {name for name in migrated if isinstance(name, str)} if isinstance(migrated, list) else set()
        written = stored.get(_KEY_WRITTEN)
        try:
            self.last_write = datetime.fromisoformat(written) if isinstance(written, str) else None
        except ValueError:
            self.last_write = None

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                _KEY_SECRET: self.secret,
                _KEY_LEGACY: self.legacy_enabled,
                _KEY_MIGRATED: sorted(self.migrated),
                _KEY_WRITTEN: self.last_write.isoformat() if self.last_write else None,
            }
        )

    async def async_save(self) -> None:
        """Persist the ledger and the legacy switch."""
        await self._async_save()

    def path_for(self, webio_class: WebioClass, item: Mapping[str, Any]) -> str:
        return command_path(self.secret, webio_class, item)

    def mark_written(self, name: str, now: datetime) -> None:
        """A command of this name now carries its own path in Comexio's configuration."""
        self.migrated.add(name)
        self.last_write = now

    def accepts(self, webhook_id: str, data: Mapping[str, Any]) -> bool:
        """Whether a request on webhook_id may deliver this payload.

        The id of a command only carries that command's value; the old id carries any while it is enabled.
        A payload that names another server is refused either way.
        """
        server = data.get("server")
        if server is not None and server != self.server_id:
            _LOGGER.warning("[%s] Webhook payload for server %r dropped: %s", self.server_id, server, _excerpt(data))
            return False
        if self.legacy_enabled and _same(webhook_id, legacy_webhook_id(self.server_id)):
            return True
        key = payload_key(data)
        return key is not None and _same(webhook_id, derive_webhook_id(self.secret, key))


def _same(received: str, expected: str) -> bool:
    """Constant-time comparison; bytes, because compare_digest refuses a str that is not ASCII."""
    return hmac.compare_digest(received.encode(), expected.encode())


def _excerpt(data: Mapping[str, Any]) -> str:
    return str(dict(data))[:200]
