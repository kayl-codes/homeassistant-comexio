"""marker_delete placement gate: every plan is loaded before each delete, placed markers stay protected."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioError
import pytest

from custom_components.comexio.api import _MARKER_CONFIG_UNREADABLE, _PLACEMENT_UNKNOWN, ComexioAPI
from custom_components.comexio.services import marker_actions
from tests.common import load_json_fixture

PLACED_REASON = "placed in function plan Test4"


def _scenario(comexio_api: ComexioAPI, plans_ok: bool = True) -> ComexioAPI:
    """The API on marker_delete_config.json; plans_ok=False makes plan 2 fail to load."""
    data = load_json_fixture("marker_delete_config.json")
    plans = {int(fid): plan for fid, plan in data["plans"].items()}
    if not plans_ok:
        del plans[2]
    comexio_api.get_raw_config = AsyncMock(return_value=data["config"])  # type: ignore[method-assign]
    comexio_api.function_plan_load_all_plans = AsyncMock(return_value=plans)  # type: ignore[method-assign]
    comexio_api.function_plan_load_elements = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return comexio_api


def _eligibility(api: ComexioAPI, ids: list[int], force: bool = False) -> tuple:
    return asyncio.run(api.get_marker_delete_eligibility(ids, force=force))


def test_a_placed_api_created_marker_is_protected_without_force(comexio_api: ComexioAPI) -> None:
    """Regression (live test 2026-10-16): M359, created in the plan editor (CategoryId 1) and
    wired in Test4, was deleted without force — the placement was only checked with force."""
    deletable, protected, categories, gate_error = _eligibility(_scenario(comexio_api), [359])
    assert deletable == []
    assert protected == {359: PLACED_REASON}
    assert categories[359] == 1
    assert gate_error is None


def test_force_never_opens_a_placed_marker(comexio_api: ComexioAPI) -> None:
    deletable, protected, _categories, gate_error = _eligibility(_scenario(comexio_api), [4, 359], force=True)
    assert deletable == []
    assert protected == {4: PLACED_REASON, 359: PLACED_REASON}
    assert gate_error is None


def test_an_unplaced_api_created_marker_stays_deletable(comexio_api: ComexioAPI) -> None:
    # M300 is only referenced as an IO id (type 1) in Test4 — not a marker placement.
    assert _eligibility(_scenario(comexio_api), [300]) == ([300], {}, _categories(), None)


def test_force_opens_only_unplaced_untitled_studio_markers(comexio_api: ComexioAPI) -> None:
    deletable, protected, _categories, _gate_error = _eligibility(_scenario(comexio_api), [1, 3, 4, 7], force=True)
    assert deletable == [3, 7]  # M7 absent: delete_marker reports it as already gone
    assert protected == {1: "factory/Studio marker with a title", 4: PLACED_REASON}


def test_a_marker_in_several_plans_names_all_of_them(comexio_api: ComexioAPI) -> None:
    _deletable, protected, _categories, _gate_error = _eligibility(_scenario(comexio_api), [360])
    assert protected == {360: "placed in function plans Test4, Lights"}


def test_a_plan_that_cannot_be_loaded_refuses_every_id(comexio_api: ComexioAPI) -> None:
    deletable, protected, _categories, gate_error = _eligibility(_scenario(comexio_api, plans_ok=False), [3, 300, 7])
    assert deletable == []
    assert protected == dict.fromkeys([3, 300, 7], _PLACEMENT_UNKNOWN)
    assert gate_error == _PLACEMENT_UNKNOWN


def test_a_missing_plan_list_refuses_every_id(comexio_api: ComexioAPI) -> None:
    api = _scenario(comexio_api)
    config = load_json_fixture("marker_delete_config.json")["config"]
    del config["Fubs"]
    api.get_raw_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
    assert _eligibility(api, [300]) == ([], {300: _PLACEMENT_UNKNOWN}, _categories(), _PLACEMENT_UNKNOWN)


def test_an_unreadable_reference_in_a_loaded_plan_refuses_every_id(comexio_api: ComexioAPI) -> None:
    """A skipped reference could be the placement of the requested marker — placement is unknown then."""
    api = _scenario(comexio_api)
    plans = api.function_plan_load_all_plans.return_value
    plans[2]["elements"]["21"] = {"reference": {"type": "x", "ref_id": 300}}
    assert _eligibility(api, [300]) == ([], {300: _PLACEMENT_UNKNOWN}, _categories(), _PLACEMENT_UNKNOWN)


def test_a_failed_bulk_load_refuses_at_once(comexio_api: ComexioAPI) -> None:
    """Regression (Sourcery, PR #186): a bulk failure loaded every plan one by one before refusing."""
    api = _scenario(comexio_api)
    api.function_plan_load_all_plans = AsyncMock(side_effect=ComexioError("timeout"))  # type: ignore[method-assign]
    assert _eligibility(api, [300]) == ([], {300: _PLACEMENT_UNKNOWN}, _categories(), _PLACEMENT_UNKNOWN)
    api.function_plan_load_elements.assert_not_awaited()


@pytest.mark.parametrize("config", [{}, {"FubModules": {"2": {}}}])
def test_an_unreadable_marker_config_refuses_every_id(comexio_api: ComexioAPI, config: dict) -> None:
    api = _scenario(comexio_api)
    api.get_raw_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
    assert _eligibility(api, [300, 7]) == (
        [],
        dict.fromkeys([300, 7], _MARKER_CONFIG_UNREADABLE),
        {},
        _MARKER_CONFIG_UNREADABLE,
    )


@pytest.mark.parametrize(
    ("fubs_entry", "cached_entry", "shown"),
    [
        ({"Id": 1, "Name": "  Test4 "}, None, "Test4"),
        ({"Id": 1, "Name": "   "}, {"Id": 1, "Name": "Cached"}, "Cached"),
        ({"Id": 1}, None, "#1"),
        ("malformed", None, "#1"),
    ],
)
def test_the_plan_name_falls_back_to_the_cache_then_the_id(
    comexio_api: ComexioAPI, fubs_entry: Any, cached_entry: Any, shown: str
) -> None:
    api = _scenario(comexio_api)
    config = load_json_fixture("marker_delete_config.json")["config"]
    config["Fubs"]["1"] = fubs_entry
    api.get_raw_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
    if cached_entry is not None:
        # _load_all_plans_verified makes $Fubs the cache; a plan only the cache names keeps its name there.
        api._replace_fub_data = lambda fubs, *_args: setattr(api, "_fub_data", {**fubs, "1": cached_entry})  # type: ignore[method-assign]
    assert _eligibility(api, [359])[1] == {359: f"placed in function plan {shown}"}


def test_two_plans_of_the_same_name_are_both_listed(comexio_api: ComexioAPI) -> None:
    api = _scenario(comexio_api)
    config = load_json_fixture("marker_delete_config.json")["config"]
    config["Fubs"]["2"]["Name"] = "Test4"
    api.get_raw_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
    assert _eligibility(api, [360])[1] == {360: "placed in function plans Test4, Test4"}


def test_an_unplaced_marker_of_unknown_category_stays_protected_with_force(comexio_api: ComexioAPI) -> None:
    api = _scenario(comexio_api)
    config = load_json_fixture("marker_delete_config.json")["config"]
    config["FubModules"]["2"]["3"]["CategoryId"] = 2
    api.get_raw_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
    assert _eligibility(api, [3], force=True)[:2] == ([], {3: "unknown CategoryId 2"})


def _categories() -> dict[int, Any]:
    markers = load_json_fixture("marker_delete_config.json")["config"]["FubModules"]["2"]
    return {int(mid): record["CategoryId"] for mid, record in markers.items()}


def _run_delete(
    api: ComexioAPI, marker_id: str, force: bool = False, results: dict[int, bool | None] | None = None
) -> tuple[dict, str, str, AsyncMock]:
    """Run the delete step against the scenario; returns (response, notification text, title, delete mock).

    results: delete_marker's answer per id (default True).
    """
    answers = results or {}
    api.delete_marker = AsyncMock(side_effect=lambda mid: answers.get(mid, True))  # type: ignore[method-assign]
    coordinator = SimpleNamespace(async_request_refresh=AsyncMock())
    call = SimpleNamespace(data={"marker_id": marker_id, "confirm": True, "force": force})
    marker_ids, _invalid = marker_actions._parse_marker_id_list(marker_id)
    with patch.object(marker_actions.persistent_notification, "async_create") as notify:
        response = asyncio.run(
            marker_actions._async_delete_eligible_markers(MagicMock(), call, coordinator, api, marker_id, marker_ids)
        )
    notify.assert_called_once()
    return response, notify.call_args.args[1], notify.call_args.kwargs["title"], api.delete_marker


def test_the_notification_lists_deleted_and_protected_ids_with_reason(comexio_api: ComexioAPI) -> None:
    response, message, title, delete = _run_delete(_scenario(comexio_api), "300,359")
    delete.assert_awaited_once_with(300)
    assert "Deleted: M300" in message
    assert f"- M359: {PLACED_REASON}" in message
    assert title == "Marker Delete — 1/2 OK, 1 protected"
    assert response["deleted"] == [300]
    assert response["protected"] == [359]
    assert response["protected_reasons"] == {359: PLACED_REASON}
    assert response["protected_reason"] == f"M359: {PLACED_REASON}"
    assert response["error"] is None


def test_a_protected_only_run_deletes_nothing_and_names_the_plan(comexio_api: ComexioAPI) -> None:
    response, message, title, delete = _run_delete(_scenario(comexio_api), "359")
    delete.assert_not_awaited()
    assert message.startswith("Deleted 0 of 1 requested marker(s).")
    assert PLACED_REASON in message
    assert title == "Marker Delete — 0/1 OK, 1 protected"
    assert response["error"] is None


def test_an_unloadable_plan_is_reported_as_an_error_and_deletes_nothing(comexio_api: ComexioAPI) -> None:
    response, message, title, delete = _run_delete(_scenario(comexio_api, plans_ok=False), "300", force=True)
    delete.assert_not_awaited()
    assert f"Protected, not deleted: M300 — {_PLACEMENT_UNKNOWN}" in message
    assert title.endswith("(errors)")
    assert response["error"] == _PLACEMENT_UNKNOWN


def test_the_log_line_names_each_markers_category(comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger=marker_actions.__name__)
    _run_delete(_scenario(comexio_api), "300,359,7")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Marker Delete:"))
    assert "deleting 2 marker(s): M7 (absent), M300 (CategoryId 1);" in line
    assert f"protected: M359 (CategoryId 1): {PLACED_REASON}" in line


def test_a_mixed_run_reports_every_outcome(comexio_api: ComexioAPI) -> None:
    # M3 (present) answered False -> failed; M7 (absent) answered False -> already absent.
    response, message, title, _delete = _run_delete(
        _scenario(comexio_api), "3,7,300,359", force=True, results={3: False, 7: False}
    )
    assert (response["deleted"], response["skipped"], response["failed"]) == ([300], [7], [3])
    assert response["protected"] == [359]
    assert "Deleted: M300" in message
    assert "Not deleted (already absent): M7" in message
    assert f"- M359: {PLACED_REASON}" in message
    assert "marker may still exist, see log): M3" in message
    assert title == "Marker Delete — 1/4 OK, 1 protected (errors)"


def test_a_gate_error_fills_every_response_key(comexio_api: ComexioAPI) -> None:
    response, _message, _title, _delete = _run_delete(_scenario(comexio_api, plans_ok=False), "300,359")
    assert response["protected"] == [300, 359]
    assert response["protected_reasons"] == dict.fromkeys([300, 359], _PLACEMENT_UNKNOWN)
    assert response["protected_reason"] == _PLACEMENT_UNKNOWN


def test_the_log_line_marks_unchecked_ids_when_the_config_is_unreadable(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    api = _scenario(comexio_api)
    api.get_raw_config = AsyncMock(return_value={})  # type: ignore[method-assign]
    caplog.set_level("INFO", logger=marker_actions.__name__)
    _run_delete(api, "300")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Marker Delete:"))
    assert f"protected: M300 (unchecked): {_MARKER_CONFIG_UNREADABLE}" in line
