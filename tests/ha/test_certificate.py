"""HTTPS to Comexio with a pinned certificate: config flow, setup upgrade, mismatch repair."""

import asyncio
from collections.abc import Generator
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioCertificateMismatchError, ComexioConnectionError
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.comexio import config_flow as config_flow_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.certificate import (
    CERTIFICATE_PUSHES,
    async_clear_certificate_changed,
    async_remove_certificate_pushes,
    async_report_certificate_changed,
    certificate_issue_id,
)
from custom_components.comexio.const import (
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    DOMAIN,
    LOGIN_ERROR_CERTIFICATE,
    LOGIN_ERROR_CONNECTION,
    LOGIN_ERROR_REJECTED,
    SETUP_UNREACHABLE_ENTRIES,
)
from custom_components.comexio.repairs import ComexioRepairFlow

from .conftest import HOST, SERVER_ID

PINNED = "a" * 64
OTHER = "b" * 64
THIRD = "c" * 64
NEW_HOST = "192.168.1.20"

USER_INPUT = {
    CONF_SERVER_ID: SERVER_ID,
    CONF_HOST: HOST,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    "scan_interval": "15",
}


@pytest.fixture
def notify() -> Generator[MagicMock]:
    """The persistent notification announcing the switch to HTTPS."""
    with patch("custom_components.comexio.certificate.persistent_notification.async_create") as create:
        yield create


@pytest.fixture
def no_auto_discovery() -> Generator[None]:
    """The user form's host suggestion resolves comexio.<domain>; DNS is disabled in tests."""
    with patch("custom_components.comexio.config_flow.socket.gethostbyname", side_effect=OSError):
        yield


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Keep a flow's entry from being set up; the setup has its own tests below."""
    with (
        patch("custom_components.comexio.async_setup_entry", return_value=True),
        patch("custom_components.comexio.async_unload_entry", return_value=True),
    ):
        yield


@pytest.fixture
def pinned_entry() -> MockConfigEntry:
    """An entry already on HTTPS with PINNED."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Comexio {SERVER_ID} ({HOST})",
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={
            CONF_HOST: HOST,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "admin",
            CONF_SERVER_ID: SERVER_ID,
            CONF_CERTIFICATE_FINGERPRINT: PINNED,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, certificate_issue_id(entry.entry_id))


# Config flow


async def _submit_user(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))


@pytest.mark.usefixtures("mock_setup_entry", "no_auto_discovery")
async def test_a_new_entry_pins_the_certificate_comexio_serves(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], certificate_handshake: AsyncMock, notify: MagicMock
) -> None:
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED

    result = await _submit_user(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert mock_comexio_api[0].fingerprint == PINNED  # the login test already ran over HTTPS
    mock_comexio_api[0].login.assert_awaited_once()
    notify.assert_called_once()
    assert PINNED_SHOWN in notify.call_args.args[1]


@pytest.mark.usefixtures("mock_setup_entry", "no_auto_discovery")
async def test_a_new_entry_stays_on_http_without_https(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], notify: MagicMock
) -> None:
    result = await _submit_user(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_CERTIFICATE_FINGERPRINT not in result["data"]
    assert mock_comexio_api[0].fingerprint is None
    mock_comexio_api[0].login.assert_awaited_once()
    notify.assert_not_called()


@pytest.mark.usefixtures("no_auto_discovery")
@pytest.mark.parametrize(
    ("login_error", "replaced", "error"),
    [
        (LOGIN_ERROR_CERTIFICATE, None, "certificate_unstable"),
        (LOGIN_ERROR_CONNECTION, None, "cannot_connect"),
        # aiohttp reports a foreign certificate on a host name with several addresses as a connection error.
        (LOGIN_ERROR_CONNECTION, OTHER, "certificate_unstable"),
    ],
)
async def test_a_failed_login_test_names_its_cause(
    hass: HomeAssistant,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    notify: MagicMock,
    login_error: str,
    replaced: str | None,
    error: str,
) -> None:
    # A certificate refused right after it was read: there is no entry and no repair to point to yet.
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = replaced
    api_attributes["last_login_error"] = login_error

    result = await _submit_user(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    notify.assert_not_called()


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry, host: str) -> dict[str, Any]:
    result = await entry.start_reconfigure_flow(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: host, CONF_USERNAME: "admin", CONF_PASSWORD: "admin"}
    )


@pytest.mark.parametrize(
    ("pin", "placeholders"),
    [
        ({CONF_CERTIFICATE_FINGERPRINT: PINNED}, {"protocol": "HTTPS", "fingerprint": ":".join(["AA"] * 32)}),
        ({}, {"protocol": "HTTP", "fingerprint": "—"}),
    ],
    ids=["pinned entry", "entry on plain HTTP"],
)
async def test_the_reconfigure_form_shows_the_connection(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, pin: dict, placeholders: dict
) -> None:
    # Whether the connection is encrypted, and with which certificate — nothing to set, just shown.
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, data={**mock_config_entry.data, **pin})

    result = await mock_config_entry.start_reconfigure_flow(hass)

    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == placeholders


PINNED_SHOWN = ":".join(["AA"] * 32)
OTHER_SHOWN = ":".join(["BB"] * 32)


def _serve(handshake: AsyncMock, served: str | Exception) -> None:
    """What the reconfigured host answers to the bare TLS handshake: a fingerprint, or no HTTPS."""
    if isinstance(served, Exception):
        handshake.side_effect = served
    else:
        handshake.side_effect = None
        handshake.return_value = served


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("host", "served", "step_id", "placeholders", "expected_pin", "title"),
    [
        (
            NEW_HOST,
            OTHER,
            "reconfigure_certificate",
            {"host": NEW_HOST, "pinned": PINNED_SHOWN, "actual": OTHER_SHOWN},
            OTHER,
            "Comexio: connection encrypted",
        ),
        (
            NEW_HOST,
            ComexioConnectionError("no HTTPS"),
            "reconfigure_http",
            {"host": NEW_HOST, "pinned": PINNED_SHOWN},
            None,
            "Comexio: connection not encrypted",
        ),
        (
            HOST,
            ComexioConnectionError("no HTTPS"),
            "reconfigure_http",
            {"host": HOST, "pinned": PINNED_SHOWN},
            None,
            "Comexio: connection not encrypted",
        ),
    ],
    ids=["new host with another certificate", "new host without HTTPS", "pinned host without HTTPS"],
)
async def test_a_pinned_entry_asks_before_it_drops_or_replaces_the_pin(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    host: str,
    served: str | Exception,
    step_id: str,
    placeholders: dict,
    expected_pin: str | None,
    title: str,
) -> None:
    # A host that answers no HTTPS may just be unreachable this moment, and another certificate may be
    # anyone's: nothing - not even the login test - is sent before the user confirmed what is shown.
    pinned_entry.add_to_hass(hass)
    data_before = dict(pinned_entry.data)
    _serve(certificate_handshake, served)

    result = await _reconfigure(hass, pinned_entry, host)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    assert result["description_placeholders"] == placeholders
    assert not mock_comexio_api
    assert dict(pinned_entry.data) == data_before
    notify.assert_not_called()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert pinned_entry.data[CONF_HOST] == host
    assert pinned_entry.data.get(CONF_CERTIFICATE_FINGERPRINT) == expected_pin
    assert mock_comexio_api[0].fingerprint == expected_pin
    mock_comexio_api[0].login.assert_awaited_once()
    notify.assert_called_once()
    assert notify.call_args.kwargs["title"] == title


def _form_default(result: dict[str, Any], field: str) -> Any:
    """The value a form field is prefilled with."""
    return next(key.default() for key in result["data_schema"].schema if key == field)


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("served", "api_attributes", "error"),
    [
        (OTHER, {"last_login_error": LOGIN_ERROR_CONNECTION}, "cannot_connect"),
        (
            OTHER,
            {
                "last_login_error": LOGIN_ERROR_CERTIFICATE,
                "certificate_mismatch": ComexioCertificateMismatchError("foreign", expected=OTHER, actual=THIRD),
            },
            "certificate_unstable",
        ),
        (ComexioConnectionError("no HTTPS"), {"last_login_error": LOGIN_ERROR_CONNECTION}, "cannot_connect"),
    ],
    ids=["replace, unreachable", "replace, yet another certificate at the login", "drop, unreachable"],
)
async def test_a_confirmed_change_whose_login_test_fails_changes_nothing(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    served: str | Exception,
    error: str,
) -> None:
    # The certificate just confirmed is not the pinned one, so a mismatch at the login is no repair's case;
    # a confirmed switch to HTTP whose login fails keeps the pin.
    pinned_entry.add_to_hass(hass)
    data_before = dict(pinned_entry.data)
    _serve(certificate_handshake, served)
    api_returns["login"] = False

    result = await _reconfigure(hass, pinned_entry, NEW_HOST)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": error}
    assert _form_default(result, CONF_HOST) == NEW_HOST  # what the user entered, not the entry's host
    assert mock_comexio_api[0].fingerprint == (None if isinstance(served, Exception) else OTHER)
    assert dict(pinned_entry.data) == data_before
    assert _issue(hass, pinned_entry) is None
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("served", "expected_pin", "notified"),
    [(PINNED, PINNED, True), (ComexioConnectionError("no HTTPS"), None, False)],
    ids=["Comexio serves HTTPS", "Comexio serves no HTTPS"],
)
async def test_an_entry_on_plain_http_takes_https_without_asking(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    served: str | Exception,
    expected_pin: str | None,
    notified: bool,
) -> None:
    # Like at setup: encryption is only gained, nothing trusted is given up.
    mock_config_entry.add_to_hass(hass)
    _serve(certificate_handshake, served)

    result = await _reconfigure(hass, mock_config_entry, HOST)

    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data.get(CONF_CERTIFICATE_FINGERPRINT) == expected_pin
    assert mock_comexio_api[0].fingerprint == expected_pin
    mock_comexio_api[0].login.assert_awaited_once()
    assert notify.called is notified


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize("host", ["http://192.168.1.20", "192.168.1.20/admin"])
async def test_an_invalid_host_is_named_as_such(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    host: str,
) -> None:
    # Not "no HTTPS": the pinned entry is not offered a switch to plain HTTP for a typo.
    pinned_entry.add_to_hass(hass)
    data_before = dict(pinned_entry.data)

    result = await _reconfigure(hass, pinned_entry, host)

    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_host"}
    assert _form_default(result, CONF_HOST) == host
    certificate_handshake.assert_not_awaited()
    assert not mock_comexio_api
    assert dict(pinned_entry.data) == data_before


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize("host", [HOST, NEW_HOST], ids=["same host", "new host, same certificate"])
async def test_the_pinned_certificate_keeps_its_pin(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    host: str,
) -> None:
    pinned_entry.add_to_hass(hass)
    _serve(certificate_handshake, PINNED)

    result = await _reconfigure(hass, pinned_entry, host)

    assert result["reason"] == "reconfigure_successful"
    assert pinned_entry.data[CONF_HOST] == host
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert mock_comexio_api[0].fingerprint == PINNED
    mock_comexio_api[0].login.assert_awaited_once()
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("actual", "error", "repaired"),
    [(OTHER, "certificate_changed", True), (None, "certificate_unstable", False)],
    ids=["names the certificate", "names none"],
)
async def test_another_certificate_at_the_login_test_on_the_pinned_host(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    actual: str | None,
    error: str,
    repaired: bool,
) -> None:
    # The handshake still met the pin, the login then another certificate: the repair if it is known which.
    pinned_entry.add_to_hass(hass)
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CERTIFICATE
    api_attributes["certificate_mismatch"] = ComexioCertificateMismatchError("foreign", expected=PINNED, actual=actual)

    result = await _reconfigure(hass, pinned_entry, HOST)

    assert result["errors"] == {"base": error}
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    issue = _issue(hass, pinned_entry)
    assert (issue is not None) is repaired
    if repaired:
        assert issue.data["actual"] == OTHER


@pytest.mark.usefixtures("mock_setup_entry")
async def test_another_certificate_at_the_login_test_on_another_host_raises_no_repair(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
) -> None:
    # The new host met the pin at the handshake, another certificate at the login: the repair would check the
    # entry's old host, so the new one goes through reconfigure again instead.
    pinned_entry.add_to_hass(hass)
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CERTIFICATE
    api_attributes["certificate_mismatch"] = ComexioCertificateMismatchError("foreign", expected=PINNED, actual=OTHER)

    result = await _reconfigure(hass, pinned_entry, NEW_HOST)

    assert result["errors"] == {"base": "certificate_unstable"}
    assert pinned_entry.data[CONF_HOST] == HOST
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, pinned_entry) is None


@pytest.mark.usefixtures("mock_setup_entry")
async def test_a_changed_certificate_on_the_pinned_host_is_left_to_the_repair(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    # Never accepted along with other settings: the form names the repair, which now exists. Decided by the
    # handshake alone — on a host name with several addresses the login test would only see a connection error.
    pinned_entry.add_to_hass(hass)
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = OTHER

    result = await _reconfigure(hass, pinned_entry, HOST)

    assert result["errors"] == {"base": "certificate_changed"}
    assert not mock_comexio_api  # no login (and no credentials) sent to that certificate
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    issue = _issue(hass, pinned_entry)
    assert issue is not None
    assert issue.data["actual"] == OTHER
    notify.assert_not_called()


async def _reauth(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await entry.start_reauth_flow(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin2", CONF_PASSWORD: "new-secret"}
    )


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_tests_the_new_credentials_over_the_pinned_https(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    # New credentials never go out unencrypted to a Comexio that serves HTTPS.
    pinned_entry.add_to_hass(hass)

    result = await _reauth(hass, pinned_entry)

    assert result["reason"] == "reauth_successful"
    api = mock_comexio_api[0]
    assert (api.username, api.password, api.fingerprint) == ("admin2", "new-secret", PINNED)
    api.login.assert_awaited_once()
    assert (pinned_entry.data[CONF_USERNAME], pinned_entry.data[CONF_PASSWORD]) == ("admin2", "new-secret")
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_of_an_http_entry_pins_the_certificate_the_login_worked_with(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    # Tested like the next setup logs in: over HTTPS, and the new credentials never go out unencrypted.
    mock_config_entry.add_to_hass(hass)
    _serve(certificate_handshake, PINNED)

    result = await _reauth(hass, mock_config_entry)

    assert result["reason"] == "reauth_successful"
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED]
    assert [api.login.await_count for api in mock_comexio_api] == [1] * len(mock_comexio_api)  # tested
    assert mock_config_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"
    notify.assert_called_once()
    assert PINNED_SHOWN in notify.call_args.args[1]


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("login_error", "replaced", "error", "fingerprints"),
    [
        # The setup asks for reauth for exactly this refusal: a test passing over HTTP would loop.
        (LOGIN_ERROR_REJECTED, None, "invalid_auth", [PINNED]),
        (LOGIN_ERROR_CERTIFICATE, None, "certificate_unstable", [PINNED]),
        # aiohttp reports a foreign certificate on a host name with several addresses as a connection error.
        (LOGIN_ERROR_CONNECTION, OTHER, "certificate_unstable", [PINNED]),
        # As the setup does: HTTPS that answers the handshake but not the login falls back to HTTP.
        (LOGIN_ERROR_CONNECTION, None, "cannot_connect", [PINNED, None]),
    ],
    ids=["refused over HTTPS", "another certificate at the login", "reported as a connection error", "unreachable"],
)
async def test_reauth_of_an_http_entry_judges_like_the_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    login_error: str,
    replaced: str | None,
    error: str,
    fingerprints: list[str | None],
) -> None:
    mock_config_entry.add_to_hass(hass)
    data_before = dict(mock_config_entry.data)
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = replaced
    api_attributes["last_login_error"] = login_error

    result = await _reauth(hass, mock_config_entry)

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert [api.fingerprint for api in mock_comexio_api] == fingerprints
    assert [api.login.await_count for api in mock_comexio_api] == [1] * len(mock_comexio_api)  # tested
    assert dict(mock_config_entry.data) == data_before
    assert _issue(hass, mock_config_entry) is None  # nothing pinned: no repair to point to
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_of_an_http_entry_whose_https_login_cannot_connect_passes_over_http(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    mock_config_entry.add_to_hass(hass)
    _serve(certificate_handshake, PINNED)
    factory = config_flow_module.ComexioAPI.side_effect

    def https_login_unreachable(*args: Any, **kwargs: Any) -> ComexioAPI:
        api = factory(*args, **kwargs)
        if kwargs.get("fingerprint") is not None:
            api.login.return_value = False
            api.last_login_error = LOGIN_ERROR_CONNECTION
        return api

    with patch.object(config_flow_module.ComexioAPI, "side_effect", https_login_unreachable):
        result = await _reauth(hass, mock_config_entry)

    assert result["reason"] == "reauth_successful"
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED, None]
    assert [api.login.await_count for api in mock_comexio_api] == [1] * len(mock_comexio_api)  # tested
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_of_an_entry_without_https_stays_on_http(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    notify: MagicMock,
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await _reauth(hass, mock_config_entry)

    assert result["reason"] == "reauth_successful"
    assert [api.fingerprint for api in mock_comexio_api] == [None]
    assert [api.login.await_count for api in mock_comexio_api] == [1] * len(mock_comexio_api)  # tested
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_meeting_another_certificate_is_left_to_the_repair(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
) -> None:
    # aiohttp reports a foreign certificate on a host name with several addresses as a connection error.
    pinned_entry.add_to_hass(hass)
    data_before = dict(pinned_entry.data)
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = OTHER
    api_attributes["last_login_error"] = LOGIN_ERROR_CONNECTION

    result = await _reauth(hass, pinned_entry)

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "certificate_changed"}
    assert dict(pinned_entry.data) == data_before
    issue = _issue(hass, pinned_entry)
    assert issue is not None
    assert issue.data["actual"] == OTHER


# Setup


async def test_setup_switches_an_http_entry_to_https(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED]
    notify.assert_called_once()


async def test_setup_stays_on_http_when_the_https_login_cannot_connect(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    # Pinned only after a login over HTTPS worked with the certificate. A Comexio whose HTTPS answers the
    # handshake but not the login keeps working over HTTP, as before; the HTTP login's failure decides.
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = None  # the fresh handshake still shows the certificate read
    api_attributes["last_login_error"] = LOGIN_ERROR_CONNECTION

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    https_api, http_api = mock_comexio_api[:2]
    assert https_api.fingerprint == PINNED
    https_api.fetch_replaced_fingerprint.assert_awaited_once()
    https_api.close.assert_called()
    assert http_api.fingerprint is None
    notify.assert_not_called()


async def test_setup_does_not_resend_credentials_rejected_over_https(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_REJECTED

    hass.data.setdefault(SETUP_UNREACHABLE_ENTRIES, set()).add(mock_config_entry.entry_id)
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.entry_id not in hass.data[SETUP_UNREACHABLE_ENTRIES]  # Comexio answered
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED]  # no second, unencrypted login
    mock_comexio_api[0].close.assert_called()
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("login_error", "replaced"),
    [(LOGIN_ERROR_CERTIFICATE, None), (LOGIN_ERROR_CONNECTION, OTHER)],
    ids=["named by the login", "reported as a connection error"],
)
async def test_setup_meeting_a_changing_certificate_during_the_switch_is_retried(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    login_error: str,
    replaced: str | None,
) -> None:
    # The handshake read one certificate, the login met another: no fallback to plain HTTP with the credentials.
    # aiohttp reports a foreign certificate on a host name with several addresses as a connection error.
    _serve(certificate_handshake, PINNED)
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = replaced
    api_attributes["last_login_error"] = login_error

    hass.data.setdefault(SETUP_UNREACHABLE_ENTRIES, set()).add(mock_config_entry.entry_id)
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.entry_id not in hass.data[SETUP_UNREACHABLE_ENTRIES]  # Comexio answered
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED]
    mock_comexio_api[0].close.assert_called()
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("login_error", "state"),
    [(LOGIN_ERROR_CONNECTION, ConfigEntryState.SETUP_RETRY), ("rejected", ConfigEntryState.SETUP_ERROR)],
)
async def test_a_pinned_entry_never_falls_back_to_http(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    login_error: str,
    state: ConfigEntryState,
) -> None:
    api_returns["login"] = False
    api_attributes["last_login_error"] = login_error

    await _setup(hass, pinned_entry)

    assert pinned_entry.state is state
    assert [api.fingerprint for api in mock_comexio_api] == [PINNED]
    certificate_handshake.assert_not_awaited()
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    notify.assert_not_called()


@pytest.mark.parametrize(
    "api_attributes",
    [
        {
            "last_login_error": LOGIN_ERROR_CERTIFICATE,
            "certificate_mismatch": ComexioCertificateMismatchError("foreign", expected=PINNED, actual=OTHER),
        }
    ],
)
async def test_setup_meeting_another_certificate_raises_the_repair(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], api_returns: dict
) -> None:
    api_returns["login"] = False

    await _setup(hass, pinned_entry)

    assert pinned_entry.state is ConfigEntryState.SETUP_RETRY
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED  # never replaced on its own
    issue = _issue(hass, pinned_entry)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.data == {"entry_id": pinned_entry.entry_id, "actual": OTHER}
    assert issue.translation_placeholders["actual"] == ":".join(["BB"] * 32)


async def test_a_connection_error_with_a_replaced_certificate_raises_the_repair(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
) -> None:
    # aiohttp reports a foreign certificate on a host name with several addresses as a connection error.
    api_returns["login"] = False
    api_returns["fetch_replaced_fingerprint"] = OTHER
    api_attributes["last_login_error"] = LOGIN_ERROR_CONNECTION

    await _setup(hass, pinned_entry)

    assert pinned_entry.state is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, pinned_entry) is not None


@pytest.mark.parametrize(("entry_fixture", "hinted"), [("pinned_entry", True), ("mock_config_entry", False)])
async def test_a_connection_failure_names_the_way_back_to_http_only_for_a_pinned_entry(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    entry_fixture: str,
    hinted: bool,
) -> None:
    # Shown under "Retrying setup" too: a Comexio whose HTTPS was switched off retries forever otherwise.
    entry: MockConfigEntry = request.getfixturevalue(entry_fixture)
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CONNECTION

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ("reconfigure the entry to go back to HTTP" in str(entry.reason)) is hinted


async def test_a_setup_with_the_pinned_certificate_drops_the_repair(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    pinned_entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        certificate_issue_id(pinned_entry.entry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="certificate_changed",
        data={"entry_id": pinned_entry.entry_id, "actual": OTHER},
    )

    await hass.config_entries.async_setup(pinned_entry.entry_id)
    await hass.async_block_till_done()

    assert pinned_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, pinned_entry) is None


# Connection sensor

CONNECTION_SENSOR = f"sensor.{SERVER_ID}_connection"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_connection_sensor_follows_the_certificate_repair(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # Watchable by an automation: the state turns to certificate_changed while the repair is open.
    await _setup(hass, pinned_entry)

    state = hass.states.get(CONNECTION_SENSOR)
    assert state.state == "https"
    assert state.attributes["pinned_fingerprint"] == ":".join(["AA"] * 32)
    assert state.attributes["presented_fingerprint"] is None

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    state = hass.states.get(CONNECTION_SENSOR)
    assert state.state == "certificate_changed"
    assert state.attributes["presented_fingerprint"] == ":".join(["BB"] * 32)

    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION_SENSOR).state == "https"


# Urgent push

PHONE = "mobile_app_phone"
FAMILY = "family"  # a notify group: gets the push, but no companion-app clear
PUSH_TARGETS = {"function_plan_watchdog_notify": [f"notify.{PHONE}", f"notify.{FAMILY}"]}


def _with_push_targets(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=PUSH_TARGETS)


async def test_another_certificate_is_pushed_urgently_once(hass: HomeAssistant, pinned_entry: MockConfigEntry) -> None:
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    family = async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()

    assert len(phone) == len(family) == 1
    push = phone[0].data
    assert push["title"] == "Comexio: certificate changed"
    assert ":".join(["BB"] * 32) in push["message"]
    assert HOST in push["message"]
    assert push["data"]["tag"] == f"{DOMAIN}_certificate_{pinned_entry.entry_id}"
    assert push["data"]["url"] == push["data"]["clickAction"] == "/config/repairs"
    assert push["data"]["group"] == f"{DOMAIN}_{SERVER_ID}"
    assert push["data"]["priority"] == "high"
    assert push["data"]["channel"] == "alarm_stream"
    assert push["data"]["ttl"] == 0
    assert push["data"]["push"]["interruption-level"] == "critical"
    assert push["data"]["push"]["sound"]["critical"] == 1
    assert family[0].data == push

    # Every retry and poll reports the same certificate again: no second push.
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    assert len(phone) == 1

    # Yet another certificate is news again.
    async_report_certificate_changed(hass, pinned_entry, THIRD)
    await hass.async_block_till_done()
    assert len(phone) == 2
    assert ":".join(["CC"] * 32) in phone[1].data["message"]


async def test_no_push_without_targets_or_for_the_pinned_certificate(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    pinned_entry.add_to_hass(hass)
    phone = async_mock_service(hass, "notify", PHONE)

    async_report_certificate_changed(hass, pinned_entry, OTHER)  # no notify targets configured
    await hass.async_block_till_done()
    assert not phone
    assert _issue(hass, pinned_entry) is not None

    hass.config_entries.async_update_entry(pinned_entry, options=PUSH_TARGETS)
    async_report_certificate_changed(hass, pinned_entry, PINNED)  # late report of the pin itself
    await hass.async_block_till_done()
    assert not phone


async def test_a_failing_notify_service_does_not_stop_the_others(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    _with_push_targets(hass, pinned_entry)

    async def _broken(_call: ServiceCall) -> None:
        raise HomeAssistantError("push gateway down")

    hass.services.async_register("notify", PHONE, _broken)
    family = async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()

    assert len(family) == 1
    assert f"Comexio certificate push to notify.{PHONE} failed: push gateway down" in caplog.text

    # The next report retries the service the push has not reached, and only that one.
    phone = async_mock_service(hass, "notify", PHONE)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    assert len(phone) == 1
    assert len(family) == 1


async def test_a_notify_service_not_there_yet_gets_the_push_at_the_next_report(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # Right after a restart the setup can report before the mobile app has registered its notify service.
    _with_push_targets(hass, pinned_entry)
    family = async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    assert f"Comexio certificate push to notify.{PHONE} failed" in caplog.text

    phone = async_mock_service(hass, "notify", PHONE)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()

    assert len(phone) == 1
    assert phone[0].data["title"] == "Comexio: certificate changed"
    assert len(family) == 1


async def test_a_hanging_notify_service_times_out_without_holding_up_the_others(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    _with_push_targets(hass, pinned_entry)

    async def _hanging(_call: ServiceCall) -> None:
        await asyncio.sleep(10)

    hass.services.async_register("notify", PHONE, _hanging)
    family = async_mock_service(hass, "notify", FAMILY)

    with patch("custom_components.comexio.certificate.FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC", 0.01):
        async_report_certificate_changed(hass, pinned_entry, OTHER)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert len(family) == 1
    assert f"Comexio certificate push to notify.{PHONE} timed out" in caplog.text


@pytest.mark.parametrize(
    ("language", "title"), [("de", "Comexio: Zertifikat geändert"), ("fr", "Comexio: certificate changed")]
)
async def test_the_push_speaks_the_language_of_home_assistant(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, language: str, title: str
) -> None:
    # German has its own texts, any other language gets the English ones.
    hass.config.language = language
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()

    assert phone[0].data["title"] == title
    for part in (SERVER_ID, HOST, ":".join(["BB"] * 32)):
        assert part in phone[0].data["message"]


CLEAR = "clear_notification"


def _messages(calls: list[ServiceCall]) -> list[str]:
    return [CLEAR if call.data["message"] == CLEAR else "push" for call in calls]


def _gated(hass: HomeAssistant, service: str) -> tuple[list[ServiceCall], asyncio.Event]:
    """A notify service that holds every call until the event is set: a push still on its way."""
    calls: list[ServiceCall] = []
    gate = asyncio.Event()

    async def _held(call: ServiceCall) -> None:
        calls.append(call)
        await gate.wait()

    hass.services.async_register("notify", service, _held)
    return calls, gate


async def test_clearing_the_repair_removes_the_push_from_the_phones(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    family = async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == ["push", CLEAR]
    assert phone[1].data == {"message": CLEAR, "data": {"tag": f"{DOMAIN}_certificate_{pinned_entry.entry_id}"}}
    assert len(family) == 1  # the clear is a companion-app command
    assert _issue(hass, pinned_entry) is None

    # Every later successful poll clears again: nothing left to send.
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(phone) == 2


async def test_the_first_clear_after_a_restart_clears_the_phones_blindly(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # A push sent before the restart may still be on the phone; which phones got it is forgotten.
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    family = async_mock_service(hass, "notify", FAMILY)

    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == [CLEAR]
    assert not family


async def test_a_failed_clear_is_retried_at_the_next_clear(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.comexio.certificate")
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", FAMILY)
    pushed = async_mock_service(hass, "notify", PHONE)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(pushed) == 1

    async def _broken(_call: ServiceCall) -> None:
        raise HomeAssistantError("push gateway down")

    hass.services.async_remove("notify", PHONE)
    hass.services.async_register("notify", PHONE, _broken)
    for _ in range(2):
        async_clear_certificate_changed(hass, pinned_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    levels = [record.levelno for record in caplog.records if f"push to notify.{PHONE} failed" in record.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]  # one warning, not one per poll

    hass.services.async_remove("notify", PHONE)
    phone = async_mock_service(hass, "notify", PHONE)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _messages(phone) == [CLEAR]


async def test_a_clear_waits_for_a_companion_app_not_set_up_yet(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # Right after a restart the mobile app may register its notify service after this integration's first clear.
    _with_push_targets(hass, pinned_entry)

    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "failed" not in caplog.text

    phone = async_mock_service(hass, "notify", PHONE)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _messages(phone) == [CLEAR]


async def test_a_report_first_after_a_restart_still_clears_the_earlier_push(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # The setup reports the changed certificate before any clear, and its push does not get through: the
    # push from before the restart is still on the phone once the pinned certificate is back.
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)  # notify.mobile_app_phone not there yet
    await hass.async_block_till_done(wait_background_tasks=True)

    phone = async_mock_service(hass, "notify", PHONE)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == [CLEAR]


async def test_no_blind_clear_for_an_entry_without_a_pin(hass: HomeAssistant, pinned_entry: MockConfigEntry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={key: value for key, value in pinned_entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT},
    )
    _with_push_targets(hass, entry)
    phone = async_mock_service(hass, "notify", PHONE)

    async_clear_certificate_changed(hass, entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not phone
    assert entry.entry_id not in hass.data.get(CERTIFICATE_PUSHES, {})


async def test_a_phone_taken_off_the_notify_option_still_gets_the_clear(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # The alarm is still on it: taking it off the option must not leave it there.
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)

    family_only = {"function_plan_watchdog_notify": [f"notify.{FAMILY}"]}
    hass.config_entries.async_update_entry(pinned_entry, options=family_only)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == ["push", CLEAR]


async def test_a_clear_reaches_the_registered_phone_and_waits_for_the_other(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.comexio.certificate")
    tablet_service = "mobile_app_tablet"
    two_phones = {"function_plan_watchdog_notify": [f"notify.{PHONE}", f"notify.{tablet_service}"]}
    pinned_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(pinned_entry, options=two_phones)
    phone = async_mock_service(hass, "notify", PHONE)

    for _ in range(2):  # restart: both taken as showing an earlier push; the tablet not registered yet
        async_clear_certificate_changed(hass, pinned_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    waiting = [record for record in caplog.records if "waits for notify.mobile_app_tablet" in record.getMessage()]
    assert len(waiting) == 1  # logged once, not at every poll

    tablet = async_mock_service(hass, "notify", tablet_service)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == [CLEAR]
    assert _messages(tablet) == [CLEAR]


async def test_no_clear_task_while_only_unregistered_phones_are_left(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # The clear at every successful poll must cost nothing while the companion app is not there.
    _with_push_targets(hass, pinned_entry)
    with patch.object(hass, "async_create_background_task", wraps=hass.async_create_background_task) as spawn:
        async_clear_certificate_changed(hass, pinned_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    spawn.assert_not_called()


async def test_a_phone_gone_again_after_it_registered_is_logged_again(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.comexio.certificate")
    _with_push_targets(hass, pinned_entry)

    async def _broken(_call: ServiceCall) -> None:
        raise HomeAssistantError("push gateway down")

    async_clear_certificate_changed(hass, pinned_entry.entry_id)  # not registered: logged
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.services.async_register("notify", PHONE, _broken)  # registered, the clear fails: stays shown
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.services.async_remove("notify", PHONE)  # gone again: logged again
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    waiting = [record for record in caplog.records if f"waits for notify.{PHONE}" in record.getMessage()]
    assert len(waiting) == 2


async def test_the_certificate_back_after_a_clear_is_pushed_again(
    hass: HomeAssistant, pinned_entry: MockConfigEntry
) -> None:
    # Pinned certificate back, then the other one again before the clear ran: the phone must end on the alarm.
    _with_push_targets(hass, pinned_entry)
    phone, gate = _gated(hass, PHONE)
    async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == ["push", CLEAR, "push"]


async def test_a_clear_waits_for_the_push_on_its_way(hass: HomeAssistant, pinned_entry: MockConfigEntry) -> None:
    # The pinned certificate back while the push is still being sent: the clear must not overtake it.
    _with_push_targets(hass, pinned_entry)
    phone, gate = _gated(hass, PHONE)
    async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    assert _messages(phone) == ["push"]

    async_report_certificate_changed(hass, pinned_entry, OTHER)  # a poll while it is on its way: no second send
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done()
    assert _messages(phone) == ["push"]  # the clear waits

    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _messages(phone) == ["push", CLEAR]


async def test_a_push_overtaken_while_waiting_is_not_sent(hass: HomeAssistant, pinned_entry: MockConfigEntry) -> None:
    # A push waiting behind another send must not sound an alarm for a certificate that is no news anymore.
    _with_push_targets(hass, pinned_entry)
    phone, gate = _gated(hass, PHONE)
    async_mock_service(hass, "notify", FAMILY)

    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done()
    async_report_certificate_changed(hass, pinned_entry, THIRD)  # waits for OTHER's send
    async_clear_certificate_changed(hass, pinned_entry.entry_id)  # THIRD's send is dropped
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == ["push", CLEAR]
    assert ":".join(["BB"] * 32) in phone[0].data["message"]


async def test_the_clear_reaches_a_phone_only_the_earlier_certificate_was_pushed_to(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", FAMILY)
    pushed = async_mock_service(hass, "notify", PHONE)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)

    hass.services.async_remove("notify", PHONE)
    async_report_certificate_changed(hass, pinned_entry, THIRD)  # does not reach the phone
    await hass.async_block_till_done(wait_background_tasks=True)
    phone = async_mock_service(hass, "notify", PHONE)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(pushed) == 1
    assert _messages(phone) == [CLEAR]  # OTHER's alarm is still on it


async def test_a_retried_failure_is_logged_at_debug_level(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # Every poll retries a dead notify service: one warning, not one per poll.
    caplog.set_level(logging.DEBUG, logger="custom_components.comexio.certificate")
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", FAMILY)

    for _ in range(3):
        async_report_certificate_changed(hass, pinned_entry, OTHER)
        await hass.async_block_till_done(wait_background_tasks=True)

    levels = [record.levelno for record in caplog.records if f"push to notify.{PHONE} failed" in record.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]


async def test_removing_the_entry_removes_its_push_from_the_phones(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    _with_push_targets(hass, pinned_entry)
    phone = async_mock_service(hass, "notify", PHONE)
    family = async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.config_entries.async_remove(pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _messages(phone) == ["push", CLEAR]
    assert len(family) == 1
    assert _issue(hass, pinned_entry) is None
    assert pinned_entry.entry_id not in hass.data[CERTIFICATE_PUSHES]
    assert "may stay on" not in caplog.text  # every phone reached

    # A poll still on its way when the entry went: no repair and no alarm for an entry that is gone.
    async_report_certificate_changed(hass, pinned_entry, THIRD)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _messages(phone) == ["push", CLEAR]
    assert _issue(hass, pinned_entry) is None
    assert pinned_entry.entry_id not in hass.data[CERTIFICATE_PUSHES]


async def test_removing_the_entry_warns_about_a_phone_the_clear_cannot_reach(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", PHONE)
    async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)
    await hass.async_block_till_done(wait_background_tasks=True)

    hass.services.async_remove("notify", PHONE)
    await hass.config_entries.async_remove(pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert f"Comexio certificate push may stay on ['notify.{PHONE}']" in caplog.text


async def test_removing_the_entry_warns_when_its_last_clear_fails(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A clear that failed before is retried at debug level, but after the removal nothing retries it.
    _with_push_targets(hass, pinned_entry)
    async_mock_service(hass, "notify", FAMILY)

    async def _broken(_call: ServiceCall) -> None:
        raise HomeAssistantError("push gateway down")

    hass.services.async_register("notify", PHONE, _broken)
    async_clear_certificate_changed(hass, pinned_entry.entry_id)  # the blind clear after a restart fails
    await hass.async_block_till_done(wait_background_tasks=True)
    caplog.clear()

    await hass.config_entries.async_remove(pinned_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    failures = [record for record in caplog.records if f"push to notify.{PHONE} failed" in record.getMessage()]
    assert [record.levelno for record in failures] == [logging.WARNING]


async def test_removing_an_unknown_entry_leaves_no_push_state(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    async_remove_certificate_pushes(hass, "unknown_entry")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "unknown_entry" not in hass.data.get(CERTIFICATE_PUSHES, {})
    assert "Comexio certificate" not in caplog.text


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_connection_sensor_shows_plain_http(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    # The default handshake finds no HTTPS, so the entry stays on HTTP.
    await _setup(hass, mock_config_entry)

    state = hass.states.get(CONNECTION_SENSOR)
    assert state.state == "http"
    assert state.attributes["pinned_fingerprint"] is None


# Repair


async def _repair_form(hass: HomeAssistant, entry: MockConfigEntry, shown: str = OTHER) -> ComexioRepairFlow:
    entry.add_to_hass(hass)
    issue_id = certificate_issue_id(entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="certificate_changed",
        data={"entry_id": entry.entry_id, "actual": shown},
    )
    flow = ComexioRepairFlow(issue_id, {"entry_id": entry.entry_id, "actual": shown})
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "certificate_changed"
    assert form["description_placeholders"]["pinned"] == ":".join(["AA"] * 32)
    assert form["description_placeholders"]["actual"] == ":".join(["BB"] * 32)
    return flow


async def test_confirming_the_shown_certificate_pins_it(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
) -> None:
    flow = await _repair_form(hass, pinned_entry)
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = OTHER

    result = await flow.async_step_certificate_changed({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert _issue(hass, pinned_entry) is None
    assert pinned_entry.state is ConfigEntryState.LOADED  # set up again with the new pin
    assert mock_comexio_api[-1].fingerprint == OTHER


@pytest.mark.usefixtures("mock_comexio_api")
async def test_confirming_the_certificate_removes_the_push_from_the_phones(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, certificate_handshake: AsyncMock
) -> None:
    flow = await _repair_form(hass, pinned_entry)
    hass.config_entries.async_update_entry(pinned_entry, options=PUSH_TARGETS)
    phone = async_mock_service(hass, "notify", PHONE)
    async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)  # the push the repair goes with
    await hass.async_block_till_done()
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = OTHER

    await flow.async_step_certificate_changed({})
    await hass.async_block_till_done()

    assert [call.data["message"] for call in phone][1:] == ["clear_notification"]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_pinned_certificate_back_in_the_repair_removes_the_push_from_the_phones(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, certificate_handshake: AsyncMock
) -> None:
    flow = await _repair_form(hass, pinned_entry)
    hass.config_entries.async_update_entry(pinned_entry, options=PUSH_TARGETS)
    phone = async_mock_service(hass, "notify", PHONE)
    family = async_mock_service(hass, "notify", FAMILY)
    async_report_certificate_changed(hass, pinned_entry, OTHER)  # the push the repair goes with
    await hass.async_block_till_done()
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED

    result = await flow.async_step_certificate_changed({})
    await hass.async_block_till_done()

    assert result["reason"] == "certificate_restored"
    assert [call.data["message"] for call in phone][1:] == ["clear_notification"]
    assert len(family) == 1
    assert _issue(hass, pinned_entry) is None


@pytest.mark.parametrize(
    ("served", "reason", "issue_actual"),
    [
        (PINNED, "certificate_restored", None),
        (THIRD, "certificate_changed_again", THIRD),
        (ComexioConnectionError("down"), "cannot_connect", OTHER),
    ],
    ids=["pinned one back", "yet another one", "unreachable"],
)
async def test_the_repair_pins_nothing_but_the_certificate_shown(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    served: str | Exception,
    reason: str,
    issue_actual: str | None,
) -> None:
    flow = await _repair_form(hass, pinned_entry)
    if isinstance(served, Exception):
        certificate_handshake.side_effect = served
    else:
        certificate_handshake.side_effect = None
        certificate_handshake.return_value = served

    result = await flow.async_step_certificate_changed({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    issue = _issue(hass, pinned_entry)
    assert (issue.data["actual"] if issue else None) == issue_actual


async def test_a_repair_of_a_removed_entry_drops_the_issue(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    flow = await _repair_form(hass, pinned_entry)
    await hass.config_entries.async_remove(pinned_entry.entry_id)

    result = await flow.async_step_certificate_changed()

    assert result["reason"] == "entry_not_found"
    assert _issue(hass, pinned_entry) is None


async def test_a_repair_of_an_entry_on_http_pins_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, certificate_handshake: AsyncMock
) -> None:
    # A stale issue must never switch an entry without a pin to HTTPS.
    mock_config_entry.add_to_hass(hass)
    issue_id = certificate_issue_id(mock_config_entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="certificate_changed",
        data={"entry_id": mock_config_entry.entry_id, "actual": OTHER},
    )
    flow = ComexioRepairFlow(issue_id, {"entry_id": mock_config_entry.entry_id, "actual": OTHER})
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["reason"] == "entry_not_found"
    assert _issue(hass, mock_config_entry) is None
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    certificate_handshake.assert_not_awaited()


async def test_confirming_on_a_loaded_entry_reloads_it_once_with_the_new_pin(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
) -> None:
    # The update listener reloads a loaded entry; the flow must not schedule a second reload.
    await _setup(hass, pinned_entry)
    assert pinned_entry.state is ConfigEntryState.LOADED
    flow = await _repair_form(hass, pinned_entry)
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = OTHER

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await flow.async_step_certificate_changed({})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    schedule_reload.assert_not_called()
    assert pinned_entry.state is ConfigEntryState.LOADED
    assert len(mock_comexio_api) == 2  # set up again by the update listener
    assert mock_comexio_api[-1].fingerprint == OTHER


async def test_the_pinned_certificate_back_sets_up_a_waiting_entry(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
) -> None:
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CERTIFICATE
    api_attributes["certificate_mismatch"] = ComexioCertificateMismatchError("foreign", expected=PINNED, actual=OTHER)
    await _setup(hass, pinned_entry)
    assert pinned_entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, pinned_entry)
    assert issue is not None
    flow = ComexioRepairFlow(issue.issue_id, issue.data)
    flow.hass = hass
    api_returns["login"] = True
    api_attributes.clear()
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED

    result = await flow.async_step_certificate_changed({})
    await hass.async_block_till_done()

    assert result["reason"] == "certificate_restored"
    assert pinned_entry.state is ConfigEntryState.LOADED
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED


async def test_removing_the_entry_drops_its_repair(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _repair_form(hass, pinned_entry)

    await hass.config_entries.async_remove(pinned_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, pinned_entry) is None


@pytest.mark.logged_exception  # HA's coordinator logs the UpdateFailed with its traceback at debug level
async def test_a_poll_meeting_another_certificate_raises_the_repair_until_one_gets_through(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], api_returns: dict
) -> None:
    await _setup(hass, pinned_entry)
    api = mock_comexio_api[0]
    coordinator = hass.data[DOMAIN][pinned_entry.entry_id]

    async def foreign_certificate() -> dict:
        # What ComexioAPI.get_raw_config does on a mismatch: keep it for the caller and hand it to the hook.
        api.certificate_mismatch = ComexioCertificateMismatchError("foreign", expected=PINNED, actual=OTHER)
        api.on_certificate_mismatch(OTHER)
        return {}

    api.get_raw_config = AsyncMock(side_effect=foreign_certificate)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    issue = _issue(hass, pinned_entry)
    assert issue is not None
    assert issue.data["actual"] == OTHER

    async def pinned_certificate() -> dict:
        api.certificate_mismatch = None
        return api_returns["get_raw_config"]

    api.get_raw_config = AsyncMock(side_effect=pinned_certificate)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert _issue(hass, pinned_entry) is None
