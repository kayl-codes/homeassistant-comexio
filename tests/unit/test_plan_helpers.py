"""Small coordinator/API helpers around function plans and polled KNX values (coordinator.py, api.py)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import IO_PLAN_KIND_QUICKBUS, WebioClass, function_plan_id, source_category
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
    plan_name: str, cluster_size: int, sizes: dict[str, int], *, renamed: bool = True, created: int = 48
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


def test_quickbus_devices_join_a_plan_while_its_column_holds_them() -> None:
    # One column of 10 rows: device 1 (4 rows) is placed; device 2 (4) stacks below it after a
    # blank row; device 3 (3) no longer fits and opens a fresh plan.
    coordinator = _cluster_coordinator("HA - QB [1]", 50, {"1": 4, "2": 4, "3": 3})

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["1", "2", "3"], IO_PLAN_KIND_QUICKBUS))

    assert placed == {"1": (47, (0, 0)), "2": (47, (0, 5)), "3": (48, (0, 0))}
    assert (created, failed) == ({48}, [])
    coordinator._rename_io_plan.assert_awaited_once_with(47, "HA - QB [1,2]")
    coordinator._persist_plan_map.assert_awaited_once_with(
        {"HA - QB [1,2]": 47, "HA - QB [3]": 48}, removals={"HA - QB [1]"}
    )


def test_a_failed_join_rename_places_the_member_in_a_fresh_plan() -> None:
    coordinator = _cluster_coordinator("HA - IO [A]", 150, dict.fromkeys("AB", 4), renamed=False, created=9)

    placed, created, failed = asyncio.run(coordinator.resolve_io_clusters(["A", "B"]))

    # Plan 47 keeps its name and member: plan_map must not claim it holds B.
    assert placed == {"A": (47, (0, 0)), "B": (9, (0, 0))}
    assert (created, failed) == ({9}, [])
    coordinator._persist_plan_map.assert_awaited_once_with({"HA - IO [A]": 47, "HA - IO [B]": 9}, removals=set())


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


def test_source_link_names_the_first_plan_wiring_the_source() -> None:
    coordinator = _coordinator()
    wired_in = {5, 8}
    coordinator._source_wired_in_plan = lambda _source, plan, _ref_type: plan["fub"] in wired_in  # type: ignore[method-assign]
    plans = {3: {"fub": 3}, 5: {"fub": 5}, 8: {"fub": 8}}

    assert coordinator._check_source_function_plan_link(12, plans) == 5
    assert coordinator._check_source_function_plan_link(12, {3: {"fub": 3}}) is None


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
