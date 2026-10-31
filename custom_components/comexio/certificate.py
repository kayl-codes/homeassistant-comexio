"""HTTPS to Comexio with a pinned certificate (CONF_CERTIFICATE_FINGERPRINT).

Comexio signs its certificate itself, so instead of a CA the integration pins the certificate's SHA-256
fingerprint, read from a bare TLS handshake at setup (trust on first use, see aiocomexio.tls). A Comexio
that serves no HTTPS keeps plain HTTP, and every setup of such an entry tries the switch again.

A certificate other than the pinned one is never accepted on its own: the certificate_changed repair
and the urgent push show the pinned and the new fingerprint and pin the new one only when the user confirms
it (the repair's button, or the push's).
"""

import asyncio
from dataclasses import dataclass, field
import logging
import re
from typing import Any

from aiocomexio import ComexioError, fetch_certificate_fingerprint, format_fingerprint
from aiocomexio.tls import https_address
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .api import ComexioAPI
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    DOMAIN,
    FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC,
    LOGIN_ERROR_CERTIFICATE,
    LOGIN_ERROR_CONNECTION,
    LOGIN_ERROR_REJECTED,
)
from .plan_watchdog import MOBILE_APP_SERVICE_PREFIX, NOTIFY_DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_CERTIFICATE_CHANGED = "certificate_changed"
REPAIRS_PATH = "/config/repairs"
# hass.data: entry_id -> _EntryPushes, the urgent push of each entry's changed certificate.
CERTIFICATE_PUSHES = f"{DOMAIN}_certificate_pushes"
# hass.data: entry_id -> lock running one acceptance of the entry's certificate at a time (repair and push button).
CERTIFICATE_ACCEPT_LOCKS = f"{DOMAIN}_certificate_accept_locks"

# The push's "Accept certificate" button: the action id names the entry and the certificate the push showed,
# so a tap pins that very certificate and nothing else. It comes back as a mobile_app_notification_action event.
ACCEPT_ACTION_PREFIX = "COMEXIO_ACCEPT_CERTIFICATE_"
_ACCEPT_ACTION_RE = re.compile(re.escape(ACCEPT_ACTION_PREFIX) + r"(?P<entry_id>.+)_(?P<actual>[0-9a-f]{64})")

# Outcomes of async_accept_certificate; all but ACCEPT_PINNED are the repair's abort reasons as well.
ACCEPT_PINNED = "pinned"
ACCEPT_RESTORED = "certificate_restored"
ACCEPT_CHANGED_AGAIN = "certificate_changed_again"
ACCEPT_UNREACHABLE = "cannot_connect"
ACCEPT_ENTRY_GONE = "entry_not_found"
ACCEPT_ENTRY_CHANGED = "certificate_entry_changed"
# The push button's outcomes for a push older than the repair (another certificate is up, or the pin is back)
# and for an unexpected error.
_ACCEPT_OUTDATED = "outdated_push"
_ACCEPT_FAILED = "failed"

# Push texts by HA language (English fallback); the certificate_changed placeholders are filled in.
CERTIFICATE_PUSH_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Comexio: certificate changed",
        "message": (
            "Comexio {server_id} ({host}) presents another TLS certificate than the pinned one. Home Assistant "
            "sends nothing to it until you accept the new certificate, here or in the repair. Compare it with "
            "the SHA-256 value on Comexio's admin page first.\n"
            "Pinned (SHA-256): {pinned}\n"
            "New (SHA-256): {actual}"
        ),
        "accept": "Accept certificate",
    },
    "de": {
        "title": "Comexio: Zertifikat geändert",
        "message": (
            "Comexio {server_id} ({host}) zeigt ein anderes TLS-Zertifikat als das gepinnte. Home Assistant "
            "sendet nichts mehr dorthin, bis du das neue Zertifikat übernimmst, hier oder in der Reparatur. "
            "Vergleiche es vorher mit dem SHA-256-Wert auf der Admin-Seite der Comexio.\n"
            "Gepinnt (SHA-256): {pinned}\n"
            "Neu (SHA-256): {actual}"
        ),
        "accept": "Zertifikat übernehmen",
    },
}

# States of the connection sensor (sensor.ComexioConnectionSensor), see connection_state.
CONNECTION_HTTPS = "https"
CONNECTION_HTTP = "http"
CONNECTION_CERTIFICATE_CHANGED = ISSUE_CERTIFICATE_CHANGED
CONNECTION_STATES = [CONNECTION_HTTPS, CONNECTION_HTTP, CONNECTION_CERTIFICATE_CHANGED]


def certificate_issue_id(entry_id: str) -> str:
    """Issue id of the certificate_changed repair of one config entry."""
    return f"{ISSUE_CERTIFICATE_CHANGED}_{entry_id}"


async def async_read_fingerprint(host: str) -> str | None:
    """Fingerprint of the certificate host serves over HTTPS, None if the TLS handshake fails."""
    try:
        return await fetch_certificate_fingerprint(host)
    except (ComexioError, ValueError) as err:
        _LOGGER.info("Comexio at %s serves no HTTPS: %s", host, err)
        return None


def is_valid_host(host: str) -> bool:
    """Whether host forms a URL to Comexio at all (name or address, optional port, nothing more)."""
    try:
        https_address(host)
    except ValueError:
        return False
    return True


def notify_https_enabled(hass: HomeAssistant, server_id: str, host: str, fingerprint: str) -> None:
    """Tell the user that the connection to Comexio is encrypted now, with the pinned fingerprint."""
    persistent_notification.async_create(
        hass,
        f"Home Assistant now connects to Comexio **{server_id}** ({host}) over HTTPS only.\n\n"
        f"Pinned certificate (SHA-256):\n`{format_fingerprint(fingerprint)}`\n\n"
        "It is the SHA-256 value Comexio's admin page lists under its certificate hashes. Should Comexio "
        "present another certificate later, for example after generating a new one, the integration stops "
        "and asks in a repair before it trusts that certificate.",
        title="Comexio: connection encrypted",
        notification_id=_https_notification_id(server_id),
    )


def notify_https_disabled(hass: HomeAssistant, server_id: str, host: str) -> None:
    """Tell the user that a reconfigure put the connection to Comexio back on plain HTTP."""
    persistent_notification.async_create(
        hass,
        f"Comexio **{server_id}** ({host}) served no HTTPS when the connection was reconfigured, so Home "
        "Assistant now connects to it over plain HTTP — unencrypted, including the admin login.\n\n"
        "As soon as Comexio serves HTTPS again, the next start of the integration switches back and pins "
        "the certificate it then presents.",
        title="Comexio: connection not encrypted",
        notification_id=_https_notification_id(server_id),
    )


def _https_notification_id(server_id: str) -> str:
    """One notification per server: the latest switch replaces an earlier one."""
    return f"{DOMAIN}_https_{server_id}"


def async_report_certificate_changed(hass: HomeAssistant, entry: ConfigEntry, actual: str) -> None:
    """Raise (or update) the certificate_changed repair: Comexio presents `actual` instead of the pin.

    A certificate not reported yet also goes out as an urgent push (see _certificate_push_data); the reports
    of the same certificate at every retry and poll only retry the notify services it has not reached yet.
    That state lives in memory only, so a certificate still changed after a restart of Home Assistant is
    pushed once more.
    """
    pinned = entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is None or actual == pinned or hass.config_entries.async_get_entry(entry.entry_id) is None:
        # Nothing pinned, a late report of a client still on the pin the user just replaced, or of an entry
        # removed meanwhile (its repair and push are gone with it).
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        certificate_issue_id(entry.entry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_CERTIFICATE_CHANGED,
        translation_placeholders=certificate_placeholders(entry, actual),
        data={"entry_id": entry.entry_id, "actual": actual},
    )
    pushes = _entry_pushes(hass, entry)
    if pushes.current is None or pushes.current.actual != actual:
        pushes.current = _CertificatePush(actual, _certificate_push_data(hass, entry, actual))
    push = pushes.current
    targets = [target for target in _notify_targets(entry) if target not in push.delivered]
    if targets and not push.sending:
        push.sending = True
        hass.async_create_background_task(
            _async_send_certificate_push(hass, pushes, push, targets), f"{DOMAIN} certificate changed push"
        )


def async_clear_certificate_changed(hass: HomeAssistant, entry_id: str, *, final: bool = False) -> None:
    """Drop the certificate_changed repair: Comexio presents the pinned certificate (again).

    The push is removed from the companion apps it reached as well (also of a removed entry), retried at the
    next clear where that failed or the companion app was not registered; the clear at every successful poll
    starts no task once no registered companion app is left to clear.

    final: the last clear of a removed entry. Nothing retries it, so a failure, or a companion app it cannot
    reach, is logged as a warning.
    """
    if presented_fingerprint(hass, entry_id) is not None:
        ir.async_delete_issue(hass, DOMAIN, certificate_issue_id(entry_id))
    pushes = _pushes_to_clear(hass, entry_id)
    if pushes is None:
        return
    # A send still waiting for the lock is dropped; one already sending adds to shown only when it is done.
    pushing, pushes.current = pushes.current is not None, None
    # The final one always runs: behind the lock it also catches a send a poll's clear started waiting for.
    if final or pushing or _registered_phones(hass, pushes):
        hass.async_create_background_task(
            _async_clear_certificate_push(hass, pushes, entry_id, final=final),
            f"{DOMAIN} certificate changed push clear",
        )


def async_remove_certificate_pushes(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Last clear of a removed entry; its push state goes with it (the clear holds on to it, no retry after).

    The entry is out of the registry already, so its state is set up from the entry itself: with none yet since
    Home Assistant started, a push from before the restart may still be on every companion app.
    """
    if CONF_CERTIFICATE_FINGERPRINT in entry.data:
        _entry_pushes(hass, entry)
    async_clear_certificate_changed(hass, entry.entry_id, final=True)
    hass.data.get(CERTIFICATE_PUSHES, {}).pop(entry.entry_id, None)
    hass.data.get(CERTIFICATE_ACCEPT_LOCKS, {}).pop(entry.entry_id, None)


@dataclass
class _CertificatePush:
    """The urgent push of one presented certificate: the notify services it reached, and whether a send runs."""

    actual: str
    service_data: dict[str, Any]
    delivered: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)  # failed once already: a retry's failure is logged at debug level
    sending: bool = False


@dataclass
class _EntryPushes:
    """One entry's certificate push; the lock keeps a clear behind the push it takes off the phones."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current: _CertificatePush | None = None
    # Companion apps a push (of any certificate) reached and no clear has since: the next clear goes to them.
    shown: set[str] = field(default_factory=set)
    # Of shown: the ones whose last clear failed, so a retried failure is logged at debug level.
    clear_failed: set[str] = field(default_factory=set)
    # Of shown: the ones logged as not registered, so each is logged once until it registers.
    unregistered: set[str] = field(default_factory=set)


def _entry_pushes(hass: HomeAssistant, entry: ConfigEntry) -> _EntryPushes:
    """The entry's push state; the first since Home Assistant started takes every companion app as showing one.

    The state lives in memory only, so a push sent before a restart may still be on the phones: the first
    clear goes to all of them, whether a report or a clear comes first after the start.
    """
    all_pushes: dict[str, _EntryPushes] = hass.data.setdefault(CERTIFICATE_PUSHES, {})
    if (pushes := all_pushes.get(entry.entry_id)) is None:
        pushes = all_pushes[entry.entry_id] = _EntryPushes(shown=_phones(entry))
    return pushes


def _pushes_to_clear(hass: HomeAssistant, entry_id: str) -> _EntryPushes | None:
    """The entry's push state, None for an entry without a pin (or one removed) that never had one this run."""
    if (pushes := hass.data.get(CERTIFICATE_PUSHES, {}).get(entry_id)) is not None:
        return pushes
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or CONF_CERTIFICATE_FINGERPRINT not in entry.data:
        return None
    return _entry_pushes(hass, entry)


def _notify_targets(entry: ConfigEntry) -> list[str]:
    return list(entry.options.get(CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY) or [])


def _phones(entry: ConfigEntry) -> set[str]:
    return {target for target in _notify_targets(entry) if _is_mobile_app(target)}


def _certificate_push_data(hass: HomeAssistant, entry: ConfigEntry, actual: str) -> dict[str, Any]:
    """Urgent push: the new certificate needs the user, Comexio gets nothing until then.

    Through Do Not Disturb (iOS: a critical alert, Android: the alarm stream), delivered at once; a tap
    opens the repairs, the button accepts the certificate shown (see async_handle_certificate_action; iOS
    asks to unlock the phone first). The fingerprints are the hashes of public certificates, no secret.
    """
    texts = CERTIFICATE_PUSH_TEXTS.get(hass.config.language, CERTIFICATE_PUSH_TEXTS["en"])
    data: dict[str, Any] = {
        "tag": _certificate_push_tag(entry.entry_id),
        "group": f"{DOMAIN}_{entry.data[CONF_SERVER_ID]}",
        "url": REPAIRS_PATH,
        "clickAction": REPAIRS_PATH,
        "ttl": 0,
        "priority": "high",
        "channel": "alarm_stream",
        "push": {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 1.0}},
        "actions": [
            {
                "action": accept_action_id(entry.entry_id, actual),
                "title": texts["accept"],
                "authenticationRequired": True,
            }
        ],
    }
    message = texts["message"].format(**certificate_placeholders(entry, actual))
    return {"title": texts["title"], "message": message, "data": data}


async def _async_send_certificate_push(
    hass: HomeAssistant, pushes: _EntryPushes, push: _CertificatePush, targets: list[str]
) -> None:
    try:
        async with pushes.lock:
            if pushes.current is not push:
                # Cleared, or another certificate reported, while it waited: its alarm is no news anymore.
                _LOGGER.debug("Comexio certificate push %s dropped: no longer current", format_fingerprint(push.actual))
                return
            delivered, failed = await _async_notify(hass, push.service_data, targets, push.failed)
            if delivered:
                # The only trace of a push that went out: the companion app reports nothing back.
                _LOGGER.info(
                    "Comexio certificate push %s sent to %s", format_fingerprint(push.actual), sorted(delivered)
                )
            push.delivered |= delivered
            push.failed |= failed
            pushes.shown |= {target for target in delivered if _is_mobile_app(target)}
    finally:
        push.sending = False


async def _async_clear_certificate_push(
    hass: HomeAssistant, pushes: _EntryPushes, entry_id: str, *, final: bool = False
) -> None:
    """Take the push off the phones, after a send still on its way (the lock); the failed ones stay for the next."""
    async with pushes.lock:
        # Also a phone taken off the notify option since: the push may still be on it.
        targets = _registered_phones(hass, pushes)
        if final and (unreachable := sorted(pushes.shown.difference(targets))):
            _LOGGER.warning(
                "Comexio certificate push may stay on %s: not registered when the entry was removed", unreachable
            )
        if not targets:
            return
        # A companion-app command: any other notify service would deliver it as the text "clear_notification".
        service_data = {"message": "clear_notification", "data": {"tag": _certificate_push_tag(entry_id)}}
        quiet = set() if final else pushes.clear_failed
        delivered, failed = await _async_notify(hass, service_data, targets, quiet)
        pushes.shown -= delivered
        pushes.unregistered -= delivered  # gone and back during the await
        pushes.clear_failed = ((pushes.clear_failed | failed) - delivered) & pushes.shown


async def _async_notify(
    hass: HomeAssistant, service_data: dict[str, Any], targets: list[str], quiet: set[str]
) -> tuple[set[str], set[str]]:
    """Send to each target; a failing one does not stop the others. Returns the delivered and the failed ones.

    quiet: targets whose failure was logged before, logged at debug level now: a dead notify service, retried
    at every poll, must not flood the log.
    """
    delivered: set[str] = set()
    failed: set[str] = set()
    for target in targets:
        service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
        log_failure = _LOGGER.debug if target in quiet else _LOGGER.warning
        try:
            # Bounded: a hanging notify service would hold the lock, and so the clear, too.
            async with asyncio.timeout(FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC):
                await hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
            delivered.add(target)
        except TimeoutError:
            failed.add(target)
            log_failure("Comexio certificate push to notify.%s timed out", service)
        except (HomeAssistantError, vol.Invalid) as err:
            failed.add(target)
            log_failure("Comexio certificate push to notify.%s failed: %s", service, err)
        except Exception:
            # Third-party notify platforms raise their own errors; none may skip the remaining targets.
            failed.add(target)
            log_failure("Comexio certificate push to notify.%s failed", service, exc_info=True)
    return delivered, failed


def _registered_phones(hass: HomeAssistant, pushes: _EntryPushes) -> list[str]:
    """The companion apps showing the push whose notify service is registered, the ones a clear can reach.

    One not registered (yet: right after a restart the companion app may come up after this integration, or
    the device was removed) stays for the next clear, logged once at debug level.
    """
    targets: list[str] = []
    for target in sorted(pushes.shown):
        if hass.services.has_service(NOTIFY_DOMAIN, target.removeprefix(f"{NOTIFY_DOMAIN}.")):
            targets.append(target)
            pushes.unregistered.discard(target)
        elif target not in pushes.unregistered:
            pushes.unregistered.add(target)
            _LOGGER.debug("Comexio certificate push clear waits for %s: not registered", target)
    return targets


def _is_mobile_app(target: str) -> bool:
    return target.removeprefix(f"{NOTIFY_DOMAIN}.").startswith(MOBILE_APP_SERVICE_PREFIX)


def _certificate_push_tag(entry_id: str) -> str:
    """One push per entry: a newer one replaces it on the phone, the clear removes it."""
    return f"{DOMAIN}_certificate_{entry_id}"


def accept_action_id(entry_id: str, actual: str) -> str:
    """Action id of the push's "Accept certificate" button for the certificate `actual` the push shows."""
    return f"{ACCEPT_ACTION_PREFIX}{entry_id}_{actual}"


def parse_accept_action(action: Any) -> tuple[str, str] | None:
    """(entry_id, certificate shown) of an "Accept certificate" button, None for any other action."""
    if not isinstance(action, str) or (match := _ACCEPT_ACTION_RE.fullmatch(action)) is None:
        return None
    return match["entry_id"], match["actual"]


async def async_accept_certificate(hass: HomeAssistant, entry: ConfigEntry, shown: str) -> tuple[str, str | None]:
    """Pin `shown`, the certificate the user has seen, if Comexio still presents it.

    Never pins anything the user has not seen: the certificate is read again, and a different one than shown
    is put up for comparison instead (repair and push). One acceptance runs at a time, so a second tap, or the
    repair confirmed alongside, finds the certificate pinned already and leaves it at that.

    Returns the outcome (ACCEPT_*) and the certificate Comexio presents now, None if it could not be read.
    """
    # The connection the user decided on: a reconfigure while this waits for the lock or reads counts as a change,
    # as does one that dropped the pin before (the push button looks the user up after checking it).
    host, pinned = entry.data[CONF_HOST], entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(CERTIFICATE_ACCEPT_LOCKS, {})
    async with locks.setdefault(entry.entry_id, asyncio.Lock()):
        if entry.data.get(CONF_CERTIFICATE_FINGERPRINT) == shown:
            # Accepted already (a second tap, or the repair alongside). A repair still showing it is left over.
            if presented_fingerprint(hass, entry.entry_id) == shown:
                async_clear_certificate_changed(hass, entry.entry_id)
            return ACCEPT_PINNED, shown
        if _connection_changed(entry, host, pinned, shown):
            return ACCEPT_ENTRY_CHANGED, None
        try:
            current = await fetch_certificate_fingerprint(host)
        except (ComexioError, ValueError) as err:
            _LOGGER.warning("Comexio certificate could not be read again to accept it: %s", err)
            return ACCEPT_UNREACHABLE, None
        if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            _LOGGER.warning("Comexio at %s: entry removed while its certificate was read, nothing pinned", host)
            return ACCEPT_ENTRY_GONE, None
        if _connection_changed(entry, host, pinned, shown):
            return ACCEPT_ENTRY_CHANGED, None
        if current == pinned:
            # The pinned certificate is back: nothing to accept. A loaded entry's next poll gets through on its own.
            async_clear_certificate_changed(hass, entry.entry_id)
            if entry.state is not ConfigEntryState.LOADED:
                hass.config_entries.async_schedule_reload(entry.entry_id)
            return ACCEPT_RESTORED, current
        if current != shown:
            # Yet another certificate than the one the user just compared: put that one up instead.
            async_report_certificate_changed(hass, entry, current)
            return ACCEPT_CHANGED_AGAIN, current
        _LOGGER.warning(
            "Comexio at %s: certificate %s pinned by the user, replacing %s",
            entry.data[CONF_HOST],
            format_fingerprint(current),
            format_fingerprint(entry.data[CONF_CERTIFICATE_FINGERPRINT]),  # still the pin: unchanged, checked above
        )
        # A loaded entry reloads through its update listener on the data change below.
        reload = entry.state is not ConfigEntryState.LOADED
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_FINGERPRINT: current})
        async_clear_certificate_changed(hass, entry.entry_id)  # also removes the push from the phones
        if reload:
            hass.config_entries.async_schedule_reload(entry.entry_id)
        return ACCEPT_PINNED, current


def _connection_changed(entry: ConfigEntry, host: str, pinned: str | None, shown: str) -> bool:
    """Whether the entry was reconfigured since the user accepted `shown` (host or pin differ); logs it if so.

    The user compared `shown` against that connection's pin: pinning it now would overwrite the new one's.
    No pin to compare against (HTTPS dropped already) counts as a change too.
    """
    now_host, now_pin = entry.data[CONF_HOST], entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is not None and (now_host, now_pin) == (host, pinned):
        return False
    _LOGGER.warning(
        "Comexio at %s: connection changed while accepting certificate %s, nothing pinned (now %s, %s)",
        host,
        format_fingerprint(shown),
        now_host,
        "no pin" if now_pin is None else format_fingerprint(now_pin),
    )
    return True


async def async_handle_certificate_action(hass: HomeAssistant, event: Event) -> None:
    """The push's "Accept certificate" button: pin the certificate the push showed, as the repair does.

    Listens for every entry, also one waiting for the repair (not loaded). Only an administrator's tap counts,
    as only an administrator sees the repair (any companion app can fire this event), and only while the repair
    shows the very certificate of the push. Comexio not answering, an error or a push older than the repair put
    the repair's push up again: the tap may have taken the push off the phone. Yet another certificate gets its
    own push from the repair it raises. Nothing is pushed once the repair is gone (the pinned certificate is back).
    """
    if (accept := await _accept_request(hass, event)) is None:
        return
    entry, shown = accept
    if presented_fingerprint(hass, entry.entry_id) == shown:
        try:
            outcome, _current = await async_accept_certificate(hass, entry, shown)
        except Exception:
            _LOGGER.exception("Comexio at %s: the certificate push button failed", entry.data[CONF_HOST])
            outcome = _ACCEPT_FAILED
    else:
        outcome = _ACCEPT_OUTDATED
    _LOGGER.info("Comexio at %s: certificate push button: %s", entry.data[CONF_HOST], outcome)
    if (
        outcome in (ACCEPT_UNREACHABLE, _ACCEPT_FAILED, _ACCEPT_OUTDATED)
        and (presented := presented_fingerprint(hass, entry.entry_id)) is not None
    ):
        if (pushes := hass.data.get(CERTIFICATE_PUSHES, {}).get(entry.entry_id)) is not None:
            pushes.current = None  # sent afresh to every target, also the ones it reached before
        async_report_certificate_changed(hass, entry, presented)


async def _accept_request(hass: HomeAssistant, event: Event) -> tuple[ConfigEntry, str] | None:
    """(entry, certificate shown) of an administrator's "Accept certificate" tap, None for any other event."""
    if (parsed := parse_accept_action(event.data.get("action"))) is None:
        return None
    entry_id, shown = parsed
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or CONF_CERTIFICATE_FINGERPRINT not in entry.data:
        _LOGGER.warning("Comexio certificate push answered for an entry that is gone or has no pin: %s", entry_id)
        return None
    user_id = event.context.user_id
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_admin:
        _LOGGER.warning(
            "Comexio at %s: certificate push button ignored, only an administrator may accept a certificate (user %s)",
            entry.data[CONF_HOST],
            user.name if user else user_id,
        )
        return None
    return entry, shown


def certificate_placeholders(entry: ConfigEntry, actual: str) -> dict[str, str]:
    """Placeholders of the certificate_changed repair: server, host, pinned and presented fingerprint."""
    return {
        "server_id": str(entry.data[CONF_SERVER_ID]),
        "host": str(entry.data[CONF_HOST]),
        "pinned": format_fingerprint(entry.data[CONF_CERTIFICATE_FINGERPRINT]),
        "actual": format_fingerprint(actual),
    }


def connection_placeholders(data: Any) -> dict[str, str]:
    """Placeholders of the reconfigure form's connection lines: protocol and the pinned fingerprint.

    The fingerprint is the hash of Comexio's public certificate, shown to anyone who connects — no secret.
    """
    pinned = data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is None:
        return {"protocol": "HTTP", "fingerprint": "—"}
    return {"protocol": "HTTPS", "fingerprint": format_fingerprint(pinned)}


def presented_fingerprint(hass: HomeAssistant, entry_id: str) -> str | None:
    """The certificate Comexio presents instead of the pinned one; None while no certificate_changed repair is open."""
    issue = ir.async_get(hass).async_get_issue(DOMAIN, certificate_issue_id(entry_id))
    if issue is None or issue.data is None:
        return None
    return issue.data.get("actual")


def connection_state(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """CONNECTION_HTTP without a pin, CONNECTION_CERTIFICATE_CHANGED while the repair is open, else CONNECTION_HTTPS."""
    if CONF_CERTIFICATE_FINGERPRINT not in entry.data:
        return CONNECTION_HTTP
    if presented_fingerprint(hass, entry.entry_id) is not None:
        return CONNECTION_CERTIFICATE_CHANGED
    return CONNECTION_HTTPS


def _new_api(hass: HomeAssistant, data: Any, fingerprint: str | None) -> ComexioAPI:
    return ComexioAPI(
        hass,
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        api_user=data.get(CONF_API_USERNAME),
        api_pass=data.get(CONF_API_PASSWORD),
        fingerprint=fingerprint,
    )


async def async_create_api(hass: HomeAssistant, entry: ConfigEntry) -> ComexioAPI:
    """The entry's ComexioAPI; an entry still on plain HTTP is switched to HTTPS when Comexio serves it.

    The switch pins the certificate only after a login over HTTPS worked with it — then the entry keeps
    the fingerprint and the user gets a notification. Otherwise the entry stays on HTTP for this setup
    (the next one tries again) and the API returned is not logged in, as before. Two failures send nothing
    over plain HTTP: a login that met a certificate other than the one just read — also the one aiohttp
    reports as a connection error — retries the setup (ConfigEntryNotReady), which reads it afresh; and
    credentials Comexio refused over HTTPS are not sent again unencrypted (ConfigEntryAuthFailed).
    Only a plain connection failure keeps the entry on HTTP: a Comexio whose HTTPS answers the handshake
    but not the login would otherwise never set up again, and the entry encrypted nothing before.
    """
    if CONF_CERTIFICATE_FINGERPRINT in entry.data:
        return _new_api(hass, entry.data, entry.data[CONF_CERTIFICATE_FINGERPRINT])
    host = entry.data[CONF_HOST]
    fingerprint = await async_read_fingerprint(host)
    if fingerprint is None:
        return _new_api(hass, entry.data, None)
    api = _new_api(hass, entry.data, fingerprint)
    if await api.login():
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_FINGERPRINT: fingerprint})
        _LOGGER.info("Comexio at %s: switched to HTTPS, certificate %s pinned", host, format_fingerprint(fingerprint))
        notify_https_enabled(hass, str(entry.data[CONF_SERVER_ID]), host, fingerprint)
        return api
    if api.last_login_error == LOGIN_ERROR_CERTIFICATE or (
        api.last_login_error == LOGIN_ERROR_CONNECTION and await async_replaced_fingerprint(api) is not None
    ):
        api.close()
        raise ConfigEntryNotReady(f"Comexio at {host} changed its certificate during the switch to HTTPS")
    if api.last_login_error == LOGIN_ERROR_REJECTED:
        api.close()
        _LOGGER.error("Comexio login rejected for %s — check credentials", host)
        raise ConfigEntryAuthFailed
    _LOGGER.warning(
        "Comexio at %s serves HTTPS, but the login over it failed (%s) — staying on plain HTTP for now",
        host,
        api.last_login_error,
    )
    api.close()
    return _new_api(hass, entry.data, None)


async def async_replaced_fingerprint(api: ComexioAPI) -> str | None:
    """After a failed login: the certificate Comexio presents instead of the pinned one, None if that is no cause.

    A login refused for the certificate names it; a connection error is checked with a fresh handshake,
    since aiohttp reports a foreign certificate on a host name with several addresses as one.
    """
    if api.certificate_mismatch is not None:
        return api.certificate_mismatch.actual
    return await api.fetch_replaced_fingerprint()
