"""The persistent notification text of an uninstall cleanup run (repairs._cleanup_result_message)."""

from typing import Any

from custom_components.comexio.cleanup_scope import CLEANUP_SCOPE_IO, CLEANUP_SCOPE_KNX, SKIPPED_KNX_BRIDGE_MARKERS
from custom_components.comexio.repairs import _cleanup_incomplete, _cleanup_result_message


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "deleted_plans": [],
        "failed_plans": [],
        "deleted_devices": [],
        "deleted_classes": [],
        "failed_classes": {},
        "reset_markers": [1],
        "normalized_markers": [2, 3],
        "failed_markers": [],
        "skipped": {},
    }
    return result | overrides


def test_the_knx_line_counts_blanked_and_normalized_markers_apart() -> None:
    message = _cleanup_result_message(_result(), CLEANUP_SCOPE_KNX)

    assert "KNX bridge markers reset: 1, in use, suffixes removed: 2 (failed: 0)" in message


def test_a_result_without_normalized_markers_still_renders() -> None:
    """A result built before normalized_markers existed (or without the KNX phase) must not raise."""
    result = _result()
    del result["normalized_markers"]

    assert "suffixes removed: 0" in _cleanup_result_message(result, CLEANUP_SCOPE_KNX)


def test_the_knx_line_is_left_out_outside_knx() -> None:
    assert "KNX bridge markers" not in _cleanup_result_message(_result(), CLEANUP_SCOPE_IO)


def test_unreadable_bridge_markers_alone_leave_the_cleanup_complete() -> None:
    """A re-run would skip them again: they must not raise the repair issue for a retry."""
    assert not _cleanup_incomplete(_result(skipped={SKIPPED_KNX_BRIDGE_MARKERS: "M5"}))
    assert _cleanup_incomplete(_result(skipped={SKIPPED_KNX_BRIDGE_MARKERS: "M5", "webio": "busy"}))
