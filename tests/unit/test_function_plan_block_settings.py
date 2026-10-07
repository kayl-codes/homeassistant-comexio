"""Block settings ($FubBaseConfig): captured in backups, written back on restore, shown in diff and preview."""

import asyncio
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.comexio import function_plan_backup as backup_module
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from custom_components.comexio.function_plan_block_settings import (
    SNAPSHOT_BLOCK_SETTINGS,
    block_settings_changed,
    block_settings_diff,
    block_settings_form,
    expand_autohide_blocks,
    live_only_settings,
    map_restored_elements,
    parse_block_settings,
    plan_block_settings,
    settings_to_write,
    snapshot_block_settings,
)
from custom_components.comexio.services import backup as backup_service
from tests.common import load_json_fixture

SERVER_ID = "cx1"
# Element 4 of the fixture plan is an Oder block, element 7 a Nicht block; 900 belongs to another plan.
SETTINGS = {"4": {"autohide": "1", "in_0": "26"}, "7": {"time_up": "45", "time_down": "40"}}


@pytest.fixture
def plan() -> dict[str, Any]:
    fixture = load_json_fixture("function_plan.json")
    return {"elements": fixture["elements"], "connections": fixture["connections"]}


def _snapshot(plan: dict[str, Any], settings: dict[str, Any] | None) -> dict[str, Any]:
    snap = copy.deepcopy(plan) | {"plan_name": "Lights"}
    if settings is not None:
        snap[SNAPSHOT_BLOCK_SETTINGS] = copy.deepcopy(settings)
    return snap


# ---------------------------------------------------------------------------
# Parsing and filtering
# ---------------------------------------------------------------------------


def test_parse_reads_the_table_and_skips_rows_without_element_name_or_value(caplog: pytest.LogCaptureFixture) -> None:
    parsed = parse_block_settings(load_json_fixture("block_settings_raw.json"))

    assert parsed == {**SETTINGS, "900": {"dimming_time": "3"}}  # numbers come back as the strings Comexio stores
    assert "2 row(s)" in caplog.text


@pytest.mark.parametrize(("raw", "expected"), [([], {}), (None, None), ("", None), (5, None)])
def test_parse_tells_an_empty_table_from_a_missing_one(raw: Any, expected: dict | None) -> None:
    # PHP serializes an empty table as [] — that is "no settings", not "not read".
    assert parse_block_settings(raw) == expected


def test_a_table_without_one_usable_row_is_not_read_as_no_settings() -> None:
    # A changed row format must not rotate a settings-less backup in for every plan.
    assert parse_block_settings({"1": {"ElementId": 4, "Key": "autohide", "Val": "1"}}) is None


def test_parse_accepts_the_list_form() -> None:
    rows = [{"FubElementId": 4, "Name": "autohide", "Value": "1"}, "junk"]
    assert parse_block_settings(rows) == {"4": {"autohide": "1"}}


def test_plan_settings_keep_only_the_plans_elements(plan: dict[str, Any]) -> None:
    assert plan_block_settings({**SETTINGS, "900": {"dimming_time": "3"}}, plan["elements"]) == SETTINGS


# ---------------------------------------------------------------------------
# Auto backup: a settings change alone stores a snapshot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "current", "changed"),
    [
        (SETTINGS, SETTINGS, False),
        (SETTINGS, {**SETTINGS, "7": {"time_up": "50", "time_down": "40"}}, True),
        (None, SETTINGS, True),  # a snapshot from before the capture gets one successor that has them
        (None, {}, False),  # ... but not for a plan whose blocks have none
        (SETTINGS, None, False),  # not read this poll
    ],
)
def test_block_settings_changed(previous: dict | None, current: dict | None, changed: bool) -> None:
    prev = {} if previous is None else {SNAPSHOT_BLOCK_SETTINGS: previous}
    assert block_settings_changed(prev, current) is changed


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store, keyed by storage key."""

    saved: dict[str, Any] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.saved.get(self.key)

    async def async_save(self, data: Any) -> None:
        FakeStore.saved[self.key] = data


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    FakeStore.saved = {}
    monkeypatch.setattr(backup_module, "Store", FakeStore)
    return FakeStore.saved


def test_auto_backup_rotates_on_a_settings_change_alone(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}
    changed_settings = {**SETTINGS, "7": {"time_up": "50", "time_down": "40"}, "900": {"x": "1"}}

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: plan}, fub_data, block_settings=SETTINGS))]
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, block_settings=SETTINGS)))
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, block_settings=None)))
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, block_settings=changed_settings)))
        return rotated

    assert asyncio.run(run()) == [1, 0, 0, 1]
    history = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert history[0][SNAPSHOT_BLOCK_SETTINGS] == {**SETTINGS, "7": {"time_up": "50", "time_down": "40"}}
    assert history[1][SNAPSHOT_BLOCK_SETTINGS] == SETTINGS


def test_change_backup_stores_the_plans_settings(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    asyncio.run(manager.async_change_backup(1, plan, "Lights", "sort", block_settings={**SETTINGS, "900": {"x": "1"}}))

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert stored[SNAPSHOT_BLOCK_SETTINGS] == SETTINGS
    assert manager.block_settings_sync("change", 1, "Lights", 0) == SETTINGS


def test_backups_stamp_the_firmware_they_were_taken_on(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """(bn)(8): without the stamp the restore's firmware warning stays silent for every backup."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)

    async def run() -> None:
        await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}, comexio_version="11.1.4")
        await manager.async_change_backup(1, plan, "Lights", "sort", comexio_version="11.1.4")

    asyncio.run(run())

    auto = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"][0]
    change = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert auto[backup_module.SNAPSHOT_COMEXIO_VERSION] == change[backup_module.SNAPSHOT_COMEXIO_VERSION] == "11.1.4"


def test_a_backup_without_the_table_stores_no_settings_key(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    asyncio.run(manager.async_change_backup(1, plan, "Lights", "sort"))

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert SNAPSHOT_BLOCK_SETTINGS not in stored  # "not captured", which a restore reports, not "none"
    assert snapshot_block_settings(stored) is None


# ---------------------------------------------------------------------------
# Restore: matching elements and writing the settings
# ---------------------------------------------------------------------------


def test_map_keeps_ids_that_still_name_the_same_reference(plan: dict[str, Any]) -> None:
    assert map_restored_elements(plan["elements"], plan["elements"]) == {eid: eid for eid in plan["elements"]}


def test_map_matches_reassigned_ids_by_reference_and_position(plan: dict[str, Any]) -> None:
    # force_override onto another plan: run_fup gives every element a fresh id.
    live = {str(int(eid) + 100): elem for eid, elem in plan["elements"].items()}
    assert map_restored_elements(plan["elements"], live) == {eid: str(int(eid) + 100) for eid in plan["elements"]}


def _block_at(x: float, y: float) -> dict[str, Any]:
    return {"reference": {"type": 5, "ref_id": 30}, "position_x": x, "position_y": y}


def test_map_follows_the_place_when_same_type_blocks_swapped_ids() -> None:
    # force_override: run_fup's fresh ids can name another block of the same type.
    snapshot = {"4": _block_at(10, 10), "5": _block_at(50, 50)}
    live = {"5": _block_at(10, 10), "4": _block_at(50, 50)}
    assert map_restored_elements(snapshot, live) == {"4": "5", "5": "4"}


def test_map_falls_back_to_the_same_id_when_the_position_was_not_restored() -> None:
    assert map_restored_elements({"4": _block_at(10, 10)}, {"4": _block_at(90, 90)}) == {"4": "4"}


def test_map_gives_an_ambiguous_place_no_same_id_fallback() -> None:
    # Two blocks of its type at its place, neither with its id: the same id elsewhere proves nothing.
    snapshot = {"4": _block_at(10, 10)}
    live = {"5": _block_at(10, 10), "6": _block_at(10, 10), "4": _block_at(90, 90)}
    assert map_restored_elements(snapshot, live) == {}


def test_map_leaves_stacked_blocks_with_one_live_counterpart_unmapped() -> None:
    snapshot = {"4": _block_at(10, 10), "5": _block_at(10, 10)}
    assert map_restored_elements(snapshot, {"104": _block_at(10, 10)}) == {}


def test_map_leaves_out_an_element_without_a_unique_match(plan: dict[str, Any]) -> None:
    live = {"104": plan["elements"]["4"], "204": copy.deepcopy(plan["elements"]["4"])}
    assert "4" not in map_restored_elements(plan["elements"], live)


def test_settings_to_write_follows_the_id_map() -> None:
    writes, unmapped = settings_to_write(SETTINGS, {"4": 104})
    assert writes == {"104": SETTINGS["4"]}
    assert unmapped == ["7"]


def test_form_matches_the_editor_request() -> None:
    form = block_settings_form(104, {"in_0": "26", "autohide": "1"}, "TS")
    assert form == {"id": "104", "element_in_0": "26", "element_autohide": "1", "timestamp": "TS"}


def _restore_api(plan: dict[str, Any], save_ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(
            return_value=({eid: int(eid) + 100 for eid in plan["elements"]}, len(plan["connections"]), [])
        ),
        function_plan_run_fup=AsyncMock(return_value=True),
        function_plan_save_block_settings=AsyncMock(return_value=save_ok),
        block_settings=None,
    )


def _restore_as_new(api: SimpleNamespace, snapshot: dict[str, Any]) -> tuple[str, str]:
    coordinator = SimpleNamespace(
        server_id=SERVER_ID,
        function_plan_backup=SimpleNamespace(async_rekey_fub_id=AsyncMock()),
        async_repoint_function_plan_fub_id=AsyncMock(return_value=[]),
    )
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(backup_service._restore_plan_as_new(MagicMock(), coordinator, api, 1, snapshot, "auto", 0))
    return notify.call_args.kwargs["title"], notify.call_args.args[1]


def test_restore_as_new_writes_the_settings_onto_the_new_elements(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    title, message = _restore_as_new(api, _snapshot(plan, SETTINGS))

    saved = {call.args[0]: call.args[1] for call in api.function_plan_save_block_settings.await_args_list}
    assert saved == {"104": SETTINGS["4"], "107": SETTINGS["7"]}
    assert title.endswith("OK")
    assert f"Block settings: {backup_service.ICON_SUCCESS} 4 written" in message


def test_a_failed_settings_save_makes_the_restore_partial(plan: dict[str, Any]) -> None:
    title, message = _restore_as_new(_restore_api(plan, save_ok=False), _snapshot(plan, SETTINGS))

    assert title.endswith("PARTIAL")
    assert "saving failed for restored element(s) #104, #107" in message


def test_restore_as_copy_writes_the_settings_and_reports_a_failed_save(plan: dict[str, Any]) -> None:
    api = _restore_api(plan, save_ok=False)
    coordinator = SimpleNamespace(server_id=SERVER_ID)
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup_service._restore_plan_as_copy(
                MagicMock(), coordinator, api, 1, _snapshot(plan, SETTINGS), "auto", 0, "Lights copy", True
            )
        )

    saved = {call.args[0]: call.args[1] for call in api.function_plan_save_block_settings.await_args_list}
    assert saved == {"104": SETTINGS["4"], "107": SETTINGS["7"]}
    assert notify.call_args.kwargs["title"].endswith("PARTIAL")
    assert "Block settings:" in notify.call_args.args[1]


def test_a_connection_drop_while_restoring_as_new_replaces_the_progress_notice(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    api.function_plan_save_block_settings.side_effect = aiohttp.ClientError("gone")

    title, message = _restore_as_new(api, _snapshot(plan, SETTINGS))

    assert title == backup_service._TITLE_RESTORE_ERR
    assert "failed while building" in message


def test_an_old_backup_says_its_settings_were_not_captured(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    title, message = _restore_as_new(api, _snapshot(plan, None))

    api.function_plan_save_block_settings.assert_not_awaited()
    assert title.endswith("OK")  # nothing was lost by this restore — the backup never had them
    assert "not in this backup" in message


def test_settings_onto_an_existing_plan_follow_the_reloaded_elements(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    live = copy.deepcopy(plan)
    live["elements"] = {str(int(eid) + 100): elem for eid, elem in plan["elements"].items()}

    result = asyncio.run(backup_service._restore_block_settings(api, 1, _snapshot(plan, SETTINGS), live_plan=live))

    assert result == {"included": True, "written": 4, "failed": [], "unmapped": [], "live_only": {}}
    assert {call.args[0] for call in api.function_plan_save_block_settings.await_args_list} == {"104", "107"}


def test_settings_onto_an_existing_plan_need_the_reloaded_plan(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)

    result = asyncio.run(backup_service._restore_block_settings(api, 1, _snapshot(plan, SETTINGS), live_plan=None))

    assert not backup_service._block_settings_ok(result)
    assert "not restored" in backup_service._block_settings_line(result)
    api.function_plan_save_block_settings.assert_not_awaited()


def test_an_unmatched_element_is_reported(plan: dict[str, Any]) -> None:
    result = asyncio.run(backup_service._restore_block_settings(_restore_api(plan), 1, _snapshot(plan, SETTINGS), {}))

    assert result["unmapped"] == ["4", "7"]
    assert "no restored element for backup element(s) #4, #7" in backup_service._block_settings_line(result)
    assert not backup_service._block_settings_ok(result)


def test_live_only_settings_name_what_the_backup_lacks() -> None:
    live = {"104": {"autohide": "1", "in_0": "26", "in_1": "3"}, "107": {"time_up": "45"}, "900": {"x": "1"}}
    assert live_only_settings(SETTINGS, {"4": "104", "7": "107"}, live) == {"104": ["in_1"]}


def test_a_restore_in_place_reports_settings_it_could_not_remove(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    api.block_settings = {"4": {**SETTINGS["4"], "in_1": "3"}}

    result = asyncio.run(
        backup_service._restore_block_settings(api, 1, _snapshot(plan, SETTINGS), live_plan=copy.deepcopy(plan))
    )

    assert result["live_only"] == {"4": ["in_1"]}
    assert not backup_service._block_settings_ok(result)
    assert "kept settings the backup does not have (Comexio cannot remove them): #4 in_1" in (
        backup_service._block_settings_line(result)
    )


def test_a_backup_without_settings_still_reports_the_live_ones(plan: dict[str, Any]) -> None:
    api = _restore_api(plan)
    api.block_settings = {"7": {"time_up": "45"}}

    result = asyncio.run(backup_service._restore_block_settings(api, 1, _snapshot(plan, {}), live_plan=plan))

    assert (result["written"], result["live_only"]) == (0, {"7": ["time_up"]})
    api.function_plan_save_block_settings.assert_not_awaited()


def _in_place_apply(block_settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_ok": True,
        "pos_ok": True,
        "run_ok": True,
        "stop_ok": True,
        "properties_changed": False,
        "recovered_comments": 0,
        "comment_warnings": [],
        "block_settings": block_settings,
    }


_VERIFY = {
    "reload_ok": True,
    "hash_match": True,
    "counts_match": True,
    "elem_count": 9,
    "conn_count": 6,
    "fresh_elem": 9,
    "fresh_conn": 6,
}


@pytest.mark.parametrize(
    ("block_settings", "status"),
    [
        ({"included": True, "written": 4, "failed": [], "unmapped": []}, "OK"),
        ({"included": False, "written": 0, "failed": [], "unmapped": []}, "OK"),
        ({"included": True, "written": 0, "failed": ["104"], "unmapped": []}, "PARTIAL"),
        ({"included": True, "written": 0, "failed": [], "unmapped": ["4"]}, "PARTIAL"),
        ({"included": True, "written": 0, "failed": [], "unmapped": [], "error": "x"}, "PARTIAL"),
    ],
)
def test_in_place_restore_status_counts_the_block_settings(block_settings: dict[str, Any], status: str) -> None:
    apply = _in_place_apply(block_settings)
    assert backup_service._restore_status(True, apply, _VERIFY) == status
    message = backup_service._restore_build_message(
        "Lights", 1, "auto", 0, {}, status, apply, _VERIFY, False, True, 1.0
    )
    assert "Block settings:" in message


def test_in_place_restore_writes_the_settings_after_run_fup(plan: dict[str, Any]) -> None:
    # run_fup reassigns the element ids on a force_override, so the match needs the plan after it.
    order: list[str] = []

    async def run_fup(*_args: Any, **_kwargs: Any) -> bool:
        order.append("run_fup")
        return True

    async def save(*_args: Any) -> bool:
        order.append("save")
        return True

    api = _restore_api(plan)
    api.fub_data = {"1": {"Name": "Lights", "Active": True}}
    api.get_fub_paper_format = api.get_fub_dpi = api.get_fub_orientation = MagicMock(return_value=None)
    api.function_plan_update_paper = AsyncMock(return_value=True)
    api.function_plan_stop_fup = AsyncMock(return_value=True)
    api.function_plan_save_elements_pos = AsyncMock(return_value=True)
    reloaded = copy.deepcopy(plan)  # run_fup handed the two blocks with settings fresh ids
    for eid in ("4", "7"):
        reloaded["elements"][str(int(eid) + 100)] = reloaded["elements"].pop(eid)
    api.function_plan_load_elements = AsyncMock(return_value=reloaded)
    api.function_plan_run_fup = AsyncMock(side_effect=run_fup)
    api.function_plan_save_block_settings = AsyncMock(side_effect=save)
    coordinator = SimpleNamespace(
        server_id=SERVER_ID,
        async_function_plan_change_backup=AsyncMock(),
        function_plan_backup=SimpleNamespace(async_mark_restored=AsyncMock(return_value=False)),
    )
    snapshot = _snapshot(plan, SETTINGS) | {"hash": "h"}

    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup_service._restore_plan_in_place(MagicMock(), coordinator, api, 1, snapshot, "auto", 0, lambda _p: "h")
        )

    assert order == ["run_fup", "save", "save"]
    saved = {call.args[0]: call.args[1] for call in api.function_plan_save_block_settings.await_args_list}
    assert saved == {"104": SETTINGS["4"], "107": SETTINGS["7"]}
    assert notify.call_args.kwargs["title"].endswith("OK")
    assert f"Block settings: {backup_service.ICON_SUCCESS} 4 written" in notify.call_args.args[1]


# ---------------------------------------------------------------------------
# Backup diff
# ---------------------------------------------------------------------------


def test_diff_lists_changed_settings_of_elements_both_snapshots_have(plan: dict[str, Any]) -> None:
    newer_settings = {"4": {"autohide": "1"}, "7": {"time_up": "50", "time_down": "40", "mode": "2"}}
    older = _snapshot(plan, SETTINGS)
    newer = _snapshot(plan, newer_settings)
    newer["elements"].pop("9")

    assert block_settings_diff(newer, older) == [
        ("4", "in_0", "26", None),
        ("7", "mode", None, "2"),
        ("7", "time_up", "45", "50"),
    ]


def test_diff_is_none_when_a_snapshot_lacks_settings(plan: dict[str, Any]) -> None:
    assert block_settings_diff(_snapshot(plan, SETTINGS), _snapshot(plan, None)) is None


def test_backup_diffs_mark_a_side_stored_without_settings_instead_of_hiding_it(plan: dict[str, Any]) -> None:
    snapshots = {
        0: _snapshot(plan, {**SETTINGS, "7": {"time_up": "50", "time_down": "40"}}),
        1: _snapshot(plan, SETTINGS),
        2: _snapshot(plan, None),
    }

    async def get_snapshot(_kind: str, _fub_id: int, _name: str, slot: int) -> dict[str, Any] | None:
        return snapshots.get(slot)

    coordinator = SimpleNamespace(
        function_plan_label_maps=lambda: ({}, {}, {}),
        function_plan_catalog=SimpleNamespace(async_get_catalog=AsyncMock(return_value={})),
        function_plan_backup=SimpleNamespace(async_get_snapshot=get_snapshot),
    )
    entries = [{"fub_id": 1, "plan_name": "Lights", "slot": 0}, {"fub_id": 1, "plan_name": "Lights", "slot": 1}]

    with patch.object(backup_service, "resolve_element_label", return_value="Nicht"):
        asyncio.run(backup_service._attach_backup_diffs(coordinator, entries, "auto"))

    assert entries[0]["diff"]["block_settings"] == ["Nicht (#7): time_up 45 → 50"]
    # (bn)(1): its predecessor was stored before the capture — said, not silently left out.
    assert entries[1]["diff"]["block_settings"] == ["#nv — no block values stored in the older backup, not compared"]


@pytest.mark.parametrize(
    ("newer_settings", "older_settings", "side"),
    [(SETTINGS, None, "the older backup"), (None, SETTINGS, "this backup"), (None, None, "either backup")],
)
def test_the_nv_line_names_the_side_without_settings(
    plan: dict[str, Any], newer_settings: dict | None, older_settings: dict | None, side: str
) -> None:
    line = backup_service._no_block_settings_line(_snapshot(plan, newer_settings), _snapshot(plan, older_settings))
    assert line == f"#nv — no block values stored in {side}, not compared"


@pytest.mark.parametrize(("settings", "count"), [(SETTINGS, 4), ({}, 0), (None, None)])
def test_list_entries_count_the_stored_block_values(
    plan: dict[str, Any], settings: dict | None, count: int | None
) -> None:
    """(bn)(3): the diff only shows changed values — the count tells how many the backup holds."""
    entry = backup_module._backup_entry("1", "Lights", 0, _snapshot(plan, settings))
    assert entry.get("block_setting_count") == count


def test_list_entries_carry_the_firmware_the_backup_was_taken_on(plan: dict[str, Any]) -> None:
    snap = _snapshot(plan, None) | {backup_module.SNAPSHOT_COMEXIO_VERSION: "11.1.4"}
    assert backup_module._backup_entry("1", "Lights", 0, snap)["comexio_version"] == "11.1.4"
    assert "comexio_version" not in backup_module._backup_entry("1", "Lights", 0, _snapshot(plan, None))


@pytest.mark.parametrize(
    ("stored", "live", "warned"),
    [("11.0.2", "11.1.4", True), ("11.1.4", "11.1.4", False), (None, "11.1.4", False), ("11.0.2", None, False)],
)
def test_a_restore_warns_when_the_backup_was_taken_on_other_firmware(
    plan: dict[str, Any], stored: str | None, live: str | None, warned: bool
) -> None:
    """(bn)(8): the restore goes on, but says the backup predates a firmware change."""
    snap = _snapshot(plan, None)
    if stored is not None:
        snap[backup_module.SNAPSHOT_COMEXIO_VERSION] = stored
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        backup_service._warn_firmware_differs(MagicMock(), snap, 1, "Lights", live)
    assert notify.called is warned
    if warned:
        assert "firmware 11.0.2, Comexio now runs 11.1.4" in notify.call_args.args[1]
        assert notify.call_args.kwargs["notification_id"] == "comexio_restore_firmware_1"


def test_diff_labels_name_the_element_and_mark_missing_values(plan: dict[str, Any]) -> None:
    newer = _snapshot(plan, {})
    with patch.object(backup_service, "resolve_element_label", return_value="Oder"):
        labels = backup_service._label_block_settings_diff([("4", "in_0", "26", None)], newer, {}, ({}, {}, {}))
    assert labels == ["Oder (#4): in_0 26 → —"]


# ---------------------------------------------------------------------------
# Preview: the extended view shows a block's auto-hidden pins
# ---------------------------------------------------------------------------


def test_expanded_blocks_render_without_their_hidden_pins(plan: dict[str, Any]) -> None:
    catalog = {"fub_base": {"5": {"name": "Oder", "in_hide": [2], "out_hide": []}, "23": {"in_hide": [1]}}}

    elements, rendered = expand_autohide_blocks(plan["elements"], catalog, SETTINGS)

    assert elements["4"]["reference"]["ref_id"] == "5:expanded"
    assert rendered["fub_base"]["5:expanded"] == {"name": "Oder", "in_hide": [], "out_hide": []}
    assert elements["7"] is plan["elements"]["7"]  # extended view not switched on
    assert plan["elements"]["4"]["reference"]["ref_id"] == 5  # the inputs stay untouched
    assert catalog["fub_base"].keys() == {"5", "23"}


@pytest.mark.parametrize(
    "settings",
    [None, {}, {"4": {"autohide": "0"}}, {"900": {"autohide": "1"}}],
    ids=["not read", "none", "collapsed", "other plan"],
)
def test_nothing_to_expand_returns_the_inputs(plan: dict[str, Any], settings: dict | None) -> None:
    catalog = {"fub_base": {"5": {"in_hide": [2]}}}
    assert expand_autohide_blocks(plan["elements"], catalog, settings) == (plan["elements"], catalog)


def test_a_block_without_hidden_pins_is_not_repointed(plan: dict[str, Any]) -> None:
    elements, rendered = expand_autohide_blocks(plan["elements"], {"fub_base": {"5": {"in_hide": []}}}, SETTINGS)
    assert elements["4"]["reference"]["ref_id"] == 5
    assert "5:expanded" not in rendered["fub_base"]
