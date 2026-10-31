"""Quickbus IOs (#86): own data key, own Web-IO class [QB], read-only entities, webhook by IO Id."""

import json
import logging
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from aiocomexio.webio import WEBIO_PROTOCOL_HTTP
import pytest

from custom_components.comexio import _quickbus_entity_uids, coordinator as coordinator_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.cleanup_scope import CLEANUP_SCOPE_QUICKBUS, CLEANUP_SCOPES, webio_classes_in_scope
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_QUICKBUS_DEVICES,
    CONF_IGNORED_KNX,
    QUICKBUS_IO_DATA_KEY,
    SOURCE_CATEGORIES,
    WEBHOOK_UNKNOWN_IO_CAP_LOG_MSG,
    WEBHOOK_UNKNOWN_IO_KEY_MAX_LEN,
    WEBIO_COMMAND_DUPLICATES_KEY,
    WebioClass,
    known_extension_names,
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
    _webhook_warn_key,
)
from custom_components.comexio.repairs import _CLEANUP_SCOPE_LABELS

_QUICKBUS_CATEGORY = SOURCE_CATEGORIES[WebioClass.QUICKBUS]


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
    commands = parsing_api.build_webio_commands("test", quickbus_result)
    # Like every class, the commands are sent over HTTP; Comexio picks HTTPS itself where the device port needs it.
    assert {cmd["Protocol"] for cmd in commands} == {WEBIO_PROTOCOL_HTTP}
    names = [cmd["Name"] for cmd in commands]
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
    # Each command addresses its own IO by its unique Id, not by ext + identifier; no physical IO in the class.
    payloads = {
        cmd["Name"]: re.search(r"\{ (.*) \}", cmd["Data"]).group(1) for cmd in json.loads(quickbus_json)["commands"]
    }
    assert payloads == {
        name: f'id="{io_id}", value=a, type="quickbus"'
        for name, io_id in {
            "HA QB1 MT Flur G1": 201,
            "HA QB1 MT Flur G18": 202,
            "HA QB1 MT Flur TL1": 203,
            "HA QB1 MT Flur R": 204,
            "HA QB2 MT Bad G1": 211,
            "HA QB2 MT Bad TL1": 213,
            "HA QB3 BM Diele PIR1": 301,
            "HA QB3 BM Diele TL1": 302,
        }.items()
    }


def test_quickbus_audit_map_names_match_the_web_io_commands(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    """The audit expects exactly the commands the sync creates — else every poll reports them missing."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    coordinator.config_entry = SimpleNamespace(data={}, options={})  # QB device selection never saved: all devices

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
    # Every Quickbus IO has its IO meta — the wiring check of the QB cluster plans reads it.
    assert {key: io_meta_by_key[key] for key in quickbus_keys} == {
        source_audit_key(_QUICKBUS_CATEGORY, io["id"]): io for io in quickbus_result["quickbus_io"]
    }


@pytest.mark.parametrize(("import_quickbus", "expected"), [(True, 8), (False, 0)])
def test_import_quickbus_option_gates_the_quickbus_ios(
    quickbus_result: dict[str, Any], import_quickbus: bool, expected: int
) -> None:
    imported = _imported_data(quickbus_result, {"import_quickbus": import_quickbus})
    assert len(imported[QUICKBUS_IO_DATA_KEY]) == expected
    assert len(imported["io"]) == 1


def test_import_quickbus_defaults_to_on(quickbus_result: dict[str, Any]) -> None:
    assert len(_imported_data(quickbus_result, {})[QUICKBUS_IO_DATA_KEY]) == 8


def test_imported_data_forwards_the_web_io_command_duplicates(quickbus_result: dict[str, Any]) -> None:
    """The audit reports every same-named command as an orphan only if the import passes them on (#251)."""
    duplicates = [{"name": "HA IO EXT-1 I1", "webIoId": 11, "cmdId": 11, "typeId": 1, "webioClass": "io"}]

    assert (
        _imported_data({**quickbus_result, WEBIO_COMMAND_DUPLICATES_KEY: duplicates}, {})[WEBIO_COMMAND_DUPLICATES_KEY]
        == duplicates
    )
    assert _imported_data(quickbus_result, {})[WEBIO_COMMAND_DUPLICATES_KEY] == []


@pytest.mark.parametrize(("import_quickbus", "keeps_quickbus_labels"), [(True, True), (False, False)])
def test_plan_labels_of_imported_quickbus_ios_survive_import_ios_off(
    quickbus_result: dict[str, Any], import_quickbus: bool, keeps_quickbus_labels: bool
) -> None:
    imported = _imported_data(quickbus_result, {"import_ios": False, "import_quickbus": import_quickbus})

    quickbus_labels = [io for io in quickbus_result["io_all"] if io.get("quickbus")]
    assert quickbus_labels
    assert len(quickbus_labels) < len(quickbus_result["io_all"])  # the physical labels are dropped
    assert imported["io_all"] == (quickbus_labels if keeps_quickbus_labels else [])
    assert imported["io"] == []


@pytest.mark.parametrize("import_quickbus", [True, False])
def test_import_ios_keeps_every_plan_label(quickbus_result: dict[str, Any], import_quickbus: bool) -> None:
    imported = _imported_data(quickbus_result, {"import_ios": True, "import_quickbus": import_quickbus})

    assert any(not io.get("quickbus") for io in quickbus_result["io_all"])
    assert imported["io_all"] == quickbus_result["io_all"]


def _webhook_coordinator(quickbus_ios: list[dict[str, Any]]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "iosrv1"
    coordinator._quickbus_index = {str(io["id"]): io for io in quickbus_ios}
    coordinator.io_states = {}
    coordinator._io_real_value_ids = set()
    coordinator._webhook_updated_io_ids = set()
    coordinator._warned_unknown_webhook_ios = set()
    coordinator._warned_unknown_webhook_cap = False
    coordinator._io_identities = {}
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
    # A real reading (#175): it stays the last known value when the next live fetch fails.
    assert coordinator._io_real_value_ids == {"211"}
    coordinator._fire_plan_event.assert_called_once_with("io", "211", "MT Bad G1", 1)
    coordinator._async_publish_pushed_value.assert_called_once()


def test_quickbus_webhook_for_an_unknown_id_is_dropped_with_a_warning(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    coordinator.update_quickbus_io("999", 1)

    assert coordinator.io_states == {}
    assert coordinator._io_real_value_ids == set()
    assert coordinator._webhook_updated_io_ids == set()
    coordinator._fire_plan_event.assert_not_called()
    assert "unknown Quickbus IO 999" in caplog.text


def test_only_the_first_push_of_an_unknown_quickbus_io_warns(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A stale command pushes on every value change: one WARNING per IO, the rest at DEBUG (#251)."""
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    with caplog.at_level(logging.DEBUG):
        for io_id in ("999", "999", "998", "999"):
            coordinator.update_quickbus_io(io_id, 1)

    levels = [(record.levelno, record.args[0]) for record in caplog.records if "unknown Quickbus IO" in record.msg]
    assert levels == [
        (logging.WARNING, "999"),
        (logging.DEBUG, "999"),
        (logging.WARNING, "998"),
        (logging.DEBUG, "999"),
    ]


def test_an_io_that_became_known_warns_again_once_unknown(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A matched push forgets the earlier warning: the IO going unknown again is news, not noise."""
    quickbus_ios = quickbus_result[QUICKBUS_IO_DATA_KEY]
    coordinator = _webhook_coordinator(quickbus_ios)
    known = coordinator._quickbus_index.pop("211")

    with caplog.at_level(logging.DEBUG):
        coordinator.update_quickbus_io("211", 1)
        coordinator._quickbus_index["211"] = known
        coordinator.update_quickbus_io("211", 0)
        del coordinator._quickbus_index["211"]
        coordinator.update_quickbus_io("211", 1)

    levels = [record.levelno for record in caplog.records if "unknown Quickbus IO" in record.msg]
    assert levels == [logging.WARNING, logging.WARNING]
    assert coordinator._warned_unknown_webhook_ios == {_webhook_warn_key("quickbus", "211")}


def test_a_push_for_a_former_io_forgets_the_earlier_warning(quickbus_result: dict[str, Any]) -> None:
    """The id is known again, if for another IO now: it going unknown later warns again."""
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])
    known = coordinator._quickbus_index.pop("211")

    coordinator.update_quickbus_io("211", 1)
    coordinator._quickbus_index["211"] = known
    coordinator._io_identities["211"] = ("another", "io")
    coordinator.update_quickbus_io("211", 0)

    assert coordinator.io_states == {}
    assert coordinator._warned_unknown_webhook_ios == set()


def test_the_warned_unknown_ios_stay_bounded(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pushed id comes from the request: past the limit nothing more is remembered, and one warning
    says so instead of a warning on every push of every further unknown IO (#251)."""
    monkeypatch.setattr(coordinator_module, "WEBHOOK_UNKNOWN_IO_WARNED_MAX", 2)
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    with caplog.at_level(logging.DEBUG):
        for io_id in ("997", "998", "999", "999", "997"):
            coordinator.update_quickbus_io(io_id, 1)

    assert coordinator._warned_unknown_webhook_ios == {
        _webhook_warn_key("quickbus", "997"),
        _webhook_warn_key("quickbus", "998"),
    }
    levels = [record.levelno for record in caplog.records if "unknown Quickbus IO" in record.msg]
    assert levels == [logging.WARNING, logging.WARNING, logging.DEBUG, logging.DEBUG, logging.DEBUG]
    caps = [record.levelno for record in caplog.records if record.msg == WEBHOOK_UNKNOWN_IO_CAP_LOG_MSG]
    assert caps == [logging.WARNING]


def test_a_remembered_unknown_io_id_is_cut_to_a_bounded_length(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The limit counts entries: a huge pushed id must not be held in full. Two ids that share
    the cut prefix are still two IOs, each warned about once."""
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])

    with caplog.at_level(logging.DEBUG):
        for io_id in ("9" * 10_000, "9" * 9_999 + "8", "9" * 10_000):
            coordinator.update_quickbus_io(io_id, 1)

    warned = coordinator._warned_unknown_webhook_ios
    assert len(warned) == 2
    assert max(len(part) for key in warned for part in key) <= WEBHOOK_UNKNOWN_IO_KEY_MAX_LEN
    levels = [record.levelno for record in caplog.records if "unknown Quickbus IO" in record.msg]
    assert levels == [logging.WARNING, logging.WARNING, logging.DEBUG]


def test_a_long_unknown_io_id_that_becomes_known_is_forgotten(quickbus_result: dict[str, Any]) -> None:
    """The matched push forgets the cut key it was remembered under, not the full id."""
    coordinator = _webhook_coordinator(quickbus_result[QUICKBUS_IO_DATA_KEY])
    long_id = "9" * 10_000

    coordinator.update_quickbus_io(long_id, 1)
    coordinator._quickbus_index[long_id] = coordinator._quickbus_index["211"]
    coordinator.update_quickbus_io(long_id, 0)

    assert coordinator._warned_unknown_webhook_ios == set()


def test_poll_keeps_a_quickbus_webhook_value_that_arrived_during_the_fetch(quickbus_result: dict[str, Any]) -> None:
    """R1 for Quickbus IOs: the webhook value wins over the older poll snapshot."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.marker_states = {}
    coordinator.io_states = {"201": 1}
    coordinator._io_real_value_ids = {"201"}
    coordinator._polled_io_real_ids = set()
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


_QB_IO = {"id": "201", "quickbus": {"device_id": 1, "name": "MT1"}}


@pytest.mark.parametrize(
    ("quickbus_ios", "expected"),
    [
        ([], (WebioClass.MARKER, WebioClass.IO)),
        ([_QB_IO], (WebioClass.MARKER, WebioClass.IO, WebioClass.QUICKBUS)),
    ],
)
def test_quickbus_class_is_only_required_with_a_quickbus_io(
    quickbus_ios: list[dict[str, Any]], expected: tuple[WebioClass, ...]
) -> None:
    """import_quickbus is on by default: a server without Quickbus devices must not get an empty
    [QB] class created, nor a "missing Web-IO class" repair until then."""
    assert required_webio_classes({}, {QUICKBUS_IO_DATA_KEY: quickbus_ios}) == expected


def test_quickbus_class_is_not_required_without_a_selected_device() -> None:
    """A Quickbus IO reaches HA only through its wired plan pair: no selected device, no [QB] class."""
    conf = {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}
    assert WebioClass.QUICKBUS not in required_webio_classes(conf, {QUICKBUS_IO_DATA_KEY: [_QB_IO]})


def test_quickbus_class_is_not_required_while_its_import_is_off() -> None:
    conf = {"import_quickbus": False}
    assert WebioClass.QUICKBUS not in required_webio_classes(conf, {QUICKBUS_IO_DATA_KEY: [_QB_IO]})


def _missing_class_coordinator(conf: dict[str, Any], api: Any = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.hass = MagicMock()
    coordinator.server_id = "iosrv1"
    coordinator.config_entry = MagicMock(entry_id="e1", data={}, options=conf)
    # Every class has a wanted command unless a test passes the real builder.
    coordinator.api = api or SimpleNamespace(build_webio_commands=lambda *_args, **_kwargs: [{"Name": "HA cmd"}])
    coordinator._knx_bridge_marker_ids = set()
    coordinator.in_sync = False
    coordinator.last_audit_results = {"kept": True}
    return coordinator


@pytest.mark.parametrize(("quickbus_ios", "missing"), [([], False), ([_QB_IO], True)])
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


@pytest.mark.parametrize(("quickbus_ios", "required"), [([], False), ([_QB_IO], True)])
def test_coordinator_requires_the_quickbus_class_from_its_current_data(
    quickbus_ios: list[dict[str, Any]], required: bool
) -> None:
    """The property the sync button and generate_web_io read."""
    coordinator = _missing_class_coordinator({})
    coordinator.data = {QUICKBUS_IO_DATA_KEY: quickbus_ios}
    assert (WebioClass.QUICKBUS in coordinator.required_webio_classes) is required


_KNX_DATA = {
    "markers": [{"id": "5", "name": "M5 Licht", "type": "digital"}],
    "io": [{"ext_name": "BASE", "identifier": "Q1", "is_binary": True, "min": 0, "max": 1}],
    "knx": [{"id": "8", "name": "K8 Temp", "type": "analog", "dpt_min": -273, "dpt_max": 670760}],
}


@pytest.mark.parametrize(
    ("ignored", "missing"),
    [("8", False), ("", True)],
    ids=["every_object_ignored", "an_object_wanted"],
)
def test_a_class_without_a_wanted_command_is_not_required(
    monkeypatch: pytest.MonkeyPatch, comexio_api: ComexioAPI, ignored: str, missing: bool
) -> None:
    """Comexio creates no device instance for an empty class: a KNX class with every object ignored
    reported missing would be a repair no sync can fix, and the sync's recreate of it would fail."""
    create_issue = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.ir.async_create_issue", create_issue)
    conf = {"import_knx": True, CONF_IGNORED_KNX: ignored}
    coordinator = _missing_class_coordinator(conf, comexio_api)
    coordinator.data = _KNX_DATA
    devices = {WebioClass.MARKER: {"device_id": "1"}, WebioClass.IO: {"device_id": "2"}}

    assert coordinator._report_missing_webio_classes(conf, devices, _KNX_DATA) is missing
    assert (WebioClass.KNX in coordinator.required_webio_classes) is missing
    if missing:
        assert create_issue.call_args.kwargs["data"]["missing_classes"] == [WebioClass.KNX]


def test_every_class_stays_required_before_the_first_poll(comexio_api: ComexioAPI) -> None:
    # No data yet: nothing is known to be empty, so no class may drop out.
    coordinator = _missing_class_coordinator({"import_knx": True, CONF_IGNORED_KNX: "8"}, comexio_api)
    coordinator.data = None

    assert coordinator.required_webio_classes == (WebioClass.MARKER, WebioClass.IO, WebioClass.KNX)


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
    coordinator.config_entry = SimpleNamespace(data={}, options={})  # QB device selection never saved: all devices
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)

    found = _audit(ha_map, _comexio_commands(parsing_api, quickbus_result))

    assert {category: items for category, items in found.items() if items} == {}


def test_a_quickbus_command_of_a_renamed_device_is_an_orphan(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    coordinator.config_entry = SimpleNamespace(data={}, options={})  # QB device selection never saved: all devices
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
    coordinator.config_entry = SimpleNamespace(data={}, options={})  # QB device selection never saved: all devices
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
    coordinator.config_entry = SimpleNamespace(data={}, options={})  # QB device selection never saved: all devices
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


@pytest.mark.parametrize(
    ("include_offline", "expected"),
    [(False, {"BASE", "QB-Host"}), (True, {"BASE", "QB-Host", "Offline-Host"})],
)
def test_an_extension_hosting_only_quickbus_devices_is_a_known_extension(
    include_offline: bool, expected: set[str]
) -> None:
    """Quickbus IOs leave ``io``: a module hosting only Quickbus devices must still get its firmware entity."""
    data = {
        "io": [{"ext_name": "BASE", "identifier": "I1"}],
        QUICKBUS_IO_DATA_KEY: [
            {"ext_name": "QB-Host", "identifier": "G1", "quickbus": True},
            {"ext_name": "Offline-Host", "identifier": "G1", "quickbus": True, "offline": True},
        ],
    }

    assert known_extension_names(data, include_offline) == expected
