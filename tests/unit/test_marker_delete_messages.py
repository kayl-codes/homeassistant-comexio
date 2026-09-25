"""marker_delete response/notification wording: protected_reason vs. gate errors."""

from custom_components.comexio.api import _FORCE_IGNORED
from custom_components.comexio.services.marker_actions import (
    _error_result,
    _protected_message,
    _protected_reason,
)


def test_gate_error_wins_over_force_wording() -> None:
    assert _protected_reason(True, _FORCE_IGNORED) == _FORCE_IGNORED


def test_force_and_default_reasons_differ() -> None:
    assert "force only deletes untitled, unplaced markers" in _protected_reason(True, None)
    assert "enable 'force'" in _protected_reason(False, None)


def test_protected_message_lists_ids() -> None:
    assert _protected_message([3, 5], "why") == "Protected, not deleted: M3, M5 — why"


def test_error_result_carries_protected_reason_key() -> None:
    # Regression (Sourcery, PR #94): every response carries "protected_reason", separate from "error".
    result = _error_result("boom", [1])
    assert result["protected_reason"] is None
    assert result["error"] == "boom"
    assert result["requested"] == [1]
