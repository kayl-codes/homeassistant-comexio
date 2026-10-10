"""KNX bridge marker reuse and reset helpers of api.py (synthetic marker pools)."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, create_autospec

from aiocomexio import ComexioClient
import pytest

from custom_components.comexio.api import (
    ComexioAPI,
    KnxBridgeReset,
    _knx_bridge_reset_candidates,
    _knx_bridge_run_end,
    _knx_bridge_title,
    _marker_form_values,
    _stale_knx_bridge_marker_ids,
    _strip_marker_kind_suffixes,
)


def _marker(marker_id: int, name: str = "", marker_type: int = 1) -> dict[str, Any]:
    return {"Id": marker_id, "Name": name, "Type": marker_type, "CategoryId": 0}


def _plan_with_markers(*marker_ids: int) -> dict[int, dict]:
    elements = {str(i): {"reference": {"type": 2, "ref_id": mid}} for i, mid in enumerate(marker_ids)}
    return {1: {"elements": elements, "connections": {}}}


# Pool shaped like the live finding of 2026-09-25, scaled down: M10 original, M11-M19 blank
# fillers, M20-M21 orphaned bridges of an earlier generation, M22 an orphaned bridge whose
# K-element has since been renamed ([TRIG]), M23-M24 active bridges, M25 a user marker.
POOL = [
    _marker(10, "Kitchen light"),
    *(_marker(i) for i in range(11, 20)),
    _marker(20, "1.001 Switch A [K1]"),
    _marker(21, "1.002 Dimmer [K3]", marker_type=2),
    _marker(22, "1.001 Switch B [K2]"),
    _marker(23, "1.001 Switch A [K1]x"),  # not a bridge title: suffix must be at the end
    _marker(24, "1.001 Switch B [TRIG] [K2]"),
    _marker(25, "User marker"),
]


def test_knx_bridge_title_matches_suffix_pattern() -> None:
    assert _knx_bridge_title(7, "1.007 Blind") == "1.007 Blind [K7]"


def test_run_end_covers_contiguous_blank_and_bridge_run() -> None:
    # M11-M22 are blank or bridge-titled; M23 breaks the run.
    assert _knx_bridge_run_end(POOL, 11, min_len=5) == 23


def test_run_end_never_below_one_block() -> None:
    assert _knx_bridge_run_end(POOL, 11, min_len=50) == 61
    assert _knx_bridge_run_end([], 300) == 350


def test_stale_bridges_exclude_titles_of_current_batch() -> None:
    keep = {"1.001 Switch A [K1]"}
    assert _stale_knx_bridge_marker_ids(POOL, 11, keep) == {21, 22, 24}
    assert _stale_knx_bridge_marker_ids(POOL, 23, set()) == {24}


def test_free_marker_ids_reclaims_unplaced_stale_bridges() -> None:
    """Regression: a renamed K-element left its old bridge marker titled forever (leak)."""
    fub_modules = {"2": {str(m["Id"]): m for m in POOL}}
    plans = _plan_with_markers(24, 12)
    batch_titles = {"1.001 Switch B [TRIG] [K2]"}

    without = ComexioAPI._free_marker_ids(fub_modules, plans, 11, max_id=23)
    with_stale = ComexioAPI._free_marker_ids(fub_modules, plans, 11, max_id=23, keep_bridge_titles=batch_titles)

    assert without == [11, 13, 14, 15, 16, 17, 18, 19]
    # Stale bridges M20-M22 become reusable; placed M24 (the active one) never does.
    assert with_stale == [11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]


def _unused(marker_id: int, binary: bool = True) -> KnxBridgeReset:
    return KnxBridgeReset(marker_id, binary, in_use=False, title="")


def test_reset_candidates_blank_unused_bridges_and_skip_non_bridge_markers() -> None:
    resets, unreadable = _knx_bridge_reset_candidates(POOL, placed_ids=set())
    assert resets == [_unused(20), _unused(21, binary=False), _unused(22), _unused(24)]
    assert unreadable == []


def test_reset_candidates_strip_the_suffixes_of_a_bridge_in_use_and_keep_its_values() -> None:
    """A bridge marker a user wired into an own plan becomes a plain marker, its form values sent along."""
    in_use = {"Id": 24, "Name": "1.001 Switch B [TRIG] [K2]", "Type": 2, "DefaultValue": 2.5, "Store": 1}
    resets, unreadable = _knx_bridge_reset_candidates([*POOL[:-2], in_use], placed_ids={24})
    assert resets == [
        _unused(20),
        _unused(21, binary=False),
        _unused(22),
        KnxBridgeReset(24, binary=False, in_use=True, title="1.001 Switch B", default=2.5, store=True),
    ]
    assert unreadable == []


def test_reset_candidates_leave_a_bridge_in_use_alone_whose_values_are_unreadable() -> None:
    """Without its default value/store flag a rename would overwrite them — the title is kept instead."""
    resets, unreadable = _knx_bridge_reset_candidates(POOL, placed_ids={24})  # POOL markers carry no values
    assert [r.marker_id for r in resets] == [20, 21, 22]
    assert unreadable == [24]


def test_reset_candidates_include_a_bridge_title_with_trailing_blanks() -> None:
    """Regression: aiocomexio counts "... [K3] " as a bridge marker — the cleanup must reset it too,
    or the import_disabled KNX repair would come back after every cleanup."""
    resets, unreadable = _knx_bridge_reset_candidates([{"Id": 30, "Name": "Rollo [K3] ", "Type": 1}], set())
    assert resets == [_unused(30)]
    assert unreadable == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Rollo [K3]", "Rollo"),
        ("Rollo [TRIG] [K3]", "Rollo"),
        ("Pulse [TP] [K5]", "Pulse"),
        ("Boiler [RO][K4] ", "Boiler"),
        ("Rollo [Küche] [K3]", "Rollo [Küche]"),  # not a kind suffix
        ("Rollo[RO]x [K3]", "Rollo[RO]x"),  # [RO] not trailing
        ("[K3]", ""),
    ],
)
def test_strip_marker_kind_suffixes(name: str, expected: str) -> None:
    assert _strip_marker_kind_suffixes(name) == expected


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ({"Type": 1, "DefaultValue": 0, "Store": 0}, (0, False)),
        ({"Type": 1, "DefaultValue": 1, "Store": 1}, (1, True)),
        ({"Type": "2", "DefaultValue": 20.5, "Store": 0}, (20.5, False)),
        ({}, None),
        ({"Type": 1, "DefaultValue": None, "Store": 0}, None),
        ({"Type": 1, "DefaultValue": "1", "Store": 0}, None),
        ({"Type": 1, "DefaultValue": True, "Store": 0}, None),
        ({"Type": 2, "DefaultValue": float("nan"), "Store": 0}, None),
        ({"Type": 1, "DefaultValue": 0, "Store": 2}, None),
        ({"Type": 1, "DefaultValue": 0, "Store": True}, None),
        ({"Type": 1, "DefaultValue": 0, "Store": "1"}, None),
        ({"Type": 1, "DefaultValue": 0}, None),
        # The save sends the type along too: a guessed one could flip the marker's kind.
        ({"DefaultValue": 0, "Store": 0}, None),
        ({"Type": 3, "DefaultValue": 0, "Store": 0}, None),
        ({"Type": True, "DefaultValue": 0, "Store": 0}, None),
        ({"Type": None, "DefaultValue": 0, "Store": 0}, None),
    ],
)
def test_marker_form_values_read_only_what_can_be_sent_back_faithfully(
    marker: dict[str, Any], expected: tuple[float, bool] | None
) -> None:
    assert _marker_form_values(marker) == expected


def test_reset_markers_keep_the_values_of_one_in_use_and_ignore_the_trigger_plan(comexio_api: ComexioAPI) -> None:
    """A placement in the trigger plan is no use (its KNX pairs belong to the cleanup); one elsewhere is."""
    markers = {
        "20": {"Id": 20, "Name": "Switch A [K1]", "Type": 1, "DefaultValue": 1, "Store": 1},
        "21": {"Id": 21, "Name": "Dimmer [RO] [K3]", "Type": 2, "DefaultValue": 20, "Store": 1},
        "25": {"Id": 25, "Name": "User marker", "Type": 1, "DefaultValue": 0, "Store": 0},
    }
    comexio_api.get_raw_config = AsyncMock(return_value={"FubModules": {"2": markers}, "Fubs": {"1": {}, "2": {}}})
    comexio_api._load_all_plans_verified = AsyncMock(  # type: ignore[method-assign]
        return_value={1: _plan_with_markers(20, 21)[1], 2: _plan_with_markers(21)[1]}
    )
    comexio_api.rename_marker = AsyncMock(side_effect=[True, False])  # type: ignore[method-assign]
    progress: list[tuple[int, int]] = []

    result = asyncio.run(
        comexio_api.reset_knx_bridge_markers(lambda done, total: progress.append((done, total)), ignored_plan_ids=[1])
    )

    assert result == ([20], [], [21], [], None)
    assert [(call.args, call.kwargs) for call in comexio_api.rename_marker.await_args_list] == [
        ((20, "", True), {"default": 0, "store": False}),
        ((21, "Dimmer", False), {"default": 20, "store": True}),
    ]
    assert progress == [(1, 2), (2, 2)]
    comexio_api._load_all_plans_verified.assert_awaited_once_with({"1": {}, "2": {}}, strict=True)


def test_reset_markers_touch_nothing_when_a_plan_is_missing(comexio_api: ComexioAPI) -> None:
    """A plan that could not be loaded would make the bridge markers wired into it look unused."""
    markers = {"20": {"Id": 20, "Name": "Switch A [K1]", "Type": 1, "DefaultValue": 1, "Store": 1}}
    comexio_api.get_raw_config = AsyncMock(return_value={"FubModules": {"2": markers}, "Fubs": {"1": {}}})
    comexio_api._load_all_plans_verified = AsyncMock(return_value=None)  # type: ignore[method-assign]
    comexio_api.rename_marker = AsyncMock()  # type: ignore[method-assign]

    result = asyncio.run(comexio_api.reset_knx_bridge_markers())

    assert result == ([], [], [], [], "could not load every function plan")
    comexio_api.rename_marker.assert_not_awaited()


def test_reset_markers_touch_nothing_when_a_marker_reference_is_unreadable(comexio_api: ComexioAPI) -> None:
    """Fail closed like marker_delete's force gate: a skipped reference would blank a placed marker."""
    markers = {"20": {"Id": 20, "Name": "Switch A [K1]", "Type": 1, "DefaultValue": 1, "Store": 1}}
    comexio_api.get_raw_config = AsyncMock(return_value={"FubModules": {"2": markers}, "Fubs": {"1": {}}})
    broken = {"elements": {"1": {"reference": {"type": 2, "ref_id": "broken"}}}, "connections": {}}
    comexio_api._load_all_plans_verified = AsyncMock(return_value={1: broken})  # type: ignore[method-assign]
    comexio_api.rename_marker = AsyncMock()  # type: ignore[method-assign]

    result = asyncio.run(comexio_api.reset_knx_bridge_markers())

    assert result == ([], [], [], [], "unreadable marker reference in a function plan")
    comexio_api.rename_marker.assert_not_awaited()


@pytest.mark.parametrize("group", [42, "markers"])
def test_reset_markers_touch_nothing_when_the_marker_group_is_malformed(comexio_api: ComexioAPI, group: Any) -> None:
    comexio_api.get_raw_config = AsyncMock(return_value={"FubModules": {"2": group}, "Fubs": {}})
    comexio_api._load_all_plans_verified = AsyncMock(return_value={})  # type: ignore[method-assign]
    comexio_api.rename_marker = AsyncMock()  # type: ignore[method-assign]

    result = asyncio.run(comexio_api.reset_knx_bridge_markers())

    assert result == ([], [], [], [], "could not fetch current Comexio config")
    comexio_api.rename_marker.assert_not_awaited()


def test_rename_marker_hands_default_and_store_flag_to_the_client(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client is spec'd from the installed aiocomexio: a keyword it lacks fails here, not live."""
    client = create_autospec(ComexioClient, instance=True)
    monkeypatch.setattr(comexio_api, "_new_client", lambda _session: client)

    assert asyncio.run(comexio_api.rename_marker(21, "Dimmer", False, default=20.5, store=True))

    client.rename_marker.assert_awaited_once_with(21, "Dimmer", binary=False, default=20.5, store_memory=True)


def test_element_refs_skip_malformed_elements() -> None:
    """A non-object element in any plan must not break the poll (every plan is scanned for bridge markers)."""
    plan = {
        "elements": {"5": None, "6": {"reference": {"type": 2, "ref_id": 7}}},
        "connections": {"1": None, "2": "broken"},
    }
    assert ComexioAPI.function_plan_element_refs(plan) == [(2, 7)]
