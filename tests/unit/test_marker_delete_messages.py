"""marker_delete response/notification wording: protected_reason vs. gate errors."""

from custom_components.comexio.api import _PLACEMENT_UNKNOWN
from custom_components.comexio.services.marker_actions import (
    _error_result,
    _protected_message,
    _protected_reason,
    _result_message,
)


def test_gate_error_wins_over_the_per_id_reasons() -> None:
    protected = dict.fromkeys([3, 5], _PLACEMENT_UNKNOWN)
    assert _protected_reason(protected, _PLACEMENT_UNKNOWN) == _PLACEMENT_UNKNOWN
    assert _protected_message(protected, _PLACEMENT_UNKNOWN) == f"Protected, not deleted: M3, M5 — {_PLACEMENT_UNKNOWN}"


def test_protected_message_lists_each_id_with_its_reason() -> None:
    protected = {3: "why", 5: "placed in function plan Test4"}
    assert _protected_message(protected, None) == (
        "Protected, not deleted:\n- M3: why\n- M5: placed in function plan Test4"
    )
    assert _protected_reason(protected, None) == "M3: why; M5: placed in function plan Test4"


def test_nothing_protected_has_no_reason() -> None:
    assert _protected_reason({}, None) is None
    assert _protected_message({}, None) is None


def test_result_message_names_the_deleted_ids() -> None:
    """Regression (live test 2026-10-16): only the count was reported, not which marker was deleted."""
    message = _result_message([7, 359], [359], [7], [], None)
    assert message == "Deleted 1 of 2 requested marker(s).\nDeleted: M359\nNot deleted (already absent): M7"


def test_error_result_carries_protected_reason_key() -> None:
    # Regression (Sourcery, PR #94): every response carries "protected_reason", separate from "error".
    result = _error_result("boom", [1])
    assert result["protected_reason"] is None
    assert result["protected_reasons"] == {}
    assert result["error"] == "boom"
    assert result["requested"] == [1]
