"""The audit summary log names every category that adds to its issue count (bg)."""

import logging
from typing import Any

import pytest

from custom_components.comexio.coordinator import ComexioCoordinator


def _audit(**overrides: Any) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "type": [],
        "missing": [],
        "rename": [],
        "orphan": [],
        "ip_mismatch": False,
        "ha_address": "192.0.2.10:8123",
        "webio_devices": {},
        "cleanup_entities": [],
        "function_plan_missing": [],
        "function_plan_dangling": [],
        "function_plan_trigger_missing": {},
        "function_plan_trigger_orphan": {},
        "knx_bridge_missing": [],
        "knx_bridge_loopback_missing": [],
    }
    audit.update(overrides)
    return audit


def _coordinator(audit: dict[str, Any]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.last_audit_results = audit
    coordinator.last_summary_hash = None
    coordinator._last_logged_mismatches = frozenset()
    return coordinator


def test_an_unwired_source_shows_up_in_the_mismatch_counts(caplog: pytest.LogCaptureFixture) -> None:
    """Live 04.10.: "1 issues detected" with every listed category at 0 — the unwired M253 was missing."""
    coordinator = _coordinator(_audit(function_plan_missing=[{"name": "M253 Licht"}]))

    with caplog.at_level(logging.INFO):
        coordinator._log_audit_summary({"function_plan_missing_M253"})

    assert "1 issues detected" in caplog.text
    assert "Not wired:1" in caplog.text
    assert "M253 Licht" in caplog.text


def test_an_ignored_source_to_clean_up_shows_up_in_the_counts_and_the_list(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(_audit(cleanup_entities=[("Marker", 295)]))

    with caplog.at_level(logging.INFO):
        coordinator._log_audit_summary({"cleanup_entity_Marker_295"})

    assert "Cleanup:1" in caplog.text
    assert "Ignored sources to clean up (1): Marker/295" in caplog.text


def test_a_new_cleanup_item_logs_the_summary_again(caplog: pytest.LogCaptureFixture) -> None:
    """The change hash covers cleanup_entities, so a new leftover is not swallowed as "unchanged".

    Same mismatch keys in both calls: only the cleanup count in the hash can trigger the second log.
    """
    keys = {"function_plan_missing_M253", "cleanup_entity_KNX_9"}
    coordinator = _coordinator(_audit(function_plan_missing=[{"name": "M253"}]))
    coordinator._log_audit_summary(keys)
    caplog.clear()
    coordinator.last_audit_results = _audit(function_plan_missing=[{"name": "M253"}], cleanup_entities=[("KNX", 9)])

    with caplog.at_level(logging.INFO):
        coordinator._log_audit_summary(keys)

    assert "Ignored sources to clean up (1): KNX/9" in caplog.text


def test_a_replaced_cleanup_item_logs_the_summary_again(caplog: pytest.LogCaptureFixture) -> None:
    """Review: one leftover swapped for another keeps every count — the item identities still differ."""
    coordinator = _coordinator(_audit(cleanup_entities=[("Marker", 295)]))
    coordinator._log_audit_summary({"cleanup_entity_Marker_295"})
    coordinator.last_audit_results = _audit(cleanup_entities=[("KNX", 9)])

    with caplog.at_level(logging.INFO):
        coordinator._log_audit_summary({"cleanup_entity_KNX_9"})

    assert "Ignored sources to clean up (1): KNX/9" in caplog.text


def test_an_unchanged_audit_is_not_logged_again(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(_audit(cleanup_entities=[("Marker", 295)]))
    coordinator._log_audit_summary({"cleanup_entity_Marker_295"})
    caplog.clear()

    with caplog.at_level(logging.INFO):
        coordinator._log_audit_summary({"cleanup_entity_Marker_295"})

    assert caplog.text == ""
