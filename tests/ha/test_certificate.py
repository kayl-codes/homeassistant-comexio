"""HTTPS to Comexio with a pinned certificate: config flow, setup upgrade, mismatch repair."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioCertificateMismatchError, ComexioConnectionError
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.certificate import (
    async_clear_certificate_changed,
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
    notify.assert_called_once()
    assert PINNED[:2].upper() in notify.call_args.args[1]


@pytest.mark.usefixtures("mock_setup_entry", "no_auto_discovery")
async def test_a_new_entry_stays_on_http_without_https(
    hass: HomeAssistant, mock_comexio_api: list[ComexioAPI], notify: MagicMock
) -> None:
    result = await _submit_user(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_CERTIFICATE_FINGERPRINT not in result["data"]
    assert mock_comexio_api[0].fingerprint is None
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


@pytest.mark.usefixtures("mock_setup_entry")
@pytest.mark.parametrize(
    ("served", "expected_pin", "title"),
    [
        (OTHER, OTHER, "Comexio: connection encrypted"),
        (ComexioConnectionError("no HTTPS"), None, "Comexio: connection not encrypted"),
    ],
    ids=["new host serves HTTPS", "new host serves no HTTPS"],
)
async def test_a_new_host_gets_its_own_certificate_pinned(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    served: str | Exception,
    expected_pin: str | None,
    title: str,
) -> None:
    pinned_entry.add_to_hass(hass)
    if isinstance(served, Exception):
        certificate_handshake.side_effect = served
    else:
        certificate_handshake.side_effect = None
        certificate_handshake.return_value = served

    result = await _reconfigure(hass, pinned_entry, NEW_HOST)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert pinned_entry.data.get(CONF_CERTIFICATE_FINGERPRINT) == expected_pin
    assert mock_comexio_api[0].fingerprint == expected_pin
    notify.assert_called_once()
    assert notify.call_args.kwargs["title"] == title


@pytest.mark.usefixtures("mock_setup_entry")
async def test_a_new_host_whose_login_test_fails_changes_nothing(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    pinned_entry.add_to_hass(hass)
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = OTHER
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CONNECTION

    result = await _reconfigure(hass, pinned_entry, NEW_HOST)

    assert result["errors"] == {"base": "cannot_connect"}
    assert pinned_entry.data[CONF_HOST] == HOST
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    notify.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_the_pinned_host_keeps_its_pin(
    hass: HomeAssistant,
    pinned_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    certificate_handshake: AsyncMock,
    notify: MagicMock,
) -> None:
    pinned_entry.add_to_hass(hass)
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED

    result = await _reconfigure(hass, pinned_entry, HOST)

    assert result["reason"] == "reconfigure_successful"
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert mock_comexio_api[0].fingerprint == PINNED
    notify.assert_not_called()


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


@pytest.mark.usefixtures("mock_setup_entry")
async def test_a_pinned_host_without_https_goes_back_to_http_with_a_notification(
    hass: HomeAssistant, pinned_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], notify: MagicMock
) -> None:
    # The way out for a Comexio whose HTTPS was switched off; the HTTP login test must get through.
    pinned_entry.add_to_hass(hass)

    result = await _reconfigure(hass, pinned_entry, HOST)

    assert result["reason"] == "reconfigure_successful"
    assert CONF_CERTIFICATE_FINGERPRINT not in pinned_entry.data
    assert mock_comexio_api[0].fingerprint is None
    notify.assert_called_once()
    assert notify.call_args.kwargs["title"] == "Comexio: connection not encrypted"


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
    assert mock_comexio_api[0].fingerprint == PINNED
    assert pinned_entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED


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


@pytest.mark.parametrize(
    ("login_error", "state"),
    [(LOGIN_ERROR_CONNECTION, ConfigEntryState.SETUP_RETRY), ("rejected", ConfigEntryState.SETUP_ERROR)],
)
async def test_setup_stays_on_http_when_the_https_login_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
    certificate_handshake: AsyncMock,
    notify: MagicMock,
    login_error: str,
    state: ConfigEntryState,
) -> None:
    # Pinned only after a login over HTTPS worked with the certificate; the HTTP login's failure decides.
    certificate_handshake.side_effect = None
    certificate_handshake.return_value = PINNED
    api_returns["login"] = False
    api_attributes["last_login_error"] = login_error

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is state
    assert CONF_CERTIFICATE_FINGERPRINT not in mock_config_entry.data
    https_api, http_api = mock_comexio_api[:2]
    assert https_api.fingerprint == PINNED
    https_api.close.assert_called()
    assert http_api.fingerprint is None
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
