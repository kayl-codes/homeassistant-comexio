"""HTTPS to Comexio with a pinned certificate (CONF_CERTIFICATE_FINGERPRINT).

Comexio signs its certificate itself, so instead of a CA the integration pins the certificate's SHA-256
fingerprint, read from a bare TLS handshake at setup (trust on first use, see aiocomexio.tls). A Comexio
that serves no HTTPS keeps plain HTTP, and every setup of such an entry tries the switch again.

A certificate other than the pinned one is never accepted on its own: the certificate_changed repair
shows the pinned and the new fingerprint and pins the new one only when the user confirms it.
"""

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

from aiocomexio import ComexioError, fetch_certificate_fingerprint, format_fingerprint
from aiocomexio.tls import https_address
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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

# Push texts by HA language (English fallback); the certificate_changed placeholders are filled in.
CERTIFICATE_PUSH_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Comexio: certificate changed",
        "message": (
            "Comexio {server_id} ({host}) presents another TLS certificate than the pinned one. Home Assistant "
            "sends nothing to it until you check the new certificate in the repair.\n"
            "New (SHA-256): {actual}"
        ),
    },
    "de": {
        "title": "Comexio: Zertifikat geändert",
        "message": (
            "Comexio {server_id} ({host}) zeigt ein anderes TLS-Zertifikat als das gepinnte. Home Assistant "
            "sendet nichts mehr dorthin, bis du das neue Zertifikat in der Reparatur prüfst.\n"
            "Neu (SHA-256): {actual}"
        ),
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


def async_clear_certificate_changed(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the certificate_changed repair: Comexio presents the pinned certificate (again).

    The push is removed from the companion apps it reached as well (also of a removed entry), retried at the
    next clear where that failed or the companion app was not registered; the clear at every successful poll
    starts no task once no registered companion app is left to clear.
    """
    if presented_fingerprint(hass, entry_id) is not None:
        ir.async_delete_issue(hass, DOMAIN, certificate_issue_id(entry_id))
    pushes = _pushes_to_clear(hass, entry_id)
    if pushes is None:
        return
    # A send still waiting for the lock is dropped; one already sending adds to shown only when it is done.
    pushing, pushes.current = pushes.current is not None, None
    if pushing or _registered_phones(hass, pushes):
        hass.async_create_background_task(
            _async_clear_certificate_push(hass, pushes, entry_id), f"{DOMAIN} certificate changed push clear"
        )


def async_remove_certificate_pushes(hass: HomeAssistant, entry_id: str) -> None:
    """Last clear of a removed entry; its push state goes with it (the clear holds on to it, no retry after)."""
    if (existing := hass.data.get(CERTIFICATE_PUSHES, {}).get(entry_id)) is not None:
        existing.clear_failed.clear()  # no retry after this one: a failure is logged as a warning
    async_clear_certificate_changed(hass, entry_id)
    if (pushes := hass.data.get(CERTIFICATE_PUSHES, {}).pop(entry_id, None)) is None:
        return
    if unreachable := sorted(pushes.shown - set(_registered_phones(hass, pushes))):
        _LOGGER.warning(
            "Comexio certificate push may stay on %s: not registered when the entry was removed", unreachable
        )


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
    # Of shown: the ones logged as not registered, so each is logged once until it registers (and so leaves
    # this set before a delivered clear can take it out of shown).
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
    """Urgent push: the repair needs the user, Comexio gets nothing until then.

    Through Do Not Disturb (iOS: a critical alert, Android: the alarm stream), delivered at once; a tap
    opens the repairs. The fingerprints are the hashes of public certificates, no secret.
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
    }
    message = texts["message"].format(**certificate_placeholders(entry, actual))
    return {"title": texts["title"], "message": message, "data": data}


async def _async_send_certificate_push(
    hass: HomeAssistant, pushes: _EntryPushes, push: _CertificatePush, targets: list[str]
) -> None:
    try:
        async with pushes.lock:
            if pushes.current is not push:
                return  # cleared, or another certificate reported, while it waited: its alarm is no news anymore
            delivered, failed = await _async_notify(hass, push.service_data, targets, push.failed)
            push.delivered |= delivered
            push.failed |= failed
            pushes.shown |= {target for target in delivered if _is_mobile_app(target)}
    finally:
        push.sending = False


async def _async_clear_certificate_push(hass: HomeAssistant, pushes: _EntryPushes, entry_id: str) -> None:
    """Take the push off the phones, after a send still on its way (the lock); the failed ones stay for the next."""
    async with pushes.lock:
        # Also a phone taken off the notify option since: the push may still be on it.
        targets = _registered_phones(hass, pushes)
        if not targets:
            return
        # A companion-app command: any other notify service would deliver it as the text "clear_notification".
        service_data = {"message": "clear_notification", "data": {"tag": _certificate_push_tag(entry_id)}}
        delivered, failed = await _async_notify(hass, service_data, targets, pushes.clear_failed)
        pushes.shown -= delivered
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
