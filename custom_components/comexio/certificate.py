"""HTTPS to Comexio with a pinned certificate (CONF_CERTIFICATE_FINGERPRINT).

Comexio signs its certificate itself, so instead of a CA the integration pins the certificate's SHA-256
fingerprint, read from a bare TLS handshake at setup (trust on first use, see aiocomexio.tls). A Comexio
that serves no HTTPS keeps plain HTTP, and every setup of such an entry tries the switch again.

A certificate other than the pinned one is never accepted on its own: the certificate_changed repair
shows the pinned and the new fingerprint and pins the new one only when the user confirms it.
"""

import logging
from typing import Any

from aiocomexio import ComexioError, fetch_certificate_fingerprint, format_fingerprint
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import ComexioAPI
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_CERTIFICATE_CHANGED = "certificate_changed"

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
    """Raise (or update) the certificate_changed repair: Comexio presents `actual` instead of the pin."""
    pinned = entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is None or actual == pinned:
        # Nothing pinned, or a late report of a client still on the pin the user just replaced.
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


def async_clear_certificate_changed(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the certificate_changed repair: Comexio presents the pinned certificate (again)."""
    ir.async_delete_issue(hass, DOMAIN, certificate_issue_id(entry_id))


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
    (the next one tries again) and the API returned is not logged in, as before.
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
