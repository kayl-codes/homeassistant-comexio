"""No new integration function exceeds cognitive complexity 15 (SonarCloud S3776), and no listed one grows.

SonarCloud only sees a function once the PR is scanned, so a too-complex function cost a bot round each time (5 of
them on #220). complexipy measures cognitive complexity locally, but not exactly like SonarCloud: it scores some
functions higher (it counts nested functions into their parent, e.g. `async_setup_entry`), and some lower. The
functions it already scores above the limit, while SonarCloud reports none of them, are listed in `BASELINE` with
their current score as a ceiling: they may get simpler, not more complex. Every other function stays at or below
`MAX_COMPLEXITY`. The cloud scan remains the binding gate - this test only catches most findings earlier.

Simplified a baseline function? Lower its entry, or drop it once within the limit; the test names stale ones.
"""

from pathlib import Path
import sys

import complexipy
import pytest

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
MAX_COMPLEXITY = 15

# "<path below custom_components/comexio>::<function>": complexipy score at the time it was listed.
BASELINE = {
    "__init__.py::async_setup_entry": 122,
    "api.py::ComexioAPI::ensure_knx_loopback_webio": 16,
    "api.py::ComexioAPI::function_plan_add_knx_bridge_pairs": 18,
    "api.py::ComexioAPI::_function_plan_trigger_wired_source_ids": 16,
    "api.py::ComexioAPI::_function_plan_paired_flanke_ids": 22,
    "binary_sensor.py::async_setup_entry": 17,
    "button.py::ComexioSyncButton::_delta_sync_class": 21,
    "button.py::ComexioSyncButton::_sync_all_classes": 19,
    "button.py::ComexioSyncButton::_wire_knx_full": 17,
    "config_flow.py::ComexioConfigFlow::async_step_user": 34,
    "coordinator.py::ComexioCoordinator::_function_plan_missing_detail": 16,
    "coordinator.py::ComexioCoordinator::_vanished_user_plans": 17,
    "coordinator.py::ComexioCoordinator::_plan_wired_pairs": 23,
    "coordinator.py::ComexioCoordinator::_plan_knx_bridge_pairs": 18,
    "coordinator.py::ComexioCoordinator::_audit_trigger_pairs": 34,
    "ha_address.py::HaAddressResolver::_async_known_host": 16,
    "number.py::async_setup_entry": 18,
    "options_flow.py::ComexioOptionsFlow::_normalize_user_input": 16,
    "plan_watchdog.py::ManagedPlanWatchdog::async_check": 19,
    "reference_monitor.py::ReferenceCatalogMonitor::check_plans": 17,
    "repairs.py::ComexioRepairFlow::async_step_select_action": 174,
    "sensor.py::async_setup_entry": 18,
    "services/_grid.py::_build_sorted_pairs": 18,
    "services/backup.py::_handle_function_plan_list_backups": 17,
    "switch.py::async_setup_entry": 17,
}


def _measured() -> list[tuple[str, int]]:
    """(key like `BASELINE`, complexipy score) of every integration function; a repeated name yields several.

    `no_ignore`: a `# complexipy: ignore` comment would otherwise drop its function from the scan, and `BASELINE`
    is the only permitted exception.
    """
    measured = []
    for path in sorted(INTEGRATION.rglob("*.py")):
        prefix = path.relative_to(INTEGRATION).as_posix()
        functions = complexipy.file_complexity(str(path), no_ignore=True).functions
        measured += [(f"{prefix}::{f.name}", f.complexity) for f in functions]
    return measured


def _scores() -> dict[str, int]:
    """complexipy score of every integration function, keyed like `BASELINE` (the highest one on a repeated name)."""
    scores: dict[str, int] = {}
    for key, score in _measured():
        scores[key] = max(scores.get(key, 0), score)
    return scores


def test_no_function_exceeds_its_cognitive_complexity_limit() -> None:
    scores = _scores()
    too_complex = [
        f"{key}: {score} (limit {BASELINE.get(key, MAX_COMPLEXITY)})"
        for key, score in sorted(scores.items())
        if score > BASELINE.get(key, MAX_COMPLEXITY)
    ]

    # The scan must see the code it guards, or a moved directory turns it vacuously green.
    assert scores
    assert not too_complex, "Cognitive complexity above the limit (SonarCloud S3776) - split the function:\n" + (
        "\n".join(too_complex)
    )


def _stale_entries(scores: dict[str, int]) -> list[str]:
    """`BASELINE` entries whose ceiling is above the current score or within the limit; a score above the ceiling is
    the limit test's finding."""
    return [
        f"{key}: drop it" if scores.get(key, 0) <= MAX_COMPLEXITY else f"{key}: lower it to {scores[key]}"
        for key, ceiling in sorted(BASELINE.items())
        if scores.get(key, 0) < ceiling or ceiling <= MAX_COMPLEXITY
    ]


def test_no_baseline_ceiling_is_above_the_current_score() -> None:
    # A ceiling above the current score would let the function grow back unnoticed.
    stale = _stale_entries(_scores())

    assert not stale, "Update these BASELINE entries:\n" + "\n".join(stale)


def test_the_stale_check_lowers_or_drops_a_ceiling_but_never_raises_it(monkeypatch: pytest.MonkeyPatch) -> None:
    ceilings = {"grown": 16, "exact": 16, "simpler": 18, "at_limit": 16, "fixed": 17, "gone": 16, "listed_low": 15}
    scores = {"grown": 17, "exact": 16, "simpler": 16, "at_limit": 15, "fixed": 12, "listed_low": 15}
    monkeypatch.setattr(sys.modules[__name__], "BASELINE", ceilings)

    stale = _stale_entries(scores)

    assert stale == [
        "at_limit: drop it",
        "fixed: drop it",
        "gone: drop it",
        "listed_low: drop it",
        "simpler: lower it to 16",
    ]


def test_an_inline_ignore_does_not_hide_a_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "module.py").write_text(
        "def busy(items, limit):  # complexipy: ignore\n"
        "    for item in items:\n"
        "        if item:\n"
        "            while limit:\n"
        "                limit -= 1\n"
        "    return limit\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "INTEGRATION", tmp_path)

    assert "module.py::busy" in _scores()


def test_baseline_names_are_unique_per_file() -> None:
    # A second function of the same name in the file would share the listed function's ceiling.
    keys = [key for key, _ in _measured()]
    shared = sorted({key for key in BASELINE if keys.count(key) > 1})

    assert not shared, "BASELINE names that more than one function in the file carries:\n" + "\n".join(shared)
