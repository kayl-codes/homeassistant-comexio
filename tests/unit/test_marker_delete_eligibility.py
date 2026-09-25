"""marker_delete eligibility: CategoryId gate, force (untitled + unplaced), record parsing."""

from typing import Any

import pytest

from custom_components.comexio.api import (
    ComexioAPI,
    _classify_marker_delete_ids,
    _placed_marker_ids,
    _placed_marker_ids_strict,
    _plan_payload_has_elements,
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


def test_without_force_only_api_created_and_absent_ids_are_deletable() -> None:
    # Regression 2026-09-25: protected ids used to abort the whole call — now they are only
    # split off, the API-created ones in the same batch stay deletable.
    deletable, protected = _classify_marker_delete_ids(ALL_IDS, RECORDS, None)
    assert deletable == [5, 6, 7]
    assert protected == [1, 2, 3, 4]


def test_force_adds_only_untitled_unplaced_markers() -> None:
    deletable, protected = _classify_marker_delete_ids(ALL_IDS, RECORDS, {4})
    assert deletable == [3, 5, 6, 7]
    assert protected == [1, 2, 4]


def test_force_with_unknown_placement_grants_nothing() -> None:
    # placed_ids=None: plans could not all be loaded — force must not open the gate.
    assert _classify_marker_delete_ids([3], RECORDS, None) == ([], [3])


@pytest.mark.parametrize("name", ["   ", "", None])
def test_force_treats_blank_names_as_untitled(name: Any) -> None:
    assert _classify_marker_delete_ids([9], {9: _marker(9, name)}, set()) == ([9], [])


@pytest.mark.parametrize("name", ["x", 0, ["a"]])
def test_force_treats_titled_or_malformed_names_as_titled(name: Any) -> None:
    assert _classify_marker_delete_ids([9], {9: _marker(9, name)}, set()) == ([], [9])


@pytest.mark.parametrize("category", [True, 1.0, "1", None, 2])
def test_malformed_category_is_not_api_created(category: Any) -> None:
    assert _classify_marker_delete_ids([9], {9: _marker(9, "t", category)}, None) == ([], [9])


@pytest.mark.parametrize("category", [2, -1, True, False, 0.0, "0", None])
def test_force_requires_studio_category(category: Any) -> None:
    # Regression (Sourcery, PR #94): force only opens CategoryId==0 — an unknown or malformed
    # CategoryId stays protected even when the marker is untitled and unplaced.
    assert _classify_marker_delete_ids([9], {9: _marker(9, "", category)}, set()) == ([], [9])


def test_force_with_missing_category_stays_protected() -> None:
    record = {"Id": 9, "ShortName": "M9", "Name": "", "Type": 1}
    assert _classify_marker_delete_ids([9], {9: record}, set()) == ([], [9])


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
    ],
)
def test_placed_marker_ids_strict_fails_closed(plan: dict) -> None:
    # Regression (review 2026-09-25): with force, any unreadable plan must make placement
    # unknown (None) instead of letting a placed marker pass as unplaced.
    assert _placed_marker_ids_strict({1: _plan(_ref(2, 4)), 2: plan}) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"elements": {}, "connections": {}}, True),
        ({"elements": [], "connections": []}, True),
        ({"elements": None}, False),
        ({"result": "0"}, False),
        ([], False),
    ],
)
def test_plan_payload_has_elements(payload: Any, expected: bool) -> None:
    assert _plan_payload_has_elements(payload) is expected
