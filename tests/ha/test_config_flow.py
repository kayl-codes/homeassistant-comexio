"""The user and reauth steps of the config flow."""

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCHEMA_MARKER,
    CONF_SERVER_ID,
    CONF_USERNAME,
    DOMAIN,
)

from .conftest import HOST, SERVER_ID

USER_INPUT = {
    CONF_SERVER_ID: " IOSRV2 ",
    CONF_HOST: HOST,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    "scan_interval": "15",
}


@pytest.fixture(autouse=True)
def no_auto_discovery() -> Generator[None]:
    """The form's host suggestion resolves comexio.<domain>; DNS is disabled in tests."""
    with patch("custom_components.comexio.config_flow.socket.gethostbyname", side_effect=OSError):
        yield


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Keep the created entry from being set up; the setup has its own tests."""
    with (
        patch("custom_components.comexio.async_setup_entry", return_value=True),
        patch("custom_components.comexio.async_unload_entry", return_value=True),
    ):
        yield


async def _submit(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


@pytest.mark.usefixtures("mock_setup_entry")
async def test_user_flow_creates_entry(hass: HomeAssistant, mock_comexio_api: list[ComexioAPI]) -> None:
    """Valid credentials create an entry with the normalised server id; the test session is closed."""
    result = await _submit(hass, dict(USER_INPUT))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Comexio iosrv2 ({HOST})"
    # The rest of data are the form's defaults for the fields the user left alone.
    assert {**USER_INPUT, CONF_SERVER_ID: "iosrv2", "scan_interval": 15}.items() <= result["data"].items()
    mock_comexio_api[0].login.assert_awaited_once()  # the credentials were tested before the entry was created
    mock_comexio_api[0].close.assert_called_once()


# (login result, ComexioAPI attributes after it, form error): Comexio refusing, not answering, transport error.
LOGIN_ERROR_CASES = pytest.mark.parametrize(
    ("login", "api_attributes", "error"),
    [
        (False, {"last_login_error": "rejected"}, "invalid_auth"),
        (False, {"last_login_error": "connection"}, "cannot_connect"),
        (OSError("unreachable"), {}, "cannot_connect"),
    ],
    ids=["rejected", "unreachable", "transport_error"],
)


@pytest.mark.logged_exception  # _test_connection logs the failed login with its traceback
@LOGIN_ERROR_CASES
async def test_user_flow_login_errors(
    hass: HomeAssistant,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    login: Any,
    error: str,
) -> None:
    """A rejected or failed login shows the form again with the matching error and closes the session."""
    api_returns["login"] = login

    result = await _submit(hass, dict(USER_INPUT))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    mock_comexio_api[0].login.assert_awaited_once()
    mock_comexio_api[0].close.assert_called_once()


@pytest.mark.parametrize(
    ("existing_id", "chosen_id"),
    [("iosrv1", " IOSRV1 "), ("my-server", "my_server")],
    ids=["case_and_whitespace", "same_slug"],
)
async def test_user_flow_rejects_taken_server_id(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], existing_id: str, chosen_id: str
) -> None:
    """A server id equal to an existing one, verbatim or slugified, is refused before any login."""
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: existing_id}).add_to_hass(hass)

    result = await _submit(hass, {**USER_INPUT, CONF_SERVER_ID: chosen_id})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SERVER_ID: "server_id_exists"}
    assert mock_comexio_api == []


async def test_user_form_suggests_lowest_free_server_id(hass: HomeAssistant) -> None:
    """With iosrv1 and iosrv3 taken, the form suggests iosrv2."""
    for server_id in ("iosrv1", "iosrv3"):
        MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: server_id}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    defaults = {str(key): key.default() for key in result["data_schema"].schema if key.default is not vol.UNDEFINED}
    assert defaults[CONF_SERVER_ID] == "iosrv2"


REAUTH_INPUT = {CONF_USERNAME: "admin2", CONF_PASSWORD: "new-secret"}


async def _submit_reauth(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]) -> dict[str, Any]:
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reauth_updates_the_credentials_and_reloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """New credentials are tested, replace the stored ones (other data stays) and reload the entry."""
    mock_config_entry.add_to_hass(hass)
    data_before = dict(mock_config_entry.data)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_PASSWORD: "stale", CONF_SCHEMA_MARKER: "M{MarkerId}"}
    )

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await _submit_reauth(hass, mock_config_entry, dict(REAUTH_INPUT))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert dict(mock_config_entry.data) == {**data_before, **REAUTH_INPUT}
    # A password left in options from an older version would shadow the new one; real options stay.
    assert dict(mock_config_entry.options) == {CONF_SCHEMA_MARKER: "M{MarkerId}"}
    reload.assert_called_once_with(mock_config_entry.entry_id)
    api = mock_comexio_api[0]
    assert (api.username, api.password) == ("admin2", "new-secret")
    api.login.assert_awaited_once()  # the new credentials were tested before they were stored
    api.close.assert_called_once()


@pytest.mark.logged_exception  # _test_connection logs the failed login with its traceback
@LOGIN_ERROR_CASES
async def test_reauth_login_errors_keep_the_stored_credentials(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    login: Any,
    error: str,
) -> None:
    """A failed test login shows the form again with the matching error and changes nothing."""
    api_returns["login"] = login
    mock_config_entry.add_to_hass(hass)
    data_before = dict(mock_config_entry.data)

    result = await _submit_reauth(hass, mock_config_entry, dict(REAUTH_INPUT))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert dict(mock_config_entry.data) == data_before
    mock_comexio_api[0].login.assert_awaited_once()
    mock_comexio_api[0].close.assert_called_once()


RECONFIGURE_INPUT = {CONF_HOST: "192.168.1.20", CONF_USERNAME: "admin2", CONF_API_USERNAME: "api"}


async def _submit_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]
) -> dict[str, Any]:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_keeps_blank_passwords_and_cleans_options(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Left-blank passwords keep the stored ones; the login test uses them; credentials leave the options."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_PASSWORD: "stale", CONF_SCHEMA_MARKER: "M{MarkerId}"}
    )
    data_before = dict(mock_config_entry.data)

    result = await _submit_reconfigure(hass, mock_config_entry, dict(RECONFIGURE_INPUT))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(mock_config_entry.data) == {**data_before, **RECONFIGURE_INPUT, CONF_API_PASSWORD: ""}
    assert dict(mock_config_entry.options) == {CONF_SCHEMA_MARKER: "M{MarkerId}"}
    assert mock_config_entry.title == f"Comexio {SERVER_ID} (192.168.1.20)"
    api = mock_comexio_api[0]
    assert (api.host, api.username, api.password) == ("192.168.1.20", "admin2", data_before[CONF_PASSWORD])
    api.login.assert_awaited_once()  # the merged settings were tested before they were stored


@pytest.mark.logged_exception  # _test_connection logs the failed login with its traceback
@LOGIN_ERROR_CASES
async def test_reconfigure_login_errors_keep_the_stored_settings(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    login: Any,
    error: str,
) -> None:
    """A failed test login shows the form again with the matching error and changes nothing."""
    api_returns["login"] = login
    mock_config_entry.add_to_hass(hass)
    data_before = dict(mock_config_entry.data)

    result = await _submit_reconfigure(hass, mock_config_entry, dict(RECONFIGURE_INPUT))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert dict(mock_config_entry.data) == data_before
    mock_comexio_api[0].login.assert_awaited_once()
    mock_comexio_api[0].close.assert_called_once()
