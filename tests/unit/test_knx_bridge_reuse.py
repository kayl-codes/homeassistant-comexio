"""KNX bridge marker reuse and reset helpers of api.py (synthetic marker pools)."""

from typing import Any

from custom_components.comexio.api import (
    ComexioAPI,
    _knx_bridge_reset_candidates,
    _knx_bridge_run_end,
    _knx_bridge_title,
    _stale_knx_bridge_marker_ids,
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


def test_reset_candidates_skip_placed_and_non_bridge_markers() -> None:
    candidates, skipped = _knx_bridge_reset_candidates(POOL, placed_ids={24})
    assert candidates == [(20, True), (21, False), (22, True)]
    assert skipped == 1
