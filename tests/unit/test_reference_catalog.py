"""Reference catalog reconciliation: block-type ids resolved by stable key, never hard-coded."""

import asyncio
import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import FUB_BASE_KEY_FLANKE
from custom_components.comexio.reference_catalog import (
    ISSUE_TRUNCATED_LINE,
    KIND_FUB_BASE,
    KIND_FUB_TYPES,
    LIVE_EXTRACTORS,
    REASON_CHECK_FAILED,
    REFERENCE_DIR,
    EntryStatus,
    IssueContext,
    ReferenceCatalog,
    build_issue_report,
    build_reference,
    extract_live_fub_base,
    find_unknown_fub_base_refs,
    format_deviations,
    github_issue_url,
    load_reference_catalogs,
    parse_reference,
    reconcile,
    unresolved,
)
from custom_components.comexio.reference_monitor import REFERENCE_REQUIRED
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


def _flanke(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["FubModules"]["5"][str(FLANKE_ID)]


def test_key_is_name_plus_port_signature_in_pos_order_without_apps(raw: dict[str, Any]) -> None:
    live = extract_live_fub_base(raw)
    assert live == {
        "or/dd/d": [5],
        "or/ddd/d": [6],
        "average/aa/a": [45],
        FUB_BASE_KEY_FLANKE: [FLANKE_ID],
    }


def test_installed_apps_are_not_reference_data_but_known_plan_targets(raw: dict[str, Any]) -> None:
    assert "appaaaa00000_fubbbbb11111/d/d" not in build_reference(KIND_FUB_BASE, raw, "11.1.4")["entries"]
    check = reconcile(_references(raw), raw, "11.1.4")
    assert check.fub_base_ids == frozenset({5, 6, 45, 65, FLANKE_ID})
    assert check.catalogs[KIND_FUB_BASE].new_keys == ()


def test_ports_serialized_as_object_or_garbage(raw: dict[str, Any]) -> None:
    flanke = _flanke(raw)
    flanke["input"] = {str(i): port for i, port in enumerate(flanke["input"])}
    flanke["output"] = 1
    assert extract_live_fub_base(raw)["flankenerkenner/daa/"] == [FLANKE_ID]


def test_identical_catalog_is_all_ok(raw: dict[str, Any]) -> None:
    check = reconcile(_references(raw), raw, "11.1.4")
    assert all(entry.status is EntryStatus.OK for c in check.catalogs.values() for entry in c.entries.values())
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) == FLANKE_ID
    assert check.resolve(KIND_FUB_TYPES, "marker") == 2
    assert unresolved(check, REFERENCE_REQUIRED) == []
    assert format_deviations(check) == []
    assert "fub_base 4/4 ok" in check.summary()


def test_moved_id_resolves_to_the_live_id(raw: dict[str, Any]) -> None:
    references = _references(raw, **{FUB_BASE_KEY_FLANKE: 100113, "or/dd/d": 1005})
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is EntryStatus.MOVED
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) == FLANKE_ID
    assert unresolved(check, REFERENCE_REQUIRED) == []
    assert f"fub_base {FUB_BASE_KEY_FLANKE}: moved ref=100113 live=113" in format_deviations(check)
    assert "2 moved" in check.summary()
    assert not check.has_unusable()


def test_missing_block_is_blocked(raw: dict[str, Any]) -> None:
    references = _references(raw)
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is EntryStatus.MISSING
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is None
    assert unresolved(check, REFERENCE_REQUIRED) == [f"fub_base:{FUB_BASE_KEY_FLANKE} (missing)"]
    assert check.has_unusable()


def test_changed_port_layout_is_blocked_even_with_the_same_id(raw: dict[str, Any]) -> None:
    references = _references(raw)
    _flanke(raw)["input"] = _flanke(raw)["input"][:1]
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is EntryStatus.CHANGED
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is None
    assert check.catalogs[KIND_FUB_BASE].new_keys == ("flankenerkenner/d/ddd",)


def test_duplicate_live_key_is_ambiguous(raw: dict[str, Any]) -> None:
    references = _references(raw)
    raw["FubModules"]["5"]["213"] = {**copy.deepcopy(_flanke(raw)), "Id": 213}
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is EntryStatus.AMBIGUOUS
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is None


def test_key_missing_from_the_reference_file_is_blocked_not_used(raw: dict[str, Any]) -> None:
    references = _references(raw)
    entries = {k: v for k, v in references[KIND_FUB_BASE].entries.items() if k != FUB_BASE_KEY_FLANKE}
    references[KIND_FUB_BASE] = ReferenceCatalog(KIND_FUB_BASE, "11.1.4", entries)
    check = reconcile(references, raw, "11.1.4")
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is None  # live has it, but unchecked
    assert unresolved(check, REFERENCE_REQUIRED) == [f"fub_base:{FUB_BASE_KEY_FLANKE} (not checked)"]


def test_unresolved_names_why_there_is_no_result() -> None:
    assert unresolved(None, REFERENCE_REQUIRED, REASON_CHECK_FAILED) == [
        f"fub_base:{FUB_BASE_KEY_FLANKE} ({REASON_CHECK_FAILED})"
    ]


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


def test_live_catalog_unavailable(raw: dict[str, Any]) -> None:
    references = _references(raw)
    raw["FubModules"]["5"] = []  # PHP serializes an empty mapping as an array
    check = reconcile(references, raw, "11.1.4")
    assert not check.catalogs[KIND_FUB_BASE].live_available
    assert check.fub_base_ids == frozenset()
    assert check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE) is None
    assert "fub_base live data unavailable" in check.summary()


def test_fingerprint_changes_only_with_the_result(raw: dict[str, Any]) -> None:
    references = _references(raw)
    first = reconcile(references, raw, "11.1.4").fingerprint()
    assert reconcile(references, raw, "11.1.4").fingerprint() == first
    assert reconcile(_references(raw, **{FUB_BASE_KEY_FLANKE: 1}), raw, "11.1.4").fingerprint() != first


@pytest.mark.parametrize(
    "data",
    [
        {"format": 2, "kind": KIND_FUB_BASE, "entries": {"a/d/d": 1}},
        {"format": 1, "kind": KIND_FUB_TYPES, "entries": {"a/d/d": 1}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {"a/d/d": "x"}},
        [],
    ],
)
def test_malformed_reference_is_rejected(data: Any) -> None:
    with pytest.raises(ValueError):
        parse_reference(KIND_FUB_BASE, data)


def test_build_reference_refuses_an_empty_catalog(raw: dict[str, Any]) -> None:
    raw["FubModules"]["5"] = []
    with pytest.raises(ValueError, match="no live entries"):
        build_reference(KIND_FUB_BASE, raw, "11.1.4")


def test_build_reference_refuses_non_unique_keys(raw: dict[str, Any]) -> None:
    raw["FubModules"]["5"]["213"] = {**copy.deepcopy(_flanke(raw)), "Id": 213}
    with pytest.raises(ValueError, match="not unique"):
        build_reference(KIND_FUB_BASE, raw, "11.1.4")


def test_shipped_reference_files_cover_every_extractor_and_the_flanke() -> None:
    references = load_reference_catalogs(REFERENCE_DIR)
    assert set(references) == set(LIVE_EXTRACTORS)
    assert references[KIND_FUB_BASE].entries[FUB_BASE_KEY_FLANKE] == FLANKE_ID
    for kind, key in REFERENCE_REQUIRED:
        assert key in references[kind].entries


def test_unknown_plan_refs_are_found(raw: dict[str, Any]) -> None:
    plans = {
        "19": {
            "elements": {
                "593": {"reference": {"type": "5", "ref_id": "113"}},
                "594": {"reference": {"type": "2", "ref_id": "999"}},
                "595": {"reference": {"type": "5", "ref_id": "5"}},
            }
        },
        "20": {"elements": []},
    }
    assert find_unknown_fub_base_refs(plans, [5, 6, 45]) == [("19", "593", "113")]
    assert find_unknown_fub_base_refs(plans, [5, 6, 45, 113]) == []


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


def _issue_context(blocked: list[str], refs: tuple = ()) -> IssueContext:
    return IssueContext("0.11.0-rc1", "2026.9.1", tuple(blocked), refs)


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
