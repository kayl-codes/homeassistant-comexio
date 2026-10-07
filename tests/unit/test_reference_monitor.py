"""Reference catalog check on the integration side: Repair, GitHub report, Flanke guards.

The reconciliation itself (keys, statuses, reference files) lives in aiocomexio.reference_catalog
and is tested there; these tests cover what the integration builds on top of it.
"""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

from aiocomexio.reference_catalog import (
    KIND_FUB_BASE,
    LIVE_EXTRACTORS,
    REASON_CHECK_FAILED,
    REASON_NO_LIVE_CATALOG,
    REFERENCE_DIR,
    ReferenceCatalog,
    build_reference,
    load_reference_catalogs,
    parse_reference,
    reconcile,
    unresolved,
)
import pytest

from custom_components.comexio import reference_monitor
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import FUB_BASE_KEY_FLANKE, REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE
from custom_components.comexio.reference_monitor import (
    ISSUE_TRANSLATION_KEY,
    ISSUE_TRANSLATION_KEY_UNCHECKED,
    ISSUE_TRUNCATED_LINE,
    REFERENCE_REQUIRED,
    IssueContext,
    ReferenceCatalogMonitor,
    build_issue_report,
    github_issue_url,
)
from tests.common import load_json_fixture

FLANKE_ID = 113


@pytest.fixture
def raw() -> dict[str, Any]:
    return load_json_fixture("reference_catalog_raw.json")


def _references(raw: dict[str, Any], **overrides: int) -> dict[str, ReferenceCatalog]:
    """Reference catalogs built from raw itself, with selected fub_base ids overridden."""
    references = {kind: parse_reference(kind, build_reference(kind, raw, "11.1.4")) for kind in LIVE_EXTRACTORS}
    entries = {**references[KIND_FUB_BASE].entries, **overrides}
    references[KIND_FUB_BASE] = ReferenceCatalog(KIND_FUB_BASE, "11.1.4", entries)
    return references


def test_shipped_reference_files_cover_every_required_block() -> None:
    """The library's reference data must know every block the integration writes into plans."""
    references = load_reference_catalogs(REFERENCE_DIR)
    assert references[KIND_FUB_BASE].entries[FUB_BASE_KEY_FLANKE] == FLANKE_ID
    for kind, key in REFERENCE_REQUIRED:
        assert key in references[kind].entries


def _trigger_plan(flanke_ref_id: int) -> dict[str, Any]:
    """Marker element 1 <-> Flanke element 2 round trip, Flanke placed with flanke_ref_id."""
    return {
        "elements": {
            "1": {"reference": {"type": "2", "ref_id": "50"}},
            "2": {"reference": {"type": "5", "ref_id": str(flanke_ref_id)}},
        },
        "connections": {
            "10": {"input": {"FubElementId": "1"}, "output": [{"FubElementId": "2"}]},
            "11": {"input": {"FubElementId": "2"}, "output": {"0": {"FubElementId": "1"}}},
        },
    }


def test_trigger_audit_uses_the_resolved_flanke_id() -> None:
    plan = _trigger_plan(213)
    assert ComexioAPI._function_plan_trigger_wired_source_ids(plan, 213, 2) == {50}
    assert ComexioAPI._function_plan_trigger_wired_source_ids(plan, FLANKE_ID, 2) == set()


def test_flanke_ref_id_follows_the_reference_check(comexio_api: ComexioAPI, raw: dict[str, Any]) -> None:
    assert comexio_api.flanke_ref_id() is None
    comexio_api.reference_check = reconcile(_references(raw), raw, "11.1.4")
    assert comexio_api.flanke_ref_id() == FLANKE_ID
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    comexio_api.reference_check = reconcile(_references(raw), raw, "11.1.4")
    assert comexio_api.flanke_ref_id() is None


def test_trigger_pairs_refused_without_a_trusted_flanke(comexio_api: ComexioAPI) -> None:
    # No reference check yet: must return before touching the server (no session calls).
    added, errors = asyncio.run(comexio_api.function_plan_add_trigger_pairs(19, [50, 51]))
    assert added == []
    assert errors == [f"Flanke block {FUB_BASE_KEY_FLANKE} not available on this Comexio"]
    assert asyncio.run(comexio_api.function_plan_remove_trigger_pairs(19, [50])) == (0, False)
    comexio_api.session.post.assert_not_called()
    comexio_api.session.get.assert_not_called()


# ---------------------------------------------------------------------------
# Monitor: Repair lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def issue_registry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    registry = MagicMock()
    registry.IssueSeverity = reference_monitor.ir.IssueSeverity
    monkeypatch.setattr(reference_monitor, "ir", registry)
    return registry


def _monitor(
    references: dict[str, ReferenceCatalog] | None,
    version: str | None = "1.2.3",
    listed: dict[str, Any] | None = None,
) -> ReferenceCatalogMonitor:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=references)
    api = SimpleNamespace(comexio_version="11.1.4", reference_check=None)
    monitor = ReferenceCatalogMonitor(hass, api, "srv", listed_plans=lambda: listed)
    monitor._async_integration_version = AsyncMock(return_value=version)
    return monitor


def _issue_kwargs(registry: MagicMock) -> dict[str, Any]:
    return registry.async_create_issue.call_args.kwargs


def test_missing_block_raises_the_mismatch_repair(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    monitor = _monitor(_references(raw))
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    asyncio.run(monitor.async_check(raw))
    assert _issue_kwargs(issue_registry)["translation_key"] == ISSUE_TRANSLATION_KEY


def test_failed_check_raises_the_unchecked_repair(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    """An exception says nothing about the firmware — the Repair must not claim an incompatibility."""
    monitor = _monitor(None)
    monitor._hass.async_add_executor_job = AsyncMock(side_effect=OSError("disk"))
    asyncio.run(monitor.async_check(raw))
    kwargs = _issue_kwargs(issue_registry)
    assert kwargs["translation_key"] == ISSUE_TRANSLATION_KEY_UNCHECKED
    assert kwargs["translation_placeholders"]["reason"] == REASON_CHECK_FAILED


def test_missing_live_catalog_repairs_only_after_repeated_polls(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    """One partial fetch (e.g. right after setup) blocks the features but must not flash a Repair."""
    monitor = _monitor(_references(raw))
    raw["FubModules"]["5"] = []  # PHP serializes an empty mapping as an array
    for _ in range(REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE - 1):
        asyncio.run(monitor.async_check(raw))
    issue_registry.async_create_issue.assert_not_called()
    assert monitor._api.reference_check is None

    asyncio.run(monitor.async_check(raw))
    kwargs = _issue_kwargs(issue_registry)
    assert kwargs["translation_key"] == ISSUE_TRANSLATION_KEY_UNCHECKED
    assert kwargs["translation_placeholders"]["reason"] == REASON_NO_LIVE_CATALOG


def test_missing_catalog_on_the_same_firmware_keeps_the_last_result(
    raw: dict[str, Any], issue_registry: MagicMock
) -> None:
    """Block ids only move with a firmware update — a partial fetch must not flap the features or the Repair."""
    monitor = _monitor(_references(raw))
    empty = {**raw, "FubModules": {**raw["FubModules"], "5": []}}
    asyncio.run(monitor.async_check(raw))
    check = monitor._api.reference_check

    for _ in range(REFERENCE_MISSING_CATALOG_POLLS_BEFORE_ISSUE + 1):
        asyncio.run(monitor.async_check(empty))
    assert monitor._api.reference_check is check
    issue_registry.async_create_issue.assert_not_called()


def test_after_a_successful_check_a_missing_catalog_repairs_at_once(
    raw: dict[str, Any], issue_registry: MagicMock
) -> None:
    """The setup grace period ends with the first good check — a poll interval of up to a day must not hide it."""
    monitor = _monitor(_references(raw))
    empty = {**raw, "FubModules": {**raw["FubModules"], "5": []}}
    asyncio.run(monitor.async_check(raw))
    issue_registry.async_create_issue.assert_not_called()

    monitor._api.comexio_version = "11.2.0"  # a firmware update: the kept result no longer counts
    asyncio.run(monitor.async_check(empty))
    kwargs = _issue_kwargs(issue_registry)
    assert kwargs["translation_key"] == ISSUE_TRANSLATION_KEY_UNCHECKED
    assert kwargs["translation_placeholders"]["reason"] == REASON_NO_LIVE_CATALOG
    assert monitor._api.reference_check is None


def test_recovery_is_logged(raw: dict[str, Any], issue_registry: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    monitor = _monitor(_references(raw))
    broken = {**raw, "FubModules": {**raw["FubModules"], "5": dict(raw["FubModules"]["5"])}}
    del broken["FubModules"]["5"][str(FLANKE_ID)]
    asyncio.run(monitor.async_check(broken))
    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        asyncio.run(monitor.async_check(raw))
    issue_registry.async_delete_issue.assert_called()
    assert any("usable again" in record.getMessage() for record in caplog.records)


UNKNOWN_REF = ("7", "3", "999")


def test_no_plans_clear_the_unknown_refs_even_without_a_check() -> None:
    """Review: deleted plans reference nothing — their findings must not wait for the next catalog check."""
    monitor = _monitor(None)
    monitor._unknown_refs = [UNKNOWN_REF]

    monitor.check_plans({})

    assert monitor._unknown_refs == []


def test_plans_keep_the_unknown_refs_without_a_check() -> None:
    """Without a catalog check, plans that still exist cannot be judged — the last findings stay."""
    monitor = _monitor(None)
    monitor._unknown_refs = [UNKNOWN_REF]

    monitor.check_plans({7: {"elements": {}, "connections": {}}})

    assert monitor._unknown_refs == [UNKNOWN_REF]


def test_deleted_plans_drop_their_unknown_refs_without_a_check() -> None:
    """Review: a deleted plan's findings must leave the log and Repair report while other plans remain."""
    monitor = _monitor(None)
    monitor._unknown_refs = [UNKNOWN_REF, ("8", "4", "998")]

    monitor.check_plans({7: {"elements": {}, "connections": {}}})

    assert monitor._unknown_refs == [UNKNOWN_REF]


@pytest.mark.parametrize(
    "snapshot",
    [{7: {"elements": {}, "connections": {}}}, {}],
    ids=["bulk-load-missed-a-plan", "empty-snapshot"],
)
def test_listed_plans_keep_their_unknown_refs_without_a_check(snapshot: dict[int, dict]) -> None:
    """Review: a plan the poll still lists but the bulk snapshot misses is live — its findings must stay."""
    second_ref = ("8", "4", "998")
    monitor = _monitor(None, listed={"7": {}, "8": {}})
    monitor._unknown_refs = [UNKNOWN_REF, second_ref]

    monitor.check_plans(snapshot)

    assert monitor._unknown_refs == [UNKNOWN_REF, second_ref]


def test_listed_plans_missing_from_the_snapshot_keep_their_unknown_refs_with_a_check() -> None:
    """Review: with a catalog, a listed plan the bulk load missed can't be judged anew — its last findings stay."""
    monitor = _monitor(None, listed={"7": {}, "8": {}})
    monitor._api.reference_check = SimpleNamespace(fub_base_ids={1})
    monitor._unknown_refs = [("7", "2", "998"), ("8", "4", "998")]
    plan_7 = {"elements": {"3": {"reference": {"type": 5, "ref_id": 999}}}, "connections": {}}

    monitor.check_plans({7: plan_7})

    assert monitor._unknown_refs == [UNKNOWN_REF, ("8", "4", "998")]


def test_a_load_missing_a_plan_does_not_log_the_same_findings_again(caplog: pytest.LogCaptureFixture) -> None:
    """Review: the same findings in another order (carried over, then reloaded) are no change — logged once."""
    monitor = _monitor(None, listed={"7": {}, "8": {}})
    monitor._api.reference_check = SimpleNamespace(fub_base_ids={1})
    plan = {"elements": {"3": {"reference": {"type": 5, "ref_id": 999}}}, "connections": {}}

    with caplog.at_level(logging.WARNING, logger=reference_monitor.__name__):
        monitor.check_plans({7: plan, 8: plan})
        monitor.check_plans({8: plan})  # the bulk load missed plan 7
        monitor.check_plans({7: plan, 8: plan})

    assert monitor._unknown_refs == [UNKNOWN_REF, ("8", "3", "999")]
    assert len(caplog.records) == 1


def test_a_confirmed_empty_plan_list_drops_the_unknown_refs_of_a_stale_snapshot() -> None:
    """The poll lists no plan any more: the snapshot's plans are deleted, so are their findings."""
    monitor = _monitor(None, listed={})
    monitor._unknown_refs = [UNKNOWN_REF]

    monitor.check_plans({7: {"elements": {}, "connections": {}}})

    assert monitor._unknown_refs == []


def test_listed_plans_never_judged_stay_unjudged_with_an_empty_snapshot(caplog: pytest.LogCaptureFixture) -> None:
    """No catalog, no earlier findings and nothing loaded: no check ran, so nothing may be logged as checked."""
    monitor = _monitor(None, listed={"7": {}})

    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({})

    assert monitor._unknown_refs is None
    assert not caplog.records


def test_dropping_findings_with_an_empty_snapshot_does_not_claim_a_check(caplog: pytest.LogCaptureFixture) -> None:
    """Listed plans but no loaded one: dropping a deleted plan's findings must not log "no elements" as checked."""
    monitor = _monitor(None, listed={"7": {}})
    monitor._unknown_refs = [("8", "4", "998")]

    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({})

    assert monitor._unknown_refs == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("the remaining plans are not checked" in message for message in messages)
    assert not any("no elements with unknown block types" in message for message in messages)


def test_plans_the_poll_no_longer_lists_drop_their_unknown_refs_without_a_check() -> None:
    """Judged by the poll's plan list: a plan still in the snapshot but deleted in Comexio loses its findings."""
    monitor = _monitor(None, listed={"7": {}})
    monitor._unknown_refs = [UNKNOWN_REF, ("8", "4", "998")]

    monitor.check_plans({7: {"elements": {}, "connections": {}}, 8: {"elements": {}, "connections": {}}})

    assert monitor._unknown_refs == [UNKNOWN_REF]


@pytest.mark.parametrize(
    ("refs", "expected_log"),
    [
        ([UNKNOWN_REF, ("8", "4", "998")], "1 element(s) reference a block type unknown"),
        ([("8", "4", "998")], "the remaining plans are not checked"),
    ],
    ids=["findings-remain", "all-findings-dropped"],
)
def test_dropping_deleted_plan_findings_refreshes_the_unchecked_repair_and_the_log(
    raw: dict[str, Any],
    issue_registry: MagicMock,
    caplog: pytest.LogCaptureFixture,
    refs: list[tuple[str, str, str]],
    expected_log: str,
) -> None:
    """The Repair is refreshed and keeps "check didn't run"; the log never claims a check that didn't run."""
    monitor = _monitor(_references(raw))
    monitor._hass.async_add_executor_job = AsyncMock(side_effect=OSError("disk"))
    asyncio.run(monitor.async_check(raw))
    monitor._unknown_refs = refs

    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({7: {"elements": {}, "connections": {}}})

    kwargs = _issue_kwargs(issue_registry)
    assert kwargs["translation_key"] == ISSUE_TRANSLATION_KEY_UNCHECKED
    assert issue_registry.async_create_issue.call_count == 2
    messages = [record.getMessage() for record in caplog.records]
    assert any(expected_log in message for message in messages)
    assert not any("no elements with unknown block types" in message for message in messages)


def test_plans_never_judged_stay_unjudged_without_a_check() -> None:
    """No catalog and no earlier findings: nothing to drop, nothing to log."""
    monitor = _monitor(None)

    monitor.check_plans({7: {"elements": {}, "connections": {}}})

    assert monitor._unknown_refs is None


CLEAN_PLAN = {"elements": {}, "connections": {}}
CARRIED_REF = ("8", "4", "998")


def test_a_clean_check_names_the_listed_plans_the_load_missed(caplog: pytest.LogCaptureFixture) -> None:
    """(bt) A plan the bulk load missed was not checked — "no unknown block types" must not cover it silently."""
    monitor = _monitor(None, listed={"7": {}, "8": {}, "9": {}})
    monitor._api.reference_check = SimpleNamespace(fub_base_ids={1})

    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({7: CLEAN_PLAN})

    assert monitor._unknown_refs == []
    assert [record.getMessage() for record in caplog.records] == [
        "[srv] Function plans: no elements with unknown block types in the loaded plans; "
        "2 listed plan(s) not loaded and not checked"
    ]


def test_a_clean_check_of_every_listed_plan_has_no_unchecked_note(caplog: pytest.LogCaptureFixture) -> None:
    monitor = _monitor(None, listed={"7": {}})
    monitor._api.reference_check = SimpleNamespace(fub_base_ids={1})

    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({7: CLEAN_PLAN})

    assert [record.getMessage() for record in caplog.records] == [
        "[srv] Function plans: no elements with unknown block types in the loaded plans"
    ]


def test_findings_of_plans_the_load_missed_are_marked_as_carried_over(caplog: pytest.LogCaptureFixture) -> None:
    """(bt) A kept finding was not re-checked this poll — the log says so, a freshly found one stays unmarked."""
    monitor = _monitor(None, listed={"7": {}, "8": {}})
    monitor._api.reference_check = SimpleNamespace(fub_base_ids={1})
    monitor._unknown_refs = [CARRIED_REF]
    plan_7 = {"elements": {"3": {"reference": {"type": 5, "ref_id": 999}}}, "connections": {}}

    with caplog.at_level(logging.WARNING, logger=reference_monitor.__name__):
        monitor.check_plans({7: plan_7})

    message = caplog.records[-1].getMessage()
    assert f"7/3: 5 999, 8/4: 5 998{reference_monitor.CARRIED_OVER_SUFFIX}; 1 listed plan(s)" in message


def test_findings_kept_without_a_catalog_are_marked_as_carried_over(caplog: pytest.LogCaptureFixture) -> None:
    """Without a catalog no plan is judged — every kept finding is carried over, but no load is "missing"."""
    monitor = _monitor(None, listed={"7": {}})
    monitor._unknown_refs = [UNKNOWN_REF, CARRIED_REF]

    with caplog.at_level(logging.WARNING, logger=reference_monitor.__name__):
        monitor.check_plans({})

    message = caplog.records[-1].getMessage()
    assert message.endswith(f": 7/3: 5 999{reference_monitor.CARRIED_OVER_SUFFIX}")


def test_the_repair_report_marks_carried_over_findings(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    """(bt) The pre-filled GitHub report tells a kept finding from one checked this poll."""
    monitor = _monitor(_references(raw), listed={"7": {}, "8": {}})
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    asyncio.run(monitor.async_check(raw))  # blocked: the Repair carries the report
    monitor._unknown_refs = [CARRIED_REF]
    plan_7 = {"elements": {"3": {"reference": {"type": 5, "ref_id": 999}}}, "connections": {}}

    monitor.check_plans({7: plan_7})

    url = _issue_kwargs(issue_registry)["learn_more_url"]
    body = parse_qs(urlsplit(url).query)["body"][0]
    assert f"8/4: 5 998{reference_monitor.CARRIED_OVER_SUFFIX}" in body
    assert "7/3: 5 999\n" in body


def test_the_repair_report_follows_a_plan_the_load_misses_and_brings_back(
    raw: dict[str, Any], issue_registry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Review: unchanged findings, only the mark changes — the report follows at once, the log stays quiet."""
    monitor = _monitor(_references(raw), listed={"7": {}, "8": {}})
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    asyncio.run(monitor.async_check(raw))
    plan_8 = {"elements": {"4": {"reference": {"type": 5, "ref_id": 998}}}, "connections": {}}
    monitor.check_plans({7: CLEAN_PLAN, 8: plan_8})
    marked = f"8/4: 5 998{reference_monitor.CARRIED_OVER_SUFFIX}"

    def body() -> str:
        return parse_qs(urlsplit(_issue_kwargs(issue_registry)["learn_more_url"]).query)["body"][0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=reference_monitor.__name__):
        monitor.check_plans({7: CLEAN_PLAN})  # the bulk load missed plan 8
        assert marked in body()
        monitor.check_plans({7: CLEAN_PLAN, 8: plan_8})  # back, checked again
        assert "8/4: 5 998\n" in body()
        assert marked not in body()

    assert monitor._unknown_refs == [CARRIED_REF]
    assert not caplog.records


def test_clearing_the_plan_refs_keeps_the_unchecked_repair(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    """Review: no plan left while no check ran must not turn "check didn't run" into "blocks unusable"."""
    monitor = _monitor(_references(raw))
    monitor._hass.async_add_executor_job = AsyncMock(side_effect=OSError("disk"))
    asyncio.run(monitor.async_check(raw))
    monitor._unknown_refs = [UNKNOWN_REF]

    monitor.check_plans({})

    kwargs = _issue_kwargs(issue_registry)
    assert kwargs["translation_key"] == ISSUE_TRANSLATION_KEY_UNCHECKED
    assert kwargs["translation_placeholders"]["reason"] == REASON_CHECK_FAILED
    assert issue_registry.async_create_issue.call_count == 2  # the refresh with the cleared plan findings


def test_integration_version_is_retried_until_known(raw: dict[str, Any], issue_registry: MagicMock) -> None:
    monitor = _monitor(_references(raw), version=None)
    asyncio.run(monitor.async_check(raw))
    monitor._async_integration_version = AsyncMock(return_value="1.2.3")
    asyncio.run(monitor.async_check(raw))
    assert monitor._integration_version == "1.2.3"
    monitor._async_integration_version.assert_awaited_once()
    asyncio.run(monitor.async_check(raw))
    monitor._async_integration_version.assert_awaited_once()  # known now: no further lookups


# ---------------------------------------------------------------------------
# Pre-filled GitHub issue
# ---------------------------------------------------------------------------


def _issue_context(blocked: list[str], refs: tuple = ()) -> IssueContext:
    return IssueContext("0.11.0-rc1", "2026.9.1", tuple(blocked), refs)


def test_issue_text_does_not_change_between_polls(raw: dict[str, Any]) -> None:
    """The Repair is re-created every poll; a varying duration would rewrite the registry each time."""
    references = _references(raw)
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    first, second = (reconcile(references, raw, "11.1.4") for _ in range(2))
    object.__setattr__(second, "duration_ms", first.duration_ms + 5)
    report = [
        build_issue_report(check, REFERENCE_REQUIRED, IssueContext("0.11.0-rc2", "2026.9.0", ()))
        for check in (first, second)
    ]
    assert report[0] == report[1]
    assert " ms)" not in first.summary(include_duration=False)


def test_issue_report_explains_where_the_reference_id_went(raw: dict[str, Any]) -> None:
    references = _references(raw)
    flanke = raw["FubModules"]["5"].pop(str(FLANKE_ID))
    raw["FubModules"]["5"]["105"] = {**flanke, "Id": 105, "input": flanke["input"][:1]}
    raw["FubModules"]["5"]["113"] = {**raw["FubModules"]["5"]["65"], "Id": 113}  # an app took 113
    check = reconcile(references, raw, "11.1.4")
    blocked = unresolved(check, REFERENCE_REQUIRED)
    unknown = (("19", "593", "113"),)
    title, body = build_issue_report(check, REFERENCE_REQUIRED, _issue_context(blocked, unknown))
    assert title == f"Comexio block not usable: fub_base:{FUB_BASE_KEY_FLANKE} (changed)"
    assert "- Integration: 0.11.0-rc1" in body
    assert "- Comexio: 11.1.4" in body
    assert "reference id 113 on this server: installed app" in body
    assert "live blocks named `flankenerkenner`: flankenerkenner/d/ddd = [105]" in body
    assert "19/593: 5 113" in body
    assert "192.168" not in body


def test_issue_report_without_a_check() -> None:
    title, body = build_issue_report(None, REFERENCE_REQUIRED, _issue_context(["fub_base:x (not checked)"]))
    assert "x (not checked)" in title
    assert "Reference check did not run" in body


def test_issue_url_is_prefilled_and_cut_to_fit() -> None:
    base = "https://github.com/owner/repo/issues"
    url = github_issue_url(base, "Title", "line one\nline two")
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{base}/new?")
    assert query == {"title": ["Title"], "body": ["line one\nline two"], "labels": ["bug"]}

    long_body = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    url = github_issue_url(base, "Title", long_body, max_chars=2000)
    body = parse_qs(urlsplit(url).query)["body"][0]
    assert len(url) <= 2000
    assert body.startswith("line 0 ")
    assert body.endswith(ISSUE_TRUNCATED_LINE)
    assert body.count(ISSUE_TRUNCATED_LINE) == 1


def test_issue_url_drops_a_single_line_that_is_too_long() -> None:
    url = github_issue_url("https://github.com/owner/repo/issues", "Title", "x" * 5000, max_chars=2000)
    assert len(url) <= 2000
    assert parse_qs(urlsplit(url).query)["body"] == [ISSUE_TRUNCATED_LINE]
