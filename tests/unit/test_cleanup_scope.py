"""Scope selection of the uninstall cleanup (cleanup_scope.py)."""

import pytest

from custom_components.comexio.cleanup_scope import (
    CLEANUP_SCOPE_FULL,
    CLEANUP_SCOPE_IO,
    CLEANUP_SCOPE_KNX,
    CLEANUP_SCOPE_MARKER,
    TRIGGER_PLAN_DELETE,
    TRIGGER_PLAN_KEEP,
    TRIGGER_PLAN_REMOVE_PAIRS,
    has_knx_artifacts,
    plan_in_scope,
    plans_in_scope,
    scope_counts,
    scope_includes_knx,
    scope_trigger_ref_type,
    trigger_plan_action,
    trigger_sources_by_category,
    webio_classes_in_scope,
)
from custom_components.comexio.const import FUNCTION_PLAN_TRIGGER_PLAN_NAME, WEBIO_CLASSES, WebioClass

PLAN_MAP = {
    "HA - Marker [1-100]": 1,
    "HA - Marker [101-200]": 2,
    "HA - IO [BASE,EOU]": 3,
    "HA - KNX [1-50]": 4,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME: 5,
    "Other - KNX [1-50]": 6,  # created under an earlier prefix — still in scope
}

WEBIO_DEVICES = {
    WebioClass.MARKER: {"device_id": "11", "base_id": "21"},
    WebioClass.IO: {"device_id": "12", "base_id": None},
    WebioClass.KNX: {"device_id": None, "base_id": None},
}


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (CLEANUP_SCOPE_FULL, set(PLAN_MAP)),
        (CLEANUP_SCOPE_MARKER, {"HA - Marker [1-100]", "HA - Marker [101-200]"}),
        (CLEANUP_SCOPE_IO, {"HA - IO [BASE,EOU]"}),
        (CLEANUP_SCOPE_KNX, {"HA - KNX [1-50]", "Other - KNX [1-50]"}),
    ],
)
def test_plans_in_scope(scope: str, expected: set[str]) -> None:
    assert set(plans_in_scope(PLAN_MAP, scope)) == expected


def test_plan_in_scope_ignores_the_prefix() -> None:
    # Regression: a plan created before a prefix change must not survive a partial cleanup.
    assert plan_in_scope("Old - KNX [1-50]", CLEANUP_SCOPE_KNX)
    assert not plan_in_scope("Old - KNX [1-50]", CLEANUP_SCOPE_MARKER)
    assert plan_in_scope("Old - IO [BASE]", CLEANUP_SCOPE_IO)
    # The label must be followed by " [" — a plan merely mentioning it is not matched.
    assert not plan_in_scope("HA - KNXish", CLEANUP_SCOPE_KNX)
    # The shared trigger plan is deleted wholesale by "full" only — partial scopes decide
    # per content (trigger_plan_action).
    assert plan_in_scope(FUNCTION_PLAN_TRIGGER_PLAN_NAME, CLEANUP_SCOPE_FULL)
    for scope in (CLEANUP_SCOPE_MARKER, CLEANUP_SCOPE_IO, CLEANUP_SCOPE_KNX):
        assert not plan_in_scope(FUNCTION_PLAN_TRIGGER_PLAN_NAME, scope)


def test_scope_trigger_ref_type() -> None:
    assert scope_trigger_ref_type(CLEANUP_SCOPE_MARKER) == 2
    assert scope_trigger_ref_type(CLEANUP_SCOPE_KNX) == 11
    assert scope_trigger_ref_type(CLEANUP_SCOPE_IO) is None
    assert scope_trigger_ref_type(CLEANUP_SCOPE_FULL) is None


def test_trigger_sources_by_category_assigns_bridge_markers_to_knx() -> None:
    """Regression: a KNX trigger pair placed via its bridge marker (type 2) was counted as a
    marker pair, so "KNX only" kept it and the bridge marker could never be reset."""
    refs = [(2, 6), (2, 7), (2, 364), (11, 9), (5, 113), (14, 5)]
    titles = {6: "update Test 2 [TRIG]", 7: "neu Test 3 [TRIG]", 364: "1.001 Schalten B [TRIG] [K2]"}

    grouped = trigger_sources_by_category(refs, titles, {2, 11})

    assert grouped == {2: [(2, 6), (2, 7)], 11: [(2, 364), (11, 9)]}
    assert trigger_plan_action(11, set(grouped)) == TRIGGER_PLAN_REMOVE_PAIRS
    assert trigger_plan_action(2, set(grouped)) == TRIGGER_PLAN_REMOVE_PAIRS


def test_trigger_sources_by_category_unknown_marker_stays_marker() -> None:
    assert trigger_sources_by_category([(2, 50)], {}, {2, 11}) == {2: [(2, 50)]}


@pytest.mark.parametrize(
    ("own", "present", "expected"),
    [
        (11, {11}, TRIGGER_PLAN_DELETE),  # KNX pairs only -> whole plan goes
        (11, set(), TRIGGER_PLAN_DELETE),  # empty plan -> whole plan goes
        (11, {2, 11}, TRIGGER_PLAN_REMOVE_PAIRS),  # marker pairs too -> only KNX pairs go
        (11, {2}, TRIGGER_PLAN_KEEP),  # nothing of ours, marker pairs stay
        (2, {2, 11}, TRIGGER_PLAN_REMOVE_PAIRS),
        (2, {2}, TRIGGER_PLAN_DELETE),
    ],
)
def test_trigger_plan_action(own: int, present: set[int], expected: str) -> None:
    assert trigger_plan_action(own, present) == expected


def test_webio_classes_in_scope() -> None:
    assert webio_classes_in_scope(CLEANUP_SCOPE_FULL) == WEBIO_CLASSES
    assert webio_classes_in_scope(CLEANUP_SCOPE_IO) == (WebioClass.IO,)


def test_only_full_and_knx_touch_knx_extras() -> None:
    assert scope_includes_knx(CLEANUP_SCOPE_FULL)
    assert scope_includes_knx(CLEANUP_SCOPE_KNX)
    assert not scope_includes_knx(CLEANUP_SCOPE_MARKER)
    assert not scope_includes_knx(CLEANUP_SCOPE_IO)


def test_scope_counts() -> None:
    counts = scope_counts(PLAN_MAP, WEBIO_DEVICES)
    assert counts[CLEANUP_SCOPE_FULL] == {"plans": 6, "devices": 2, "classes": 1}
    assert counts[CLEANUP_SCOPE_MARKER] == {"plans": 2, "devices": 1, "classes": 1}
    assert counts[CLEANUP_SCOPE_IO] == {"plans": 1, "devices": 1, "classes": 0}
    assert counts[CLEANUP_SCOPE_KNX] == {"plans": 2, "devices": 0, "classes": 0}


def test_has_knx_artifacts_ignores_the_shared_trigger_plan() -> None:
    marker_only = {"HA - Marker [1-100]": 1, FUNCTION_PLAN_TRIGGER_PLAN_NAME: 5}
    assert not has_knx_artifacts(marker_only, WEBIO_DEVICES)
    assert has_knx_artifacts({**marker_only, "HA - KNX [1-50]": 4}, WEBIO_DEVICES)
    knx_device = {WebioClass.KNX: {"device_id": "13"}}
    assert has_knx_artifacts(marker_only, knx_device)


def test_has_knx_artifacts_counts_bridge_markers() -> None:
    """Titled bridge markers alone are pre-release leftovers the KNX cleanup must reset."""
    assert has_knx_artifacts({}, {}, has_bridge_markers=True)
    assert not has_knx_artifacts({}, {}, has_bridge_markers=False)
