"""Small coordinator/API helpers around function plans and polled KNX values (coordinator.py, api.py)."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioConnectionError
import aiohttp
import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    IO_PLAN_KIND_IO,
    IO_PLAN_KIND_QUICKBUS,
    PLAN_FAILURE_AFTER_GONE_MEMBER,
    PLAN_FAILURE_NOT_RESOLVED,
    PLAN_FAILURE_OFF_CANVAS,
    IoPlanKind,
    WebioClass,
    function_plan_id,
    source_category,
)
from custom_components.comexio.coordinator import ComexioCoordinator

PREFIX = "HA"


def _coordinator(**attrs: object) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


def test_polled_knx_values_merge_with_webhook_and_cache() -> None:
    coordinator = _coordinator(_webhook_updated_knx_ids={1}, knx_states={1: "pushed", 2: "old", 3: "cached"})
    items = [{"id": 1, "value": "polled"}, {"id": 2, "value": "polled"}, {"id": 3, "value": 0}]

    coordinator._merge_polled_knx_states(items, knx_live_states={1: "polled", 2: "polled"})

    # A webhook value received during the fetch wins; a polled value refreshes the cache;
    # an object missing from the response keeps its cached value instead of the parser's 0.
    assert [k["value"] for k in items] == ["pushed", "polled", "cached"]
    assert coordinator.knx_states == {1: "pushed", 2: "polled", 3: "cached"}


@pytest.mark.parametrize(("raw", "ids"), [(None, set()), ("  ", set()), ("M3, 5-6", {3, 5, 6})])
def test_ignored_ids_for_a_category(raw: str | None, ids: set[int]) -> None:
    conf_key = source_category(WebioClass.MARKER).ignored_conf_key
    options = {} if raw is None else {conf_key: raw}
    coordinator = _coordinator(config_entry=SimpleNamespace(options=options), _knx_bridge_marker_ids=set())

    assert coordinator._ignored_ids_for(WebioClass.MARKER) == ids


@pytest.mark.parametrize(
    ("name", "members"),
    [("HA - IO [A, B ,]", ["A", "B"]), ("HA - IO [A]", ["A"]), ("Other - IO [A]", None), ("HA - Marker 1", None)],
)
def test_io_plan_members_from_the_plan_name(name: str, members: list[str] | None) -> None:
    assert ComexioCoordinator._io_plan_members(name, PREFIX) == members


@pytest.mark.parametrize(
    ("fub_data", "scraped", "expected"),
    [
        ({"1": {"Name": "A"}}, {1}, {"1": {"Name": "A"}}),
        ({}, None, None),  # no plan list read yet: never "every plan deleted"
        # Only a plan HA created (create_fup) before the first poll: not the whole list.
        ({"42": {"Name": "New"}}, None, None),
        ({}, set(), {}),  # a full poll really read an empty $Fubs: no plan left
        ({}, {4}, {}),  # the last plan deleted by HA after the poll
    ],
    ids=["read", "not-read-yet", "only-created-by-ha", "none-left", "last-deleted-by-ha"],
)
def test_live_plan_list(fub_data: dict, scraped: set[int] | None, expected: dict | None) -> None:
    coordinator = _coordinator(api=SimpleNamespace(fub_data=fub_data), scraped_plan_ids=scraped)

    assert coordinator.live_plan_list() == expected


def test_io_plan_membership_skips_distrusted_and_foreign_plans() -> None:
    fub_data = {
        "4": {"Name": "HA - IO [A]"},
        "5": {"Name": "HA - IO [B,C]"},
        "6": {"Name": "HA - IO [D]"},
        "7": {"Name": "Lights"},
        "x": {"Name": "HA - IO [E]"},
    }
    coordinator = _coordinator(api=SimpleNamespace(fub_data=fub_data), _distrusted_fub_ids={6})

    assert coordinator._io_plan_membership(PREFIX) == {4: ["A"], 5: ["B", "C"]}


@pytest.mark.parametrize(("created", "placed"), [(None, None), (9, 9)], ids=["create-failed", "created"])
def test_a_fresh_io_plan_takes_the_extension(created: int | None, placed: int | None) -> None:
    coordinator = _coordinator(
        _io_plan_rows_per_col=lambda _orientation: 10,
        _io_rows_needed=lambda _ext, _kind: 4,
        _create_managed_plan=AsyncMock(return_value=created),
    )

    assert asyncio.run(coordinator._join_or_create_io_plan("A", {}, 2, PREFIX)) == placed


def _cluster_coordinator(
    plan_name: str,
    cluster_size: int,
    sizes: dict[str, int],
    *,
    renamed: bool = True,
    created: int = 48,
    canvas_columns: int = 3,
) -> ComexioCoordinator:
    """A coordinator with one managed plan 47 (plan_name) for resolve_io_clusters."""
    return _coordinator(
        server_id="cx1",
        data={},
        api=SimpleNamespace(fub_data={"47": {"Name": plan_name}, "3": {"Name": "Lights"}}),
        config_entry=SimpleNamespace(options={"logikplan_plan_map": {plan_name: 47, "Lights": 3}}),
        _distrusted_fub_ids=set(),
        _function_plan_prefix=lambda: PREFIX,
        _function_plan_cluster_size=lambda: cluster_size,
        _plan_rows_per_col=lambda _fid, _orientation=None: 10,
        _plan_canvas_columns=lambda _fid, _orientation=None: canvas_columns,
        _io_plan_rows_per_col=lambda _orientation: 10,
        _io_rows_needed=lambda member, _kind: sizes[member],
        _rename_io_plan=AsyncMock(return_value=renamed),
        _create_managed_plan=AsyncMock(return_value=created),
        _persist_plan_map=AsyncMock(),
    )


def test_joins_in_one_run_keep_only_the_final_plan_name() -> None:
    coordinator = _cluster_coordinator("HA - IO [A]", 150, dict.fromkeys("ABC", 4))

    asyncio.run(coordinator.resolve_io_clusters(["A", "B", "C"]))

    # B and C join plan 47 one after the other: neither the old name nor the intermediate
    # "[A,B]" may stay in plan_map, or the watchdog warns about plans that do not exist.
    coordinator._persist_plan_map.assert_awaited_once_with({"HA - IO [A,B,C]": 47}, removals={"HA - IO [A]"})


def test_a_rename_before_a_failing_placement_still_reaches_plan_map() -> None:
    # B joins plan 47 (renamed in Comexio to [A,B]), then creating a plan for C fails hard: plan_map
    # must know plan 47 by its new name, or the watchdog reports it lost until the next sync.
    coordinator = _cluster_coordinator("HA - IO [A]", 100, dict.fromkeys("ABC", 4))
    coordinator._create_managed_plan = AsyncMock(side_effect=RuntimeError("connection lost"))
    resolve = coordinator.resolve_io_clusters(["A", "B", "C"])

    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(resolve)

    coordinator._persist_plan_map.assert_awaited_once_with({"HA - IO [A,B]": 47}, removals={"HA - IO [A]"})


def test_quickbus_devices_join_a_plan_while_its_column_holds_them() -> None:
    # One column of 10 rows: device 1 (4 rows) is placed; device 2 (4) stacks below it after a
    # blank row; device 3 (3) no longer fits and opens a fresh plan.
    coordinator = _cluster_coordinator("HA - QB [1]", 50, {"1": 4, "2": 4, "3": 3})

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["1", "2", "3"], IO_PLAN_KIND_QUICKBUS))

    assert placed == {"1": (47, (0, 0)), "2": (47, (0, 5)), "3": (48, (0, 0))}
    assert (created, failed) == ({48}, {})
    coordinator._rename_io_plan.assert_awaited_once_with(47, "HA - QB [1,2]")
    coordinator._persist_plan_map.assert_awaited_once_with(
        {"HA - QB [1,2]": 47, "HA - QB [3]": 48}, removals={"HA - QB [1]"}
    )


@pytest.mark.parametrize(
    ("canvas_columns", "placed", "failed"),
    [
        (2, {"1": (47, (0, 0)), "2": (47, (1, 0))}, {}),  # past the configured column, still on the canvas
        (1, {"1": (47, (0, 0))}, {"2": PLAN_FAILURE_OFF_CANVAS}),  # past the canvas edge: not wired
    ],
    ids=["on-the-canvas", "off-the-canvas"],
)
def test_a_quickbus_device_pushed_past_the_canvas_by_a_grown_one_is_not_wired(
    canvas_columns: int, placed: dict, failed: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    # Device 1 grew from 4 to 8 rows after device 2 joined below it: device 2 moves to column 1.
    coordinator = _cluster_coordinator("HA - QB [1,2]", 50, {"1": 8, "2": 4}, canvas_columns=canvas_columns)

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(coordinator.resolve_io_clusters(["1", "2"], IO_PLAN_KIND_QUICKBUS))

    assert result == (placed, set(), failed)
    coordinator._rename_io_plan.assert_not_awaited()
    # The log names the device and the way out; the sync summary only carries the short reason.
    assert ("'QB2' no longer fits" in caplog.text) is bool(failed)
    assert ("delete the plan in Comexio" in caplog.text) is bool(failed)


def test_a_device_alone_too_tall_for_the_canvas_keeps_its_fresh_plan() -> None:
    # No layout would fit it: failing it would leave the fresh plan empty on every sync.
    coordinator = _cluster_coordinator("HA - IO [A]", 150, {"1": 12}, canvas_columns=1)

    result = asyncio.run(coordinator.resolve_io_clusters(["1"], IO_PLAN_KIND_QUICKBUS))

    assert result == ({"1": (48, (0, 0))}, {48}, {})


def test_a_first_device_off_a_shrunk_canvas_is_not_wired() -> None:
    # The plan's paper shrank to one column: a fresh plan (three columns) would take device 1 again.
    coordinator = _cluster_coordinator("HA - QB [1]", 150, {"1": 12})
    coordinator._plan_canvas_columns = lambda _fid, orientation=None: 3 if orientation else 1

    result = asyncio.run(coordinator.resolve_io_clusters(["1"], IO_PLAN_KIND_QUICKBUS))

    assert result == ({}, set(), {"1": PLAN_FAILURE_OFF_CANVAS})


def test_a_quickbus_device_behind_one_without_ios_is_not_wired(caplog: pytest.LogCaptureFixture) -> None:
    # Device 2 lost every IO, its pairs still in the plan: device 3 would move up into its slots.
    coordinator = _cluster_coordinator("HA - QB [1,2,3]", 50, {"1": 4, "2": 0, "3": 4})

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(coordinator.resolve_io_clusters(["1", "3"], IO_PLAN_KIND_QUICKBUS))

    assert result == ({"1": (47, (0, 0))}, set(), {"3": PLAN_FAILURE_AFTER_GONE_MEMBER})
    assert "'QB3' sits in QB plan fub=47 behind a Quickbus device with no IOs loaded" in caplog.text


@pytest.mark.parametrize(
    ("live_name", "kept"),
    [("HA - QB [1,2]", {"HA - QB [1,2]": 48}), ("Rolladen", {})],
    ids=["managed-name", "foreign-name"],
)
def test_a_stale_entry_of_another_kind_follows_its_renamed_plan(live_name: str, kept: dict[str, int]) -> None:
    # Plan 48 was renamed by a run that stopped before plan_map followed; an IO run keeps it under its
    # live name as long as that is a managed one.
    coordinator = _cluster_coordinator("HA - IO [A]", 150, {"A": 4, "B": 4})
    coordinator.api.fub_data["48"] = {"Name": live_name}
    coordinator.api.function_plan_name = lambda fid: coordinator.api.fub_data[str(fid)]["Name"]
    coordinator.config_entry.options["logikplan_plan_map"]["HA - QB [1]"] = 48

    asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    coordinator._persist_plan_map.assert_awaited_once_with(
        {"HA - IO [A,B]": 47, **kept}, removals={"HA - IO [A]", "HA - QB [1]"}
    )


def test_a_renamed_plan_whose_name_is_taken_drops_out_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    # Plan 48 now carries the name plan_map already gives plan 49: two plans of one name, 48 is not re-keyed.
    coordinator = _cluster_coordinator("HA - IO [A]", 150, {"A": 4, "B": 4})
    coordinator.api.fub_data |= {"48": {"Name": "HA - QB [1,2]"}, "49": {"Name": "HA - QB [1,2]"}}
    coordinator.api.function_plan_name = lambda fid: coordinator.api.fub_data[str(fid)]["Name"]
    coordinator.config_entry.options["logikplan_plan_map"] |= {"HA - QB [1]": 48, "HA - QB [1,2]": 49}

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    coordinator._persist_plan_map.assert_awaited_once_with(
        {"HA - IO [A,B]": 47}, removals={"HA - IO [A]", "HA - QB [1]"}
    )
    assert "plan_map drops plan fub=48: its name 'HA - QB [1,2]' already maps to fub=49" in caplog.text


def test_a_renamed_plan_also_listed_under_its_live_name_logs_no_clash(caplog: pytest.LogCaptureFixture) -> None:
    # plan_map lists plan 48 under its old and its live name: one plan, not two of one name.
    coordinator = _cluster_coordinator("HA - IO [A]", 150, {"A": 4, "B": 4})
    coordinator.api.fub_data["48"] = {"Name": "HA - QB [1,2]"}
    coordinator.api.function_plan_name = lambda fid: coordinator.api.fub_data[str(fid)]["Name"]
    coordinator.config_entry.options["logikplan_plan_map"] |= {"HA - QB [1]": 48, "HA - QB [1,2]": 48}

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    assert "plan_map drops plan" not in caplog.text


def test_a_quickbus_device_does_not_join_a_plan_behind_one_without_ios(caplog: pytest.LogCaptureFixture) -> None:
    # Device 2 lost every IO: device 3 joining plan 47 would get origins inside device 2's old slots.
    coordinator = _cluster_coordinator("HA - QB [1,2]", 150, {"1": 4, "2": 0, "3": 4})

    with caplog.at_level(logging.INFO):
        slots, _created, failed = asyncio.run(coordinator.resolve_io_clusters(["3"], IO_PLAN_KIND_QUICKBUS))

    assert slots == {"3": (48, (0, 0))}
    assert failed == {}
    coordinator._rename_io_plan.assert_not_awaited()
    assert "QB plan fub=47 holds a Quickbus device with no IOs loaded, so 'QB3' does not join it" in caplog.text


@pytest.mark.parametrize(
    ("plan_name", "sizes", "kind", "new_name"),
    [
        ("HA - QB [1,2]", {"1": 4, "2": 4, "3": 0}, IO_PLAN_KIND_QUICKBUS, "HA - QB [1,2,3]"),
        ("HA - IO [A]", {"A": 0, "B": 4}, IO_PLAN_KIND_IO, "HA - IO [A,B]"),
    ],
    ids=["joining-device-without-ios", "io-extension-behind-one-without-ios"],
)
def test_a_member_still_joins_when_nothing_sits_behind_a_gone_one(
    plan_name: str, sizes: dict[str, int], kind: IoPlanKind, new_name: str
) -> None:
    # Only members placed after a gone one are refused: the gone one itself, and IO extensions (own column), join.
    coordinator = _cluster_coordinator(plan_name, 150, sizes)
    new_member = next(iter(sizes.keys() - set(plan_name[plan_name.index("[") + 1 : -1].split(","))))

    asyncio.run(coordinator.resolve_io_clusters([new_member], kind))

    coordinator._rename_io_plan.assert_awaited_once_with(47, new_name)


def test_a_marker_run_keeps_a_stale_entry_of_a_renamed_managed_plan() -> None:
    # A marker run prunes every stale entry too: QB plan 47, renamed without plan_map following, stays.
    coordinator = _coordinator(
        server_id="cx1",
        api=SimpleNamespace(
            fub_data={"47": {"Name": "HA - QB [1,2]"}}, function_plan_name=lambda _fid: "HA - QB [1,2]"
        ),
        config_entry=SimpleNamespace(options={"logikplan_plan_map": {"HA - QB [1]": 47}}),
        _function_plan_prefix=lambda: PREFIX,
        _function_plan_cluster_size=lambda: 50,
        _resolve_single_cluster_plan=AsyncMock(return_value=(60, False)),
        _persist_plan_map=AsyncMock(),
    )

    asyncio.run(coordinator.resolve_marker_clusters([5], "Marker"))

    cluster = ComexioCoordinator._cluster_plan_name(5, PREFIX, 50, "Marker")
    coordinator._persist_plan_map.assert_awaited_once_with({cluster: 60, "HA - QB [1,2]": 47}, removals={"HA - QB [1]"})


def test_the_canvas_check_of_a_fresh_plan_uses_its_orientation() -> None:
    coordinator = _cluster_coordinator("HA - IO [A]", 50, {"1": 8, "2": 4})
    coordinator._plan_canvas_columns = lambda _fid, orientation=None: 1 if orientation == "portrait" else 2
    failed: dict[str, str] = {}

    slots = coordinator._io_member_slots(
        {"1": 48, "2": 48}, {48: ["1", "2"]}, IO_PLAN_KIND_QUICKBUS, {48: "portrait"}, failed
    )

    assert slots == {"1": (48, (0, 0))}
    assert failed == {"2": PLAN_FAILURE_OFF_CANVAS}


@pytest.mark.parametrize(
    ("fub_data", "orientation", "expected_call"),
    [
        ({"7": {}}, None, ((7,), {})),  # an existing plan: its own paper
        ({"7": {}}, "portrait", ((-1,), {"paper_name": "A3", "orientation": "portrait"})),  # created in this run
        ({}, None, ((-1,), {"paper_name": "A3", "orientation": "landscape"})),  # not in $Fubs yet
    ],
    ids=["existing", "fresh", "unknown"],
)
def test_plan_canvas_columns(fub_data: dict, orientation: str | None, expected_call: tuple) -> None:
    api = SimpleNamespace(fub_data=fub_data, get_fub_canvas_bounds=MagicMock(return_value=(870.0, 600.0)))
    coordinator = _coordinator(api=api)

    assert coordinator._plan_canvas_columns(7, orientation) == 2  # 870 wide: two 450 columns
    assert api.get_fub_canvas_bounds.call_args == expected_call


def test_a_quickbus_device_joins_no_plan_whose_canvas_lacks_the_column() -> None:
    # Capacity 150 offers 3 columns, the (portrait) canvas only 1: device 2 would need column 1.
    coordinator = _cluster_coordinator("HA - QB [1]", 150, {"1": 8, "2": 4}, canvas_columns=1)

    result = asyncio.run(coordinator.resolve_io_clusters(["1", "2"], IO_PLAN_KIND_QUICKBUS))

    assert result == ({"1": (47, (0, 0)), "2": (48, (0, 0))}, {48}, {})
    coordinator._rename_io_plan.assert_not_awaited()


def _plan_list(name: str) -> dict[str, Any]:
    """A config page whose plan list holds plan 47 under name."""
    return {"Fubs": {"47": {"Name": name}, "3": {"Name": "Lights"}}}


def _reread_plan_list(coordinator: ComexioCoordinator, fetched: dict[str, Any] | Exception) -> None:
    """Let a config fetch return fetched (raise it, if an error)."""
    api = coordinator.api
    api.get_raw_config = AsyncMock(side_effect=[fetched])
    api.parse_config = lambda conf: api.fub_data.update(conf["Fubs"])
    api.function_plan_name = lambda fub_id: api.fub_data[str(fub_id)]["Name"]


def test_a_failed_join_rename_places_the_member_in_a_fresh_plan() -> None:
    coordinator = _cluster_coordinator("HA - IO [A]", 150, dict.fromkeys("AB", 4), renamed=False, created=9)
    _reread_plan_list(coordinator, _plan_list("HA - IO [A]"))

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    # Plan 47 keeps its name and member: plan_map must not claim it holds B.
    assert placed == {"A": (47, (0, 0)), "B": (9, (0, 0))}
    assert (created, failed) == ({9}, {})
    coordinator._persist_plan_map.assert_awaited_once_with({"HA - IO [A]": 47, "HA - IO [B]": 9}, removals=set())


def test_a_join_rename_whose_answer_got_lost_still_joins_the_plan() -> None:
    # Comexio applied the rename, but its answer got lost: a fresh plan would hold B a second time.
    coordinator = _cluster_coordinator("HA - IO [A]", 150, dict.fromkeys("AB", 4), renamed=False, created=9)
    _reread_plan_list(coordinator, _plan_list("HA - IO [A,B]"))

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    assert placed == {"A": (47, (0, 0)), "B": (47, (1, 0))}
    assert (created, failed) == (set(), {})
    coordinator._create_managed_plan.assert_not_awaited()
    coordinator._persist_plan_map.assert_awaited_once_with({"HA - IO [A,B]": 47}, removals={"HA - IO [A]"})


@pytest.mark.parametrize(
    "fetched",
    [
        {},
        aiohttp.ClientError("connection lost"),
        TimeoutError(),
        ComexioConnectionError("no route"),
        {"Fubs": {"x": None}},
    ],
    ids=["fetch-failed", "transport-error", "timeout", "connection-error", "unreadable-entry"],
)
def test_a_failed_join_rename_creates_no_plan_while_the_plan_list_is_unreadable(fetched: object) -> None:
    coordinator = _cluster_coordinator("HA - IO [A]", 150, dict.fromkeys("AB", 4), renamed=False, created=9)
    _reread_plan_list(coordinator, fetched)  # type: ignore[arg-type]

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    # Whether plan 47 holds B is unknown: B waits for the next sync instead of landing in a second plan.
    assert placed == {"A": (47, (0, 0))}
    assert (created, failed) == (set(), {"B": PLAN_FAILURE_NOT_RESOLVED})
    coordinator._create_managed_plan.assert_not_awaited()


@pytest.mark.parametrize(
    ("plan_map", "superseded"),
    [
        ({"HA - QB [1]": 5, "HA - QB [1,2]": 5, "Lights": 3, "HA - IO [A]": 7, "bogus": "x"}, {"HA - QB [1]"}),
        ("not a map", set()),
    ],
    ids=["other-plans-kept", "no-map"],
)
def test_superseded_plan_map_names(plan_map: object, superseded: set[str]) -> None:
    coordinator = _coordinator(config_entry=SimpleNamespace(options={"logikplan_plan_map": plan_map}))

    assert coordinator._superseded_plan_map_names({5: "HA - QB [1,2]"}) == superseded


def _wired_plan(*targets: tuple[str, dict[str, Any]], source: int = 12) -> dict[str, Any]:
    """A plan whose marker element "1" (ref source) feeds each target element."""
    elements = {"1": {"reference": {"type": 2, "ref_id": source}}, **dict(targets)}
    connections = {
        str(i): {"input": {"FubElementId": "1"}, "output": [{"FubElementId": elem_id}]}
        for i, (elem_id, _) in enumerate(targets)
    }
    return {"elements": elements, "connections": connections}


WEBIO_TARGET = ("2", {"reference": {"type": 10, "ref_id": 40}})
LOGIC_TARGET = ("3", {"reference": {"type": 5, "ref_id": 1}})


def test_source_links_name_every_plan_wiring_the_source_to_a_web_io() -> None:
    # (bj): only the first plan counted, and logic-only wiring flagged a source the cleanup keeps.
    coordinator = _coordinator()
    plans = {
        3: _wired_plan(LOGIC_TARGET),
        5: _wired_plan(WEBIO_TARGET),
        7: _wired_plan(source=13),
        8: _wired_plan(LOGIC_TARGET, WEBIO_TARGET),
    }

    assert coordinator._source_function_plan_links(12, plans) == [5, 8]
    assert coordinator._source_function_plan_links(12, {3: plans[3]}) == []


def test_a_stray_duplicate_element_wired_to_a_web_io_counts() -> None:
    plan = _wired_plan()
    plan["elements"]["9"] = {"reference": {"type": "2", "ref_id": "12"}}
    plan["elements"]["10"] = WEBIO_TARGET[1]
    plan["connections"]["x"] = {"input": {"FubElementId": 9}, "output": {"0": {"FubElementId": "10"}}}

    assert ComexioCoordinator._source_webio_wired_in_plan(12, plan)


def test_knx_bridge_pairs_count_only_marker_to_knx() -> None:
    plan = {
        "elements": {
            "1": {"reference": {"type": 2, "ref_id": 40}},
            "2": {"reference": {"type": 11, "ref_id": 7}},
            "3": {"reference": {"type": 11, "ref_id": 8}},
            "4": {"reference": {"type": 10, "ref_id": 99}},
        },
        "connections": {
            "a": {"input": {"FubElementId": 1}, "output": {"x": {"FubElementId": 2}, "y": {"FubElementId": 4}}},
            "b": {"input": {"FubElementId": 3}, "output": [{"FubElementId": 1}]},
        },
    }

    assert ComexioCoordinator._plan_knx_bridge_pairs(plan) == {("40", "7")}


def _bridge_plan(*pairs: tuple[int, int]) -> dict:
    """A plan wiring each (marker_id, k_id) pair Marker -> KNX."""
    elements, connections = {}, {}
    for n, (marker_id, k_id) in enumerate(pairs):
        elements[f"m{n}"] = {"reference": {"type": 2, "ref_id": marker_id}}
        elements[f"k{n}"] = {"reference": {"type": 11, "ref_id": k_id}}
        connections[str(n)] = {"input": {"FubElementId": f"m{n}"}, "output": [{"FubElementId": f"k{n}"}]}
    return {"elements": elements, "connections": connections}


def _bridge_coordinator(relevant: set[int], loaded: bool = True) -> ComexioCoordinator:
    coordinator = _coordinator(
        function_plan_plans={7: _bridge_plan((40, 1)), 8: _bridge_plan((41, 2), (42, 1)), 9: _bridge_plan((99, 3))}
    )
    coordinator._function_plan_check_fub_ids = lambda: relevant
    coordinator._relevant_plans_loaded = lambda fub_ids: loaded
    return coordinator


def test_knx_bridge_map_reads_only_the_relevant_plans() -> None:
    # Plan 9 is not relevant; K1 is bridged by plans 7 and 8 — the later plan wins.
    assert _bridge_coordinator({7, 8})._knx_bridge_marker_by_k_id() == {"1": "42", "2": "41"}


@pytest.mark.parametrize(("relevant", "loaded"), [(set(), True), ({7}, False)], ids=["no plan", "not loaded"])
def test_knx_bridge_map_is_unknown_without_loaded_relevant_plans(relevant: set[int], loaded: bool) -> None:
    assert _bridge_coordinator(relevant, loaded)._knx_bridge_marker_by_k_id() is None


@pytest.mark.parametrize("key", ["M5", "K3"])
def test_range_cluster_plan_name_for_a_source_key(key: str) -> None:
    cat = next(
        c for c in (source_category(WebioClass.MARKER), source_category(WebioClass.KNX)) if key[0] == c.audit_key_prefix
    )
    expected = ComexioCoordinator._cluster_plan_name(int(key[1:]), PREFIX, 50, cat.label)

    assert _coordinator()._range_cluster_plan_name_for_key(key, PREFIX, 50) == expected


def test_range_cluster_plan_name_is_none_for_an_io_key() -> None:
    assert _coordinator()._range_cluster_plan_name_for_key("IO_A_I1", PREFIX, 50) is None


def test_paired_flanke_ids_are_found_in_either_direction() -> None:
    plan = {
        "elements": {
            "10": {"reference": {"type": 5, "ref_id": 77}},
            "11": {"reference": {"type": 5, "ref_id": 77}},
            "12": {"reference": {"type": 5, "ref_id": 78}},
            "13": {"reference": {"type": 5, "ref_id": 77}},
        },
        "connections": {
            "a": {"input": {"FubElementId": "1"}, "output": [{"FubElementId": 10}]},
            "b": {"input": {"FubElementId": 11}, "output": {"x": {"FubElementId": 1}}},
            "c": {"input": {"FubElementId": 1}, "output": [{"FubElementId": 12}]},
            "d": {"input": {"FubElementId": 2}, "output": [{"FubElementId": 13}]},
        },
    }

    assert sorted(ComexioAPI._function_plan_paired_flanke_ids(plan, [1], 77)) == [10, 11]


@pytest.mark.parametrize(
    ("existing", "added", "result"),
    [(4, 9, 4), (0, 9, 0), (None, 9, 9), (None, None, "M1: add_element (marker) failed")],
    ids=["reuse", "reuse-id-0", "create", "create-failed"],
)
def test_resolve_or_create_element(
    comexio_api: ComexioAPI, existing: int | None, added: int | None, result: int | str
) -> None:
    add_element = AsyncMock(return_value=added)
    comexio_api.function_plan_add_element = add_element  # type: ignore[method-assign]

    resolved = asyncio.run(comexio_api._resolve_or_create_element(19, existing, 5, 2, 0.0, 0.0, "M1", "marker"))

    assert resolved == result
    assert add_element.await_count == (0 if existing is not None else 1)


@pytest.mark.parametrize(
    ("fub_data", "name"),
    [
        ({"8": {"Name": "Pumps"}}, "Pumps"),
        ({8: {"Name": "Pumps"}}, "Pumps"),
        ({"x": {"Name": "Odd"}, "²": {"Name": "Odd"}, "8": {"Name": "Pumps"}}, "Pumps"),
        ({"8": "no dict"}, "8"),
        ({"8": {"Id": 9, "Name": "Pumps"}}, "8"),
        ({"8": {}}, "8"),
        ({"8": {"Name": None}}, "8"),
        ({"08": {"Name": "Pumps"}}, "8"),
        ({"18": {"Name": "Other"}, "80": {"Name": "Other"}}, "8"),
        ({}, "8"),
    ],
    ids=[
        "named",
        "int-key",
        "key-no-number",
        "metadata-no-dict",
        "id-differs-from-key",
        "unnamed",
        "name-none",
        "not-canonical",
        "other-plans",
        "unlisted",
    ],
)
def test_a_malformed_plan_list_falls_back_to_the_plan_id(comexio_api: ComexioAPI, fub_data: dict, name: str) -> None:
    """The backup cycle names left-out plans with it: a malformed $Fubs entry must not raise."""
    comexio_api._fub_data = fub_data

    assert comexio_api.function_plan_name(8) == name


@pytest.mark.parametrize(
    ("fid", "fub_id"),
    [
        ("8", 8),
        (8, 8),
        ("0", 0),
        ("08", None),
        ("", None),
        (" 8", None),
        ("+8", None),
        ("-1", None),
        ("²", None),
        ("８", None),
        ("x", None),
    ],
)
def test_only_a_canonical_plan_key_is_a_plan_id(fid: object, fub_id: int | None) -> None:
    """Every lookup goes by fub_data.get(str(fub_id)): a key in any other spelling is no plan id."""
    assert function_plan_id(fid) == fub_id


@pytest.mark.parametrize(("live_name", "confirmed"), [("HA - IO [A,B]", True), ("HA - IO [A]", False)])
def test_a_rename_confirmation_reads_the_plan_list_parse_config_takes_in(
    comexio_api: ComexioAPI, live_name: str, confirmed: bool
) -> None:
    # The real parse_config must refresh the cache function_plan_name reads, or nothing is confirmed.
    comexio_api.parse_config({"Fubs": {"47": {"Id": 47, "Name": "HA - IO [A]", "Active": 1}}})
    comexio_api.get_raw_config = AsyncMock(  # type: ignore[method-assign]
        return_value={"Fubs": {"47": {"Id": 47, "Name": live_name, "Active": 1}}}
    )
    coordinator = _coordinator(server_id="cx1", api=comexio_api, _rename_io_plan=AsyncMock(return_value=False))

    assert asyncio.run(coordinator._rename_io_plan_confirmed(47, "HA - IO [A,B]")) is confirmed
