"""A category whose import is switched off while its Web-IO commands still exist (import_disabled repair)."""

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import CONF_IGNORED_MARKERS, CONF_IMPORT_DISABLED_IGNORED, DOMAIN
from custom_components.comexio.repairs import ComexioRepairFlow
from tests.common import load_json_fixture

from .conftest import SERVER_ID

MARKER_ISSUE = f"import_disabled_marker_{SERVER_ID}"
SYNC_ISSUE = f"sync_mismatch_{SERVER_ID}"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _run_repair(hass: HomeAssistant, action: str) -> dict[str, Any]:
    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    flow = ComexioRepairFlow(MARKER_ISSUE, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM
    assert form["description_placeholders"]["commands"] == "2"
    return await flow.async_step_import_disabled({"action": action})


def _with_options(entry: MockConfigEntry, options: dict[str, Any]) -> MockConfigEntry:
    """The fixture's entry with other options (they must be set before the entry is added)."""
    return MockConfigEntry(
        domain=DOMAIN, title=entry.title, minor_version=entry.minor_version, data=dict(entry.data), options=options
    )


@pytest.fixture
def markers_off_entry(mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    """The fixture config has two marker Web-IO commands; the marker import is switched off."""
    return _with_options(mock_config_entry, {"import_markers": False})


async def test_switched_off_import_raises_its_own_issue_instead_of_orphans(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)

    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"] == {"commands": 2, "plans": 0, "devices": 1, "classes": 1}
    assert issue.translation_placeholders["category"] == "Marker"
    # The marker commands are no orphans: "delete orphans" would have removed them.
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    assert all(o["webio_class"] != "marker" for o in coordinator.last_audit_results["orphan"])
    assert coordinator.last_audit_results["orphan"] == []
    if (sync_issue := _issue(hass, SYNC_ISSUE)) is not None:
        assert sync_issue.data["counts"]["orphan"] == 0


async def test_import_on_raises_no_import_disabled_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, mock_config_entry)

    assert _issue(hass, MARKER_ISSUE) is None
    assert _issue(hass, f"import_disabled_knx_{SERVER_ID}") is None  # KNX off, but nothing on the server


async def test_ignore_keeps_everything_and_does_not_ask_again(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)

    result = await _run_repair(hass, "ignore")
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert markers_off_entry.options[CONF_IMPORT_DISABLED_IGNORED] == ["marker"]
    assert markers_off_entry.options["import_markers"] is False
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert _issue(hass, MARKER_ISSUE) is None


async def test_enable_switches_the_import_on_and_drops_the_ignore(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)

    result = await _run_repair(hass, "enable")
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert markers_off_entry.options["import_markers"] is True
    assert CONF_IMPORT_DISABLED_IGNORED not in markers_off_entry.options
    assert markers_off_entry.state is ConfigEntryState.LOADED  # reloaded by the options write
    assert _issue(hass, MARKER_ISSUE) is None
    assert hass.states.get("switch.iosrv1_m1") is not None


async def test_cleanup_runs_the_marker_scope(
    hass: HomeAssistant,
    markers_off_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup(hass, markers_off_entry)
    scopes: list[str] = []

    async def _run_cleanup(_self: ComexioRepairFlow, _coordinator: Any, _entry: Any, scope: str) -> None:
        scopes.append(scope)

    monkeypatch.setattr(ComexioRepairFlow, "_async_run_cleanup", _run_cleanup)

    result = await _run_repair(hass, "cleanup")
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert scopes == ["marker"]


async def test_options_flow_switching_the_import_on_drops_the_ignore(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    entry = _with_options(markers_off_entry, {"import_markers": False, CONF_IMPORT_DISABLED_IGNORED: ["marker"]})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"import_markers": True})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_IMPORT_DISABLED_IGNORED not in entry.options


async def test_options_flow_emptying_the_ignore_list_removes_it(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The last entry of an ignore-list can be removed: HA sends no key for the emptied field."""
    entry = _with_options(mock_config_entry, {CONF_IGNORED_MARKERS: "2"})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_IGNORED_MARKERS not in entry.options


async def test_an_unreadable_scrape_keeps_the_issue(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)
    assert _issue(hass, MARKER_ISSUE) is not None

    mock_comexio_api[-1].get_raw_config.return_value = {}  # no FubModules: nothing readable
    await hass.data[DOMAIN][markers_off_entry.entry_id].async_refresh()

    assert _issue(hass, MARKER_ISSUE) is not None


async def test_the_issue_clears_once_nothing_is_left(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)
    assert _issue(hass, MARKER_ISSUE) is not None

    config = load_json_fixture("config_basic.json")
    del config["WebDevices"]["30"]  # the marker Web-IO device, and with it its commands
    mock_comexio_api[-1].get_raw_config.return_value = config
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert _issue(hass, MARKER_ISSUE) is None


async def test_cleanup_and_enable_wait_for_a_running_sync_but_ignore_does_not(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, markers_off_entry)
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]

    async with coordinator._sync_lock:
        for action in ("cleanup", "enable"):
            result = await _run_repair(hass, action)
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "sync_running"
            assert _issue(hass, MARKER_ISSUE) is not None
        assert markers_off_entry.options["import_markers"] is False

        result = await _run_repair(hass, "ignore")
        assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.logged_exception
async def test_a_failed_cleanup_raises_the_uninstall_cleanup_issue_for_its_scope(
    hass: HomeAssistant,
    markers_off_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import_disabled counters can miss leftovers, so the retry goes through the scope cleanup."""
    await _setup(hass, markers_off_entry)
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    monkeypatch.setattr(coordinator, "async_uninstall_cleanup", AsyncMock(side_effect=RuntimeError("boom")))

    await _run_repair(hass, "cleanup")
    await hass.async_block_till_done()

    coordinator.async_uninstall_cleanup.assert_awaited_once()
    retry = _issue(hass, f"uninstall_cleanup_{SERVER_ID}")
    assert retry is not None
    assert retry.data["default_scope"] == "marker"


async def test_options_form_prefills_the_ignore_list(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Prefilled as suggested value: an empty form field would wipe the list on every save."""
    entry = _with_options(mock_config_entry, {CONF_IGNORED_MARKERS: "2"})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    key = next(k for k in result["data_schema"].schema if k == CONF_IGNORED_MARKERS)
    assert key.description["suggested_value"] == "2"
