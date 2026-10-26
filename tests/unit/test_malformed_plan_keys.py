"""A $Fubs key function_plan_id rejects, or an entry that is no dict, is skipped instead of crashing its consumer."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import (
    api as api_module,
    coordinator as coordinator_module,
    function_plan_backup as backup_module,
    repairs,
)
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    DOMAIN,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    function_plan_info,
    function_plans,
    unreadable_plan_keys,
)
from custom_components.comexio.coordinator import ComexioCoordinator, _readable_plan_ids
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from custom_components.comexio.ha_address import WebhookTarget
from custom_components.comexio.services import _context, _yaml_sync, misc
from custom_components.comexio.services._context import _available_plans_str, _resolve_fub_id
from tests.common import load_json_fixture

# Plan 7 is the only real plan: "08" and "x" are no plan ids as str(fub_id) spells them, "9" carries no plan info.
MALFORMED_FUBS = {"7": {"Name": "Pumps"}, "08": {"Name": "Pumps"}, "x": {"Name": "Pumps"}, "9": "garbage"}


def _coordinator(fub_data: dict) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.hass = MagicMock()
    coordinator.api = SimpleNamespace(fub_data=fub_data)
    coordinator.function_plan_plans = {}
    coordinator._lp_missing_recheck_pending = False
    return coordinator


def test_function_plans_yields_only_canonical_keys_with_plan_info() -> None:
    assert list(function_plans(MALFORMED_FUBS)) == [(7, {"Name": "Pumps"})]


def test_duplicate_plan_names_count_only_real_plans() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)

    with (
        patch("custom_components.comexio.coordinator.ir.async_create_issue") as create,
        patch("custom_components.comexio.coordinator.ir.async_delete_issue") as delete,
    ):
        coordinator._check_duplicate_plan_names()

    create.assert_not_called()
    delete.assert_called_once()


@pytest.mark.parametrize(
    ("fub_data", "loaded"),
    [({"7": {"Name": "Pumps"}}, True), (MALFORMED_FUBS, False)],
    ids=["readable", "unreadable-entry"],
)
def test_relevant_plans_are_not_loaded_while_the_plan_list_has_an_unreadable_entry(
    fub_data: dict, loaded: bool
) -> None:
    """ "08" may be relevant plan 8: the audit must not read it as deleted and report its wires as missing."""
    coordinator = _coordinator(fub_data)
    coordinator.function_plan_plans = {7: {"elements": {}, "connections": {}}}
    coordinator._function_plan_snapshot_loaded = lambda: True  # type: ignore[method-assign]

    assert coordinator._relevant_plans_loaded({7, 8}) is loaded


@pytest.mark.parametrize(
    ("fub_data", "expected"),
    [({"7": {"Name": "Pumps"}}, {"7": {"Name": "Pumps"}}), (MALFORMED_FUBS, None)],
    ids=["readable", "unreadable-entry"],
)
def test_a_plan_list_with_an_unreadable_entry_is_no_live_plan_list(fub_data: dict, expected: dict | None) -> None:
    """Judged by it, a plan behind the skipped entry would count as deleted."""
    coordinator = _coordinator(fub_data)
    coordinator.scraped_plan_ids = {7}

    assert coordinator.live_plan_list() == expected


def test_unreadable_plan_keys() -> None:
    assert unreadable_plan_keys(MALFORMED_FUBS) == ["08", "x", "9"]
    assert unreadable_plan_keys({"7": {}}) == []


def test_the_plan_map_is_not_pruned_by_a_plan_list_with_an_unreadable_entry() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Markers": 8}})

    assert coordinator._stale_plan_map_entries(MALFORMED_FUBS) == {}


def test_no_cluster_plan_is_created_while_the_plan_list_has_an_unreadable_entry() -> None:
    """ "08" may be the wanted plan: creating it again would leave two plans of that name."""
    coordinator = _coordinator({"08": {"Name": "HA - Markers"}})
    coordinator.api.create_fup = AsyncMock()

    assert asyncio.run(coordinator._create_managed_plan("HA - Markers")) is None
    coordinator.api.create_fup.assert_not_awaited()


def test_a_cluster_plan_is_found_past_malformed_entries() -> None:
    coordinator = _coordinator({"x": {"Name": "HA - Markers"}, "9": "garbage", "7": {"Name": "HA - Markers"}})
    coordinator._distrusted_fub_ids = set()
    coordinator._create_managed_plan = AsyncMock()  # type: ignore[method-assign]

    result = asyncio.run(coordinator._resolve_single_cluster_plan("HA - Markers", {}, coordinator.api.fub_data))

    assert result == (7, False)
    coordinator._create_managed_plan.assert_not_awaited()


def test_io_plan_membership_skips_malformed_entries() -> None:
    coordinator = _coordinator({"4": {"Name": "HA - IO [A]"}, "08": {"Name": "HA - IO [B]"}, "5": "garbage"})
    coordinator._distrusted_fub_ids = set()

    assert coordinator._io_plan_membership("HA") == {4: ["A"]}


@pytest.mark.parametrize("fub_data", [MALFORMED_FUBS, {"x": "garbage", "7": {"Name": "Pumps"}}])
def test_the_plan_selector_name_resolves_past_malformed_entries(fub_data: dict) -> None:
    coordinator = _coordinator(fub_data)
    coordinator._active_plan_selector_state = lambda: SimpleNamespace(state="Pumps")  # type: ignore[method-assign]

    assert coordinator.get_active_function_plan_fub_id() == 7


def test_service_plan_labels_skip_malformed_entries() -> None:
    coordinator = SimpleNamespace(api=SimpleNamespace(fub_data=MALFORMED_FUBS))
    with (
        patch.object(_yaml_sync, "_iter_active_coordinators", return_value=[("e1", coordinator)]),
        patch.object(_yaml_sync, "_is_managed_cluster_plan", return_value=False),
    ):
        assert _yaml_sync._collect_plan_options(MagicMock()) == (["Pumps (ID 7)"], [], ["e1"])


def test_ensure_relevant_plans_cached_fetches_only_real_plans() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)
    load = AsyncMock(return_value={"elements": {}, "connections": {}})
    coordinator.api.function_plan_load_elements = load
    coordinator._function_plan_snapshot_loaded = lambda: True  # type: ignore[method-assign]

    asyncio.run(coordinator._ensure_relevant_plans_cached({7, 8}))

    load.assert_awaited_once_with(7)


def test_current_plan_format_skips_malformed_keys() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)
    coordinator.api.get_fub_known_format = MagicMock(side_effect={7: ("A4", 90, "landscape")}.get)

    assert coordinator._current_plan_format(MALFORMED_FUBS) == {"7": ("A4", 90, "landscape")}
    assert [call.args for call in coordinator.api.get_fub_known_format.call_args_list] == [(7,), (9,)]


def test_known_format_of_an_entry_that_is_no_dict_is_unknown(comexio_api: ComexioAPI) -> None:
    comexio_api._fub_data = {"9": "garbage"}

    assert comexio_api.get_fub_known_format(9) is None


@pytest.mark.parametrize("bad_key", ["08", "x", "-1"])
def test_placement_check_refuses_a_plan_list_with_a_malformed_key(bad_key: str) -> None:
    assert api_module._verified_plan_ids({"7": {}, bad_key: {}}, "$Fubs") is None


def test_placement_check_refuses_a_plan_list_with_an_id_differing_from_its_key() -> None:
    assert api_module._verified_plan_ids({"7": {}, "12": {"Id": "13"}}, "$Fubs") is None


def test_placement_check_reads_a_canonical_plan_list() -> None:
    assert api_module._verified_plan_ids({"7": {}, "12": {}}, "$Fubs") == {7, 12}


def test_a_created_plan_is_found_despite_a_non_canonical_twin(comexio_api: ComexioAPI) -> None:
    """ "08" is no plan id, so plan 12 is the one plan named 'Pumps' Comexio created."""
    fubs = {"08": {"Name": "Pumps"}, "12": {"Name": "Pumps"}}
    comexio_api.get_raw_config = AsyncMock(return_value={"Fubs": fubs})  # type: ignore[method-assign]

    created = asyncio.run(comexio_api._find_created_plan("Pumps", set()))

    assert created is not None
    assert created.fub_id == 12


def test_a_created_plan_is_not_taken_from_an_entry_whose_id_differs_from_its_key(comexio_api: ComexioAPI) -> None:
    """The entry under "12" carries plan 13: returning 12 could make a restore write into another plan."""
    comexio_api.get_raw_config = AsyncMock(return_value={"Fubs": {"12": {"Id": 13, "Name": "Pumps"}}})  # type: ignore[method-assign]

    assert asyncio.run(comexio_api._find_created_plan("Pumps", set())) is None


def test_a_created_plan_is_found_in_a_plan_list_sent_as_an_array(comexio_api: ComexioAPI) -> None:
    """Comexio sends a gap-free id group as a JSON array: the index is the plan id."""
    fubs = [{"Id": 0, "Name": "Lights"}, {"Id": 1, "Name": "Pumps"}]
    comexio_api.get_raw_config = AsyncMock(return_value={"Fubs": fubs})  # type: ignore[method-assign]

    created = asyncio.run(comexio_api._find_created_plan("Pumps", {"0"}))

    assert created is not None
    assert created.fub_id == 1


@pytest.mark.parametrize(
    ("plans", "fub_data", "identities"),
    [
        ({8: "plan"}, {}, [(8, "8")]),
        ({8: "plan"}, {"08": {"Name": "Pumps"}}, []),
        ({7: "plan"}, {"7": {"Name": "Pumps"}, "08": {"Name": "Lights"}}, [(7, "Pumps")]),
        ({8: "plan"}, {"8": {"Id": 9, "Name": "Pumps"}}, []),
        ({8: "plan"}, {"8": "garbage"}, []),
    ],
    ids=[
        "unlisted",
        "maybe-08",
        "readable-next-to-an-unreadable-entry",
        "own-entry-carries-another-id",
        "own-entry-not-a-dict",
    ],
)
def test_the_auto_backup_skips_a_plan_the_list_may_hide_behind_an_unreadable_entry(
    monkeypatch: pytest.MonkeyPatch, plans: dict, fub_data: dict, identities: list
) -> None:
    """Behind "08", plan 8 has an unknown name: backed up, it would land under the bogus identity (8, "8").

    So does plan 8 whose own entry is unreadable: one carrying plan 9 would even lend it plan 9's name.
    """
    saved: dict = {}

    class _Store:
        def __init__(self, _hass, _version, key: str) -> None:
            self.key = key

        async def async_load(self):
            return saved.get(self.key)

        async def async_save(self, data) -> None:
            saved[self.key] = data

    monkeypatch.setattr(backup_module, "Store", _Store)
    fixture = load_json_fixture("function_plan.json")
    plan = {"elements": fixture["elements"], "connections": fixture["connections"]}
    manager = FunctionPlanBackupManager(MagicMock(), "cx1", None)

    changed = asyncio.run(manager.async_auto_backup(dict.fromkeys(plans, plan), fub_data))

    assert [(entry["fub_id"], entry["plan_name"]) for entry in changed] == identities


def test_service_plan_name_resolves_to_the_real_plan() -> None:
    assert _resolve_fub_id("pumps", {"x": {"Name": "Pumps"}, "9": "garbage", "7": {"Name": "Pumps"}}) == 7


def test_available_plans_list_skips_malformed_keys() -> None:
    assert _available_plans_str(MALFORMED_FUBS) == "'Pumps' (ID 7) (+3 unreadable plan list entries)"


def test_plan_selector_options_skip_malformed_keys() -> None:
    from custom_components.comexio.select import ComexioPlanSelectEntity

    select = ComexioPlanSelectEntity.__new__(ComexioPlanSelectEntity)
    select.coordinator = SimpleNamespace(
        api=SimpleNamespace(fub_data=MALFORMED_FUBS),
        function_plan_backup=SimpleNamespace(orphaned_plans_sync=lambda _plans: False),
        live_plan_list=lambda: MALFORMED_FUBS,
    )

    assert select.options == ["Pumps (ID 7)"]


@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        ({"Fubs": {"7": {"Name": "Pumps"}}}, {7}),
        ({"Fubs": {}}, set()),
        ({"FubModules": {}}, None),
        ({"Fubs": MALFORMED_FUBS}, None),
    ],
    ids=["read", "no-plan-left", "not-read", "unreadable-entry"],
)
def test_a_poll_reads_the_plan_ids_only_from_a_readable_plan_list(raw_config: dict, expected: set[int] | None) -> None:
    assert _readable_plan_ids(raw_config) == expected


def test_the_search_reports_the_plan_list_entries_it_could_not_read() -> None:
    coordinator = SimpleNamespace(
        function_plan_label_maps=lambda: ({}, {}, {}),
        function_plan_knx_label_map=dict,
        function_plan_catalog=SimpleNamespace(async_get_catalog=AsyncMock(return_value={})),
        function_plan_plans={7: {"elements": {}, "connections": {}}},
    )
    api = SimpleNamespace(fub_data=MALFORMED_FUBS)
    with (
        patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, api, None))),
        patch.object(misc.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(misc._handle_function_plan_search(MagicMock(), SimpleNamespace(data={"query": "Lamp"})))

    message = notify.call_args.args[1]
    assert "3 plan(s) could not be loaded" in message
    assert "unreadable plan list entry 'x'" in message


@pytest.mark.parametrize(
    ("live", "is_live", "label"),
    [({"7": {"Name": "Pumps"}}, True, "live"), ({}, False, "deleted"), (None, None, "unknown")],
    ids=["listed", "not-listed", "no-whole-plan-list"],
)
def test_the_restore_options_call_a_plan_unknown_without_a_whole_plan_list(
    live: dict | None, is_live: bool | None, label: str
) -> None:
    """Without a whole plan list a backed-up plan is neither live nor deleted."""
    coordinator = SimpleNamespace(
        live_plan_list=lambda: live,
        function_plan_backup=SimpleNamespace(
            async_backed_up_plans=AsyncMock(return_value=[(7, "Pumps", 2)]),
            async_list_backups=AsyncMock(return_value={}),
        ),
    )
    hass = MagicMock()
    hass.data = {}
    hass.async_add_executor_job = AsyncMock(return_value=None)
    with patch.object(_yaml_sync, "_iter_active_coordinators", return_value=[("e1", coordinator)]):
        asyncio.run(_yaml_sync._refresh_service_descriptions(hass))

    restore_plans = hass.async_add_executor_job.await_args.args[1]
    assert restore_plans == [(7, "Pumps", is_live, 2)]
    assert f"({label}, " in _yaml_sync._build_restore_options(restore_plans)[0]["label"]


@pytest.mark.parametrize(
    ("fub_data", "info"),
    [
        ({"7": {"Id": 7, "Name": "Pumps"}}, {"Id": 7, "Name": "Pumps"}),
        ({}, {}),
        ({"7": "garbage"}, {}),
        ({"7": {"Id": 8, "Name": "Pumps"}}, {}),
    ],
    ids=["listed", "not-listed", "no-dict", "id-differs-from-key"],
)
def test_function_plan_info_is_empty_unless_the_entry_is_readable(fub_data: dict, info: dict) -> None:
    assert function_plan_info(fub_data, 7) == info
    assert function_plan_info(fub_data, "7") == info


def test_an_entry_whose_id_differs_from_its_key_is_unreadable() -> None:
    """Checklist "Data read from Comexio": an Id the entry carries must match its key."""
    fub_data = {"5": {"Id": 7, "Name": "HA - Marker 1-50"}, "7": {"Id": 7, "Name": "Pumps"}, "8": {"Name": "Lights"}}
    assert unreadable_plan_keys(fub_data) == ["5"]
    assert [fid for fid, _ in function_plans(fub_data)] == [7, 8]


def test_active_flag_of_an_entry_that_is_no_dict_is_unknown(comexio_api: ComexioAPI) -> None:
    comexio_api._fub_data = {"9": "garbage"}
    assert comexio_api.get_fub_active(9) is None


def test_function_plan_info_reads_a_listing_keyed_by_int() -> None:
    assert function_plan_info({7: {"Name": "Pumps"}}, 7) == {"Name": "Pumps"}


def test_no_new_extension_joins_an_io_plan_while_the_list_may_hide_its_plan() -> None:
    """Its IO plan may be the unreadable entry: joining another plan would wire its IOs twice."""
    coordinator = _coordinator({"7": {"Name": "HA - IO EXT1"}, "8": "garbage"})
    coordinator._function_plan_prefix = lambda: "HA"
    coordinator._function_plan_cluster_size = lambda: 100
    coordinator._stale_plan_map_entries = lambda _fub_data: {}
    coordinator._io_plan_membership = lambda _prefix: {7: ["EXT1"]}
    coordinator._io_plan_name = lambda _prefix, members: f"HA - IO {' '.join(members)}"
    coordinator._join_or_create_io_plan = AsyncMock()
    coordinator._persist_plan_map = AsyncMock()

    result, created, failed = asyncio.run(coordinator.resolve_io_clusters(["EXT1", "EXT2"]))

    assert result == {"EXT1": (7, 0)}
    assert (created, failed) == (set(), ["EXT2"])
    coordinator._join_or_create_io_plan.assert_not_awaited()


@pytest.mark.parametrize("fub_id", ["9", "Pumps"])
def test_a_service_refuses_a_plan_whose_list_entry_is_unreadable(fub_id: str) -> None:
    """Name and run state of plan 9 are unknown: a connect would restart a plan the user had stopped."""
    coordinator = _coordinator({"7": {"Name": "Pumps"}, "9": {"Id": 10, "Active": 0}})
    coordinator.get_active_function_plan_fub_id = lambda: None
    call = SimpleNamespace(data={"fub_id": fub_id})
    with (
        patch.object(_context, "_resolve_coordinator", return_value=coordinator),
        patch.object(_context, "refuse_while_plans_busy", return_value=False),
        patch.object(_context.persistent_notification, "async_create") as notify,
    ):
        context = asyncio.run(_context._async_get_service_context(MagicMock(), call, "t", do_login=False))
        resolved = _context._resolve_function_plan(MagicMock(), call, "t")

    if fub_id == "9":
        assert (context, resolved) == (None, None)
        assert "plan list entry of plan ID 9 is unreadable" in notify.call_args.args[1]
    else:
        assert context[2] == 7
        assert resolved[2] == 7


@pytest.mark.parametrize(
    ("entry", "stored"), [({"Name": "Pumps"}, True), ("garbage", False)], ids=["readable", "unreadable"]
)
def test_the_pre_change_backup_skips_a_plan_whose_list_entry_is_unreadable(entry, stored: bool) -> None:
    """Stored, it would land under the bogus identity (9, "9") and later read as an orphaned plan's backup."""
    coordinator = _coordinator({"9": entry})
    coordinator.api = SimpleNamespace(
        fub_data={"9": entry},
        get_fub_known_format=lambda _fub_id: None,
        comexio_version="1",
        block_settings={},
        module_configs={},
    )
    coordinator.function_plan_label_maps = lambda: ({}, {}, {})
    coordinator._async_refresh_service_descriptions = AsyncMock()
    coordinator.function_plan_backup = SimpleNamespace(async_change_backup=AsyncMock())

    asyncio.run(coordinator.async_function_plan_change_backup(9, "connect", plan_data={"Elements": {"1": {}}}))

    assert coordinator.function_plan_backup.async_change_backup.await_count == int(stored)


@pytest.mark.parametrize(
    ("fub_data", "still_live"),
    [({"5": "garbage"}, True), ({"05": {"Name": "Lights"}}, True), ({}, False), ({"7": {"Name": "Pumps"}}, False)],
    ids=["unreadable", "maybe-behind-an-unreadable-key", "gone", "gone-from-a-readable-list"],
)
def test_a_restore_keeps_the_bare_pick_while_the_old_id_entry_is_unreadable(fub_data: dict, still_live: bool) -> None:
    coordinator = _coordinator(fub_data)
    coordinator.config_entry = SimpleNamespace(options={})
    with patch.object(coordinator_module, "repointed_watchdog_user_plan_picks", return_value=None) as repoint:
        coordinator._repoint_vanished_pick("Pumps", 5, 6)

    assert repoint.call_args.kwargs == {"old_id_still_live": still_live}


@pytest.mark.parametrize("entry", ["garbage", {"Id": 10, "Name": "Pumps"}], ids=["no-dict", "id-differs-from-key"])
def test_paper_settings_of_a_plan_whose_list_entry_is_unreadable_are_not_saved(
    comexio_api: ComexioAPI, entry: object
) -> None:
    comexio_api._fub_data = {"9": entry}
    comexio_api._client = MagicMock()

    assert asyncio.run(comexio_api.function_plan_update_paper(9, "A4", 90, "landscape")) is False
    assert comexio_api._client.mock_calls == []


@pytest.mark.parametrize(
    ("fub_data", "audit"),
    [
        ({"9": {"Id": 10, "Name": "HA - TRIGGER"}}, None),
        ({"9": {"Name": "Pumps"}}, ([1], [])),
    ],
    ids=["trigger-plan-unreadable", "mapping-stale"],
)
def test_the_trigger_audit_waits_while_the_trigger_plan_may_be_an_unreadable_entry(fub_data: dict, audit) -> None:
    """Unknown, not "every trigger source missing": a sync would only try to create a second trigger plan."""
    coordinator = _coordinator(fub_data)
    coordinator.api = SimpleNamespace(fub_data=fub_data, flanke_ref_id=lambda: 5)
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - TRIGGER": 9}})

    assert coordinator._audit_trigger_pairs([1], 2) == audit


def test_a_live_plan_service_refuses_a_plan_the_list_may_hide_behind_an_unreadable_entry() -> None:
    """Plan 5 is listed under "05": a connect would read it as stopped-unknown and restart it."""
    coordinator = _coordinator({"7": {"Name": "Pumps"}, "05": {"Name": "Lights", "Active": 0}})
    coordinator.get_active_function_plan_fub_id = lambda: None
    call = SimpleNamespace(data={"fub_id": "5"})
    with (
        patch.object(_context, "_resolve_coordinator", return_value=coordinator),
        patch.object(_context, "refuse_while_plans_busy", return_value=False),
        patch.object(_context.persistent_notification, "async_create") as notify,
    ):
        resolved = _context._resolve_function_plan(MagicMock(), call, "t")
        backup_context = asyncio.run(_context._async_get_service_context(MagicMock(), call, "t", do_login=False))

    assert resolved is None
    assert "Plan ID 5 is not readable in the plan list" in notify.call_args_list[0].args[1]
    # A backup service still reaches the backups of a plan 5 that may be gone.
    assert backup_context[2] == 5


def test_the_pre_change_backup_skips_a_plan_the_list_may_hide_behind_an_unreadable_entry() -> None:
    fub_data = {"05": {"Name": "Lights"}}
    coordinator = _coordinator(fub_data)
    coordinator.api = SimpleNamespace(
        fub_data=fub_data,
        get_fub_known_format=lambda _fub_id: None,
        comexio_version="1",
        block_settings={},
        module_configs={},
    )
    coordinator.function_plan_label_maps = lambda: ({}, {}, {})
    coordinator._async_refresh_service_descriptions = AsyncMock()
    coordinator.function_plan_backup = SimpleNamespace(async_change_backup=AsyncMock())

    asyncio.run(coordinator.async_function_plan_change_backup(5, "connect", plan_data={"Elements": {"1": {}}}))

    coordinator.function_plan_backup.async_change_backup.assert_not_awaited()


def test_a_run_state_does_not_rewrite_an_unreadable_entry(comexio_api: ComexioAPI) -> None:
    """Its active flag reads as unknown, so every run-state tick would count it as changed."""
    comexio_api._fub_data = {"9": {"Id": 10, "Name": "Pumps", "Active": 0}}

    assert comexio_api.apply_fub_run_states({9: True}) is False
    assert comexio_api._fub_data["9"]["Active"] == 0


def _fresh_audit() -> tuple[dict, set[str]]:
    """A deferred plan-derived audit: empty plan buckets, only a non-plan mismatch."""
    buckets = dict.fromkeys(coordinator_module._PLAN_AUDIT_BUCKETS, {})
    return {"missing": {"m1": {}}, **buckets}, {"missing_m1"}


def test_an_audit_keeps_the_plan_findings_while_the_plan_list_is_unreadable() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)
    coordinator._plan_audit_buckets = {
        **dict.fromkeys(coordinator_module._PLAN_AUDIT_BUCKETS, {}),
        "function_plan_dangling": {"M": [5]},
        "knx_bridge_missing": {"K3": {}},
    }
    coordinator._plan_audit_mismatches = {"function_plan_dangling_M5", "knx_bridge_missing_K3"}
    results, mismatches = _fresh_audit()
    mismatches.add("function_plan_missing_m9")  # judged on a list that may hide its plan

    assert coordinator._carry_over_plan_audit(results, mismatches, False)

    assert results["function_plan_dangling"] == {"M": [5]}
    assert results["knx_bridge_missing"] == {"K3": {}}
    assert results["missing"] == {"m1": {}}
    assert mismatches == {"missing_m1", "function_plan_dangling_M5", "knx_bridge_missing_K3"}


def test_an_audit_of_a_readable_plan_list_replaces_the_plan_findings() -> None:
    coordinator = _coordinator({"7": {"Name": "Pumps"}})
    coordinator._plan_audit_buckets = {"function_plan_dangling": {"M": [5]}}
    coordinator._plan_audit_mismatches = {"function_plan_dangling_M5"}
    results, mismatches = _fresh_audit()
    mismatches.add("function_plan_missing_m2")

    assert coordinator._carry_over_plan_audit(results, mismatches, False)

    assert results["function_plan_dangling"] == {}
    assert mismatches == {"missing_m1", "function_plan_missing_m2"}
    assert coordinator._plan_audit_buckets == dict.fromkeys(coordinator_module._PLAN_AUDIT_BUCKETS, {})
    assert coordinator._plan_audit_mismatches == {"function_plan_missing_m2"}


def test_a_first_audit_of_an_unreadable_plan_list_leaves_the_plan_findings_unknown() -> None:
    coordinator = _coordinator(MALFORMED_FUBS)
    coordinator._plan_audit_buckets = None
    coordinator._plan_audit_mismatches = set()
    results, mismatches = _fresh_audit()
    mismatches.add("function_plan_missing_m9")

    assert not coordinator._carry_over_plan_audit(results, mismatches, False)

    assert mismatches == {"missing_m1"}
    assert coordinator._plan_audit_buckets is None


def _audit_coordinator(fub_data: dict) -> ComexioCoordinator:
    """A coordinator whose audit steps are stubbed: no finding unless a test makes one."""
    coordinator = _coordinator(fub_data)
    coordinator.api.build_webio_commands = MagicMock(return_value=[])
    coordinator.ha_address = SimpleNamespace(
        async_target=AsyncMock(return_value=WebhookTarget("10.0.0.2", False)), fallback_reason=None
    )
    coordinator.last_audit_results = {}
    coordinator._plan_audit_buckets = None
    coordinator._plan_audit_mismatches = set()
    coordinator._cleanup_entity_ids = []
    coordinator._cleanup_function_plan_count = 0
    coordinator._ignored_ids_for = MagicMock(return_value=set())
    coordinator.config_entry = SimpleNamespace(options={})
    coordinator.offline_extensions = set()
    coordinator._build_ha_audit_map = MagicMock(return_value=({}, {}))
    coordinator._update_import_disabled_issues = MagicMock()
    coordinator._report_missing_webio_classes = MagicMock(return_value=False)
    coordinator._async_audit_webio_devices = AsyncMock(return_value={})
    coordinator._has_active_function_plan = MagicMock(return_value=True)
    coordinator._audit_wired_pairs = MagicMock(return_value=(set(), set(), set(), set()))
    found = dict.fromkeys(("type", "missing", "rename", "orphan", "function_plan_missing"), {})
    coordinator._compare_audit_maps = MagicMock(return_value=found)
    coordinator._function_plan_dangling_items = MagicMock(return_value={})
    coordinator._audit_trigger_constructs = MagicMock(return_value=({}, {}))
    coordinator._audit_knx_bridge_items = MagicMock(return_value=({}, {}))
    coordinator._log_audit_summary = MagicMock()
    coordinator._update_sync_mismatch_issue = MagicMock()
    return coordinator


def _missing_m1(*args) -> dict:
    args[-1].add("missing_m1")
    return {**dict.fromkeys(("type", "rename", "orphan", "function_plan_missing"), {}), "missing": {"m1": {}}}


def _waiting_for_plans(coordinator: ComexioCoordinator):
    """A dangling check whose plans are not loaded yet: it flags the recheck and judges nothing."""

    def check(*_args) -> dict:
        coordinator._lp_missing_recheck_pending = True
        return {}

    return check


def _dangling_m5(_final_data, _wired, _has_plan, _exts, mismatches: set[str]) -> dict:
    mismatches.add("function_plan_dangling_M5")
    return {"M": [5]}


def _open_repair(translation_key: str, **counts: int) -> SimpleNamespace:
    return SimpleNamespace(translation_key=translation_key, data={"counts": counts})


def _run_audit(coordinator: ComexioCoordinator, open_issue: SimpleNamespace | None = None) -> None:
    registry = SimpleNamespace(async_get_issue=MagicMock(return_value=open_issue))
    with (
        patch.object(coordinator_module.ir, "async_get", return_value=registry),
        patch.object(coordinator_module, "_split_disabled_commands", return_value=({}, {})),
        patch.object(coordinator_module, "_build_com_audit_map", return_value={}),
        patch.object(coordinator_module, "active_webio_classes", return_value=[]),
        patch.object(coordinator_module, "webio_device_hints", return_value=()),
    ):
        assert asyncio.run(coordinator._async_audit_webio({}, {"webio_commands": {}, "knx": []}, {}))


def test_a_poll_of_an_unreadable_plan_list_keeps_the_open_plan_findings() -> None:
    coordinator = _audit_coordinator({"7": {"Name": "Pumps"}})
    coordinator._function_plan_dangling_items = MagicMock(side_effect=_dangling_m5)
    _run_audit(coordinator)
    # A missing Web-IO class in between empties last_audit_results; the plan findings survive it.
    coordinator.last_audit_results = {}

    coordinator.api.fub_data = MALFORMED_FUBS
    coordinator._function_plan_dangling_items = MagicMock(return_value={})  # deferred: nothing judged
    _run_audit(coordinator)

    assert coordinator.last_audit_results["function_plan_dangling"] == {"M": [5]}
    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == {"function_plan_dangling_M5"}


def test_a_first_poll_of_an_unreadable_plan_list_keeps_open_plan_findings_and_reports_no_sync() -> None:
    coordinator = _audit_coordinator(MALFORMED_FUBS)  # e.g. the reload after a sync

    _run_audit(coordinator, _open_repair("sync_mismatch", missing=0, function_plan_dangling=2))

    # The repair is rewritten from the open plan findings (see the _update_sync_mismatch_issue tests).
    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == set()
    assert coordinator._update_sync_mismatch_issue.call_args.kwargs == {"plan_audit_known": False}
    coordinator._log_audit_summary.assert_not_called()


@pytest.mark.parametrize(
    "open_issue",
    [
        _open_repair("sync_mismatch", missing=3, rename=1, function_plan_dangling=0),
        _open_repair("missing_webio_class"),
        None,
    ],
    ids=["delta-findings", "missing-class", "none"],
)
def test_a_first_poll_of_an_unreadable_plan_list_clears_a_repair_without_plan_findings(
    open_issue: SimpleNamespace | None,
) -> None:
    coordinator = _audit_coordinator(MALFORMED_FUBS)

    _run_audit(coordinator, open_issue)

    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == set()
    coordinator._log_audit_summary.assert_not_called()  # the plan findings are unknown: no "in sync"


def test_a_first_poll_of_an_unreadable_plan_list_reports_the_other_findings() -> None:
    coordinator = _audit_coordinator(MALFORMED_FUBS)
    coordinator._compare_audit_maps = MagicMock(side_effect=_missing_m1)

    _run_audit(coordinator, _open_repair("sync_mismatch", function_plan_dangling=2))

    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == {"missing_m1"}


def test_a_readable_poll_waiting_for_unloaded_plans_keeps_the_known_plan_findings() -> None:
    coordinator = _audit_coordinator({"7": {"Name": "Pumps"}})
    coordinator._function_plan_dangling_items = MagicMock(side_effect=_dangling_m5)
    _run_audit(coordinator)
    # e.g. the first poll after a reload: the plan checks wait for the backup cycle.
    coordinator._function_plan_dangling_items = MagicMock(side_effect=_waiting_for_plans(coordinator))
    _run_audit(coordinator)
    assert coordinator._lp_missing_recheck_pending  # the backup cycle still rechecks

    coordinator.api.fub_data = MALFORMED_FUBS
    _run_audit(coordinator)

    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == {"function_plan_dangling_M5"}


def test_a_complete_readable_poll_replaces_the_plan_findings_while_a_recheck_is_still_pending() -> None:
    coordinator = _audit_coordinator({"7": {"Name": "Pumps"}})
    coordinator._function_plan_dangling_items = MagicMock(side_effect=_dangling_m5)
    _run_audit(coordinator)
    # Left over by an earlier unreadable or deferred poll: only the backup cycle clears it.
    coordinator._lp_missing_recheck_pending = True
    coordinator._function_plan_dangling_items = MagicMock(return_value={})  # M5 fixed, every check ran
    _run_audit(coordinator)
    assert coordinator._lp_missing_recheck_pending

    coordinator.api.fub_data = MALFORMED_FUBS
    _run_audit(coordinator)

    assert coordinator.last_audit_results["function_plan_dangling"] == {}
    assert "function_plan_dangling_M5" not in coordinator._update_sync_mismatch_issue.call_args.args[0]


def test_an_unknown_plan_audit_rewrites_the_repair_without_dropping_its_plan_findings() -> None:
    coordinator = _audit_coordinator(MALFORMED_FUBS)
    coordinator._compare_audit_maps = MagicMock(side_effect=_missing_m1)

    _run_audit(coordinator, _open_repair("sync_mismatch", function_plan_dangling=2))

    assert coordinator._update_sync_mismatch_issue.call_args.kwargs == {"plan_audit_known": False}


def _issue_writer(fub_data: dict) -> ComexioCoordinator:
    coordinator = _coordinator(fub_data)
    coordinator.last_audit_results = {
        **dict.fromkeys(("type", "rename", "orphan"), {}),
        **dict.fromkeys(coordinator_module._PLAN_AUDIT_BUCKETS, {}),
        "missing": {"m1": {}},
        "ip_mismatch": False,
    }
    coordinator._cleanup_entity_ids = []
    coordinator._cleanup_function_plan_count = 0
    coordinator.config_entry = SimpleNamespace(entry_id="e1")
    coordinator._function_plan_missing_eta_sec = MagicMock(return_value=0)
    coordinator._function_plan_missing_detail = MagicMock(return_value=[])
    return coordinator


@pytest.mark.parametrize(("known", "dangling", "detail"), [(False, 2, ["M5"]), (True, 0, [])])
def test_the_repair_keeps_its_plan_findings_only_while_they_are_unknown(
    known: bool, dangling: int, detail: list
) -> None:
    coordinator = _issue_writer(MALFORMED_FUBS)
    open_issue = SimpleNamespace(
        translation_key="sync_mismatch",
        data={"counts": {"missing": 0, "function_plan_dangling": 2}, "function_plan_missing_detail": ["M5"]},
    )
    registry = SimpleNamespace(async_get_issue=MagicMock(return_value=open_issue))
    with (
        patch.object(coordinator_module.ir, "async_get", return_value=registry),
        patch.object(coordinator_module.ir, "async_create_issue") as create_issue,
    ):
        coordinator._update_sync_mismatch_issue({"missing_m1"}, {}, {}, {}, plan_audit_known=known)

    data = create_issue.call_args.kwargs["data"]
    assert data["counts"]["missing"] == 1
    assert data["counts"]["function_plan_dangling"] == dangling
    assert data["function_plan_missing_detail"] == detail


def test_a_poll_of_a_readable_plan_list_in_sync_clears_the_repair() -> None:
    coordinator = _audit_coordinator({"7": {"Name": "Pumps"}})

    _run_audit(coordinator)

    assert coordinator._update_sync_mismatch_issue.call_args.args[0] == set()
    coordinator._log_audit_summary.assert_called_once_with(set(), plan_audit_known=True)


def _write_issue(
    mismatches: set[str], open_issue: SimpleNamespace | None, *, known: bool
) -> tuple[MagicMock, MagicMock]:
    coordinator = _issue_writer(MALFORMED_FUBS)
    coordinator.last_audit_results["missing"] = {}  # the delta was fixed
    registry = SimpleNamespace(async_get_issue=MagicMock(return_value=open_issue))
    with (
        patch.object(coordinator_module.ir, "async_get", return_value=registry),
        patch.object(coordinator_module.ir, "async_create_issue") as create_issue,
        patch.object(coordinator_module.ir, "async_delete_issue") as delete_issue,
    ):
        coordinator._update_sync_mismatch_issue(mismatches, {}, {}, {}, plan_audit_known=known)
    return create_issue, delete_issue


def test_an_unknown_plan_audit_with_nothing_else_open_rewrites_the_repair_from_its_plan_findings() -> None:
    open_issue = SimpleNamespace(
        translation_key="sync_mismatch",
        data={"counts": {"missing": 3, "function_plan_dangling": 2}, "function_plan_missing_detail": ["M5"]},
    )
    create_issue, delete_issue = _write_issue(set(), open_issue, known=False)

    delete_issue.assert_not_called()
    counts = create_issue.call_args.kwargs["data"]["counts"]
    assert (counts["missing"], counts["function_plan_dangling"]) == (0, 2)  # the fixed delta drops out


@pytest.mark.parametrize(
    ("open_issue", "known"),
    [
        (_open_repair("sync_mismatch", missing=3, function_plan_dangling=0), False),
        (_open_repair("missing_webio_class"), False),
        (None, False),
        (_open_repair("sync_mismatch", function_plan_dangling=2), True),
    ],
    ids=["delta-only", "missing-class", "none", "known"],
)
def test_a_repair_without_unknown_plan_findings_is_cleared_once_nothing_mismatches(
    open_issue: SimpleNamespace | None, known: bool
) -> None:
    create_issue, delete_issue = _write_issue(set(), open_issue, known=known)

    delete_issue.assert_called_once()
    create_issue.assert_not_called()


def test_an_unknown_plan_audit_is_named_in_the_audit_summary() -> None:
    coordinator = _issue_writer(MALFORMED_FUBS)
    coordinator.last_summary_hash = None
    coordinator._last_logged_mismatches = frozenset()
    coordinator.last_audit_results |= {"missing": {}, "cleanup_entities": [], "webio_devices": {}, "ha_address": "h"}
    with patch.object(coordinator_module, "_LOGGER") as logger:
        coordinator._log_audit_summary({"ip_address"}, plan_audit_known=False)

    assert any("Function plan findings unknown" in call.args[0] for call in logger.warning.call_args_list)


def test_a_failing_plan_check_keeps_a_pending_recheck() -> None:
    coordinator = _audit_coordinator({"7": {"Name": "Pumps"}})
    coordinator._lp_missing_recheck_pending = True
    coordinator._function_plan_dangling_items = MagicMock(side_effect=KeyError("Id"))
    registry = SimpleNamespace(async_get_issue=MagicMock(return_value=None))
    with (
        patch.object(coordinator_module.ir, "async_get", return_value=registry),
        patch.object(coordinator_module, "_split_disabled_commands", return_value=({}, {})),
        patch.object(coordinator_module, "_build_com_audit_map", return_value={}),
        patch.object(coordinator_module, "active_webio_classes", return_value=[]),
        patch.object(coordinator_module, "webio_device_hints", return_value=()),
    ):
        audit = coordinator._async_audit_webio({}, {"webio_commands": {}, "knx": []}, {})
        with pytest.raises(KeyError):
            asyncio.run(audit)

    assert coordinator._lp_missing_recheck_pending


def _cleanup_coordinator(fub_data: dict) -> ComexioCoordinator:
    coordinator = _coordinator(fub_data)
    coordinator.api.function_plan_name = lambda fub_id: str(function_plan_info(fub_data, fub_id).get("Name") or fub_id)
    coordinator.function_plan_plans = {7: {"elements": {}}, 12: {"elements": {}}}
    coordinator._function_plan_check_fub_ids = lambda: [7, 12]
    coordinator.config_entry = SimpleNamespace(options={})
    return coordinator


@pytest.mark.parametrize(
    ("fub_data", "skipped"),
    [
        ({"7": {"Name": "HA - Marker 1-50"}, "12": {"Name": "Pumps"}}, []),
        ({"7": {"Name": "HA - Marker 1-50"}, "12": {"Id": "13", "Name": "HA - Marker 51-100"}}, [12]),
        ({"7": {"Name": "HA - Marker 1-50"}, "x": {}}, [12]),
    ],
)
def test_a_cleanup_reports_a_plan_without_a_readable_list_entry_as_skipped(fub_data: dict, skipped: list) -> None:
    """Managed or not is unknown for it: its wiring is not checked, which must not read as clean."""
    coordinator = _cleanup_coordinator(fub_data)
    load_failures: list[int] = []
    asyncio.run(coordinator._load_function_plan_check_data(load_failures))

    assert load_failures == skipped
    assert coordinator.plan_load_failures([*load_failures, 7]) == [
        *[(str(fub_id), coordinator_module.PLAN_ENTRY_UNREADABLE) for fub_id in skipped],
        ("HA - Marker 1-50", coordinator_module.PLAN_LOAD_FAILED),
    ]


@pytest.mark.parametrize(("fub_data", "offered"), [({"7": {}}, True), ({"7": {}, "x": {}}, False)])
def test_the_repair_offers_plan_wiring_only_while_the_plan_list_is_readable(fub_data: dict, offered: bool) -> None:
    """The sync refuses it then, and the non-blocking press would report it as executed."""
    hass = SimpleNamespace(data={DOMAIN: {"e1": SimpleNamespace(api=SimpleNamespace(fub_data=fub_data))}})
    options = {"create_missing": "a", "function_plan_add_missing": "b", "knx_bridge_add_missing": "c"}

    kept = repairs._without_refused_plan_options(hass, "e1", options)

    assert kept == (options if offered else {"create_missing": "a"})


@pytest.mark.parametrize(("fub_data", "offered"), [({"7": {}}, True), ({"7": {}, "x": {}}, False)])
def test_the_sync_repair_dialog_drops_plan_wiring_only_while_the_plan_list_is_unreadable(
    fub_data: dict, offered: bool
) -> None:
    coordinator = SimpleNamespace(in_sync=False, api=SimpleNamespace(fub_data=fub_data))
    flow = repairs.ComexioRepairFlow(
        "sync_mismatch_cx1",
        {"entry_id": "e1", "counts": {"missing": 2, "function_plan_missing": 3, "knx_bridge_missing": 1}},
    )
    flow.hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}}, config=SimpleNamespace(language="en"))
    with patch.object(repairs.ir, "async_get", return_value=MagicMock(async_get_issue=MagicMock(return_value=None))):
        form = asyncio.run(flow.async_step_select_action())

    option_keys = set(form["data_schema"].schema[next(iter(form["data_schema"].schema))].container)
    assert option_keys == (
        {"full_sync", "create_missing", "function_plan_add_missing", "knx_bridge_add_missing"}
        if offered
        else {"create_missing"}
    )


@pytest.mark.parametrize(
    ("include_unknown", "plan_map", "loaded"),
    [
        (False, {"HA - Marker 51-100": 12}, [7]),
        (True, {"HA - Marker 51-100": 12}, [7, 12]),
        # Selector-only: may be the user's own plan. The trigger plan is left to the trigger audit.
        (True, {}, [7]),
        (True, {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 12}, [7]),
    ],
)
def test_the_ignored_source_audit_counts_wiring_in_a_plan_with_an_unreadable_entry(
    include_unknown: bool, plan_map: dict, loaded: list[int]
) -> None:
    """HA's plan 12 may be managed: the audit must not read a leftover there as clean, the cleanup must not touch it."""
    coordinator = _cleanup_coordinator(
        {"7": {"Name": "HA - Marker 1-50"}, "12": {"Id": "13", "Name": "HA - Marker 51-100"}}
    )
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: plan_map})
    load_failures: list[int] = []
    plans = asyncio.run(coordinator._load_managed_plan_check_data(load_failures, include_unknown=include_unknown))

    assert sorted(plans) == loaded
    assert load_failures == [12]  # the cleanup reports it as skipped either way
