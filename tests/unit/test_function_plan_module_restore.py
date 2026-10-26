"""Restoring scene block / Quantum module configs (function_plan_module_restore)."""

import asyncio
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioConnectionError, ComexioDataError, ComexioRequestRejectedError, PlacedModuleBlock
import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import ICON_ERROR, ICON_SUCCESS, ICON_WARNING
from custom_components.comexio.function_plan_module_configs import (
    SNAPSHOT_MODULE_CONFIGS,
    parse_module_config_tables,
    plan_module_configs,
    quantum_settings_to_save,
    rules_not_matching,
    scene_config_to_save,
)
from custom_components.comexio.function_plan_module_restore import (
    MODULE_CONFIGS_NOT_IN_BACKUP,
    ModuleConfigRestore,
    check_rules,
    module_configs_line,
    place_module_block,
    restore_in_place,
)
from custom_components.comexio.services import backup as backup_service
from tests.common import load_json_fixture

SERVER_ID = "cx1"


def _element(elem_id: int, elem_type: int, ref_id: int, name: str = "") -> dict[str, Any]:
    return {
        "id": elem_id,
        "fub_id": 1,
        "name": name,
        "position_x": float(elem_id),
        "position_y": 2.0,
        "reference": {"type": elem_type, "ref_id": ref_id},
    }


# 31 scene block, 32/34 Quantum modules 1/2, 33 central Quantum module 1, 35 a plain logic block.
PLAN = {
    "elements": {
        "31": _element(31, 13, 1, "Living room"),
        "32": _element(32, 20, 1, "Shade"),
        "33": _element(33, 21, 1),
        "34": _element(34, 20, 2, "Heating"),
        "35": _element(35, 5, 7),
    },
    "connections": {},
}


@pytest.fixture
def tables() -> dict[str, Any]:
    return parse_module_config_tables(load_json_fixture("module_configs_raw.json"))


@pytest.fixture
def configs(tables: dict[str, Any]) -> dict[str, Any]:
    result = plan_module_configs(tables, PLAN["elements"])
    assert result is not None
    return result


def _snapshot(configs: dict[str, Any] | None) -> dict[str, Any]:
    snap = copy.deepcopy(PLAN) | {"plan_name": "Lights"}
    if configs is not None:
        snap[SNAPSHOT_MODULE_CONFIGS] = copy.deepcopy(configs)
    return snap


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


def test_a_scene_config_is_saved_without_its_row_ids(configs: dict[str, Any]) -> None:
    saved = scene_config_to_save(configs["31"]["config"])

    assert "id" not in saved
    assert saved["common"] == {"1": {"value": "latch_s_r"}, "2": {"value": "_set_analog_"}}
    evening = saved["scene"]["2"]
    assert evening == {
        "name": "Evening",
        "user_save": True,
        "default_scene": True,
        # A value names its channel by position (common_id), which stays.
        "data": {"1": {"value": 1, "common_id": 1}, "2": {"value": 40, "common_id": 2}},
    }
    # The stored config itself is left as it was.
    assert configs["31"]["config"]["scene"]["2"]["id"] == 2


def test_a_scene_config_keeps_list_shaped_tables() -> None:
    config = {"id": 4, "name": "x", "common": [{"value": "latch_s_r", "id": 9}], "scene": [], "category": 1}
    assert scene_config_to_save(config) == {
        "name": "x",
        "common": [{"value": "latch_s_r"}],
        "scene": [],
        "category": 1,
    }


def test_quantum_settings_are_sent_as_strings() -> None:
    config = {"settings": {"modus": "jalousie", "unset": None, "count": 3, "flag": True}}
    assert quantum_settings_to_save(config) == {"modus": "jalousie", "unset": "", "count": "3", "flag": "true"}
    assert quantum_settings_to_save({"settings": []}) == {}


_RULE = {
    "Id": 9,
    "Name": "User event 1",
    "Freq": "EVENT",
    "ByDay": "user_event_1",
    "Value": "50;0;0",
    "Priority": 104,
    "RandomId": 1,
    "CentralGroupId": None,
}


@pytest.mark.parametrize(
    ("live_rule", "settings", "expected"),
    [
        # Row id, RandomId and an unset group stored as "" instead of null are no difference.
        ({"Id": 40, "RandomId": 2, "CentralGroupId": ""}, (), []),
        ({"Value": "70;0;0"}, (), ["user_event_1"]),
        # The name follows the user_event_1 setting the restore writes.
        ({"Name": "Shade up"}, ("user_event_1",), []),
        ({"Name": "Shade up"}, (), ["user_event_1"]),
    ],
    ids=["ids-only", "value", "name-follows-setting", "name"],
)
def test_rules_are_matched_by_their_event(live_rule: dict[str, Any], settings: tuple[str, ...], expected: list) -> None:
    wanted = {"rules": {"9": _RULE}}
    live = {"rules": {"40": _RULE | live_rule}}
    assert rules_not_matching(wanted, live, settings) == expected


def test_a_rule_only_one_side_has_differs() -> None:
    window = _RULE | {"Id": 10, "ByDay": "window"}
    assert rules_not_matching({"rules": {"9": _RULE, "10": window}}, {"rules": {"1": _RULE}}, ()) == ["window"]
    assert rules_not_matching({"rules": []}, {"rules": {"1": _RULE}}, ()) == ["user_event_1"]


@pytest.mark.parametrize(("live_value", "expected"), [("50;0;0", []), ("70;0;0", ["#2"])], ids=["same", "value"])
def test_rules_without_an_event_are_matched_by_their_position(live_value: str, expected: list) -> None:
    # A rebuilt module numbers its rows anew: rows 9/12 of the backup are rows 40/41 now.
    blank = _RULE | {"ByDay": ""}
    wanted = {"rules": {"9": blank, "12": blank | {"ByDay": None, "Value": "50;0;0"}}}
    live = {"rules": {"40": blank | {"Id": 40}, "41": blank | {"Id": 41, "ByDay": None, "Value": live_value}}}
    assert rules_not_matching(wanted, live, ()) == expected


def test_a_rule_without_an_event_is_counted_among_those_only() -> None:
    # Row order differs around a rule with an event: it does not shift the position of the one without.
    blank = _RULE | {"ByDay": ""}
    assert rules_not_matching({"rules": {"5": blank, "9": _RULE}}, {"rules": {"40": _RULE, "41": blank}}, ()) == []


# ---------------------------------------------------------------------------
# Rebuilding a plan: place, then configure
# ---------------------------------------------------------------------------


def _rebuild_api(*, save_ok: bool = True, delete_ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        function_plan_add_scene_block=AsyncMock(return_value=PlacedModuleBlock(131, 7)),
        function_plan_add_quantum_module=AsyncMock(return_value=PlacedModuleBlock(132, 8)),
        function_plan_save_scene=AsyncMock(return_value=save_ok),
        function_plan_save_quantum_settings=AsyncMock(return_value=save_ok),
        function_plan_delete_elements=AsyncMock(return_value=delete_ok),
        get_raw_config=AsyncMock(return_value={}),
        module_configs=None,
    )


def _place(api: SimpleNamespace, restore: ModuleConfigRestore, elem_id: str) -> int | None:
    return asyncio.run(place_module_block(api, 50, elem_id, PLAN["elements"][elem_id], restore))


def test_a_scene_block_is_placed_and_gets_its_config(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)

    assert _place(api, restore, "31") == 131

    api.function_plan_add_scene_block.assert_awaited_once_with(50, x=31.0, y=2.0)
    api.function_plan_save_scene.assert_awaited_once_with(7, scene_config_to_save(configs["31"]["config"]))
    assert (restore.restored, restore.problems) == (1, [])


def test_a_scene_block_without_a_stored_config_keeps_comexios_default(configs: dict[str, Any]) -> None:
    # A scene block never saved in Comexio has no config row.
    api, restore = _rebuild_api(), ModuleConfigRestore({k: v for k, v in configs.items() if k != "31"})

    assert _place(api, restore, "31") == 131
    api.function_plan_save_scene.assert_not_awaited()
    api.function_plan_delete_elements.assert_not_awaited()
    assert (restore.restored, restore.problems) == (0, [])


@pytest.mark.parametrize(
    ("delete_ok", "expected"),
    [
        (True, "backup element #31: its config could not be saved — the placed block #131 was removed again"),
        (False, "removing the placed block #131 failed too — it has Comexio's defaults, delete it in Comexio Studio"),
    ],
    ids=["removed", "removal-failed"],
)
def test_a_block_whose_config_save_fails_is_removed_again(
    configs: dict[str, Any], delete_ok: bool, expected: str
) -> None:
    api, restore = _rebuild_api(save_ok=False, delete_ok=delete_ok), ModuleConfigRestore(configs)

    # Not placed: no connection is drawn to a block with Comexio's defaults.
    assert _place(api, restore, "31") is None
    api.function_plan_delete_elements.assert_awaited_once_with([131])
    assert restore.restored == 0
    assert len(restore.problems) == 1
    assert expected in restore.problems[0]
    assert not restore.ok()


def test_a_quantum_module_is_placed_with_its_type_settings_and_name(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)

    assert _place(api, restore, "32") == 132

    api.function_plan_add_quantum_module.assert_awaited_once_with(50, 2, central=False, x=32.0, y=2.0)
    api.function_plan_save_quantum_settings.assert_awaited_once_with(
        132, 8, {"inverted_output": "true"}, central=False, name="Shade"
    )
    assert (restore.restored, restore.problems, restore.quantum) == (1, [], {"32": (132, "20", 8)})


def test_a_central_quantum_module_is_saved_as_central_without_a_name(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)

    assert _place(api, restore, "33") == 132

    api.function_plan_add_quantum_module.assert_awaited_once_with(50, 2, central=True, x=33.0, y=2.0)
    api.function_plan_save_quantum_settings.assert_awaited_once_with(
        132, 8, {"central_lock": "true"}, central=True, name=None
    )


@pytest.mark.parametrize(("name", "problem"), [("Alarm", True), ("", False)])
def test_a_quantum_module_with_default_settings_is_not_saved(configs: dict[str, Any], name: str, problem: bool) -> None:
    configs["32"]["config"]["settings"] = {}
    elem = PLAN["elements"]["32"] | {"name": name}
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)

    assert asyncio.run(place_module_block(api, 50, "32", elem, restore)) == 132

    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert restore.restored == 1
    assert bool(restore.problems) is problem
    if problem:
        assert "name 'Alarm' not restored" in restore.problems[0]


@pytest.mark.parametrize("configs_stored", [None, {}], ids=["not-captured", "no-entry"])
def test_a_quantum_module_without_a_stored_config_is_not_placed(configs_stored: dict | None) -> None:
    # Its module type is unknown: add_element would read the old module id as a type.
    api, restore = _rebuild_api(), ModuleConfigRestore(configs_stored)

    assert _place(api, restore, "32") is None

    api.function_plan_add_quantum_module.assert_not_awaited()
    assert restore.problems == [
        "backup element #32: Quantum module not recreated — the backup does not name its module type"
    ]


def test_a_block_comexio_does_not_place_is_left_to_the_rebuild_warning(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)
    api.function_plan_add_quantum_module.return_value = None

    assert _place(api, restore, "32") is None
    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert (restore.restored, restore.problems) == (0, [])


def test_the_rebuild_configures_a_block_before_drawing_its_connections(
    comexio_api: ComexioAPI, configs: dict[str, Any]
) -> None:
    order: list[str] = []
    snapshot = _snapshot(configs)
    snapshot["elements"] = {"31": snapshot["elements"]["31"], "35": snapshot["elements"]["35"]}
    snapshot["connections"] = {
        "1": {"type": 0, "input": {"FubElementId": 35, "IOPos": 0}, "output": [{"FubElementId": 31, "IOPos": 1}]}
    }

    def record(step: str, result: Any) -> AsyncMock:
        return AsyncMock(side_effect=lambda *_a, **_k: order.append(step) or result)

    restore = ModuleConfigRestore.for_snapshot(snapshot)
    with (
        patch.object(comexio_api, "function_plan_add_scene_block", record("place scene", PlacedModuleBlock(131, 7))),
        patch.object(comexio_api, "function_plan_save_scene", record("save scene", True)),
        patch.object(comexio_api, "function_plan_add_element", record("place block", 135)),
        patch.object(comexio_api, "function_plan_save_connection", record("connect", 1)),
    ):
        id_map, connections, warnings = asyncio.run(
            comexio_api.function_plan_rebuild_plan_from_snapshot(50, snapshot, restore)
        )

    assert order == ["place scene", "save scene", "place block", "connect"]
    assert (id_map, connections, warnings) == ({"31": 131, "35": 135}, 1, [])
    assert restore.restored == 1


# ---------------------------------------------------------------------------
# Quantum rules after a rebuild
# ---------------------------------------------------------------------------


def _live_tables(tables: dict[str, Any], module_id: int, source: str = "1") -> dict[str, Any]:
    """tables with module source of type 20 also listed as module_id (the module a restore created)."""
    live = copy.deepcopy(tables)
    live["20"][str(module_id)] = copy.deepcopy(live["20"][source])
    return live


def test_check_rules_names_the_rules_that_differ_from_the_backup(
    configs: dict[str, Any], tables: dict[str, Any]
) -> None:
    live = _live_tables(tables, 8)
    live["20"]["8"]["rules"]["1"]["Value"] = "100;0;0"
    api = _rebuild_api()
    api.get_raw_config.side_effect = lambda: setattr(api, "module_configs", live)
    restore = ModuleConfigRestore(configs, quantum={"32": (132, "20", 8)})

    asyncio.run(check_rules(api, restore))

    assert restore.rules == {"132": ["user_event_1"]}
    assert not restore.ok()


def test_check_rules_accepts_matching_rules_and_skips_the_reload_without_modules(
    configs: dict[str, Any], tables: dict[str, Any]
) -> None:
    api = _rebuild_api()
    asyncio.run(check_rules(api, ModuleConfigRestore(configs)))
    api.get_raw_config.assert_not_awaited()

    api.get_raw_config.side_effect = lambda: setattr(api, "module_configs", _live_tables(tables, 8))
    restore = ModuleConfigRestore({"32": configs["32"]}, restored=1, quantum={"32": (132, "20", 8)})
    asyncio.run(check_rules(api, restore))
    assert (restore.rules, restore.rules_unchecked, restore.ok()) == ({}, False, True)


@pytest.mark.parametrize("failure", ["unreadable", "transport"])
def test_rules_that_cannot_be_reloaded_are_named_unchecked(configs: dict[str, Any], failure: str) -> None:
    api = _rebuild_api()
    if failure == "transport":
        api.get_raw_config.side_effect = TimeoutError
    restore = ModuleConfigRestore(configs, quantum={"32": (132, "20", 8)})

    asyncio.run(check_rules(api, restore))

    assert restore.rules_unchecked
    assert not restore.ok()


# ---------------------------------------------------------------------------
# In place
# ---------------------------------------------------------------------------


_LIVE_IDS = {"31": 131, "32": 132, "33": 133, "34": 134, "35": 135}


def _live_plan(**refs: tuple[int, int]) -> dict[str, Any]:
    """The reloaded plan: each backup element under its live id, with its reference (type, ref_id) overridable."""
    elements = {}
    for old_id, live_id in _LIVE_IDS.items():
        ref = PLAN["elements"][old_id]["reference"]
        elem_type, ref_id = refs.get(f"e{old_id}", (ref["type"], ref["ref_id"]))
        elements[str(live_id)] = _element(live_id, elem_type, ref_id)
    return {"elements": elements, "connections": {}}


def _in_place_api(tables: dict[str, Any] | None, *, save_ok: bool = True) -> SimpleNamespace:
    api = _rebuild_api(save_ok=save_ok)
    api.get_raw_config.side_effect = lambda: setattr(api, "module_configs", tables)
    return api


def _in_place(api: SimpleNamespace, configs: dict | None, live_plan: dict | None = None) -> ModuleConfigRestore:
    plan = _live_plan() if live_plan is None else live_plan
    return asyncio.run(restore_in_place(api, _snapshot(configs), dict(_LIVE_IDS), plan))


def test_in_place_writes_every_config_onto_the_matched_live_block(
    configs: dict[str, Any], tables: dict[str, Any]
) -> None:
    api = _in_place_api(tables)

    restore = _in_place(api, configs)

    api.function_plan_save_scene.assert_awaited_once_with(1, scene_config_to_save(configs["31"]["config"]))
    saved = {call.args[0]: call for call in api.function_plan_save_quantum_settings.await_args_list}
    assert saved[132].args == (132, 1, {"inverted_output": "true"})
    assert saved[132].kwargs == {"central": False, "name": None}
    assert saved[133].kwargs == {"central": True, "name": None}
    assert saved[134].args == (134, 2, {"local_lock": "false"})
    assert (restore.restored, restore.problems, restore.rules, restore.ok()) == (4, [], {}, True)
    # Read before writing (module types, rules), and again after it for the cached configs.
    assert api.get_raw_config.await_count == 2


def test_in_place_does_not_change_a_module_of_another_type(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    # Live element 132 now names module 2, which is of type 1 (the backup's module 1 was type 2).
    api = _in_place_api(tables)

    restore = _in_place(api, {"32": configs["32"]}, _live_plan(e32=(20, 2)))

    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert restore.problems == [
        "element #132: not restored — the module is of type 1, the backup's of type 2; "
        "replace the module in Comexio Studio"
    ]
    # Nothing was written, so the page is not read a second time.
    assert api.get_raw_config.await_count == 1


@pytest.mark.parametrize(
    ("live_plan", "expected"),
    [
        (_live_plan(e31=(5, 1)), "element #131: not restored — the block is not the backup's scene block"),
        (_live_plan(e31=(13, 0)), "element #131: not restored — the block is not the backup's scene block"),
    ],
    ids=["other-type", "no-scene-id"],
)
def test_in_place_skips_a_block_that_is_no_longer_the_backups(
    configs: dict[str, Any], tables: dict[str, Any], live_plan: dict[str, Any], expected: str
) -> None:
    api = _in_place_api(tables)
    restore = _in_place(api, {"31": configs["31"]}, live_plan)
    api.function_plan_save_scene.assert_not_awaited()
    assert restore.problems == [expected]


def test_in_place_names_unmapped_blocks_and_failed_saves(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    api = _in_place_api(tables, save_ok=False)

    restore = asyncio.run(
        restore_in_place(api, _snapshot({k: configs[k] for k in ("31", "32")}), {"31": 131}, _live_plan())
    )

    assert restore.problems == [
        "element #131: saving the scene config failed (see the log)",
        "no restored element for backup element #32",
    ]
    assert restore.restored == 0


def test_in_place_without_a_reloaded_plan_restores_nothing(configs: dict[str, Any]) -> None:
    api = _in_place_api(None)
    restore = asyncio.run(restore_in_place(api, _snapshot(configs), None, None))
    assert restore.problems == ["not restored — the plan could not be reloaded to match its blocks"]
    api.get_raw_config.assert_not_awaited()


def test_in_place_without_the_module_table_still_writes_scenes(configs: dict[str, Any]) -> None:
    api = _in_place_api(None)

    restore = _in_place(api, {k: configs[k] for k in ("31", "32")})

    api.function_plan_save_scene.assert_awaited_once()
    assert restore.restored == 1
    assert restore.problems == ["element #132: not restored — Comexio's Quantum module table could not be read"]


def test_in_place_cannot_reset_live_settings_to_the_defaults(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    configs = copy.deepcopy(configs)  # the configs share their rows with the live tables
    configs["32"]["config"]["settings"] = {}
    api = _in_place_api(tables)

    restore = _in_place(api, {"32": configs["32"]})

    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert "element #132: settings not reset to the module type's defaults" in restore.problems[0]


def test_in_place_names_rules_that_differ(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    live = copy.deepcopy(tables)
    live["20"]["2"]["rules"]["2"]["Value"] = "9;0;1"
    api = _in_place_api(live)

    restore = _in_place(api, {"34": configs["34"]})

    assert restore.rules == {"134": ["window"]}


def test_in_place_without_stored_configs_writes_nothing(tables: dict[str, Any]) -> None:
    api = _in_place_api(tables)

    # A backup from before configs were captured: its blocks keep whatever Comexio has.
    missing = _in_place(api, None)
    assert (missing.configs_missing, missing.ok()) == (True, False)
    api.get_raw_config.assert_not_awaited()

    # Captured, but no block was ever saved, nor is the live one (#131 has no scene row): nothing to write.
    assert _in_place(api, {}).ok()
    api.function_plan_save_scene.assert_not_awaited()
    api.function_plan_save_quantum_settings.assert_not_awaited()


def test_in_place_names_a_scene_saved_since_the_backup(tables: dict[str, Any]) -> None:
    # The backup's scene block was never saved (no config row); the live one (#131) now is.
    live = copy.deepcopy(tables)
    live["13"]["131"] = live["13"].pop("31")
    api = _in_place_api(live)

    restore = _in_place(api, {})

    assert restore.problems == [
        "element #131: scene not reset to Comexio's default (the backup's, never saved) — reset it in Comexio Studio"
    ]
    api.function_plan_save_scene.assert_not_awaited()


@pytest.mark.parametrize("readable", [None, {"20": {}}], ids=["page-not-reloaded", "scene-table-unreadable"])
def test_in_place_names_a_never_saved_scene_it_cannot_check(readable: dict[str, Any] | None) -> None:
    api = _in_place_api(readable)

    restore = _in_place(api, {})

    assert restore.problems == [
        "element #131: scene not checked for Comexio's default (the backup's, never saved) — Comexio's scene "
        "table could not be read"
    ]
    assert not restore.ok()


def test_in_place_checks_a_never_saved_scene_against_an_empty_scene_table() -> None:
    # Readable, just no scene saved anywhere: checked, nothing to report.
    restore = _in_place(_in_place_api({"13": {}}), {})

    assert (restore.problems, restore.ok()) == ([], True)


# ---------------------------------------------------------------------------
# The result line and the restore status
# ---------------------------------------------------------------------------


def test_the_line_counts_the_restored_configs(configs: dict[str, Any]) -> None:
    restore = ModuleConfigRestore(configs, restored=4)
    assert module_configs_line(_snapshot(configs), restore) == f"Scene/Quantum configs: {ICON_SUCCESS} 4/4 restored\n"


def test_the_line_names_problems_and_rules(configs: dict[str, Any]) -> None:
    restore = ModuleConfigRestore(
        configs,
        restored=3,
        problems=["element #131: saving the scene config failed (see the log)"],
        rules={"134": ["window"]},
    )
    assert module_configs_line(_snapshot(configs), restore) == (
        f"Scene/Quantum configs: {ICON_ERROR} 3/4 restored | {ICON_WARNING} element #131: saving the scene config "
        f"failed (see the log) | {ICON_WARNING} Quantum rules not restored (Comexio has no request for them), they "
        "differ from the backup: element #134 window — set them in Comexio Studio\n"
    )


def test_the_line_names_unchecked_rules(configs: dict[str, Any]) -> None:
    restore = ModuleConfigRestore(configs, restored=4, rules_unchecked=True)
    assert "Quantum rules not checked — Comexio's module table could not be reloaded" in module_configs_line(
        _snapshot(configs), restore
    )


def _plain() -> dict[str, Any]:
    return {"elements": {"35": PLAN["elements"]["35"]}, "connections": {}}


def test_the_line_without_stored_configs() -> None:
    assert module_configs_line(_snapshot(None), ModuleConfigRestore(None)) == MODULE_CONFIGS_NOT_IN_BACKUP
    assert module_configs_line(_snapshot({}), ModuleConfigRestore({})) == ""
    assert module_configs_line(_plain(), ModuleConfigRestore(None)) == ""
    # A backup without configs still names the Quantum modules it could not recreate.
    restore = ModuleConfigRestore(None, problems=["backup element #32: Quantum module not recreated"])
    assert module_configs_line(_snapshot(None), restore) == (
        f"{MODULE_CONFIGS_NOT_IN_BACKUP.rstrip()} | {ICON_WARNING} backup element #32: Quantum module not recreated\n"
    )


def _restore_api(fill: Any) -> SimpleNamespace:
    """The api of a rebuilding restore; fill(module_restore) plays the rebuild's part."""

    async def rebuild(_fub_id: int, snapshot: dict[str, Any], module_restore: ModuleConfigRestore) -> tuple:
        fill(module_restore)
        return {eid: int(eid) + 100 for eid in snapshot["elements"]}, 0, []

    return SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(side_effect=rebuild),
        function_plan_run_fup=AsyncMock(return_value=True),
        function_plan_save_block_settings=AsyncMock(return_value=True),
        get_raw_config=AsyncMock(return_value={}),
        module_configs=None,
        block_settings=None,
    )


def _restore_as_new(snapshot: dict[str, Any], api: SimpleNamespace) -> tuple[str, str]:
    coordinator = SimpleNamespace(
        server_id=SERVER_ID,
        function_plan_backup=SimpleNamespace(async_rekey_fub_id=AsyncMock()),
        async_repoint_function_plan_fub_id=AsyncMock(return_value=[]),
        note_plan_restored_as_new=MagicMock(),
    )
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(backup_service._restore_plan_as_new(MagicMock(), coordinator, api, 1, snapshot, "auto", 0))
    return notify.call_args.kwargs["title"], notify.call_args.args[1]


def _restore_as_copy(snapshot: dict[str, Any], api: SimpleNamespace) -> tuple[str, str]:
    coordinator = SimpleNamespace(server_id=SERVER_ID)
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup_service._restore_plan_as_copy(
                MagicMock(), coordinator, api, 1, snapshot, "auto", 0, "Lights copy", True
            )
        )
    return notify.call_args.kwargs["title"], notify.call_args.args[1]


@pytest.mark.parametrize("restore", [_restore_as_new, _restore_as_copy], ids=["new", "copy"])
def test_a_rebuilding_restore_reports_its_configs(configs: dict[str, Any], restore: Any) -> None:
    def all_restored(module_restore: ModuleConfigRestore) -> None:
        module_restore.restored = 4

    title, message = restore(_snapshot(configs), _restore_api(all_restored))
    assert title.endswith("OK")
    assert f"Scene/Quantum configs: {ICON_SUCCESS} 4/4 restored\n" in message

    def one_failed(module_restore: ModuleConfigRestore) -> None:
        module_restore.restored = 3
        module_restore.problems.append("#131: saving the scene config failed (see the log)")

    title, message = restore(_snapshot(configs), _restore_api(one_failed))
    assert title.endswith("PARTIAL")
    assert f"Scene/Quantum configs: {ICON_ERROR} 3/4 restored" in message


_VERIFY = {
    "reload_ok": True,
    "hash_match": True,
    "counts_match": True,
    "elem_count": 5,
    "conn_count": 0,
    "fresh_elem": 5,
    "fresh_conn": 0,
}


def _in_place_apply(module_restore: ModuleConfigRestore) -> dict[str, Any]:
    return {
        "paper_ok": True,
        "pos_ok": True,
        "run_ok": True,
        "stop_ok": True,
        "properties_changed": False,
        "recovered_comments": 0,
        "comment_warnings": [],
        "block_settings": {"included": False, "written": 0, "failed": [], "unmapped": []},
        "module_configs": module_restore,
    }


def test_an_in_place_restore_is_partial_when_a_config_is_not_restored(configs: dict[str, Any]) -> None:
    done = _in_place_apply(ModuleConfigRestore(configs, restored=4))
    assert backup_service._restore_status(True, done, _VERIFY) == "OK"
    assert f"Scene/Quantum configs: {ICON_SUCCESS} 4/4 restored\n" in backup_service._restore_build_message(
        "Lights", 1, "auto", 0, _snapshot(configs), "OK", done, _VERIFY, False, True, 1.0
    )

    rules = _in_place_apply(ModuleConfigRestore(configs, restored=4, rules={"134": ["window"]}))
    assert backup_service._restore_status(True, rules, _VERIFY) == "PARTIAL"


# ---------------------------------------------------------------------------
# The ComexioAPI adapters
# ---------------------------------------------------------------------------


def test_the_placing_adapters_return_the_placed_block(comexio_api: ComexioAPI) -> None:
    scene = AsyncMock(return_value=PlacedModuleBlock(131, 7))
    quantum = AsyncMock(return_value=PlacedModuleBlock(132, 8))
    with (
        patch.object(comexio_api.client, "add_function_plan_scene", scene),
        patch.object(comexio_api.client, "add_function_plan_quantum_module", quantum),
    ):
        assert asyncio.run(comexio_api.function_plan_add_scene_block("50", x=1, y=2)) == PlacedModuleBlock(131, 7)
        assert asyncio.run(
            comexio_api.function_plan_add_quantum_module(50, "2", central=True, x=3, y=4)
        ) == PlacedModuleBlock(132, 8)
    scene.assert_awaited_once_with(50, x=1.0, y=2.0)
    quantum.assert_awaited_once_with(50, 2, central=True, x=3.0, y=4.0)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ComexioRequestRejectedError("refused"), None),
        # Placed, but Comexio's answer names no scene: ref_id 0 tells the caller to remove it.
        (
            ComexioDataError("Placing a scene: element 77 was placed, but the answer carries no ref_id: {}"),
            PlacedModuleBlock(77, 0),
        ),
        (ComexioDataError("Placing a scene: no element id in the answer"), None),
    ],
    ids=["refused", "no-ref-id", "no-element-id"],
)
def test_a_placing_adapter_reports_a_failed_placement(
    comexio_api: ComexioAPI, error: Exception, expected: PlacedModuleBlock | None
) -> None:
    with patch.object(comexio_api.client, "add_function_plan_scene", AsyncMock(side_effect=error)):
        assert asyncio.run(comexio_api.function_plan_add_scene_block(50, x=1, y=2)) == expected


def test_the_save_adapters_confirm_or_refuse(comexio_api: ComexioAPI) -> None:
    scene = AsyncMock(return_value=None)
    quantum = AsyncMock(side_effect=ComexioRequestRejectedError("refused"))
    with (
        patch.object(comexio_api.client, "save_function_plan_scene", scene),
        patch.object(comexio_api.client, "save_function_plan_quantum_settings", quantum),
    ):
        assert asyncio.run(comexio_api.function_plan_save_scene("7", {"name": "x"})) is True
        saved = asyncio.run(
            comexio_api.function_plan_save_quantum_settings(132, "8", {"a": "1"}, central=False, name="Shade")
        )
        assert saved is False
    scene.assert_awaited_once_with(7, {"name": "x"})
    quantum.assert_awaited_once_with(132, 8, {"a": "1"}, central=False, name="Shade")


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("add_function_plan_scene", lambda api: api.function_plan_add_scene_block(50, x=1, y=2)),
        (
            "add_function_plan_quantum_module",
            lambda api: api.function_plan_add_quantum_module(50, 2, central=False, x=1, y=2),
        ),
        ("save_function_plan_scene", lambda api: api.function_plan_save_scene(7, {"name": "x"})),
        (
            "save_function_plan_quantum_settings",
            lambda api: api.function_plan_save_quantum_settings(1, 8, {"a": "1"}, central=False, name=None),
        ),
    ],
    ids=["add-scene", "add-quantum", "save-scene", "save-quantum"],
)
def test_the_adapters_raise_a_transport_failure(comexio_api: ComexioAPI, method: str, call: Any) -> None:
    error = ComexioConnectionError("timed out")
    error.__cause__ = TimeoutError()
    with patch.object(comexio_api.client, method, AsyncMock(side_effect=error)):
        coro = call(comexio_api)
        with pytest.raises(TimeoutError):
            asyncio.run(coro)


# ---------------------------------------------------------------------------
# Rebuild: failures after placing
# ---------------------------------------------------------------------------


def test_a_block_comexio_names_no_object_for_is_removed_again(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)
    api.function_plan_add_scene_block.return_value = PlacedModuleBlock(131, 0)

    assert _place(api, restore, "31") is None

    api.function_plan_save_scene.assert_not_awaited()
    api.function_plan_delete_elements.assert_awaited_once_with([131])
    assert restore.problems == [
        "backup element #31: Comexio named no scene/module for it — the placed block #131 was removed again "
        "(see the log)"
    ]


def test_a_connection_drop_while_saving_removes_the_block_and_aborts(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)
    api.function_plan_save_quantum_settings.side_effect = TimeoutError
    coro = place_module_block(api, 50, "32", PLAN["elements"]["32"], restore)

    with pytest.raises(TimeoutError):
        asyncio.run(coro)

    api.function_plan_delete_elements.assert_awaited_once_with([132])
    assert restore.quantum == {}
    assert "saving its config failed (connection) — the placed block #132 was removed again" in restore.problems[0]


def test_a_scene_block_comexio_does_not_place_leaves_the_configs_short(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore({"31": configs["31"]})
    api.function_plan_add_scene_block.return_value = None

    assert _place(api, restore, "31") is None

    assert (restore.restored, restore.problems, restore.ok()) == (0, [], False)
    assert module_configs_line(_snapshot(configs), restore) == f"Scene/Quantum configs: {ICON_ERROR} 0/1 restored\n"


@pytest.mark.parametrize("type_id", [0, None, "x"])
def test_a_quantum_module_without_a_usable_type_is_not_placed(configs: dict[str, Any], type_id: Any) -> None:
    configs = copy.deepcopy(configs)
    configs["32"]["config"]["type_id"] = type_id
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)

    assert _place(api, restore, "32") is None

    api.function_plan_add_quantum_module.assert_not_awaited()
    assert "Quantum module not recreated — the backup does not name its module type" in restore.problems[0]


# ---------------------------------------------------------------------------
# In place: failures and defaults
# ---------------------------------------------------------------------------


def test_in_place_names_a_failed_quantum_save(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    api = _in_place_api(tables, save_ok=False)

    restore = _in_place(api, {"32": configs["32"]})

    assert restore.problems == ["element #132: saving the Quantum module settings failed (see the log)"]
    assert (restore.restored, restore.quantum) == (0, {})


def test_in_place_counts_a_module_on_its_defaults_on_both_sides(
    configs: dict[str, Any], tables: dict[str, Any]
) -> None:
    configs, live = copy.deepcopy(configs), copy.deepcopy(tables)
    configs["32"]["config"]["settings"] = {}
    live["20"]["1"]["settings"] = {}
    api = _in_place_api(live)

    restore = _in_place(api, {"32": configs["32"]})

    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert (restore.restored, restore.problems, restore.quantum) == (1, [], {"32": (132, "20", 1)})


def test_in_place_stops_writing_on_a_connection_drop(configs: dict[str, Any], tables: dict[str, Any]) -> None:
    api = _in_place_api(tables)
    api.function_plan_save_scene.side_effect = TimeoutError

    restore = _in_place(api, {k: configs[k] for k in ("31", "32")})

    api.function_plan_save_quantum_settings.assert_not_awaited()
    assert restore.problems == [
        "element #131: writing its config failed (connection) — the remaining configs were not written"
    ]
    assert not restore.ok()


def test_rules_of_a_module_missing_from_the_reloaded_table_are_unchecked(configs: dict[str, Any]) -> None:
    api = _rebuild_api()
    api.get_raw_config.side_effect = lambda: setattr(api, "module_configs", {"20": {}})
    restore = ModuleConfigRestore(configs, quantum={"32": (132, "20", 8)})

    asyncio.run(check_rules(api, restore))

    assert (restore.rules, restore.rules_unchecked) == ({}, True)


# ---------------------------------------------------------------------------
# Status and message wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"restored": 3, "problems": ["element #131: saving the scene config failed (see the log)"]},
        {"restored": 4, "rules_unchecked": True},
        {"restored": 4, "rules": {"134": ["window"]}},
    ],
    ids=["problem", "rules-unchecked", "rules"],
)
def test_an_in_place_restore_is_partial_for_any_config_not_restored(
    configs: dict[str, Any], result: dict[str, Any]
) -> None:
    done = _in_place_apply(ModuleConfigRestore(configs, **result))
    assert backup_service._restore_status(True, done, _VERIFY) == "PARTIAL"


def test_an_in_place_restore_of_a_backup_without_configs_is_partial() -> None:
    done = _in_place_apply(ModuleConfigRestore.for_snapshot(_snapshot(None)))
    assert backup_service._restore_status(True, done, _VERIFY) == "PARTIAL"
    assert ModuleConfigRestore.for_snapshot(_plain()).ok()


@pytest.mark.parametrize("restore", [_restore_as_new, _restore_as_copy], ids=["new", "copy"])
def test_a_rebuilding_restore_checks_the_rules_of_its_modules(configs: dict[str, Any], restore: Any) -> None:
    def module_placed(module_restore: ModuleConfigRestore) -> None:
        module_restore.restored = 4
        module_restore.quantum["32"] = (132, "20", 8)

    # The rebuild's api cannot reload the module table: the rules are named unchecked.
    title, message = restore(_snapshot(configs), _restore_api(module_placed))

    assert title.endswith("PARTIAL")
    assert "Quantum rules not checked — Comexio's module table could not be reloaded" in message


def test_a_connection_error_without_an_aiohttp_cause_ends_a_copy_restore(configs: dict[str, Any]) -> None:
    api = _restore_api(lambda _restore: None)
    api.function_plan_rebuild_plan_from_snapshot.side_effect = ComexioConnectionError("session lost")

    title, _message = _restore_as_copy(_snapshot(configs), api)

    assert title == backup_service._TITLE_RESTORE_ERR


# ---------------------------------------------------------------------------
# Review round 2: removal, lib contract, wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.function_plan_add_scene_block(50, x=1, y=2),
        lambda api: api.function_plan_add_quantum_module(50, 2, central=False, x=1, y=2),
    ],
    ids=["scene", "quantum"],
)
def test_aiocomexios_answer_without_a_ref_id_names_the_placed_element(comexio_api: ComexioAPI, call: Any) -> None:
    # The real client builds the error: a reworded aiocomexio message fails here, not in the field.
    answer = AsyncMock(return_value={"id": 77})
    with patch.object(comexio_api.client, "_add_plan_element_answer", answer):
        assert asyncio.run(call(comexio_api)) == PlacedModuleBlock(77, 0)
    answer.assert_awaited_once()


@pytest.mark.parametrize(
    ("placed", "stored", "reason"),
    [
        (PlacedModuleBlock(131, 0), None, "Comexio named no scene/module for it"),
        (PlacedModuleBlock(131, 7), {"config": "garbled"}, "its stored config is unreadable"),
    ],
    ids=["no-scene", "unreadable-config"],
)
def test_a_scene_block_that_would_act_differently_is_removed_again(
    configs: dict[str, Any], placed: PlacedModuleBlock, stored: dict | None, reason: str
) -> None:
    others = {k: v for k, v in configs.items() if k != "31"}
    api, restore = _rebuild_api(), ModuleConfigRestore(others if stored is None else others | {"31": stored})
    api.function_plan_add_scene_block.return_value = placed

    assert _place(api, restore, "31") is None

    api.function_plan_save_scene.assert_not_awaited()
    api.function_plan_delete_elements.assert_awaited_once_with([131])
    assert restore.problems == [f"backup element #31: {reason} — the placed block #131 was removed again (see the log)"]


def test_a_failing_config_save_removes_the_block_before_the_error_propagates(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(), ModuleConfigRestore(configs)
    api.function_plan_save_scene.side_effect = KeyError("pins")
    coro = place_module_block(api, 50, "31", PLAN["elements"]["31"], restore)

    with pytest.raises(KeyError):
        asyncio.run(coro)

    api.function_plan_delete_elements.assert_awaited_once_with([131])
    assert restore.problems == [
        "backup element #31: saving its config failed — the placed block #131 was removed again (see the log)"
    ]


def test_a_quantum_module_whose_save_is_refused_is_removed_and_its_rules_skipped(configs: dict[str, Any]) -> None:
    api, restore = _rebuild_api(save_ok=False), ModuleConfigRestore(configs)

    assert _place(api, restore, "32") is None

    api.function_plan_delete_elements.assert_awaited_once_with([132])
    assert (restore.restored, restore.quantum) == (0, {})
    assert "#32: its config could not be saved — the placed block #132 was removed again" in restore.problems[0]


def test_in_place_reports_the_configs_written_before_a_connection_drop(
    configs: dict[str, Any], tables: dict[str, Any]
) -> None:
    api = _in_place_api(tables)
    api.function_plan_save_quantum_settings.side_effect = TimeoutError

    restore = _in_place(api, {k: configs[k] for k in ("31", "32")})

    assert restore.restored == 1
    assert restore.problems == [
        "element #132: writing its config failed (connection) — the remaining configs were not written"
    ]
    assert not restore.ok()
    # The scene written before the drop: the cached configs are reloaded after the writes too.
    assert api.get_raw_config.await_count == 2


@pytest.mark.parametrize("restore", [_restore_as_new, _restore_as_copy], ids=["new", "copy"])
def test_a_rebuild_from_a_backup_without_configs_is_partial(restore: Any) -> None:
    title, message = restore(_snapshot(None), _restore_api(lambda _restore: None))

    assert title.endswith("PARTIAL")
    assert MODULE_CONFIGS_NOT_IN_BACKUP.rstrip("\n") in message


def test_a_connection_error_without_an_aiohttp_cause_ends_a_restore_as_new(configs: dict[str, Any]) -> None:
    api = _restore_api(lambda _restore: None)
    api.function_plan_rebuild_plan_from_snapshot.side_effect = ComexioConnectionError("session lost")

    title, _message = _restore_as_new(_snapshot(configs), api)

    assert title == backup_service._TITLE_RESTORE_ERR


def _run_in_place(
    snapshot: dict[str, Any], api: SimpleNamespace, verify_error: Exception | None = None, **verify: Any
) -> tuple[str, str, AsyncMock]:
    """_restore_plan_in_place with run_fup's part stubbed: (title, message, the restore_in_place spy)."""
    api.fub_data = {"1": {"Name": "Lights", "Active": True}}
    coordinator = SimpleNamespace(server_id=SERVER_ID, async_function_plan_change_backup=AsyncMock())
    apply = _in_place_apply(ModuleConfigRestore(None))
    del apply["module_configs"]
    spy = AsyncMock(wraps=restore_in_place)
    with (
        patch.object(backup_service, "_restore_apply_snapshot", AsyncMock(return_value=apply)),
        patch.object(
            backup_service, "_restore_verify", AsyncMock(return_value=_VERIFY | verify, side_effect=verify_error)
        ),
        patch.object(backup_service, "_restore_block_settings", AsyncMock(return_value=apply["block_settings"])),
        patch.object(backup_service, "map_restored_elements", MagicMock(return_value=dict(_LIVE_IDS))),
        patch.object(backup_service, "_async_promote_restored", AsyncMock(return_value=True)),
        patch.object(backup_service, "restore_in_place", spy),
        patch.object(backup_service.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(backup_service._restore_plan_in_place(MagicMock(), coordinator, api, 1, snapshot, "auto", 0, None))
    return notify.call_args.kwargs["title"], notify.call_args.args[1], spy


@pytest.mark.parametrize(
    ("save_ok", "status", "line"),
    [
        (True, "OK", f"Scene/Quantum configs: {ICON_SUCCESS} 4/4 restored"),
        (False, "PARTIAL", "element #131: saving the scene config failed (see the log)"),
    ],
    ids=["saved", "refused"],
)
def test_an_in_place_restore_writes_the_configs_onto_the_reloaded_plan(
    configs: dict[str, Any], tables: dict[str, Any], save_ok: bool, status: str, line: str
) -> None:
    snapshot, plan = _snapshot(configs), _live_plan()

    title, message, spy = _run_in_place(snapshot, _in_place_api(tables, save_ok=save_ok), plan=plan)

    spy.assert_awaited_once()
    assert spy.await_args.args[1:] == (snapshot, _LIVE_IDS, plan)
    assert title == f"Function Plan Restore — {status}"
    assert line in message


def test_a_connection_error_without_an_aiohttp_cause_ends_an_in_place_restore(configs: dict[str, Any]) -> None:
    title, _message, spy = _run_in_place(_snapshot(configs), _in_place_api(None), ComexioConnectionError("lost"))

    assert title == backup_service._TITLE_RESTORE_ERR
    spy.assert_not_awaited()
