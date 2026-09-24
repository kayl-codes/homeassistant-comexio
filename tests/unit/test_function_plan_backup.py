"""Renumbering-tolerant plan hashing and semantic snapshot diffing (function_plan_backup.py)."""

import copy
from typing import Any

import pytest

from custom_components.comexio.function_plan_backup import (
    build_source_id_translation,
    diff_snapshots,
    format_backup_label,
    plan_hash,
)
from tests.common import load_json_fixture

_ID_OFFSET = 1000


@pytest.fixture
def plan() -> dict[str, Any]:
    fixture = load_json_fixture("function_plan.json")
    return {"elements": fixture["elements"], "connections": fixture["connections"]}


def _renumbered(plan: dict[str, Any]) -> dict[str, Any]:
    """The same plan after Comexio renumbered every FubElementId (and connection id) by an offset."""

    def shift(port: dict[str, Any]) -> dict[str, Any]:
        return {**port, "FubElementId": port["FubElementId"] + _ID_OFFSET}

    return {
        "elements": {str(int(eid) + _ID_OFFSET): elem for eid, elem in plan["elements"].items()},
        "connections": {
            str(int(cid) + _ID_OFFSET): {"input": shift(conn["input"]), "output": [shift(o) for o in conn["output"]]}
            for cid, conn in plan["connections"].items()
        },
    }


def test_plan_hash_ignores_element_renumbering(plan: dict[str, Any]) -> None:
    assert plan_hash(_renumbered(plan)) == plan_hash(plan)


def test_plan_hash_changes_with_wiring(plan: dict[str, Any]) -> None:
    changed = copy.deepcopy(plan)
    changed["connections"]["11"]["output"][0]["Inverted"] = False

    assert plan_hash(changed) != plan_hash(plan)


def test_diff_of_renumbered_plan_is_empty(plan: dict[str, Any]) -> None:
    diff = diff_snapshots(_renumbered(plan), plan)

    assert diff == {
        "markers": {"added": [], "removed": []},
        "ios": {"added": [], "removed": []},
        "connections": {"added": [], "removed": [], "moved": []},
    }


def test_diff_reports_added_marker_and_wire(plan: dict[str, Any]) -> None:
    newer = copy.deepcopy(plan)
    newer["elements"]["20"] = {"name": "", "position_x": 420, "position_y": 120, "reference": {"type": 2, "ref_id": 9}}
    newer["connections"]["21"] = {
        "input": {"FubElementId": 7, "IOPos": 0, "Inverted": False},
        "output": [{"FubElementId": 20, "IOPos": 0, "Inverted": False}],
    }

    diff = diff_snapshots(newer, plan)

    assert diff["markers"] == {"added": [(2, 9, None, None, None)], "removed": []}
    assert len(diff["connections"]["added"]) == 1
    assert diff["connections"]["removed"] == diff["connections"]["moved"] == []


def test_diff_reports_moved_block_as_moved_not_added_and_removed(plan: dict[str, Any]) -> None:
    """A dragged block (position-identified) must not surface as a removed+added wire pair."""
    newer = copy.deepcopy(plan)
    newer["elements"]["4"]["position_y"] = 60

    connections = diff_snapshots(newer, plan)["connections"]

    assert connections["added"] == connections["removed"] == []
    assert len(connections["moved"]) == 3  # the Oder gate's two inputs and its output


def test_source_id_translation_maps_only_stable_reference_types(plan: dict[str, Any]) -> None:
    translation = build_source_id_translation(plan["elements"], _renumbered(plan)["elements"])

    # marker, IO, Web-IO, marker, time module — not the comment, blocks or constant.
    assert translation == {"2": "1002", "3": "1003", "5": "1005", "8": "1008", "9": "1009"}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"kind": "auto", "slot": 0, "captured_at": "2026-09-24T21:58:50+00:00"},
            "auto[0] — 24.09.2026 21:58",
        ),
        (
            {
                "kind": "change",
                "slot": 2,
                "captured_at": "2026-09-24T21:58:50+00:00",
                "operation": "add_marker_pairs [1, 2, 3, 4, 5]",
            },
            "change[2] — 24.09.2026 21:58 (add_marker_pairs [1, 2, 3, …])",
        ),
        (
            {
                "kind": "auto",
                "slot": 0,
                "captured_at": "2026-09-24T21:58:50+00:00",
                "restored_at": "x",
                "operation": "o",
            },
            "auto[0] — 24.09.2026 21:58*",
        ),
        ({"kind": "auto", "slot": 1, "captured_at": "garbage"}, "auto[1] — ?"),
    ],
)
def test_format_backup_label(entry: dict[str, Any], expected: str) -> None:
    assert format_backup_label(entry) == expected
