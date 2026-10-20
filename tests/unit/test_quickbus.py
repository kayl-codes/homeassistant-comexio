"""Quickbus IOs (#86): own data key, own Web-IO class [QB], read-only entities, webhook by IO Id."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio import _quickbus_entity_uids
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.cleanup_scope import CLEANUP_SCOPE_QUICKBUS, CLEANUP_SCOPES, webio_classes_in_scope
from custom_components.comexio.const import (
    QUICKBUS_IO_DATA_KEY,
    SOURCE_CATEGORIES,
    WebioClass,
    quickbus_io_platform,
    quickbus_io_unique_id,
    required_webio_classes,
    source_audit_key,
    stable_object_id,
)
from custom_components.comexio.coordinator import (
    ComexioCoordinator,
    _build_com_audit_map,
    _imported_data,
    _split_disabled_commands,
)
from custom_components.comexio.repairs import _CLEANUP_SCOPE_LABELS
from tests.common import load_json_fixture

# IO type tables for config_quickbus.json, as Comexio sends them: 134 = Multitouch gesture, 126 = motion (PIR),
# 12 = temperature, 111 = LED colour channel.
_QUICKBUS_IO_TYPES = {
    "1": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "12": {"binary": False, "min": 0, "max": 100, "unit": "°C"},
    "111": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "126": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "134": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
_QUICKBUS_IO_INPUT_TYPES = {"1": {"input": True}, "12": {"input": True}, "111": {"input": True}}
_QUICKBUS_CATEGORY = SOURCE_CATEGORIES[WebioClass.QUICKBUS]


@pytest.fixture
def quickbus_result(parsing_api: ComexioAPI) -> dict[str, Any]:
    parsing_api.io_types = _QUICKBUS_IO_TYPES
    parsing_api.io_input_types = _QUICKBUS_IO_INPUT_TYPES
    return parsing_api.parse_config(load_json_fixture("config_quickbus.json"))


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in items}


def test_quickbus_ios_get_their_own_data_key(quickbus_result: dict[str, Any]) -> None:
    """The ext+identifier IO paths never see a Quickbus IO: its identifier repeats per device."""
    assert [(io["ext_name"], io["identifier"]) for io in quickbus_result["io"]] == [("BASE", "I1")]
    assert [io["id"] for io in quickbus_result[QUICKBUS_IO_DATA_KEY]] == [
        "201",
        "202",
        "203",
        "204",
        "211",
        "213",
        "301",
        "302",
    ]
    # io_all keeps them, so a plan wired to a Quickbus IO still shows its name.
    assert {io["identifier"] for io in quickbus_result["io_all"] if io["quickbus"]} >= {"G1", "G18", "PIR1"}


def test_quickbus_ios_get_web_io_commands_of_their_own_class(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    assert WebioClass.QUICKBUS in quickbus_result["webio_devices"]
    names = [cmd["Name"] for cmd in parsing_api.build_webio_commands("test", quickbus_result)]
    assert names == [
        "HA IO BASE I1",
        "HA QB1 MT Flur G1",
        "HA QB1 MT Flur G18",
        "HA QB1 MT Flur TL1",
        "HA QB1 MT Flur R",
        "HA QB2 MT Bad G1",
        "HA QB2 MT Bad TL1",
        "HA QB3 BM Diele PIR1",
        "HA QB3 BM Diele TL1",
    ]
    quickbus_json = parsing_api.generate_webio_json("test", "HomeAssistant [QB]", quickbus_result, WebioClass.QUICKBUS)
    assert "HA QB1 MT Flur G1" in quickbus_json
    assert "HA IO BASE I1" not in quickbus_json
    # The payload addresses the IO by its unique Id, not by ext + identifier.
    assert 'id=\\"201\\"' in quickbus_json
    assert 'type=\\"quickbus\\"' in quickbus_json


def test_quickbus_audit_map_names_match_the_web_io_commands(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    """The audit expects exactly the commands the sync creates — else every poll reports them missing."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()

    ha_map, io_meta_by_key = coordinator._build_ha_audit_map(quickbus_result)

    quickbus_keys = {key for key in ha_map if key.startswith(_QUICKBUS_CATEGORY.audit_key_prefix)}
    assert quickbus_keys == {source_audit_key(_QUICKBUS_CATEGORY, io["id"]) for io in quickbus_result["quickbus_io"]}
    commands = {cmd["Name"]: cmd for cmd in parsing_api.build_webio_commands("test", quickbus_result)}
    for key in quickbus_keys:
        expected = ha_map[key]
        assert expected["name"] in commands
        assert expected["type"] == ("digital" if commands[expected["name"]]["TypeId"] == 1 else "analog")
    assert ha_map["QB204"]["type"] == "analog"
    assert ha_map["QB201"]["type"] == "digital"
    # Not wired into managed function plans (this PR): no IO meta, so no wiring gap is reported.
    assert not set(io_meta_by_key) & quickbus_keys


@pytest.mark.parametrize(("import_quickbus", "expected"), [(True, 8), (False, 0)])
def test_import_quickbus_option_gates_the_quickbus_ios(
    quickbus_result: dict[str, Any], import_quickbus: bool, expected: int
) -> None:
    imported = _imported_data(quickbus_result, {"import_quickbus": import_quickbus})
    assert len(imported[QUICKBUS_IO_DATA_KEY]) == expected
    assert len(imported["io"]) == 1


def test_import_quickbus_defaults_to_on(quickbus_result: dict[str, Any]) -> None:
    assert len(_imported_data(quickbus_result, {})[QUICKBUS_IO_DATA_KEY]) == 8


def _webhook_coordinator(quickbus_ios: list[dict[str, Any]]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator._quickbus_index = {str(io["id"]): io for io in quickbus_ios}
    coordinator.io_states = {}
    coordinator._webhook_updated_io_ids = set()
    coordinator._async_publish_pushed_value = MagicMock()
    coordinator._fire_plan_event = MagicMock()
    coordinator.schedule_plan_preview_refresh = MagicMock()
    return coordinator


def test_quickbus_webhook_updates_the_io_by_its_id(quickbus_result: dict[str, Any]) -> None:
    """Two devices share G1 — the push for MT Bad G1 (211) must not touch MT Flur G1 (201)."""
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    coordinator.update_quickbus_io(211, 1)

    assert coordinator.io_states == {"211": 1}
    assert _by_id(quickbus_result[QUICKBUS_IO_DATA_KEY])["211"]["value"] == 1
    assert _by_id(quickbus_result[QUICKBUS_IO_DATA_KEY])["201"]["value"] == 0.0
    assert coordinator._webhook_updated_io_ids == {"211"}
    coordinator._fire_plan_event.assert_called_once_with("io", "211", "MT Bad G1", 1)
    coordinator._async_publish_pushed_value.assert_called_once()


def test_quickbus_webhook_for_an_unknown_id_is_dropped_with_a_warning(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    coordinator.update_quickbus_io("999", 1)

    assert coordinator.io_states == {}
    assert coordinator._webhook_updated_io_ids == set()
    coordinator._fire_plan_event.assert_not_called()
    assert "unknown Quickbus IO 999" in caplog.text


def test_poll_keeps_a_quickbus_webhook_value_that_arrived_during_the_fetch(quickbus_result: dict[str, Any]) -> None:
    """R1 for Quickbus IOs: the webhook value wins over the older poll snapshot."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.marker_states = {}
    coordinator.io_states = {"201": 1}
    coordinator._webhook_updated_markers = set()
    coordinator._webhook_updated_io_ids = {"201"}

    coordinator._merge_polled_states(quickbus_result)

    ios = _by_id(quickbus_result[QUICKBUS_IO_DATA_KEY])
    assert ios["201"]["value"] == 1
    assert coordinator.io_states["202"] == 0.0


def test_quickbus_entities_are_read_only_and_keyed_by_io_id(quickbus_result: dict[str, Any]) -> None:
    ios = _by_id(quickbus_result[QUICKBUS_IO_DATA_KEY])
    assert quickbus_io_platform(ios["201"]) == "binary_sensor"
    assert quickbus_io_platform(ios["203"]) == "sensor"
    # An output (R, IntIsOutput=1) stays read-only too.
    assert not ios["204"]["is_input"]
    assert quickbus_io_platform(ios["204"]) == "sensor"
    assert quickbus_io_unique_id("IOSrv1", "211") == "comexio_iosrv1_qb211"
    assert stable_object_id(quickbus_io_unique_id("IOSrv1", "211")) == "iosrv1_qb211"


def test_entity_cleanup_keeps_quickbus_entities_on_their_platform(quickbus_result: dict[str, Any]) -> None:
    active, offline = _quickbus_entity_uids("iosrv1", quickbus_result, include_offline=False)
    assert active["comexio_iosrv1_qb201"] == "binary_sensor"
    assert active["comexio_iosrv1_qb204"] == "sensor"
    assert len(active) == 8
    assert offline == set()


@pytest.mark.parametrize(("include_offline", "active_count"), [(False, 0), (True, 1)])
def test_entity_cleanup_treats_ios_of_an_offline_host_as_offline(include_offline: bool, active_count: int) -> None:
    data = {QUICKBUS_IO_DATA_KEY: [{"id": "201", "is_binary": True, "offline": True}]}
    active, offline = _quickbus_entity_uids("iosrv1", data, include_offline=include_offline)
    assert len(active) == active_count
    assert offline == (set() if include_offline else {"comexio_iosrv1_qb201"})


@pytest.mark.parametrize(
    ("quickbus_ios", "expected"),
    [
        ([], (WebioClass.MARKER, WebioClass.IO)),
        ([{"id": "201"}], (WebioClass.MARKER, WebioClass.IO, WebioClass.QUICKBUS)),
    ],
)
def test_quickbus_class_is_only_required_with_a_quickbus_io(
    quickbus_ios: list[dict[str, Any]], expected: tuple[WebioClass, ...]
) -> None:
    """import_quickbus is on by default: a server without Quickbus devices must not get an empty
    [QB] class created, nor a "missing Web-IO class" repair until then."""
    assert required_webio_classes({}, {QUICKBUS_IO_DATA_KEY: quickbus_ios}) == expected


def test_quickbus_class_is_not_required_while_its_import_is_off() -> None:
    conf = {"import_quickbus": False}
    assert WebioClass.QUICKBUS not in required_webio_classes(conf, {QUICKBUS_IO_DATA_KEY: [{"id": "201"}]})


def _missing_class_coordinator(conf: dict[str, Any]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.hass = MagicMock()
    coordinator.server_id = "iosrv1"
    coordinator.config_entry = MagicMock(entry_id="e1", data={}, options=conf)
    coordinator.in_sync = False
    coordinator.last_audit_results = {"kept": True}
    return coordinator


@pytest.mark.parametrize(("quickbus_ios", "missing"), [([], False), ([{"id": "201"}], True)])
def test_missing_quickbus_class_is_reported_only_with_a_quickbus_io(
    monkeypatch: pytest.MonkeyPatch, quickbus_ios: list[dict[str, Any]], missing: bool
) -> None:
    """Without a Quickbus IO a missing [QB] class must neither raise the repair nor blank the audit."""
    create_issue = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.ir.async_create_issue", create_issue)
    coordinator = _missing_class_coordinator({})
    devices = {WebioClass.MARKER: {"device_id": "1"}, WebioClass.IO: {"device_id": "2"}}

    result = coordinator._report_missing_webio_classes({}, devices, {QUICKBUS_IO_DATA_KEY: quickbus_ios})

    assert result is missing
    assert (coordinator.last_audit_results == {}) is missing
    assert create_issue.called is missing
    if missing:
        assert create_issue.call_args.kwargs["data"]["missing_classes"] == [WebioClass.QUICKBUS]


@pytest.mark.parametrize(("quickbus_ios", "required"), [([], False), ([{"id": "201"}], True)])
def test_coordinator_requires_the_quickbus_class_from_its_current_data(
    quickbus_ios: list[dict[str, Any]], required: bool
) -> None:
    """The property the sync button and generate_web_io read."""
    coordinator = _missing_class_coordinator({})
    coordinator.data = {QUICKBUS_IO_DATA_KEY: quickbus_ios}
    assert (WebioClass.QUICKBUS in coordinator.required_webio_classes) is required


def _comexio_commands(parsing_api: ComexioAPI, quickbus_result: dict[str, Any]) -> dict[str, Any]:
    """The Web-IO commands as Comexio lists them after a sync."""
    return {
        cmd["Name"]: {
            "typeId": cmd["TypeId"],
            "cmdId": str(idx),
            "webioClass": WebioClass.QUICKBUS if cmd["Name"].startswith("HA QB") else WebioClass.IO,
        }
        for idx, cmd in enumerate(parsing_api.build_webio_commands("test", quickbus_result))
    }


def _audit(ha_map: dict[str, Any], com_commands: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    com_map = _build_com_audit_map(com_commands, ha_map)
    return coordinator._compare_audit_maps(ha_map, com_map, {}, (None, None, None), {}, set(), set())


def test_synced_quickbus_commands_audit_clean(parsing_api: ComexioAPI, quickbus_result: dict[str, Any]) -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)

    found = _audit(ha_map, _comexio_commands(parsing_api, quickbus_result))

    assert {category: items for category, items in found.items() if items} == {}


def test_a_quickbus_command_of_a_renamed_device_is_an_orphan(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)
    commands = _comexio_commands(parsing_api, quickbus_result)
    commands["HA QB9 Old G1"] = {"typeId": 1, "cmdId": "99", "webioClass": WebioClass.QUICKBUS}

    found = _audit(ha_map, commands)

    assert [(o["name"], o["webio_class"]) for o in found["orphan"]] == [("HA QB9 Old G1", WebioClass.QUICKBUS)]
    assert not found["missing"]


def test_a_quickbus_command_of_a_renamed_device_is_renamed_in_place(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    """Device id + identifier still pin the IO: rename the command, never delete and recreate it."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)
    commands = _comexio_commands(parsing_api, quickbus_result)
    commands["HA QB1 Old Name G1"] = commands.pop("HA QB1 MT Flur G1")

    found = _audit(ha_map, commands)

    assert [(r["id"], r["name"], r["webio_class"]) for r in found["rename"]] == [
        (commands["HA QB1 Old Name G1"]["cmdId"], "HA QB1 MT Flur G1", WebioClass.QUICKBUS)
    ]
    assert not found["orphan"]
    assert not found["missing"]


def test_a_missing_quickbus_command_is_created_in_the_quickbus_class(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)
    commands = _comexio_commands(parsing_api, quickbus_result)
    del commands["HA QB1 MT Flur G1"]

    found = _audit(ha_map, commands)

    assert [(m["name"], m["webio_class"]) for m in found["missing"]] == [("HA QB1 MT Flur G1", WebioClass.QUICKBUS)]
    assert not found["orphan"]


def test_quickbus_commands_of_a_disabled_import_are_no_orphans(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    commands = _comexio_commands(parsing_api, quickbus_result)
    kept, disabled = _split_disabled_commands(commands, (WebioClass.MARKER, WebioClass.IO))
    assert list(kept) == ["HA IO BASE I1"]
    assert disabled == {WebioClass.QUICKBUS: 8}


def test_quickbus_cleanup_scope_has_a_label_and_only_its_class() -> None:
    assert set(_CLEANUP_SCOPE_LABELS) == set(CLEANUP_SCOPES)
    assert webio_classes_in_scope(CLEANUP_SCOPE_QUICKBUS) == (WebioClass.QUICKBUS,)
    assert WebioClass.QUICKBUS in webio_classes_in_scope("full")
