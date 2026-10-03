"""The user step of the config flow."""

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
from custom_components.comexio.const import CONF_HOST, CONF_PASSWORD, CONF_SERVER_ID, CONF_USERNAME, DOMAIN

from .conftest import HOST

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
    mock_comexio_api[0].close.assert_called_once()


@pytest.mark.logged_exception  # _test_connection logs the failed login with its traceback
@pytest.mark.parametrize(
    ("login", "error"),
    [(False, "invalid_auth"), (OSError("unreachable"), "cannot_connect")],
)
async def test_user_flow_login_errors(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], api_returns: dict, login: Any, error: str
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
