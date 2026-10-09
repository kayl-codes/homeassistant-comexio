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
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_IGNORED_MARKERS,
    CONF_IMPORT_DISABLED_IGNORED,
    DOMAIN,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
)
from custom_components.comexio.repairs import ComexioRepairFlow
from tests.common import load_json_fixture

from .conftest import SERVER_ID

MARKER_ISSUE = f"import_disabled_marker_{SERVER_ID}"
KNX_ISSUE = f"import_disabled_knx_{SERVER_ID}"
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
    assert form["description_placeholders"]["trigger_pairs"] == "0"
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
    assert issue.data["counts"] == {
        "commands": 2,
        "trigger_pairs": 0,
        "bridge_markers": 0,
        "plans": 0,
        "devices": 1,
        "classes": 1,
    }
    assert issue.translation_placeholders["category"] == "Marker"
    # The marker commands are no orphans: "delete orphans" would have removed them.
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    assert all(o["webio_class"] != "marker" for o in coordinator.last_audit_results["orphan"])
    assert coordinator.last_audit_results["orphan"] == []
    if (sync_issue := _issue(hass, SYNC_ISSUE)) is not None:
        assert sync_issue.data["counts"]["orphan"] == 0


def _without_the_bridge_marker(config: dict[str, Any]) -> dict[str, Any]:
    """The fixture config with marker 7 ("Rollo Wohnen [K3]") titled like a plain marker."""
    config["FubModules"]["2"]["7"]["Name"] = "Rollo Wohnen"
    return config


async def test_import_on_raises_no_import_disabled_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    api_returns: dict[str, Any],
    mock_comexio_api: list[ComexioAPI],
) -> None:
    api_returns["get_raw_config"] = _without_the_bridge_marker(load_json_fixture("config_basic.json"))
    await _setup(hass, mock_config_entry)

    assert _issue(hass, MARKER_ISSUE) is None
    assert _issue(hass, KNX_ISSUE) is None  # KNX off, but nothing on the server


async def _refresh_with_plans(hass: HomeAssistant, entry: MockConfigEntry, plans: dict[int, dict]) -> Any:
    """Set up (KNX off, marker 7 is a titled bridge marker), then poll with these plan snapshots."""
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.function_plan_plans.clear()
    coordinator.function_plan_plans.update(plans)
    await coordinator.async_refresh()
    return coordinator


_EMPTY_PLANS = {1: {"elements": {}}, 2: {"elements": {}}}


async def test_a_titled_bridge_marker_alone_keeps_the_knx_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Regression: the KNX cleanup resets bridge marker titles — a marker left alone still needs it."""
    await _refresh_with_plans(hass, mock_config_entry, _EMPTY_PLANS)

    issue = _issue(hass, KNX_ISSUE)
    assert issue is not None
    assert issue.data["counts"] == {
        "commands": 0,
        "trigger_pairs": 0,
        "bridge_markers": 1,
        "plans": 0,
        "devices": 0,
        "classes": 0,
    }
    assert _issue(hass, MARKER_ISSUE) is None  # the bridge marker is KNX's, not the marker import's
    flow = ComexioRepairFlow(KNX_ISSUE, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["description_placeholders"]["bridge_markers"] == "1"


async def test_a_bridge_marker_a_user_plan_places_raises_no_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The cleanup leaves a still placed bridge marker alone — counting it would loop the repair."""
    plans = {**_EMPTY_PLANS, 2: {"elements": {"10": {"reference": {"type": 2, "ref_id": 7}}}}}
    await _refresh_with_plans(hass, mock_config_entry, plans)

    assert _issue(hass, KNX_ISSUE) is None


async def test_unknown_bridge_marker_placement_keeps_the_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """A plan without a snapshot may place the marker: the issue stays until that is known."""
    coordinator = await _refresh_with_plans(hass, mock_config_entry, _EMPTY_PLANS)
    assert _issue(hass, KNX_ISSUE) is not None

    del coordinator.function_plan_plans[2]
    await coordinator.async_refresh()

    assert _issue(hass, KNX_ISSUE) is not None


async def test_the_knx_issue_clears_once_the_bridge_title_is_gone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    coordinator = await _refresh_with_plans(hass, mock_config_entry, _EMPTY_PLANS)
    assert _issue(hass, KNX_ISSUE) is not None

    mock_comexio_api[-1].get_raw_config.return_value = _without_the_bridge_marker(
        load_json_fixture("config_basic.json")
    )
    await coordinator.async_refresh()

    assert _issue(hass, KNX_ISSUE) is None


# Every ComexioAPI call that removes something on the server.
_DESTRUCTIVE_API_METHODS = (
    "delete_webio_device",
    "delete_webio_base",
    "delete_fup",
    "delete_single_command",
    "function_plan_delete_elements",
    "delete_marker",
    "function_plan_remove_trigger_pairs",
)


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
    api = mock_comexio_api[-1]
    for name in _DESTRUCTIVE_API_METHODS:
        getattr(api, name).assert_not_awaited()


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
    assert entry.options["import_markers"] is True
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


async def test_options_flow_keeps_a_submitted_ignore_list(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Counterpart of the emptying test: a flow that dropped the list on every save would pass that one."""
    entry = _with_options(mock_config_entry, {CONF_IGNORED_MARKERS: "2"})
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_IGNORED_MARKERS: "1, 2"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_IGNORED_MARKERS] == "1,2"  # normalized


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


async def test_plans_count_only_while_they_still_exist_in_comexio(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """A plan deleted in Comexio (id 99, not in $Fubs) must not count as a leftover."""
    plan_map = {"HA - Marker [1-100]": 1, "HA - Marker [101-200]": 99}
    entry = _with_options(mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: plan_map})
    await _setup(hass, entry)

    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"]["plans"] == 1


async def test_no_plan_list_leaves_the_issue_alone(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Without any $Fubs read a deleted plan would count, so the issue is neither raised nor cleared."""
    await _setup(hass, markers_off_entry)
    coordinator = hass.data[DOMAIN][markers_off_entry.entry_id]
    assert _issue(hass, MARKER_ISSUE) is not None

    config = load_json_fixture("config_basic.json")
    del config["Fubs"]
    del config["WebDevices"]["30"]  # nothing left — would clear the issue if the poll judged it
    mock_comexio_api[-1].get_raw_config.return_value = config
    coordinator.scraped_plan_ids = None
    await coordinator.async_refresh()

    assert _issue(hass, MARKER_ISSUE) is not None


async def test_trigger_pairs_alone_keep_the_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Marker pairs left in the shared trigger plan are leftovers too — the scope cleanup removes them."""
    entry = _with_options(
        mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 2}}
    )
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    trigger_plan = {"elements": {"10": {"reference": {"type": 2, "ref_id": 1}}}, "connections": {}}
    coordinator.function_plan_plans[2] = trigger_plan

    config = load_json_fixture("config_basic.json")
    del config["WebDevices"]["30"]  # commands, device and class gone — only the pair is left
    mock_comexio_api[-1].get_raw_config.return_value = config
    await coordinator.async_refresh()

    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"] == {
        "commands": 0,
        "trigger_pairs": 1,
        "bridge_markers": 0,
        "plans": 0,
        "devices": 0,
        "classes": 0,
    }


async def _refresh_with_only_the_trigger_plan_left(
    hass: HomeAssistant, entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], trigger_plan: dict | None
) -> Any:
    """Set up, then poll a config whose marker commands, device and class are gone."""
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if trigger_plan is not None:
        coordinator.function_plan_plans[2] = trigger_plan
        coordinator.function_plan_plans[1] = {"elements": {}}  # every plan loaded: bridge placement known
    else:
        coordinator.function_plan_plans.pop(2, None)
    config = load_json_fixture("config_basic.json")
    del config["WebDevices"]["30"]
    mock_comexio_api[-1].get_raw_config.return_value = config
    await coordinator.async_refresh()
    return coordinator


async def test_a_bridge_marker_pair_counts_for_knx_not_markers(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Marker 7 ("Rollo Wohnen [K3]") is a KNX bridge: its pair belongs to the KNX cleanup."""
    entry = _with_options(
        mock_config_entry,
        {
            "import_markers": False,
            "import_knx": False,
            CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 2},
        },
    )
    trigger_plan = {"elements": {"10": {"reference": {"type": 2, "ref_id": 7}}}, "connections": {}}
    await _refresh_with_only_the_trigger_plan_left(hass, entry, mock_comexio_api, trigger_plan)

    assert _issue(hass, MARKER_ISSUE) is None
    knx_issue = _issue(hass, KNX_ISSUE)
    assert knx_issue is not None
    assert knx_issue.data["counts"]["trigger_pairs"] == 1


async def test_an_unloaded_trigger_plan_does_not_clear_the_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Its pairs are unknown until the snapshot loads — they may be all that is left."""
    entry = _with_options(
        mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 2}}
    )
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.function_plan_plans[2] = {"elements": {}}
    await coordinator.async_refresh()
    assert _issue(hass, MARKER_ISSUE) is not None

    del coordinator.function_plan_plans[2]
    config = load_json_fixture("config_basic.json")
    del config["WebDevices"]["30"]
    mock_comexio_api[-1].get_raw_config.return_value = config
    await coordinator.async_refresh()

    assert _issue(hass, MARKER_ISSUE) is not None


async def test_a_deleted_trigger_plan_counts_no_pairs(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Plan 99 is not in $Fubs: a still cached snapshot of it must not keep the issue alive."""
    entry = _with_options(
        mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 99}}
    )
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.function_plan_plans[99] = {"elements": {"10": {"reference": {"type": 2, "ref_id": 1}}}}
    config = load_json_fixture("config_basic.json")
    del config["WebDevices"]["30"]
    mock_comexio_api[-1].get_raw_config.return_value = config
    await coordinator.async_refresh()

    assert _issue(hass, MARKER_ISSUE) is None


async def test_a_poll_without_plan_list_judges_plans_by_the_last_one(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """$Fubs unread this poll: the last poll's list still filters out the deleted plan 99."""
    plan_map = {"HA - Marker [1-100]": 1, "HA - Marker [101-200]": 99}
    entry = _with_options(mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: plan_map})
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.scraped_plan_ids = {1, 2}
    mock_comexio_api[-1]._fub_data = load_json_fixture("config_basic.json")["Fubs"]  # the last poll's list

    config = load_json_fixture("config_basic.json")
    del config["Fubs"]
    mock_comexio_api[-1].get_raw_config.return_value = config
    await coordinator.async_refresh()

    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"]["plans"] == 1


def _with_data(entry: MockConfigEntry, data: dict[str, Any], options: dict[str, Any]) -> MockConfigEntry:
    """The fixture's entry with extra data (the config flow saves the import flags there)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        minor_version=entry.minor_version,
        data={**entry.data, **data},
        options=options,
    )


async def test_import_switched_off_in_the_config_flow_still_offers_the_repair(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The flag lives in entry.data only: the repair must not take the category for active."""
    entry = _with_data(mock_config_entry, {"import_markers": False}, {})
    await _setup(hass, entry)

    result = await _run_repair(hass, "ignore")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_IMPORT_DISABLED_IGNORED] == ["marker"]


@pytest.mark.parametrize("flag_in_data", [False, True])
async def test_a_stale_dialog_changes_nothing_once_the_import_is_on_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
    flag_in_data: bool,
) -> None:
    """Regression: opened while off, submitted after an options flow switched the import on.

    flag_in_data: switched off in the config flow (entry.data) — options must win over it.
    """
    if flag_in_data:
        entry = _with_data(mock_config_entry, {"import_markers": False}, {})
    else:
        entry = _with_options(mock_config_entry, {"import_markers": False})
    await _setup(hass, entry)
    scopes: list[str] = []

    async def _run_cleanup(_self: ComexioRepairFlow, _coordinator: Any, _entry: Any, scope: str) -> None:
        scopes.append(scope)

    monkeypatch.setattr(ComexioRepairFlow, "_async_run_cleanup", _run_cleanup)
    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    actions = ("cleanup", "ignore", "enable")
    flows = []
    for _ in actions:
        flow = ComexioRepairFlow(MARKER_ISSUE, issue.data)
        flow.hass = hass
        await flow.async_step_init()
        flows.append(flow)

    hass.config_entries.async_update_entry(entry, options={"import_markers": True})
    await hass.async_block_till_done()

    for flow, action in zip(flows, actions, strict=True):
        # As if a poll had left the issue standing (unreadable scrape, sync lock): the guard drops it.
        ir.async_create_issue(
            hass,
            DOMAIN,
            MARKER_ISSUE,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="import_disabled",
            data=issue.data,
        )
        result = await flow.async_step_import_disabled({"action": action})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "import_enabled"
        assert _issue(hass, MARKER_ISSUE) is None
    await hass.async_block_till_done()
    assert scopes == []
    assert CONF_IMPORT_DISABLED_IGNORED not in entry.options


async def test_a_stale_dialog_is_refused_while_the_entry_reloads(
    hass: HomeAssistant, markers_off_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """No coordinator yet (reload after the switch still running): import_enabled, not entry_not_found."""
    await _setup(hass, markers_off_entry)
    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    flow = ComexioRepairFlow(MARKER_ISSUE, issue.data)
    flow.hass = hass
    await flow.async_step_init()
    hass.config_entries.async_update_entry(markers_off_entry, options={"import_markers": True})
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN].pop(markers_off_entry.entry_id)

    result = await flow.async_step_import_disabled({"action": "cleanup"})
    hass.data[DOMAIN][markers_off_entry.entry_id] = coordinator  # for the unload at teardown

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_enabled"


async def test_unknown_trigger_pairs_show_as_unknown_not_zero(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The known commands raise the issue; the pairs of an unloaded trigger plan read "?", then their count."""
    entry = _with_options(
        mock_config_entry, {"import_markers": False, CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 2}}
    )
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.function_plan_plans.pop(2, None)
    await coordinator.async_refresh()
    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"]["trigger_pairs"] is None
    assert issue.translation_placeholders["trigger_pairs"] == "?"
    flow = ComexioRepairFlow(MARKER_ISSUE, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["description_placeholders"]["trigger_pairs"] == "?"

    coordinator.function_plan_plans[2] = {"elements": {"10": {"reference": {"type": 2, "ref_id": 1}}}}
    await coordinator.async_refresh()

    issue = _issue(hass, MARKER_ISSUE)
    assert issue is not None
    assert issue.data["counts"]["trigger_pairs"] == 1
