"""Quickbus IOs in managed column plans ("HA - QB [..]"): naming, membership, audit, wiring, repair text."""

import asyncio
import copy
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import ComexioSyncButton, _merge_io_items, _parse_io_from_webio_name
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_FUNCTION_PLAN_PLAN_PREFIX,
    CONF_FUNCTION_PLAN_QUICKBUS_DEVICES,
    DOMAIN,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_X_WEBIO,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    IO_PLAN_KIND_IO,
    IO_PLAN_KIND_QUICKBUS,
    QUICKBUS_IO_DATA_KEY,
    SYNC_DURATION_FUNCTION_PLAN_ELEMENT,
    SYNC_DURATION_FUNCTION_PLAN_FINALIZE,
    IoPlanKind,
    canvas_columns,
    io_plan_columns,
    io_plan_gap_item,
    io_plan_item_member,
    io_plan_kind_of,
    io_plan_label,
    io_plan_member_origins,
    io_plan_overflow,
    io_plan_shifted,
    quickbus_column_blocks,
    selected_quickbus_devices,
    webio_quickbus_ios,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.options_flow import (
    ComexioOptionsFlow,
    _drop_unchanged_quickbus_default,
    _quickbus_device_options,
    _shown_quickbus_pick,
)
from custom_components.comexio.repairs import _function_plan_gap_lines, _function_plan_option_label
from custom_components.comexio.services import misc as services_misc
from custom_components.comexio.services._grid import _io_header_slots, _io_ref_slots
from custom_components.comexio.services.plan_actions import _column_plan_sort_skip, _sort_compute_positions

PREFIX = "HA"


def _qb_io(result: dict[str, Any], io_id: str) -> dict[str, Any]:
    return next(io for io in result[QUICKBUS_IO_DATA_KEY] if io["id"] == io_id)


def _coordinator(options: dict[str, Any], data: dict[str, Any], offline: set[str] | None = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.config_entry = SimpleNamespace(data={}, options=options)
    coordinator.data = data
    coordinator.offline_extensions = offline or set()
    coordinator.server_id = "test"
    # A fresh A3 canvas wide enough for any test member (io_plan_overflow asks what a fresh plan takes).
    coordinator.api = SimpleNamespace(get_fub_canvas_bounds=lambda *_a, **_k: (4000.0, 4000.0), fub_data={})
    return coordinator


# --- plan names and membership ---


@pytest.mark.parametrize(
    ("name", "kind", "members"),
    [
        ("HA - QB [1,3]", IO_PLAN_KIND_QUICKBUS, ["1", "3"]),
        ("HA - QB [1]", IO_PLAN_KIND_IO, None),  # an IO plan never claims a QB plan ...
        ("HA - IO [BASE]", IO_PLAN_KIND_QUICKBUS, None),  # ... nor the other way round
        ("Other - QB [1]", IO_PLAN_KIND_QUICKBUS, None),
    ],
)
def test_column_plan_members_from_the_plan_name(name: str, kind: Any, members: list[str] | None) -> None:
    assert ComexioCoordinator._io_plan_members(name, PREFIX, kind) == members


def test_a_quickbus_plan_name_round_trips() -> None:
    name = ComexioCoordinator._io_plan_name(PREFIX, ["1", "2"], IO_PLAN_KIND_QUICKBUS)

    assert name == "HA - QB [1,2]"
    assert ComexioCoordinator._io_plan_members(name, PREFIX, IO_PLAN_KIND_QUICKBUS) == ["1", "2"]


def test_membership_and_column_plan_lookup_keep_the_kinds_apart() -> None:
    fub_data = {"4": {"Name": "HA - IO [BASE]"}, "5": {"Name": "HA - QB [1,2]"}, "6": {"Name": "Lights"}}
    coordinator = _coordinator({}, {})
    coordinator.api = SimpleNamespace(fub_data=fub_data)
    coordinator._distrusted_fub_ids = set()
    coordinator._function_plan_prefix = lambda: PREFIX  # type: ignore[method-assign]

    assert coordinator._io_plan_membership(PREFIX, IO_PLAN_KIND_QUICKBUS) == {5: ["1", "2"]}
    assert coordinator._io_plan_membership(PREFIX, IO_PLAN_KIND_IO) == {4: ["BASE"]}
    assert coordinator.managed_column_plan(5) == (IO_PLAN_KIND_QUICKBUS, ["1", "2"])
    assert coordinator.managed_column_plan(4) == (IO_PLAN_KIND_IO, ["BASE"])
    assert coordinator.managed_column_plan(6) is None
    assert coordinator.io_cluster_plan_contains("HA - QB [1,2]", "2", IO_PLAN_KIND_QUICKBUS)
    assert not coordinator.io_cluster_plan_contains("HA - QB [1,2]", "2")


# --- column layout ---


def test_a_quickbus_column_lists_its_ios_by_io_id_under_the_device_name(quickbus_result: dict[str, Any]) -> None:
    ios = IO_PLAN_KIND_QUICKBUS.member_ios(quickbus_result, "1")
    rows, headers = quickbus_column_blocks("1", list(reversed(ios)))

    # Comexio's own order (IO Id 201..204), not the type-group order of an extension column.
    assert rows == {"G1": 1, "G18": 2, "TL1": 3, "R": 4}
    assert list(headers) == [0]
    assert "MT Flur" in headers[0]
    assert quickbus_column_blocks("1", []) == ({}, {})


# --- audit items ---


def test_gap_items_name_their_column_member(quickbus_result: dict[str, Any]) -> None:
    qb_io = _qb_io(quickbus_result, "301")
    plain_io = quickbus_result["io"][0]

    qb_item = io_plan_gap_item("HA QB3 BM Diele PIR1", qb_io)
    io_item = io_plan_gap_item("HA IO BASE I1", plain_io)

    assert qb_item == {"name": "HA QB3 BM Diele PIR1", "quickbus_device": "3", "identifier": "PIR1"}
    assert io_item == {"name": "HA IO BASE I1", "ext_name": "BASE", "identifier": "I1"}
    assert io_plan_item_member(qb_item) == (IO_PLAN_KIND_QUICKBUS, "3")
    assert io_plan_item_member(io_item) == (IO_PLAN_KIND_IO, "BASE")
    assert io_plan_item_member({"name": "x", "marker_id": 5}) is None
    assert io_plan_kind_of(qb_io) is IO_PLAN_KIND_QUICKBUS
    # The Quickbus device, not its host extension (BASE), labels the IO in a plan.
    assert io_plan_label(qb_io) == "QB3 BM Diele PIR1"
    assert IO_PLAN_KIND_QUICKBUS.member_label(quickbus_result, "3") == "QB3 BM Diele"
    assert IO_PLAN_KIND_QUICKBUS.member_label(quickbus_result, "9") == "QB9"  # gone: its id only
    assert IO_PLAN_KIND_IO.member_label(quickbus_result, "UD9") == "UD9"


def test_managed_io_groups_take_only_present_online_quickbus_devices(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    data = copy.deepcopy(quickbus_result)
    for io in data[QUICKBUS_IO_DATA_KEY]:
        if io["quickbus"]["name"] == "MT Bad":
            io["offline"] = True
    # A second device carrying the name "BM Diele" is a member of its own: ids, not names, identify them.
    data[QUICKBUS_IO_DATA_KEY].append(
        {**_qb_io(data, "302"), "id": "999", "quickbus": {"device_id": 77, "name": "BM Diele"}}
    )
    options = {
        CONF_FUNCTION_PLAN_IO_EXTENSIONS: ["BASE", "UD1"],
        CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["1", "2", "3", "77", "9"],
    }

    with caplog.at_level(logging.WARNING):
        groups = _coordinator(options, data, offline={"UD1"}).managed_io_groups()

    assert groups == {("IO", "BASE"), ("QB", "1"), ("QB", "3"), ("QB", "77")}
    assert "'QB2 MT Bad'" in caplog.text
    assert "'QB9'" in caplog.text


def test_managed_io_groups_take_every_quickbus_device_while_the_option_was_never_saved(
    quickbus_result: dict[str, Any],
) -> None:
    options = {CONF_FUNCTION_PLAN_IO_EXTENSIONS: ["BASE", "UD1"]}

    assert _coordinator(options, quickbus_result, offline={"UD1"}).managed_io_groups() == {
        ("IO", "BASE"),
        ("QB", "1"),
        ("QB", "2"),
        ("QB", "3"),
    }


def test_managed_io_groups_with_no_quickbus_device_selected_are_the_online_extensions(
    quickbus_result: dict[str, Any],
) -> None:
    options = {CONF_FUNCTION_PLAN_IO_EXTENSIONS: ["BASE", "UD1"], CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}

    assert _coordinator(options, quickbus_result, offline={"UD1"}).managed_io_groups() == {("IO", "BASE")}


@pytest.mark.parametrize(
    ("options", "devices"),
    [
        ({}, {"1", "2", "3"}),  # never saved: every device
        ({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["2"]}, {"2"}),
        ({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}, set()),
    ],
)
def test_only_selected_quickbus_devices_get_web_io_commands(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any], options: dict[str, Any], devices: set[str]
) -> None:
    """A command of an unselected device is never wired, so it would push nothing to HA."""
    assert selected_quickbus_devices(options, quickbus_result) == devices
    coordinator = _coordinator(options, quickbus_result)
    coordinator.ignored_ids_for = lambda _cls: set()  # type: ignore[method-assign]
    expected_ids = {
        io["id"] for io in quickbus_result[QUICKBUS_IO_DATA_KEY] if str(io["quickbus"]["device_id"]) in devices
    }

    source = coordinator.webio_source_data()
    ha_map, _meta = coordinator._build_ha_audit_map(quickbus_result)
    commands = parsing_api.build_webio_commands("test", source)

    assert {io["id"] for io in webio_quickbus_ios(options, quickbus_result)} == expected_ids
    assert {io["id"] for io in source[QUICKBUS_IO_DATA_KEY]} == expected_ids
    assert {key[len("QB") :] for key in ha_map if key.startswith("QB")} == expected_ids
    assert {c["Name"] for c in commands if c["Name"].startswith("HA QB")} == {
        ha_map[f"QB{io_id}"]["name"] for io_id in expected_ids
    }
    assert source["io"] == quickbus_result["io"]  # IO extensions are untouched


@pytest.mark.parametrize(
    ("groups", "wired", "expected_member"),
    [
        ({("QB", "3")}, set(), "3"),  # managed, unwired: a gap
        ({("QB", "3")}, {("301", "55")}, None),  # wired
        ({("IO", "BASE")}, set(), None),  # its host extension is managed, the device is not
    ],
    ids=["gap", "wired", "not-managed"],
)
def test_gap_item_of_a_quickbus_io(
    quickbus_result: dict[str, Any], groups: set, wired: set, expected_member: str | None
) -> None:
    item = ComexioCoordinator._function_plan_gap_item(
        "QB301", "HA QB3 BM Diele PIR1", {"webIoId": 55}, (None, wired, None), _qb_io(quickbus_result, "301"), groups
    )

    assert (item or {}).get("quickbus_device") == expected_member


def test_missing_detail_counts_quickbus_devices_apart_from_extensions(quickbus_result: dict[str, Any]) -> None:
    coordinator = _coordinator({}, quickbus_result)
    coordinator.ignored_ids_for = lambda _cls: set()  # type: ignore[method-assign]
    ha_map, io_meta_by_key = coordinator._build_ha_audit_map(quickbus_result)
    missing = [
        io_plan_gap_item("a", _qb_io(quickbus_result, "301")),
        io_plan_gap_item("b", _qb_io(quickbus_result, "211")),
        io_plan_gap_item("c", _qb_io(quickbus_result, "213")),
        io_plan_gap_item("d", quickbus_result["io"][0]),
    ]

    detail = coordinator._function_plan_missing_detail(missing, ha_map, io_meta_by_key)

    assert detail["quickbus_by_device"] == {"QB3 BM Diele": [1, 2], "QB2 MT Bad": [2, 2]}
    assert detail["ios_by_ext"] == {"BASE": [1, 1]}


_NO_WIRING = (set(), set(), set(), set(), set(), set())
_QB_PLAN, _IO_PLAN = 70, 80


def _dangling_coordinator(quickbus_result: dict[str, Any], dangling: dict[str, list[int]]) -> ComexioCoordinator:
    coordinator = _coordinator({}, quickbus_result)
    coordinator._dangling_source_ids = lambda source_type, *_args: dangling if source_type == "1" else {}  # type: ignore[method-assign]
    plans = {_QB_PLAN: (IO_PLAN_KIND_QUICKBUS, ["3"]), _IO_PLAN: (IO_PLAN_KIND_IO, ["BASE"])}
    coordinator.managed_column_plan = plans.get  # type: ignore[method-assign]
    return coordinator


def test_dangling_quickbus_debris_is_removed_by_the_quickbus_class(quickbus_result: dict[str, Any]) -> None:
    # 301 is a QB IO; 999 is gone from the data and sat in a QB plan, 998 gone from an IO plan.
    dangling = {"301": [_QB_PLAN], "999": [_QB_PLAN], "998": [_IO_PLAN], "11": [_IO_PLAN]}
    coordinator = _dangling_coordinator(quickbus_result, dangling)

    items = coordinator._function_plan_dangling_items(
        quickbus_result, _NO_WIRING, False, {("QB", "3"), ("IO", "BASE")}, set()
    )

    by_ref = {item["ref_id"]: item for item in items}
    assert by_ref["301"]["name"] == "QB3 BM Diele PIR1"
    assert by_ref["301"]["webio_class"] == "quickbus"
    assert by_ref["999"]["webio_class"] == "quickbus"
    assert by_ref["998"]["webio_class"] == "io"
    assert by_ref["11"]["webio_class"] == "io"


def test_dangling_debris_of_a_kind_without_a_selected_member_is_not_reported(
    quickbus_result: dict[str, Any],
) -> None:
    # Only Quickbus devices are managed: the IO class is not synced, so IO debris would stay a
    # repair nobody can clear.
    coordinator = _dangling_coordinator(quickbus_result, {"301": [_QB_PLAN], "11": [_IO_PLAN], "998": [_IO_PLAN]})
    mismatches: set[str] = set()

    items = coordinator._function_plan_dangling_items(quickbus_result, _NO_WIRING, False, {("QB", "3")}, mismatches)

    assert [item["ref_id"] for item in items] == ["301"]
    assert mismatches == {"function_plan_dangling_IO301"}


# --- sync wiring ---


def test_created_quickbus_commands_resolve_to_their_device() -> None:
    known = {"HA QB3 BM Diele PIR1": ("QB", "3", "PIR1")}

    assert _parse_io_from_webio_name("HA QB3 BM Diele PIR1", known) == ("QB", "3", "PIR1")
    assert _parse_io_from_webio_name("HA IO UD1 Q3", known) == ("IO", "UD1", "Q3")
    assert _parse_io_from_webio_name("HA QB9 Unknown G1", known) is None


def test_merge_io_items_keeps_managed_members_only() -> None:
    created = [("QB", "1", "G1"), ("QB", "2", "G1"), ("IO", "BASE", "I1")]
    gaps = [
        {"name": "x", "quickbus_device": "1", "identifier": "G1"},  # also created: once
        {"name": "y", "quickbus_device": "3", "identifier": "PIR1"},
        {"name": "w", "quickbus_device": "4", "identifier": "G1"},  # deselected since the audit
        {"name": "z", "marker_id": 5},  # not a column-plan item
    ]

    merged = _merge_io_items(created, gaps, {("QB", "1"), ("QB", "3"), ("IO", "BASE")})

    assert merged == [("QB", "1", "G1"), ("IO", "BASE", "I1"), ("QB", "3", "PIR1")]


def test_quickbus_pairs_are_wired_to_their_quickbus_commands(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any]
) -> None:
    fresh = {**quickbus_result, "webio_commands": {"HA QB1 MT Flur G18": {"webIoId": 502}}}
    api = parsing_api
    api._reload_config_until_commands_ready = AsyncMock(return_value=fresh)
    api.function_plan_load_elements = AsyncMock(return_value={})
    api._function_plan_existing_refs = lambda _plan: ({}, [])
    api.get_fub_canvas_bounds = lambda _fub: (2000.0, 2000.0)
    api._function_plan_wire_ref_pair = AsyncMock(return_value=None)

    added, errors = asyncio.run(
        api.function_plan_add_io_pairs(7, "1", ["G18", "R"], column_index=1, kind=IO_PLAN_KIND_QUICKBUS, row_offset=6)
    )

    # R has no Web-IO command yet: reported, G18 wired to its own command.
    assert added == ["G18"]
    assert len(errors) == 1
    assert "HA QB1 MT Flur R" in errors[0]
    expected_names = api._reload_config_until_commands_ready.call_args.args[0]
    assert expected_names(fresh) == {"HA QB1 MT Flur G18", "HA QB1 MT Flur R"}
    args = api._function_plan_wire_ref_pair.call_args.args
    assert args[:5] == (7, 1, 202, 502, "binary")
    assert args[5] == "QB1 MT Flur G18"
    # Row 2 under the device header, which starts at row 6 of column 1 (stacked below another device).
    assert args[8][0] == FUNCTION_PLAN_LAYOUT_X_MARKER + FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
    assert args[8][2] == FUNCTION_PLAN_LAYOUT_Y_START + (6 + 2) * FUNCTION_PLAN_LAYOUT_Y_STEP


@pytest.mark.parametrize(("grown", "wired"), [(0, True), (1, False)], ids=["unchanged", "changed-since-poll"])
def test_quickbus_pairs_wait_when_an_io_count_changed_since_the_poll(
    parsing_api: ComexioAPI, quickbus_result: dict[str, Any], grown: int, wired: bool
) -> None:
    api = parsing_api
    api._reload_config_until_commands_ready = AsyncMock(return_value={**quickbus_result, "webio_commands": {}})
    api.function_plan_load_elements = AsyncMock(return_value={})
    api._function_plan_existing_refs = lambda _plan: ({}, [])
    api.get_fub_canvas_bounds = lambda _fub: (2000.0, 2000.0)
    # Device 3 sits below device 1: the last poll had one row more for device 1 than the fresh config.
    polled_rows = {
        "1": IO_PLAN_KIND_QUICKBUS.rows_needed(quickbus_result, "1") + grown,
        "3": IO_PLAN_KIND_QUICKBUS.rows_needed(quickbus_result, "3"),
    }

    _added, errors = asyncio.run(
        api.function_plan_add_io_pairs(7, "3", ["PIR1"], kind=IO_PLAN_KIND_QUICKBUS, stacked_rows=polled_rows)
    )

    assert api.function_plan_load_elements.await_count == int(wired)
    # The message names the device whose count changed, not the one being wired.
    assert any("IO count of QB1 MT Flur changed" in error for error in errors) is not wired


@pytest.mark.parametrize(
    ("fresh_canvas", "skipped"), [(4000.0, True), (100.0, False)], ids=["shrunk-paper", "too-tall-for-any"]
)
def test_the_sort_skips_a_plan_whose_first_device_left_a_shrunk_canvas(
    quickbus_result: dict[str, Any], fresh_canvas: float, skipped: bool
) -> None:
    # One row per column and one column: device 1 runs past it. A fresh plan taking it means the plan's
    # paper shrank (deleting the plan helps); none taking it keeps the sort, as no layout fits it.
    coordinator = _coordinator({}, {**quickbus_result, "io": []})
    coordinator.api = SimpleNamespace(get_fub_canvas_bounds=lambda *_a, **_k: (fresh_canvas, fresh_canvas), fub_data={})

    line = _column_plan_sort_skip(coordinator, 7, (IO_PLAN_KIND_QUICKBUS, ["1"]), 1, 1)

    if skipped:
        assert "QB1 MT Flur no longer fit its 1 columns" in (line or "")
    else:
        assert line is None


# --- repair text ---


def test_repair_text_names_quickbus_devices() -> None:
    qb_by_device = {"QB1 MT Flur": [4, 4], "QB3 BM Diele": [1, 2]}
    detail = {"markers_by_plan": {}, "ios_by_ext": {}, "quickbus_by_device": qb_by_device}

    lines = _function_plan_gap_lines(5, detail)

    assert "Quickbus device QB1 MT Flur not yet in a QB cluster plan:** all 4 IOs" in lines[0]
    assert "Quickbus device QB3 BM Diele partially wired:** 1 of 2 IOs missing" in lines[1]
    assert "Wire in Function Plan (QB1 MT Flur +4 IOs, QB3 BM Diele +1 IOs," in _function_plan_option_label(
        5, detail, " ~1 min"
    )


@pytest.mark.parametrize(
    ("ios_by_ext", "qb_by_device", "noun"),
    [
        ({}, {"MT Flur": [4, 4]}, "Add Quickbus device to Function Plan"),
        ({}, {"MT Flur": [4, 4], "MT Bad": [2, 2]}, "Add Quickbus devices to Function Plan"),
        ({"BASE": [1, 1]}, {"MT Flur": [4, 4]}, "Add extensions and Quickbus devices to Function Plan"),
        ({"BASE": [1, 1]}, {}, "Add extension to Function Plan"),
    ],
)
def test_repair_option_names_whole_members(ios_by_ext: dict, qb_by_device: dict, noun: str) -> None:
    detail = {"markers_by_plan": {}, "ios_by_ext": ios_by_ext, "quickbus_by_device": qb_by_device}

    assert noun in _function_plan_option_label(5, detail, "")


# --- review follow-ups: log dedupe, wiring split, grid, sort, ETA, options ---


def test_skipped_quickbus_devices_are_logged_once_per_change(
    quickbus_result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    data = copy.deepcopy(quickbus_result)
    for io in data[QUICKBUS_IO_DATA_KEY]:
        if io["quickbus"]["name"] == "MT Bad":
            io["offline"] = True
    coordinator = _coordinator({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["1", "2", "9"]}, data)

    with caplog.at_level(logging.WARNING):
        groups = coordinator.managed_io_groups()
    assert groups == {("QB", "1")}
    assert "'QB2 MT Bad'" in caplog.text
    assert "'QB9'" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        coordinator.managed_io_groups()  # every poll calls this — an unchanged state stays quiet
    assert not caplog.text

    coordinator.config_entry.options[CONF_FUNCTION_PLAN_QUICKBUS_DEVICES] = ["1", "9"]
    with caplog.at_level(logging.WARNING):
        coordinator.managed_io_groups()
    assert "'QB9'" in caplog.text
    assert "MT Bad" not in caplog.text


def test_wire_io_clusters_wires_each_kind_on_its_own() -> None:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button._wire_io_kind_clusters = AsyncMock(side_effect=[(["io"], 1, 0), (["qb"], 2, 1)])  # type: ignore[method-assign]
    items = [("IO", "BASE", "I1"), ("QB", "1", "G1"), ("QB", "1", "G18")]

    lines, added, errors = asyncio.run(button._wire_io_clusters(SimpleNamespace(), items, {}))

    calls = button._wire_io_kind_clusters.await_args_list
    assert [(c.args[1], c.args[2]) for c in calls] == [
        (IO_PLAN_KIND_IO, {"BASE": ["I1"]}),
        (IO_PLAN_KIND_QUICKBUS, {"1": ["G1", "G18"]}),
    ]
    assert (lines, added, errors) == (["io", "qb"], 3, 1)


def test_wire_io_clusters_skips_a_kind_without_items() -> None:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button._wire_io_kind_clusters = AsyncMock(return_value=([], 1, 0))  # type: ignore[method-assign]

    asyncio.run(button._wire_io_clusters(SimpleNamespace(), [("QB", "2", "G1")], {}))

    assert [c.args[1] for c in button._wire_io_kind_clusters.await_args_list] == [IO_PLAN_KIND_QUICKBUS]


@pytest.mark.parametrize(
    ("kind", "is_fresh", "added", "sorted_"),
    [
        (IO_PLAN_KIND_QUICKBUS, False, ["G1"], True),
        (IO_PLAN_KIND_QUICKBUS, True, ["G1"], False),
        (IO_PLAN_KIND_QUICKBUS, False, [], False),
        (IO_PLAN_KIND_IO, False, ["G1"], False),
    ],
    ids=["existing-stacked-plan", "fresh-plan", "nothing-added", "io-plan"],
)
def test_pairs_added_to_an_existing_stacked_plan_restore_its_grid(
    monkeypatch: pytest.MonkeyPatch, kind: Any, is_fresh: bool, added: list[str], sorted_: bool
) -> None:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.server_id = "test"
    button.coordinator = SimpleNamespace(  # type: ignore[assignment]
        io_cluster_plan_contains=lambda *_args: True,
        async_function_plan_change_backup=AsyncMock(),
        data={},
        managed_column_plan=lambda _fub_id: (kind, ["1", "3"]),
        io_member_rows=lambda members, _kind: dict.fromkeys(members, 4),
    )
    button._plan_name = lambda _fub_id: "HA - QB [1,3]"  # type: ignore[method-assign]
    button._sort_and_reactivate = AsyncMock(return_value=", sorted")  # type: ignore[method-assign]
    resync = AsyncMock(return_value=1)
    monkeypatch.setattr("custom_components.comexio.button.async_resync_io_group_headers", resync)
    api = SimpleNamespace(
        fub_data={"7": {"Active": True}},
        function_plan_stop_fup=AsyncMock(),
        function_plan_add_io_pairs=AsyncMock(return_value=(added, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
    )
    ctx = SimpleNamespace(api=api, update_status=lambda *_args, **_kwargs: None, failed_writes=[])

    line, _added, _errors = asyncio.run(
        button._add_io_pairs_to_plan(ctx, 7, [("3", (0, 5))], {"3": ["G1"]}, is_fresh, {"done": 0}, kind)
    )

    if sorted_:
        button._sort_and_reactivate.assert_awaited_once_with(ctx, 7, "HA - QB [1,3]", True)
        assert ", sorted" in line
    else:
        button._sort_and_reactivate.assert_not_awaited()
    assert resync.await_count == int(bool(added) and not sorted_)
    # A stacked member's origin is checked against the fresh config: its own rows and those above it.
    expected_rows = {"1": 4, "3": 4} if kind.stacked else None
    assert api.function_plan_add_io_pairs.await_args.kwargs["stacked_rows"] == expected_rows


def test_dangling_source_ids_names_every_plan_an_id_dangles_in() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    elements = {"1": {"reference": {"type": "1", "ref_id": "301"}}, "2": {"reference": {"type": "1", "ref_id": "11"}}}
    coordinator.function_plan_plans = {
        70: {"elements": elements},
        80: {"elements": elements},
        90: {"elements": elements},
    }
    coordinator._function_plan_check_fub_ids = lambda: {70, 80}  # type: ignore[method-assign]
    coordinator._is_managed_function_plan = lambda _fub_id: True  # type: ignore[method-assign]
    coordinator._plan_wired_pairs = lambda plan, _type: (
        {("11", "x")} if plan is coordinator.function_plan_plans[80] else set()
    )  # type: ignore[method-assign]
    coordinator._plan_connected_source_ids = lambda _plan, _type: set()  # type: ignore[method-assign]

    dangling = coordinator._dangling_source_ids("1", set(), set())

    # 90 is no managed plan of the audit; 11 is wired in 80.
    assert {rid: sorted(fub_ids) for rid, fub_ids in dangling.items()} == {"301": [70, 80], "11": [70]}


@pytest.mark.parametrize("upload", [False, True], ids=["preview", "upload"])
def test_generate_web_io_service_builds_from_the_selected_quickbus_devices(
    monkeypatch: pytest.MonkeyPatch, upload: bool
) -> None:
    source = {"quickbus_io": ["selected only"]}
    api = SimpleNamespace(generate_webio_json=MagicMock(return_value="{}"))
    coordinator = SimpleNamespace(
        api=api,
        server_id="test",
        config_entry=SimpleNamespace(data={}, options={}),
        required_webio_classes=("quickbus",),
        data={"quickbus_io": ["every device"]},
        webio_source_data=lambda: source,
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
    )
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    monkeypatch.setattr(services_misc, "_upload_webio_class", AsyncMock(return_value="ok"))
    hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}})
    call = SimpleNamespace(data={"config_entry": "e1", "upload": upload})

    asyncio.run(services_misc.handle_generate_web_io(hass, call))

    assert api.generate_webio_json.call_args.args[2] is source


def test_quickbus_grid_slots_stack_devices_in_a_column(quickbus_result: dict[str, Any]) -> None:
    coordinator = _coordinator({}, quickbus_result)
    members = ["1", "2", "3"]

    # 10 rows per column: MT Flur (header + 4 IOs), a blank row and MT Bad (header + 2 IOs) share
    # column 0; BM Diele (3 rows) no longer fits below them and opens column 1.
    slots = _io_ref_slots(coordinator, members, 10, IO_PLAN_KIND_QUICKBUS)
    headers = _io_header_slots(coordinator, members, 10, IO_PLAN_KIND_QUICKBUS)

    y = [FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_LAYOUT_Y_STEP for row in range(10)]
    assert slots == {
        201: (0, y[1]),
        202: (0, y[2]),
        203: (0, y[3]),
        204: (0, y[4]),
        211: (0, y[7]),
        213: (0, y[8]),
        301: (FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH, y[1]),
        302: (FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH, y[2]),
    }
    assert [(x, y) for x, y, _text in headers] == [
        (FUNCTION_PLAN_LAYOUT_X_MARKER, y[0]),
        (FUNCTION_PLAN_LAYOUT_X_MARKER, y[6]),
        (FUNCTION_PLAN_LAYOUT_X_MARKER + FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH, y[0]),
    ]
    assert "MT Flur" in headers[0][2]
    assert "MT Bad" in headers[1][2]
    assert "BM Diele" in headers[2][2]


def test_sort_of_a_quickbus_plan_uses_its_column_grid(quickbus_result: dict[str, Any]) -> None:
    data = {**quickbus_result, "io": []}  # only the Quickbus data decides for a QB plan
    coordinator = _coordinator({}, data)
    coordinator.managed_column_plan = lambda _fid: (IO_PLAN_KIND_QUICKBUS, ["1"])  # type: ignore[method-assign]
    # IO 202 of MT Flur (its second row below the header) and the Web-IO element it feeds, both moved away.
    elements = {"1": {"reference": {"type": 1, "ref_id": 202}, "x": 900, "y": 900}, "2": {"x": 0, "y": 0}}
    connections = {"c1": {"input": {"FubElementId": 1}, "output": [{"FubElementId": "2"}]}}

    positions, n_pairs, n_single, sort_line, headers, members = _sort_compute_positions(
        coordinator, {"elements": elements, "connections": connections}, 5, 4000, 10, 10, FUNCTION_PLAN_LAYOUT_Y_STEP
    )

    row = FUNCTION_PLAN_LAYOUT_Y_START + 2 * FUNCTION_PLAN_LAYOUT_Y_STEP
    assert sorted(positions) == [(1, FUNCTION_PLAN_LAYOUT_X_MARKER, row), (2, FUNCTION_PLAN_LAYOUT_X_WEBIO, row)]
    assert (n_pairs, n_single) == (1, 0)
    assert "QB pairs restored" in sort_line
    assert members == ["1"]
    assert len(headers) == 1


def test_sort_of_a_quickbus_plan_past_its_canvas_is_skipped(quickbus_result: dict[str, Any]) -> None:
    coordinator = _coordinator({}, {**quickbus_result, "io": []})
    coordinator.managed_column_plan = lambda _fid: (IO_PLAN_KIND_QUICKBUS, ["1", "2", "3"])  # type: ignore[method-assign]
    elements = {"1": {"reference": {"type": 1, "ref_id": 301}, "x": 0, "y": 0}}

    # BM Diele (device 3) takes column 1 (see test_quickbus_grid_slots_stack_devices_in_a_column); the canvas has one.
    positions, _n_pairs, _n_single, sort_line, _headers, _members = _sort_compute_positions(
        coordinator, {"elements": elements, "connections": {}}, 5, 4000, 10, 1, FUNCTION_PLAN_LAYOUT_Y_STEP
    )

    assert positions == []
    assert "BM Diele" in sort_line
    assert "sort skipped" in sort_line


def test_sort_of_a_column_plan_without_its_data_is_skipped(quickbus_result: dict[str, Any]) -> None:
    data = {**quickbus_result, QUICKBUS_IO_DATA_KEY: []}  # Quickbus import off
    coordinator = _coordinator({}, data)
    coordinator.managed_column_plan = lambda _fid: (IO_PLAN_KIND_QUICKBUS, ["1"])  # type: ignore[method-assign]
    elements = {"1": {"reference": {"type": 2, "id": 7}, "x": 0, "y": 0}}

    positions, _n_pairs, _n_single, sort_line, _headers, _members = _sort_compute_positions(
        coordinator, {"elements": elements, "connections": {}}, 5, 4000, 10, 10, FUNCTION_PLAN_LAYOUT_Y_STEP
    )

    # Not the marker sort: that would scramble the column grid.
    assert positions == []
    assert "sort skipped" in sort_line


@pytest.mark.parametrize(
    ("kind", "members", "missing"),
    [(IO_PLAN_KIND_QUICKBUS, ["1", "9", "3"], "QB9"), (IO_PLAN_KIND_IO, ["BASE", "UD9"], "UD9")],
    ids=["quickbus_device", "io_extension"],
)
def test_sort_of_a_column_plan_with_a_member_without_data_is_skipped(
    quickbus_result: dict[str, Any], kind: IoPlanKind, members: list[str], missing: str
) -> None:
    # The member is still in the plan but has no IOs in the data: its pairs would be parked off
    # the grid and its header dropped (a Quickbus device below it would move into its slots).
    coordinator = _coordinator({}, quickbus_result)
    coordinator.managed_column_plan = lambda _fid: (kind, members)  # type: ignore[method-assign]
    elements = {"1": {"reference": {"type": 1, "ref_id": 301}, "x": 0, "y": 0}}

    positions, _n_pairs, _n_single, sort_line, _headers, _members = _sort_compute_positions(
        coordinator, {"elements": elements, "connections": {}}, 5, 4000, 10, 10, FUNCTION_PLAN_LAYOUT_Y_STEP
    )

    assert positions == []
    assert missing in sort_line
    assert "sort skipped" in sort_line
    assert "delete the plan in Comexio" in sort_line


def test_missing_eta_counts_quickbus_plans_apart_from_io_plans() -> None:
    coordinator = _coordinator(
        {CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - QB [1]": 7}, CONF_FUNCTION_PLAN_PLAN_PREFIX: PREFIX}, {}
    )
    coordinator.api = SimpleNamespace(fub_data={"7": {"Name": "HA - QB [1]"}})
    coordinator._distrusted_fub_ids = set()
    coordinator.function_plan_plans = {7: {"elements": {str(i): {} for i in range(10)}}}
    qb_items = [{"name": f"HA QB1 MT Flur G{i}", "quickbus_device": "1", "identifier": f"G{i}"} for i in range(3)]
    # An IO extension named like the device id belongs to its own (new, empty) IO plan.
    io_item = {"name": "HA IO 1 I1", "ext_name": "1", "identifier": "I1"}

    eta = coordinator._function_plan_missing_eta_sec([*qb_items, io_item])

    expected = (
        2 * SYNC_DURATION_FUNCTION_PLAN_FINALIZE
        + (10 + 2 * 3) * SYNC_DURATION_FUNCTION_PLAN_ELEMENT
        + 2 * SYNC_DURATION_FUNCTION_PLAN_ELEMENT
    )
    assert eta == round(expected)


def test_options_offer_online_quickbus_devices_by_id_and_saved_ones(quickbus_result: dict[str, Any]) -> None:
    data = copy.deepcopy(quickbus_result)
    for io in data[QUICKBUS_IO_DATA_KEY]:
        if io["quickbus"]["name"] == "MT Bad":
            io["offline"] = True
        elif io["quickbus"]["name"] == "BM Diele":
            io["quickbus"] = {**io["quickbus"], "name": "BM, Diele"}  # any name: the id is the value
    data[QUICKBUS_IO_DATA_KEY].append({**_qb_io(data, "302"), "id": "999", "quickbus": {"device_id": 10, "name": ""}})

    options = _quickbus_device_options(data, {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: [9, "1", "9"]})

    # Numeric id order (10 after 3), the gone id 9 last and once.
    assert options == [
        {"value": "1", "label": "QB1 MT Flur"},
        {"value": "3", "label": "QB3 BM, Diele"},
        {"value": "10", "label": "QB10"},
        {"value": "9", "label": "QB9"},
    ]
    assert _quickbus_device_options({}, {}) == []


def test_options_keep_an_offline_quickbus_device_selected_while_never_saved(quickbus_result: dict[str, Any]) -> None:
    data = copy.deepcopy(quickbus_result)
    for io in data[QUICKBUS_IO_DATA_KEY]:
        if io["quickbus"]["name"] == "MT Bad":
            io["offline"] = True

    options = _quickbus_device_options(data, {})

    # Every device is selected by default: the offline one stays offered (after the live ones),
    # so saving the form does not drop it.
    assert options == [
        {"value": "1", "label": "QB1 MT Flur"},
        {"value": "3", "label": "QB3 BM Diele"},
        {"value": "2", "label": "QB2 MT Bad"},
    ]
    assert _shown_quickbus_pick(options, {}, data) == ["1", "3", "2"]
    assert _shown_quickbus_pick(options, {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: [2]}, data) == ["2"]


_SHOWN = ["1", "3", "2"]  # what the form showed: every device, live ones first


@pytest.mark.parametrize(
    ("conf", "submitted", "saved"),
    [
        # The never-saved default, sent back as shown (any order): stays implicit, so a device
        # added in Comexio later is selected too.
        ({}, ["3", "1", "2"], None),
        ({}, [3, 1, 2], None),  # ids sent as numbers
        ({}, ["1", "2"], ["1", "2"]),  # a deselected device: the pick is saved
        ({}, ["1", "2", "3", "9"], ["1", "2", "3", "9"]),  # more than shown: saved
        ({}, [], []),  # all deselected
        # An already saved pick stays saved, even when it equals every current device.
        ({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["1"]}, ["1", "2", "3"], ["1", "2", "3"]),
        ({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}, ["1", "2", "3"], ["1", "2", "3"]),  # a saved "none" too
    ],
    ids=[
        "unchanged_default",
        "numeric_ids",
        "deselected",
        "more_than_shown",
        "none_selected",
        "saved_before",
        "saved_none_before",
    ],
)
def test_an_unchanged_quickbus_default_is_not_saved(
    conf: dict[str, Any], submitted: list[Any], saved: list[str] | None
) -> None:
    user_input = {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: submitted}

    _drop_unchanged_quickbus_default(user_input, conf, _SHOWN)

    if saved is None:
        assert CONF_FUNCTION_PLAN_QUICKBUS_DEVICES not in user_input
    else:
        assert user_input[CONF_FUNCTION_PLAN_QUICKBUS_DEVICES] == saved


def test_the_shown_default_counts_even_after_a_poll_added_a_device(quickbus_result: dict[str, Any]) -> None:
    # The form showed devices 1 and 2; a poll found device 3 before the submit. The untouched
    # default still stays implicit, so device 3 is selected too instead of a frozen ["1", "2"].
    user_input = {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["1", "2"]}

    _drop_unchanged_quickbus_default(user_input, {}, ["1", "2"])

    assert not user_input
    assert selected_quickbus_devices(user_input, quickbus_result) == {"1", "2", "3"}


@pytest.mark.parametrize(
    ("submitted", "saved"),
    [(["3", "1", "2"], None), (["1", "2"], ["1", "2"])],
    ids=["unchanged_default", "deselected"],
)
def test_the_options_submit_keeps_an_unchanged_quickbus_default_implicit(
    submitted: list[str], saved: list[str] | None
) -> None:
    # Through the real submit, not the helper alone: saving the shown default as a list would
    # freeze it, and a Quickbus device added in Comexio later would get no value in HA.
    flow = ComexioOptionsFlow(SimpleNamespace(entry_id="e1", data={}, options={"scan_interval": 15}))
    flow.hass = SimpleNamespace(config=SimpleNamespace(external_url=None))  # type: ignore[assignment]
    flow.async_create_entry = MagicMock(side_effect=lambda **kw: kw)  # type: ignore[method-assign]
    flow._quickbus_shown = _SHOWN
    submit = flow._async_submit_init({CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: submitted}, {}, {}, set())

    data = asyncio.run(submit)["data"]

    assert data["scan_interval"] == 15
    # Absent, not None: selected_quickbus_devices would iterate a stored None.
    if saved is None:
        assert CONF_FUNCTION_PLAN_QUICKBUS_DEVICES not in data
    else:
        assert data[CONF_FUNCTION_PLAN_QUICKBUS_DEVICES] == saved


def test_an_empty_pick_is_saved_even_when_nothing_was_shown() -> None:
    user_input = {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}

    _drop_unchanged_quickbus_default(user_input, {}, [])

    assert user_input == {CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: []}


def test_no_quickbus_field_leaves_the_input_alone() -> None:
    user_input = {"scan_interval": 10}

    _drop_unchanged_quickbus_default(user_input, {}, _SHOWN)

    assert user_input == {"scan_interval": 10}


# --- stacking Quickbus devices within a plan's columns ---


def _rows(sizes: dict[str, int]):
    return lambda member: sizes[member]


@pytest.mark.parametrize(
    ("sizes", "rows_per_col", "origins", "columns"),
    [
        ({}, 10, {}, 0),
        ({"a": 4, "b": 4}, 10, {"a": (0, 0), "b": (0, 5)}, 1),  # two fit one column, a blank row apart
        ({"a": 4, "b": 4, "c": 3}, 10, {"a": (0, 0), "b": (0, 5), "c": (1, 0)}, 2),  # c does not fit below
        ({"a": 4, "b": 5, "c": 2}, 10, {"a": (0, 0), "b": (0, 5), "c": (1, 0)}, 2),  # exactly full
        ({"a": 5, "b": 5}, 10, {"a": (0, 0), "b": (1, 0)}, 2),  # the blank row does not fit any more
        ({"a": 25, "b": 3}, 10, {"a": (0, 0), "b": (3, 0)}, 4),  # taller than a column: wraps on
        ({"a": 2, "b": 25}, 10, {"a": (0, 0), "b": (1, 0)}, 4),  # a tall device starts a fresh column
        ({"a": 4, "b": 0, "c": 4}, 10, {"a": (0, 0), "b": (0, 4), "c": (0, 5)}, 1),  # b gone: takes no row
        ({"a": 0, "b": 4}, 10, {"a": (0, 0), "b": (0, 0)}, 1),  # gone first
        ({"a": 4, "b": 0}, 10, {"a": (0, 0), "b": (0, 4)}, 1),  # gone last: no extra column
        ({"a": 10, "b": 0}, 10, {"a": (0, 0), "b": (1, 0)}, 1),  # gone after a full column
    ],
    ids=[
        "empty",
        "share",
        "overflow",
        "exact",
        "no-room-for-the-gap",
        "wrap",
        "tall-after-small",
        "gone",
        "gone-first",
        "gone-last",
        "gone-after-full-column",
    ],
)
def test_quickbus_devices_stack_within_columns(
    sizes: dict[str, int], rows_per_col: int, origins: dict, columns: int
) -> None:
    members = list(sizes)

    assert io_plan_member_origins(IO_PLAN_KIND_QUICKBUS, members, _rows(sizes), rows_per_col) == origins
    assert io_plan_columns(IO_PLAN_KIND_QUICKBUS, members, _rows(sizes), rows_per_col) == columns


def test_io_extensions_keep_one_column_each() -> None:
    sizes = {"BASE": 2, "UD1": 2}

    assert io_plan_member_origins(IO_PLAN_KIND_IO, list(sizes), _rows(sizes), 10) == {
        "BASE": (0, 0),
        "UD1": (1, 0),
    }
    assert io_plan_columns(IO_PLAN_KIND_IO, list(sizes), _rows(sizes), 10) == 2


@pytest.mark.parametrize(
    ("sizes", "max_columns", "overflow"),
    [
        ({"a": 4, "b": 4, "c": 3}, 2, []),  # c opens column 1, the last one on the canvas
        ({"a": 4, "b": 4, "c": 3}, 1, ["c"]),
        ({"a": 8, "b": 4, "c": 1}, 1, ["b", "c"]),  # a grew: b moves right, c behind it with it
        ({"a": 12, "b": 1}, 1, ["b"]),  # a alone wraps past the edge: no layout fits it, b behind it
        ({"a": 0, "b": 4}, 1, []),  # a gone: takes no column
    ],
    ids=["fits", "last-off", "grown", "wrap", "gone"],
)
def test_quickbus_devices_past_the_canvas_overflow(sizes: dict[str, int], max_columns: int, overflow: list) -> None:
    assert io_plan_overflow(IO_PLAN_KIND_QUICKBUS, list(sizes), _rows(sizes), 10, max_columns) == overflow


def test_a_first_quickbus_device_past_the_canvas_overflows_only_when_checked() -> None:
    # check_first: a fresh plan would take it (this plan's paper shrank), so failing it has a way out.
    sizes = {"a": 12, "b": 1}

    assert io_plan_overflow(IO_PLAN_KIND_QUICKBUS, list(sizes), _rows(sizes), 10, 1, check_first=True) == ["a", "b"]


@pytest.mark.parametrize(
    ("sizes", "shifted"),
    [
        ({"a": 4, "b": 0, "c": 4, "d": 2}, ["c", "d"]),  # b gone: c and d would move up into its slots
        ({"a": 4, "b": 4, "c": 0}, []),  # the last one gone: nothing behind it
        ({"a": 4, "b": 4}, []),
    ],
    ids=["gone-in-between", "gone-last", "none-gone"],
)
def test_quickbus_devices_behind_one_without_ios_are_shifted(sizes: dict[str, int], shifted: list[str]) -> None:
    assert io_plan_shifted(IO_PLAN_KIND_QUICKBUS, list(sizes), _rows(sizes)) == shifted


def test_io_extensions_are_never_shifted() -> None:
    sizes = {"BASE": 0, "UD1": 2}

    assert io_plan_shifted(IO_PLAN_KIND_IO, list(sizes), _rows(sizes)) == []


def test_io_extensions_never_overflow() -> None:
    # One column per extension whatever the canvas — the wire path has always kept them.
    sizes = {"BASE": 2, "UD1": 2, "UD2": 2}

    assert io_plan_overflow(IO_PLAN_KIND_IO, list(sizes), _rows(sizes), 10, 1) == []


@pytest.mark.parametrize(
    ("x_max", "columns"), [(1230.0, 3), (870.0, 2), (100.0, 1)], ids=["a3-landscape", "a3-portrait", "tiny"]
)
def test_canvas_columns(x_max: float, columns: int) -> None:
    assert canvas_columns(x_max) == columns


@pytest.mark.parametrize(
    ("kind", "member", "label"), [(IO_PLAN_KIND_QUICKBUS, "9", "QB9"), (IO_PLAN_KIND_IO, "UD9", "UD9")]
)
def test_a_pair_of_a_member_gone_from_the_config_names_the_member(
    comexio_api: ComexioAPI, kind: Any, member: str, label: str
) -> None:
    error = asyncio.run(
        comexio_api._function_plan_add_single_io_pair(1, member, "Q1", {}, {}, {}, [], (0, 0, 10), kind=kind)
    )

    assert error == f"{label} Q1: not found in fresh config"
