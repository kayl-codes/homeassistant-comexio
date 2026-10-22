"""HTTPS to Comexio with a pinned certificate (CONF_CERTIFICATE_FINGERPRINT).

Comexio signs its certificate itself, so instead of a CA the integration pins the certificate's SHA-256
fingerprint, read from a bare TLS handshake at setup (trust on first use, see aiocomexio.tls). A Comexio
that serves no HTTPS keeps plain HTTP, and every setup of such an entry tries the switch again.

A certificate other than the pinned one is never accepted on its own: the certificate_changed repair
shows the pinned and the new fingerprint and pins the new one only when the user confirms it.
"""

import asyncio
import logging
from typing import Any

from aiocomexio import ComexioError, fetch_certificate_fingerprint, format_fingerprint
from aiocomexio.tls import https_address
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

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

    A certificate not reported yet also goes out as an urgent push (see _async_push_certificate_changed);
    the reports of the same certificate at every retry and poll do not. The repair lives in memory only, so
    a certificate still changed after a restart of Home Assistant is pushed once more.
    """
    pinned = entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is None or actual == pinned:
        # Nothing pinned, or a late report of a client still on the pin the user just replaced.
        return
    if presented_fingerprint(hass, entry.entry_id) != actual:
        hass.async_create_background_task(
            _async_push_certificate_changed(hass, entry, actual), f"{DOMAIN} certificate changed push"
        )
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


def async_clear_certificate_changed(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the certificate_changed repair: Comexio presents the pinned certificate (again).

    The push of an open repair is removed from the phones as well; without one nothing is sent, so the
    clear at every successful poll costs nothing.
    """
    if presented_fingerprint(hass, entry_id) is None:
        return
    ir.async_delete_issue(hass, DOMAIN, certificate_issue_id(entry_id))
    if (entry := hass.config_entries.async_get_entry(entry_id)) is not None:
        hass.async_create_background_task(
            _async_send_certificate_push(
                hass, entry, {"message": "clear_notification", "data": {"tag": _certificate_push_tag(entry_id)}}
            ),
            f"{DOMAIN} certificate changed push clear",
        )


async def _async_push_certificate_changed(hass: HomeAssistant, entry: ConfigEntry, actual: str) -> None:
    """Urgent push to the configured notify services: the repair needs the user, Comexio gets nothing until then.

    Through Do Not Disturb (iOS: a critical alert, Android: high priority, delivered at once); a tap opens
    the repairs. The fingerprints are the hashes of public certificates, no secret.
    """
    texts = CERTIFICATE_PUSH_TEXTS.get(hass.config.language, CERTIFICATE_PUSH_TEXTS["en"])
    data: dict[str, Any] = {
        "tag": _certificate_push_tag(entry.entry_id),
        "group": f"{DOMAIN}_{entry.data[CONF_SERVER_ID]}",
        "url": REPAIRS_PATH,
        "clickAction": REPAIRS_PATH,
        "ttl": 0,
        "priority": "high",
        "push": {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 1.0}},
    }
    message = texts["message"].format(**certificate_placeholders(entry, actual))
    await _async_send_certificate_push(hass, entry, {"title": texts["title"], "message": message, "data": data})


async def _async_send_certificate_push(hass: HomeAssistant, entry: ConfigEntry, service_data: dict[str, Any]) -> None:
    """Send to every notify service of CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY; a failing one does not stop the others.

    A clear (message "clear_notification") is a companion-app command and goes to the mobile apps only.
    """
    clear = service_data["message"] == "clear_notification"
    for target in entry.options.get(CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY) or []:
        service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
        if clear and not service.startswith(MOBILE_APP_SERVICE_PREFIX):
            continue
        try:
            async with asyncio.timeout(FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC):
                await hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
        except Exception:
            # Third-party notify platforms raise their own errors; none may skip the remaining targets.
            _LOGGER.warning("Comexio certificate push to notify.%s failed", service, exc_info=True)


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
