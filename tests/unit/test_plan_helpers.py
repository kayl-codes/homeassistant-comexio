"""Small coordinator/API helpers around function plans and polled KNX values (coordinator.py, api.py)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import WebioClass, source_category
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
    coordinator = _coordinator(config_entry=SimpleNamespace(options=options))

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


@pytest.mark.parametrize(("created", "placed"), [(None, None), (9, (9, 0))], ids=["create-failed", "created"])
def test_a_fresh_io_plan_places_the_extension_in_column_0(created: int | None, placed: tuple | None) -> None:
    coordinator = _coordinator(
        _io_plan_rows_per_col=lambda _orientation: 10,
        _io_rows_needed=lambda _ext: 4,
        _create_managed_plan=AsyncMock(return_value=created),
    )

    assert asyncio.run(coordinator._join_or_create_io_plan("A", {}, 2, PREFIX)) == placed


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
