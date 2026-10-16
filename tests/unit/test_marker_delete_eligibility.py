"""marker_delete eligibility: placement + CategoryId gate, force (untitled only), record parsing."""

import asyncio
from typing import Any

import pytest

from custom_components.comexio.api import (
    ComexioAPI,
    _classify_marker_delete_ids,
    _placed_marker_ids,
    _placed_marker_ids_strict,
)


def _marker(marker_id: int, name: Any = "", category: Any = 0) -> dict[str, Any]:
    return {"Id": marker_id, "ShortName": f"M{marker_id}", "Name": name, "CategoryId": category, "Type": 1}


# M1 factory, M2 Studio-titled, M3/M4 Studio-untitled (M4 placed in a plan), M5/M6 API-created.
RECORDS = {
    1: _marker(1, "System rebooted"),
    2: _marker(2, "Kitchen light"),
    3: _marker(3, ""),
    4: _marker(4, None),
    5: _marker(5, "1.001 Switch [K1]", category=1),
    6: _marker(6, "", category=1),
}
ALL_IDS = [1, 2, 3, 4, 5, 6, 7]  # M7 absent (already deleted / never existed)


def _classify(ids: list[int], records: dict, placements: dict | None = None, force: bool = False) -> tuple:
    """(deletable, sorted protected ids) — the reasons are checked in test_marker_delete_placement."""
    deletable, protected = _classify_marker_delete_ids(ids, records, placements or {}, force)
    return deletable, sorted(protected)


def test_without_force_only_api_created_and_absent_ids_are_deletable() -> None:
    # Regression 2026-09-25: protected ids used to abort the whole call — now they are only
    # split off, the API-created ones in the same batch stay deletable.
    assert _classify(ALL_IDS, RECORDS) == ([5, 6, 7], [1, 2, 3, 4])


def test_force_adds_only_untitled_unplaced_markers() -> None:
    assert _classify(ALL_IDS, RECORDS, {4: ["Test4"]}, force=True) == ([3, 5, 6, 7], [1, 2, 4])


def test_a_placed_marker_is_protected_whatever_its_category() -> None:
    """Regression (live test 2026-10-16): a placed CategoryId==1 marker was deletable without force."""
    placements = {mid: ["Test4"] for mid in ALL_IDS}
    assert _classify(ALL_IDS, RECORDS, placements) == ([], ALL_IDS)
    assert _classify(ALL_IDS, RECORDS, placements, force=True) == ([], ALL_IDS)


@pytest.mark.parametrize("name", ["   ", "", None])
def test_force_treats_blank_names_as_untitled(name: Any) -> None:
    assert _classify([9], {9: _marker(9, name)}, force=True) == ([9], [])


@pytest.mark.parametrize("name", ["x", 0, ["a"]])
def test_force_treats_titled_or_malformed_names_as_titled(name: Any) -> None:
    assert _classify([9], {9: _marker(9, name)}, force=True) == ([], [9])


@pytest.mark.parametrize("category", [True, 1.0, "1", None, 2])
def test_malformed_category_is_not_api_created(category: Any) -> None:
    assert _classify([9], {9: _marker(9, "t", category)}) == ([], [9])


@pytest.mark.parametrize("category", [2, -1, True, False, 0.0, "0", None])
def test_force_requires_studio_category(category: Any) -> None:
    # Regression (Sourcery, PR #94): force only opens CategoryId==0 — an unknown or malformed
    # CategoryId stays protected even when the marker is untitled and unplaced.
    assert _classify([9], {9: _marker(9, "", category)}, force=True) == ([], [9])


def test_force_with_missing_category_stays_protected() -> None:
    record = {"Id": 9, "ShortName": "M9", "Name": "", "Type": 1}
    assert _classify([9], {9: record}, force=True) == ([], [9])


def test_load_all_plans_verified_fails_closed_on_malformed_plan_id(comexio_api: ComexioAPI) -> None:
    # Regression (Sourcery, PR #94): a non-numeric $Fubs key raised ValueError out of the
    # placement check instead of refusing — and replaced the cached plan list before failing.
    cached = {"1": {"Id": 1}}
    comexio_api._fub_data = cached
    assert asyncio.run(comexio_api._load_all_plans_verified({"1": {}, "x": {}}, strict=True)) is None
    assert comexio_api._fub_data is cached


def test_placed_marker_ids_only_counts_marker_references() -> None:
    plans = {
        1: {"elements": {"a": {"reference": {"type": 2, "ref_id": "4"}}, "b": {"reference": {"type": 1, "ref_id": 3}}}},
        2: {"elements": {"c": {"reference": {"type": "2", "ref_id": 8}}, "d": "malformed"}},
    }
    assert _placed_marker_ids(plans, 2) == {4, 8}


def test_parse_marker_records_keeps_full_records() -> None:
    conf = {"FubModules": {"2": {"1": _marker(1, "System rebooted"), "3": _marker(3, "")}}}
    records = ComexioAPI._parse_marker_records(conf)
    assert records == {1: conf["FubModules"]["2"]["1"], 3: conf["FubModules"]["2"]["3"]}


@pytest.mark.parametrize(
    "conf",
    [
        {},
        {"FubModules": []},
        {"FubModules": {"2": {}}},
        {"FubModules": {"2": [_marker(1), _marker(1)]}},  # duplicate Id
        {"FubModules": {"2": [_marker(1), {"Id": "x1"}]}},  # unparseable Id
    ],
)
def test_parse_marker_records_fails_closed(conf: dict) -> None:
    assert ComexioAPI._parse_marker_records(conf) is None


def _plan(*elements: Any) -> dict[str, Any]:
    return {"elements": {str(i): e for i, e in enumerate(elements)}, "connections": {}}


def _ref(ref_type: Any, ref_id: Any) -> dict[str, Any]:
    return {"reference": {"type": ref_type, "ref_id": ref_id}}


def test_placed_marker_ids_strict_collects_marker_refs() -> None:
    plans = {1: _plan(_ref(2, "4"), _ref(1, 3), {"name": "AND"}), 2: _plan(_ref("2", 8)), 3: _plan()}
    assert _placed_marker_ids_strict(plans) == {4, 8}


@pytest.mark.parametrize(
    "plan",
    [
        {"elements": None},  # error payload normalized elsewhere would look like an empty plan
        {"error": "no permission"},
        _plan(_ref(2, "x")),  # unreadable marker ref_id — the placed marker would look unplaced
        _plan(_ref(2, None)),
        _plan("malformed element"),
        _plan({"reference": "malformed"}),
        # Regression (Sourcery, PR #94): a reference without a readable type used to be skipped
        # as "not a marker", so a placed marker behind it looked unplaced.
        _plan({"reference": {"ref_id": 4}}),
        _plan(_ref(None, 4)),
        _plan(_ref(True, 4)),
        _plan(_ref("x", 4)),
        _plan(_ref(2.0, 4)),
        _plan(_ref("²", 4)),  # isdigit() but not int()-parseable — must not raise
    ],
)
def test_placed_marker_ids_strict_fails_closed(plan: dict) -> None:
    # Regression (review 2026-09-25): with force, any unreadable plan must make placement
    # unknown (None) instead of letting a placed marker pass as unplaced.
    assert _placed_marker_ids_strict({1: _plan(_ref(2, 4)), 2: plan}) is None


def test_an_ignored_plan_does_not_count_but_is_still_read_strictly() -> None:
    """Regression (Sourcery, PR #164): an unreadable reference in the ignored trigger plan may be
    a KNX pair its cleanup skipped, so it must stop the KNX bridge marker reset too."""
    assert _placed_marker_ids_strict({1: _plan(_ref(2, 4)), 2: _plan(_ref(2, 8))}, ignored={2}) == {4}
    assert _placed_marker_ids_strict({1: _plan(_ref(2, 4)), 2: _plan(_ref(2, "x"))}, ignored={2}) is None
