"""Scene block / Quantum module configs: captured in backups, shown in the diff, named by the restore."""

import asyncio
import copy
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioError
from aiocomexio.client import RawConfig
import pytest

from custom_components.comexio import function_plan_backup as backup_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from custom_components.comexio.function_plan_module_configs import (
    SNAPSHOT_MODULE_CONFIGS,
    module_configs_changed,
    module_configs_diff,
    parse_module_config_tables,
    plan_module_configs,
    snapshot_module_configs,
)
from custom_components.comexio.services import backup as backup_service
from tests.common import load_json_fixture

SERVER_ID = "cx1"


def _element(elem_id: int, elem_type: int, ref_id: int) -> dict[str, Any]:
    return {"id": elem_id, "fub_id": 1, "reference": {"type": elem_type, "ref_id": ref_id}}


# 31 scene block, 32/34 Quantum modules 1/2, 33 central Quantum module 1, 35 a plain logic block.
PLAN = {
    "elements": {
        "31": _element(31, 13, 1),
        "32": _element(32, 20, 1),
        "33": _element(33, 21, 1),
        "34": _element(34, 20, 2),
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
# Parsing and filtering
# ---------------------------------------------------------------------------


def test_parse_reads_scenes_by_element_and_quantum_modules_by_module(caplog: pytest.LogCaptureFixture) -> None:
    tables = parse_module_config_tables(load_json_fixture("module_configs_raw.json"))

    assert set(tables) == {"13", "20", "21"}
    assert set(tables["13"]) == {"31", "990"}
    assert tables["13"]["31"]["scene"]["2"]["name"] == "Evening"
    module = tables["20"]["1"]
    assert (module["type_id"], module["default_value"], module["settings"]) == (2, "0;0", {"inverted_output": "true"})
    assert module["rules"]["1"]["ByDay"] == "user_event_1"
    assert "CombinedModuleId" not in module["rules"]["1"]  # the element's ref_id, differs on a rebuilt plan
    # Module 1 exists in both tables: the central one keeps its own rows.
    central = tables["21"]["1"]
    assert (central["type_id"], central["settings"]) == (2, {"central_lock": "true"})
    assert central["rules"]["7"]["Name"] == "Night"
    assert "2 row(s) of $CombinedModuleSettingData" in caplog.text  # unknown module, row without name


def test_a_page_without_the_tables_reads_no_type(caplog: pytest.LogCaptureFixture) -> None:
    assert parse_module_config_tables({"FubModules": {"20": {}}}) == {}
    assert not caplog.text  # no module, nothing lost


def test_a_type_fubmodules_does_not_list_has_no_modules(caplog: pytest.LogCaptureFixture) -> None:
    raw = load_json_fixture("module_configs_raw.json")
    del raw["FubModules"]["21"]
    raw["CombinedModuleCentralSettingData"] = []

    assert parse_module_config_tables(raw)["21"] == {}
    assert "CombinedModuleCentralSettingData" not in caplog.text


@pytest.mark.parametrize("part", ["settings", "rules"])
def test_a_malformed_sub_table_is_not_read_as_empty(part: str, caplog: pytest.LogCaptureFixture) -> None:
    raw = load_json_fixture("module_configs_raw.json")
    raw["CombinedModuleSettingData"][part] = "x"

    assert "20" not in parse_module_config_tables(raw)
    assert "$CombinedModuleSettingData has an unknown format" in caplog.text


def test_a_missing_table_of_listed_modules_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    raw = load_json_fixture("module_configs_raw.json")
    del raw["CombinedModuleCentralSettingData"]

    assert "21" not in parse_module_config_tables(raw)
    assert "$CombinedModuleCentralSettingData is missing" in caplog.text


def test_empty_php_tables_read_as_empty() -> None:
    tables = parse_module_config_tables(
        {
            "FubModules": {"20": [], "21": []},
            "CurrentSceneConfig": [],
            "CombinedModuleSettingData": {"settings": [], "rules": []},
            "CombinedModuleCentralSettingData": [],
        }
    )
    assert tables == {"13": {}, "20": {}, "21": {}}


def test_an_unknown_table_format_is_not_read_as_empty(caplog: pytest.LogCaptureFixture) -> None:
    tables = parse_module_config_tables(
        {"FubModules": {"20": {}}, "CurrentSceneConfig": "x", "CombinedModuleSettingData": ["x"]}
    )
    assert tables == {}
    assert "scene blocks are not backed up" in caplog.text
    assert "Quantum modules are not backed up" in caplog.text


def test_plan_configs_keep_only_the_plans_scene_blocks_and_modules(configs: dict[str, Any]) -> None:
    assert set(configs) == {"31", "32", "33", "34"}
    assert configs["31"]["type"] == "13"
    # 32 and 33 both name module 1: each from its own table.
    assert configs["32"]["config"]["settings"] == {"inverted_output": "true"}
    assert (configs["33"]["type"], configs["33"]["config"]["settings"]) == ("21", {"central_lock": "true"})
    assert configs["31"]["config"]["name"] == "Living room"
    assert configs["34"] == {
        "type": "20",
        "config": {
            "type_id": 1,
            "default_value": "20;0;0",
            "settings": {"local_lock": "false"},
            "rules": {"2": configs["34"]["config"]["rules"]["2"]},
        },
    }


@pytest.mark.parametrize("unreadable", ["13", "20", "21"])
def test_a_plan_using_an_unreadable_type_has_no_configs(tables: dict[str, Any], unreadable: str) -> None:
    del tables[unreadable]
    assert plan_module_configs(tables, PLAN["elements"]) is None


def test_a_plan_without_the_unreadable_type_does_not_depend_on_it(tables: dict[str, Any]) -> None:
    del tables["20"]
    # A plan without Quantum modules does not depend on that table.
    scene_only = {"31": PLAN["elements"]["31"]}
    assert plan_module_configs(tables, scene_only) == {"31": {"type": "13", "config": tables["13"]["31"]}}


def test_a_scene_block_never_saved_has_no_config(tables: dict[str, Any]) -> None:
    assert plan_module_configs(tables, {"40": _element(40, 13, 3)}) == {}


def test_a_quantum_element_without_its_module_is_logged(
    tables: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert plan_module_configs(tables, {"41": _element(41, 20, 8)}) == {}
    assert "Quantum element 41 names module 8" in caplog.text


@pytest.mark.parametrize(
    ("previous", "current", "changed"),
    [
        ({}, None, False),
        ({}, {}, False),
        ({}, {"31": {"type": "13", "config": {}}}, True),
        ({SNAPSHOT_MODULE_CONFIGS: {"31": {"type": "13", "config": {}}}}, {"31": {"type": "13", "config": {}}}, False),
        ({SNAPSHOT_MODULE_CONFIGS: {"31": {"type": "13", "config": {}}}}, {}, True),
    ],
)
def test_module_configs_changed(previous: dict, current: dict | None, changed: bool) -> None:
    assert module_configs_changed(previous, current) is changed


# ---------------------------------------------------------------------------
# Capture in backups
# ---------------------------------------------------------------------------


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


def test_auto_backup_rotates_on_a_config_change_alone(
    stores: dict[str, Any], tables: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}
    changed = copy.deepcopy(tables)
    changed["20"]["1"]["settings"]["inverted_output"] = "false"

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: PLAN}, fub_data, module_configs=tables))]
        rotated.append(len(await manager.async_auto_backup({1: PLAN}, fub_data, module_configs=tables)))
        rotated.append(len(await manager.async_auto_backup({1: PLAN}, fub_data, module_configs=None)))
        rotated.append(len(await manager.async_auto_backup({1: PLAN}, fub_data, module_configs=changed)))
        return rotated

    assert asyncio.run(run()) == [1, 0, 0, 1]
    history = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert history[0][SNAPSHOT_MODULE_CONFIGS]["32"]["config"]["settings"] == {"inverted_output": "false"}
    assert history[1][SNAPSHOT_MODULE_CONFIGS]["32"]["config"]["settings"] == {"inverted_output": "true"}
    assert "changed (module configs)" in caplog.text


def test_change_backup_stores_the_plans_configs(
    stores: dict[str, Any], tables: dict[str, Any], configs: dict[str, Any]
) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    asyncio.run(manager.async_change_backup(1, PLAN, "Lights", "sort", module_configs=tables))

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert stored[SNAPSHOT_MODULE_CONFIGS] == configs


def test_a_backup_without_the_tables_stores_no_configs_key(stores: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    asyncio.run(manager.async_change_backup(1, PLAN, "Lights", "sort"))

    stored = stores[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"]["1"]["Lights"][0]
    assert SNAPSHOT_MODULE_CONFIGS not in stored  # "not captured", not "none"
    assert snapshot_module_configs(stored) is None


@pytest.mark.parametrize(("stored", "count"), [("configs", 4), ("none", 0), ("not captured", None)])
def test_list_entries_count_the_stored_configs(configs: dict[str, Any], stored: str, count: int | None) -> None:
    snapshot = _snapshot({"configs": configs, "none": {}, "not captured": None}[stored])
    entry = backup_module._backup_entry("1", "Lights", 0, snapshot)
    assert entry.get("module_config_count") == count
    assert ("module_config_count" in entry) is (count is not None)


# ---------------------------------------------------------------------------
# API: the cache follows the config fetches
# ---------------------------------------------------------------------------


def test_a_config_fetch_fills_the_cache_and_a_failed_one_drops_it(comexio_api: ComexioAPI) -> None:
    client = MagicMock()
    comexio_api._client = client
    comexio_api._client_credentials = comexio_api._credentials()
    page = RawConfig(load_json_fixture("module_configs_raw.json"), {}, {}, None)
    client.get_raw_config = AsyncMock(return_value=page)

    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.module_configs == parse_module_config_tables(load_json_fixture("module_configs_raw.json"))

    client.get_raw_config = AsyncMock(side_effect=ComexioError("broken"))
    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.module_configs is None  # a backup then stores none rather than an old copy


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _edited(configs: dict[str, Any]) -> dict[str, Any]:
    """configs with a scene value, a rule value and the rule's internal RandomId changed."""
    edited = copy.deepcopy(configs)
    edited["31"]["config"]["scene"]["2"]["data"]["2"]["value"] = 60
    rule = edited["32"]["config"]["rules"]["1"]
    rule["Value"], rule["RandomId"] = "70;0;0", 999
    return edited


def test_diff_lists_changed_fields_and_ignores_internal_ids(configs: dict[str, Any]) -> None:
    assert module_configs_diff(_snapshot(_edited(configs)), _snapshot(configs)) == [
        ("31", "scene 2 value 2 (channel 2)", "40", "60"),
        ("32", "rule 1 Value", "50;0;0", "70;0;0"),
    ]


def test_diff_names_added_and_removed_fields(configs: dict[str, Any]) -> None:
    newer = copy.deepcopy(configs)
    newer["34"]["config"]["settings"]["user_event"] = "true"
    del newer["32"]["config"]["rules"]["1"]

    changes = module_configs_diff(_snapshot(newer), _snapshot(configs))

    assert ("34", "setting user_event", None, "true") in changes
    assert ("32", "rule 1 ByDay", "user_event_1", None) in changes


def test_diff_is_none_when_a_snapshot_lacks_configs(configs: dict[str, Any]) -> None:
    assert module_configs_diff(_snapshot(configs), _snapshot(None)) is None


def _attach(snapshots: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    async def get_snapshot(_kind: str, _fub_id: int, _name: str, slot: int) -> dict[str, Any] | None:
        return snapshots.get(slot)

    coordinator = SimpleNamespace(
        function_plan_label_maps=lambda: ({}, {}, {}),
        function_plan_catalog=SimpleNamespace(async_get_catalog=AsyncMock(return_value={})),
        function_plan_backup=SimpleNamespace(async_get_snapshot=get_snapshot),
    )
    entries = [{"fub_id": 1, "plan_name": "Lights", "slot": slot} for slot in range(len(snapshots) - 1)]
    with patch.object(backup_service, "resolve_element_label", return_value="Scene"):
        asyncio.run(backup_service._attach_backup_diffs(coordinator, entries, "auto"))
    return entries


def test_backup_diffs_list_config_changes_and_mark_a_side_without_configs(configs: dict[str, Any]) -> None:
    entries = _attach({0: _snapshot(_edited(configs)), 1: _snapshot(configs), 2: _snapshot(None)})

    assert entries[0]["diff"]["module_configs"] == [
        "Scene (#31): scene 2 value 2 (channel 2) 40 → 60",
        "Scene (#32): rule 1 Value 50;0;0 → 70;0;0",
    ]
    assert entries[1]["diff"]["module_configs"] == [
        "#nv — no scene/Quantum configs stored in the older backup, not compared"
    ]


def _plain(configs: dict[str, Any] | None) -> dict[str, Any]:
    """A snapshot of a plan with neither scene blocks nor Quantum modules."""
    snap = _snapshot(configs)
    snap["elements"] = {"35": snap["elements"]["35"]}
    return snap


@pytest.mark.parametrize(("newer", "older"), [({}, None), (None, None), (None, {})])
def test_a_plan_without_scene_blocks_or_modules_gets_no_nv_line(newer: dict | None, older: dict | None) -> None:
    entries = _attach({0: _plain(newer), 1: _plain(older)})
    assert entries[0]["diff"]["module_configs"] == []


@pytest.mark.parametrize(
    ("newer", "older", "side"),
    [({}, None, "the older backup"), (None, {}, "this backup"), (None, None, "either backup")],
)
def test_a_plan_with_scene_blocks_names_every_side_without_configs(
    newer: dict | None, older: dict | None, side: str
) -> None:
    # Not captured (stored before they were, or the tables were unreadable) on a plan with such blocks.
    entries = _attach({0: _snapshot(newer), 1: _snapshot(older)})
    assert entries[0]["diff"]["module_configs"] == [f"#nv — no scene/Quantum configs stored in {side}, not compared"]


@pytest.mark.parametrize("plain_side", ["newer", "older"])
def test_one_side_with_scene_blocks_is_enough_for_the_nv_line(plain_side: str) -> None:
    newer, older = (_plain(None), _snapshot(None)) if plain_side == "newer" else (_snapshot(None), _plain(None))
    entries = _attach({0: newer, 1: older})
    assert entries[0]["diff"]["module_configs"] == [
        "#nv — no scene/Quantum configs stored in either backup, not compared"
    ]


def test_identical_configs_have_no_diff_lines(configs: dict[str, Any]) -> None:
    entries = _attach({0: _snapshot(configs), 1: _snapshot(configs)})
    assert entries[0]["diff"]["module_configs"] == []


def test_a_scene_name_with_a_line_break_is_shown_on_one_line(configs: dict[str, Any]) -> None:
    newer = copy.deepcopy(configs)
    newer["31"]["config"]["scene"]["2"]["name"] = "Eve\nning"
    changes = module_configs_diff(_snapshot(newer), _snapshot(configs))
    assert changes == [("31", "scene 2 name", "Evening", "Eve ning")]


def test_a_newer_side_without_configs_is_named(configs: dict[str, Any]) -> None:
    entries = _attach({0: _snapshot(None), 1: _snapshot(configs)})
    assert entries[0]["diff"]["module_configs"] == [
        "#nv — no scene/Quantum configs stored in this backup, not compared"
    ]


# ---------------------------------------------------------------------------
# Restore: not written back yet, but said
# ---------------------------------------------------------------------------


_LINE = "Scene/Quantum configs: ⚠️ not restored (not supported yet) — check "
_RESTORED_IDS = {eid: int(eid) + 100 for eid in PLAN["elements"]}


@pytest.mark.parametrize(
    ("element_map", "expected"),
    [
        (None, "backup element(s) #31, #32, #33, #34 in Comexio"),
        ({"31": 131, "32": 132, "33": 133, "34": 9}, "element(s) #9, #131, #132, #133 in Comexio"),
        (
            {"31": 131, "33": 133},
            "element(s) #131, #133 in Comexio | no restored element for backup element(s) #32, #34",
        ),
        ({}, "no restored element for backup element(s) #31, #32, #33, #34"),
    ],
    ids=["no-map", "all-mapped", "partly-mapped", "none-mapped"],
)
def test_the_restore_names_the_restored_blocks_whose_configs_it_does_not_write(
    configs: dict[str, Any], element_map: dict[str, int] | None, expected: str
) -> None:
    assert backup_service._module_configs_line(_snapshot(configs), element_map) == f"{_LINE}{expected}\n"


_NOT_IN_BACKUP = "Scene/Quantum configs: ⚠️ not in this backup — check scene blocks/Quantum modules in Comexio\n"


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [(_snapshot({}), ""), (_plain(None), ""), (_snapshot(None), _NOT_IN_BACKUP)],
    ids=["no-configs-stored", "plain-plan", "not-captured"],
)
def test_the_restore_line_without_stored_configs(snapshot: dict[str, Any], expected: str) -> None:
    assert backup_service._module_configs_line(snapshot, {}) == expected


def _restore_api() -> SimpleNamespace:
    return SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=(dict(_RESTORED_IDS), 0, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
        function_plan_save_block_settings=AsyncMock(return_value=True),
        block_settings=None,
    )


def _restore_as_new(snapshot: dict[str, Any]) -> str:
    coordinator = SimpleNamespace(
        server_id=SERVER_ID,
        function_plan_backup=SimpleNamespace(async_rekey_fub_id=AsyncMock()),
        async_repoint_function_plan_fub_id=AsyncMock(return_value=[]),
        note_plan_restored_as_new=MagicMock(),
    )
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup_service._restore_plan_as_new(MagicMock(), coordinator, _restore_api(), 1, snapshot, "auto", 0)
        )
    return notify.call_args.args[1]


def _restore_as_copy(snapshot: dict[str, Any]) -> str:
    coordinator = SimpleNamespace(server_id=SERVER_ID)
    with patch.object(backup_service.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup_service._restore_plan_as_copy(
                MagicMock(), coordinator, _restore_api(), 1, snapshot, "auto", 0, "Lights copy", True
            )
        )
    return notify.call_args.args[1]


_VERIFY = {
    "reload_ok": True,
    "hash_match": True,
    "counts_match": True,
    "elem_count": 5,
    "conn_count": 0,
    "fresh_elem": 5,
    "fresh_conn": 0,
}


def _restore_in_place(snapshot: dict[str, Any]) -> str:
    apply = {
        "paper_ok": True,
        "pos_ok": True,
        "run_ok": True,
        "stop_ok": True,
        "properties_changed": False,
        "recovered_comments": 0,
        "comment_warnings": [],
        "block_settings": {"included": False, "written": 0, "failed": [], "unmapped": []},
        "element_map": dict(_RESTORED_IDS),
    }
    return backup_service._restore_build_message(
        "Lights", 1, "auto", 0, snapshot, "OK", apply, _VERIFY, False, True, 1.0
    )


@pytest.mark.parametrize(
    "restore", [_restore_in_place, _restore_as_new, _restore_as_copy], ids=["in-place", "new", "copy"]
)
def test_every_restore_names_the_restored_blocks(configs: dict[str, Any], restore: Any) -> None:
    # The restore hands out new element ids: the line names those, not the backup's.
    assert f"{_LINE}element(s) #131, #132, #133, #134 in Comexio\n" in restore(_snapshot(configs))
    assert _NOT_IN_BACKUP in restore(_snapshot(None))
    assert "Scene/Quantum configs" not in restore(_snapshot({}))
