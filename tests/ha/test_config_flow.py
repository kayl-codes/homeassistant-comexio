"""The user and reauth steps of the config flow."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
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
    DEFAULT_HOST,
    DOMAIN,
    KNOWN_DOMAINS,
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


def _dns(table: dict[str, str]):
    """gethostbyname answering from the table; an IP resolves to itself, every other name fails."""

    def resolve(host: str) -> str:
        if host in table:
            return table[host]
        if host.replace(".", "").isdigit():
            return host
        raise OSError(host)

    return patch("custom_components.comexio.config_flow.socket.gethostbyname", side_effect=resolve)


@pytest.mark.parametrize(
    ("existing_host", "chosen_host"),
    [
        ("comexio.fritz.box", "192.168.0.5"),
        ("192.168.0.5", "comexio.fritz.box"),
        ("Comexio.Fritz.Box", " comexio.fritz.box "),
        ("comexio.offline", "COMEXIO.OFFLINE"),
        ("comexio.fritz.box:443", "192.168.0.5"),
        ("192.168.0.5", "comexio.fritz.box:80"),
        ("comexio.fritz.box:8443", "192.168.0.5:8443"),
        ("[fd00::5]:8443", "[FD00::5]:8443"),
        ("fd00::5", "FD00::5"),
        ("comexio.fritz.box:abc", "COMEXIO.FRITZ.BOX:abc"),
    ],
    ids=[
        "name_then_ip",
        "ip_then_name",
        "case_and_whitespace",
        "unresolvable_same_name",
        "explicit_then_implicit_port",
        "implicit_then_explicit_port",
        "same_custom_port",
        "ipv6_with_port",
        "bare_ipv6",
        "unparsable_port_same_text",
    ],
)
async def test_user_flow_rejects_configured_host(
    hass: HomeAssistant,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    existing_host: str,
    chosen_host: str,
) -> None:
    """A host an existing entry already reaches, as written or resolved, is refused before any connection."""
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv1", CONF_HOST: existing_host}).add_to_hass(hass)

    with _dns({"comexio.fritz.box": "192.168.0.5"}):
        result = await _submit(hass, {**USER_INPUT, CONF_HOST: chosen_host})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "already_configured"}
    assert _form_default(result, CONF_HOST) == chosen_host  # the form keeps what the user typed
    certificate_handshake.assert_not_awaited()
    assert mock_comexio_api == []


async def test_user_flow_checks_the_server_id_before_the_host(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Server id and host both taken: the server id error is shown, the host is not looked at."""
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv2", CONF_HOST: HOST}).add_to_hass(hass)

    result = await _submit(hass, dict(USER_INPUT))

    assert result["errors"] == {CONF_SERVER_ID: "server_id_exists"}
    assert mock_comexio_api == []


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("existing", "chosen_host"),
    [
        ({CONF_HOST: "comexio.fritz.box"}, "192.168.0.6"),
        ({}, "192.168.0.6"),
        ({CONF_HOST: ""}, "192.168.0.6"),
        ({CONF_HOST: "comexio.fritz.box:8443"}, "192.168.0.5:9443"),
        ({CONF_HOST: "comexio.fritz.box:8443"}, "192.168.0.5"),
        ({CONF_HOST: "comexio.fritz.box:80"}, "192.168.0.5:443"),
        ({CONF_HOST: "comexio.fritz.box:99999"}, "192.168.0.5"),
    ],
    ids=[
        "other_host",
        "no_host",
        "empty_host",
        "other_custom_port",
        "custom_then_implicit_port",
        "explicit_80_then_443",
        "stored_port_out_of_range",
    ],
)
async def test_user_flow_accepts_another_host(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], existing: dict[str, str], chosen_host: str
) -> None:
    """A second server on another address or port is created; an entry without a host blocks nothing."""
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv1", **existing}).add_to_hass(hass)

    with _dns({"comexio.fritz.box": "192.168.0.5"}):
        result = await _submit(hass, {**USER_INPUT, CONF_HOST: chosen_host})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == chosen_host  # stored as typed, not resolved
    mock_comexio_api[0].login.assert_awaited_once()


# What gethostbyname raises for a malformed name: UnicodeError (a ValueError) for "a..b", TypeError for a NUL.
MALFORMED_HOST_ERRORS = pytest.mark.parametrize(
    "lookup_error", [UnicodeError("label empty"), TypeError("embedded null")], ids=["idna", "nul"]
)


@pytest.mark.usefixtures("mock_setup_entry")
@MALFORMED_HOST_ERRORS
async def test_a_malformed_host_is_compared_as_written(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], lookup_error: Exception
) -> None:
    """A host gethostbyname refuses outright: the form still loads and the setup goes on."""
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv1", CONF_HOST: "a..b"}).add_to_hass(hass)

    with patch("custom_components.comexio.config_flow.socket.gethostbyname", side_effect=lookup_error):
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(form["flow_id"], dict(USER_INPUT))

    assert _form_default(form, CONF_HOST) == DEFAULT_HOST
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_comexio_api[0].login.assert_awaited_once()


def _form_default(result: dict[str, Any], field: str) -> Any:
    return next(key.default() for key in result["data_schema"].schema if str(key) == field)


@pytest.mark.parametrize(
    ("existing_hosts", "suggested"),
    [
        ((), "192.168.0.5"),
        (("192.168.0.5",), "192.168.0.6"),
        (("comexio",), "192.168.0.6"),
        (("comexio", "192.168.0.6"), DEFAULT_HOST),
        (("comexio", "192.168.0.6", DEFAULT_HOST), ""),
        (("comexio:443",), "192.168.0.6"),
        (("192.168.0.5:8443",), "192.168.0.5"),
        (("comexio:99999",), "192.168.0.5"),
    ],
    ids=[
        "none_configured",
        "ip_configured",
        "name_configured",
        "all_configured",
        "default_host_configured",
        "name_with_port",
        "custom_port",
        "port_out_of_range",
    ],
)
async def test_user_form_suggests_an_unconfigured_server(
    hass: HomeAssistant, existing_hosts: tuple[str, ...], suggested: str
) -> None:
    """Auto-discovery suggests the first comexio.<domain> no existing entry reaches yet, else the default."""
    for index, host in enumerate(existing_hosts, start=1):
        MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: f"iosrv{index}", CONF_HOST: host}).add_to_hass(hass)

    with _dns({"comexio": "192.168.0.5", f"comexio.{KNOWN_DOMAINS[0]}": "192.168.0.6"}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    assert _form_default(result, CONF_HOST) == suggested


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


async def test_reconfigure_refuses_a_host_another_entry_uses(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
) -> None:
    """Moving an entry onto another entry's server is refused before any connection, like a new entry."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv2", CONF_HOST: "comexio.fritz.box"}).add_to_hass(hass)
    data_before = dict(mock_config_entry.data)

    with _dns({"comexio.fritz.box": RECONFIGURE_INPUT[CONF_HOST]}):
        result = await _submit_reconfigure(hass, mock_config_entry, dict(RECONFIGURE_INPUT))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "already_configured"}
    assert _form_default(result, CONF_HOST) == RECONFIGURE_INPUT[CONF_HOST]  # the form keeps what the user typed
    assert dict(mock_config_entry.data) == data_before
    certificate_handshake.assert_not_awaited()
    assert mock_comexio_api == []


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_onto_its_own_server_under_another_name_succeeds(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Switching from the IP to the DNS name of the same server: the entry's own host does not count as taken."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv2", CONF_HOST: "192.168.1.30"}).add_to_hass(hass)

    with _dns({"comexio.fritz.box": HOST}):
        result = await _submit_reconfigure(
            hass, mock_config_entry, {**RECONFIGURE_INPUT, CONF_HOST: "comexio.fritz.box"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_HOST] == "comexio.fritz.box"


@pytest.mark.usefixtures("mock_setup_entry")
@MALFORMED_HOST_ERRORS
async def test_reconfigure_with_a_malformed_host_does_not_crash(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], lookup_error: Exception
) -> None:
    """A host gethostbyname refuses outright is compared as written; the flow goes on to the login test."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv2", CONF_HOST: "192.168.1.30"}).add_to_hass(hass)

    with patch("custom_components.comexio.config_flow.socket.gethostbyname", side_effect=lookup_error):
        result = await _submit_reconfigure(hass, mock_config_entry, {**RECONFIGURE_INPUT, CONF_HOST: "comexio.lan"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_comexio_api[0].login.assert_awaited_once()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_keeping_a_shared_host_still_succeeds(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Two entries that already share a host (set up before the check) still take new credentials."""
    mock_config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={CONF_SERVER_ID: "iosrv2", CONF_HOST: HOST}).add_to_hass(hass)

    result = await _submit_reconfigure(hass, mock_config_entry, {**RECONFIGURE_INPUT, CONF_HOST: HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    mock_comexio_api[0].login.assert_awaited_once()


async def test_unchanged_reauth_without_a_coordinator_falls_back_to_ha_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """A loaded entry whose coordinator is missing still reloads: through HA's reload, there is no guard to use."""
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    same = {key: mock_config_entry.data[key] for key in REAUTH_INPUT}

    with (
        patch.dict(hass.data[DOMAIN]),  # puts the coordinator back for the unload at teardown
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        del hass.data[DOMAIN][mock_config_entry.entry_id]
        result = await _submit_reauth(hass, mock_config_entry, same)
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    schedule_reload.assert_called_once_with(mock_config_entry.entry_id)
    # Setup and the flow's login test only (HA's reload is patched out); the credentials were tested first.
    assert len(mock_comexio_api) == 2
    mock_comexio_api[1].login.assert_awaited_once()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("submit", "user_input"),
    [
        (lambda hass, entry: _submit_reconfigure(hass, entry, dict(RECONFIGURE_INPUT)), RECONFIGURE_INPUT),
        (lambda hass, entry: _submit_reauth(hass, entry, dict(REAUTH_INPUT)), REAUTH_INPUT),
    ],
    ids=["reconfigure", "reauth"],
)
async def test_changes_on_a_loaded_entry_reload_it_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    caplog: pytest.LogCaptureFixture,
    submit: Any,
    user_input: dict[str, Any],
) -> None:
    """The update listener reloads a loaded entry; the flow must not schedule a second reload."""
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await submit(hass, mock_config_entry)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    schedule_reload.assert_not_called()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    # Set up again by the update listener, with the stored changes (setup, the flow's login test, the reload).
    assert len(mock_comexio_api) == 3
    assert mock_comexio_api[-1].username == user_input[CONF_USERNAME]
    assert "should use it for scheduling a reload" not in caplog.text


UNCHANGED_FLOW_CASES = pytest.mark.parametrize(
    ("submit", "reason"),
    [
        (
            lambda hass, entry: _submit_reconfigure(
                hass, entry, {CONF_HOST: HOST, CONF_USERNAME: "admin", CONF_API_USERNAME: "api"}
            ),
            "reconfigure_successful",
        ),
        (
            lambda hass, entry: _submit_reauth(hass, entry, {CONF_USERNAME: "admin", CONF_PASSWORD: "admin"}),
            "reauth_successful",
        ),
    ],
    ids=["reconfigure", "reauth"],
)


async def _setup_for_unchanged_flows(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Store the data as a reconfigure stores it, so submitting UNCHANGED_FLOW_CASES' values changes nothing."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_API_USERNAME: "api", CONF_API_PASSWORD: ""})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@UNCHANGED_FLOW_CASES
async def test_unchanged_flow_still_reloads_a_loaded_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    submit: Any,
    reason: str,
) -> None:
    """No change, no update listener run: the flow reloads the loaded entry itself (here set up normally)."""
    await _setup_for_unchanged_flows(hass, mock_config_entry)
    data_before = dict(mock_config_entry.data)

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await submit(hass, mock_config_entry)
        await hass.async_block_till_done()

    assert result["reason"] == reason
    assert dict(mock_config_entry.data) == data_before
    # Through the coordinator's guarded reload, not HA's unguarded one (setup, the flow's login test, the reload).
    schedule_reload.assert_not_called()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(mock_comexio_api) == 3


@UNCHANGED_FLOW_CASES
async def test_unchanged_flow_during_a_sync_leaves_the_reload_to_the_sync(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    submit: Any,
    reason: str,
) -> None:
    """A reload mid-sync would close the API session under it; the sync's closing reload covers the flow."""
    await _setup_for_unchanged_flows(hass, mock_config_entry)
    data_before = dict(mock_config_entry.data)

    # The sync ends with the flow, even a failing one: else the teardown unload waits FUNCTION_PLAN_UNLOAD_WAIT_SEC
    # for it, 120 s in which a scheduled check (e.g. the nightly range check) can fire against the mocked API.
    with (
        patch.object(hass.data[DOMAIN][mock_config_entry.entry_id], "in_sync", True),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await submit(hass, mock_config_entry)
        await hass.async_block_till_done()

    assert result["reason"] == reason
    assert dict(mock_config_entry.data) == data_before
    schedule_reload.assert_not_called()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    # Setup and the flow's login test only: no reload while the sync runs.
    assert len(mock_comexio_api) == 2
    mock_comexio_api[1].login.assert_awaited_once()
