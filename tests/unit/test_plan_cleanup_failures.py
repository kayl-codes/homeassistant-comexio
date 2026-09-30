"""Plan cleanup steps that did not complete count as a failed sync step: (y) coordinator, (z) button, (ag) visualize."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio.button import ComexioSyncButton, _failed_writes_note
from custom_components.comexio.const import SOURCE_CATEGORIES, WebioClass
from custom_components.comexio.coordinator import (
    PLAN_DELETE_FAILED,
    PLAN_LOAD_FAILED,
    ComexioCoordinator,
    plan_cleanup_outcome,
)
from custom_components.comexio.services import plan_actions

PLAN = "HA - Marker [1-100]"


def _delete_result(**flags: Any) -> dict[str, Any]:
    return {"deleted_elem_count": 0, "webio_cmd_ids": [], "fub_id": 5, "plan_name": PLAN, **flags}


def test_plan_cleanup_outcome_classifies_each_result() -> None:
    assert plan_cleanup_outcome(_delete_result(stop_failed=True), 5) == (None, (PLAN, 5), None)
    assert plan_cleanup_outcome(_delete_result(plan_stopped=True), 5) == ((PLAN, 5), None, None)
    assert plan_cleanup_outcome(_delete_result(plan_stopped=True, delete_failed=True), 5) == (
        (PLAN, 5),
        None,
        (PLAN, PLAN_DELETE_FAILED),
    )
    assert plan_cleanup_outcome(_delete_result(plan_stopped=False, deleted_elem_count=2), 5) == (None, None, None)


def _coordinator(api: Any, plans: dict[int, Any] | None = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = api  # type: ignore[assignment]
    coordinator.server_id = "iosrv1"
    coordinator.data = {"webio_commands": {}}
    coordinator.function_plan_plans = plans or {}
    return coordinator


def _api(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(function_plan_name=lambda fub_id: PLAN if fub_id == 5 else f"Plan {fub_id}", **kwargs)


def test_unwire_reports_a_plan_that_could_not_be_loaded() -> None:
    # (y): the preferred plan failing to load used to return an empty result — the Web-IO
    # command was deleted anyway and its plan elements stayed behind unnoticed.
    coordinator = _coordinator(_api(function_plan_load_elements=AsyncMock(return_value=None)))
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True):
        result = asyncio.run(coordinator.unwire_webio_commands([40], preferred_fub_id=5))
    assert result["failures"] == [(PLAN, PLAN_LOAD_FAILED)]
    assert result["cmd_ids"] == []


def test_unwire_reports_a_failed_element_deletion() -> None:
    api = _api(
        function_plan_load_elements=AsyncMock(return_value={"elements": {}, "connections": {}}),
        _find_webio_wiring=MagicMock(return_value=[11, 12]),
        _delete_plan_elements_and_restart=AsyncMock(return_value=_delete_result(delete_failed=True)),
    )
    coordinator = _coordinator(api)
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True):
        result = asyncio.run(coordinator.unwire_webio_commands([40], preferred_fub_id=5))
    assert result["failures"] == [(PLAN, PLAN_DELETE_FAILED)]


def test_dangling_cleanup_reports_load_delete_and_stop_outcomes() -> None:
    # (y): delete_dangling_plan_elements reported neither a plan it could not load, nor a
    # failed delete, nor a plan left stopped or not stoppable.
    elements = {"11": {"reference": {"type": 2, "ref_id": 7}}}
    api = _api(
        function_plan_load_elements=AsyncMock(return_value=None),
        _delete_plan_elements_and_restart=AsyncMock(
            side_effect=[
                _delete_result(delete_failed=True, plan_stopped=True),
                _delete_result(stop_failed=True, fub_id=6),
            ]
        ),
    )
    plans = {5: {"elements": elements, "connections": {}}, 6: {"elements": elements, "connections": {}}}
    coordinator = _coordinator(api, plans)
    with (
        patch.object(ComexioCoordinator, "_function_plan_check_fub_ids", return_value=[5, 6, 9]),
        patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True),
        patch.object(ComexioCoordinator, "_plan_connected_elem_ids", return_value=set()),
    ):
        result = asyncio.run(coordinator.delete_dangling_plan_elements("2", ["7"]))
    assert result["failures"] == [("Plan 9", PLAN_LOAD_FAILED), (PLAN, PLAN_DELETE_FAILED)]
    assert result["stopped_plans"] == [(PLAN, 5)]
    assert result["stop_failures"] == [(PLAN, 6)]
    assert result["deleted_elem_count"] == 0
    assert result["touched_fub_ids"] == []


def _ctx(api: Any = None) -> SimpleNamespace:
    return SimpleNamespace(failed_writes=[], api=api or MagicMock())


def _button(**coordinator: Any) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(cancel_sync=False, **coordinator)  # type: ignore[assignment]
    button.hass = MagicMock()
    button.server_id = "iosrv1"
    return button


def test_delta_debris_failures_are_failed_steps() -> None:
    # (z): stopped plans and stop failures of the delta cleanup were discarded. A stopped plan
    # that gets re-sorted is left to the sort, which reactivates it (or reports that it could not).
    outcome = {"touched_fub_ids": [5], "deleted_elem_count": 2, "cmd_ids": []}
    button = _button(
        unwire_webio_commands=AsyncMock(
            return_value={**outcome, "stopped_plans": [(PLAN, 5), ("P8", 8)], "stop_failures": [], "failures": []}
        ),
        delete_dangling_plan_elements=AsyncMock(
            return_value={**outcome, "stopped_plans": [], "stop_failures": [("P6", 6)], "failures": [("P7", "x")]}
        ),
    )
    ctx = _ctx()
    tasks = [{"type": "delete", "item": {"webIoId": 40}}]
    resort, removed = asyncio.run(button._cleanup_delta_debris(ctx, WebioClass.MARKER, tasks, [{"ref_id": "7"}]))
    assert (resort, removed) == ({5}, 4)
    assert ctx.failed_writes == [
        "function plan 'P8': left stopped after cleanup",
        "function plan 'P6': not cleaned up (not stopped)",
        "function plan 'P7': x",
    ]


def _sort(button: ComexioSyncButton, ctx: SimpleNamespace, sort_res: Any, **kwargs: Any) -> str:
    with patch("custom_components.comexio.button.async_sort_function_plan", AsyncMock(return_value=sort_res)):
        return asyncio.run(button._sort_checked(ctx, 5, PLAN, **kwargs))


def test_failed_sort_is_a_failed_step() -> None:
    # (z): "sort failed" only showed up in the summary line, the sensor stayed "idle".
    ctx = _ctx(SimpleNamespace(function_plan_run_fup=AsyncMock(return_value=True)))
    note = _sort(_button(), ctx, {"success": False, "activated": False, "duration": 1.0}, was_active=True)
    assert "sort failed" in note
    assert ctx.failed_writes == [f"function plan '{PLAN}': sort failed"]
    ctx.api.function_plan_run_fup.assert_awaited_once_with(5)


def test_sort_without_result_is_a_failed_step() -> None:
    # None: the plan could not be loaded (a managed plan always keeps its template comment).
    ctx = _ctx(SimpleNamespace(function_plan_run_fup=AsyncMock(return_value=False)))
    _sort(_button(), ctx, None, was_active=True)
    assert ctx.failed_writes == [f"function plan '{PLAN}': sort failed", f"function plan '{PLAN}': not activated"]


def test_repeated_failures_count_once() -> None:
    note = _failed_writes_note(["function plan 'P': x", "function plan 'P': x", "create M1"])
    assert "2 sync step(s) failed" in note


def test_successful_sort_leaves_no_failed_step() -> None:
    ctx = _ctx()
    note = _sort(_button(), ctx, {"success": True, "activated": True, "duration": 1.25}, was_active=True)
    assert note == ", sorted in 1.2s"
    assert not ctx.failed_writes


def test_cleanup_plan_that_could_not_be_loaded_is_a_failed_step() -> None:
    # (y): the ignored-source cleanup skipped a plan it could not load without a word.
    button = _button(
        resolve_source_cleanup_plans=AsyncMock(return_value={5: [7]}),
        plan_load_failures=MagicMock(return_value=[]),
        async_function_plan_change_backup=AsyncMock(),
    )
    ctx = _ctx(_api(function_plan_load_elements=AsyncMock(return_value=None)))
    category = SOURCE_CATEGORIES[WebioClass.MARKER]
    result = asyncio.run(button._cleanup_function_plan_plans(ctx, [7], None, category))
    assert result == (0, [], [], [])
    assert ctx.failed_writes == [f"function plan '{PLAN}': {PLAN_LOAD_FAILED}"]


def test_visualize_text_returns_a_response() -> None:
    # (ag): the text format returned None, which HA rejects when a response is requested (HTTP 500).
    coordinator = SimpleNamespace(
        function_plan_label_maps=lambda: ({}, {}, {}),
        function_plan_catalog=SimpleNamespace(async_get_catalog=AsyncMock(return_value={})),
    )
    source = (coordinator, MagicMock(), 5, PLAN, {}, {}, "snapshot:auto:1", None)
    call = SimpleNamespace(data={"format": "text", "snapshot": "5:auto:1:" + PLAN})
    with (
        patch.object(plan_actions, "_resolve_visualize_snapshot_source", AsyncMock(return_value=source)),
        patch.object(plan_actions.persistent_notification, "async_create") as notify,
    ):
        response = asyncio.run(plan_actions.handle_function_plan_visualize(MagicMock(), call))
    assert response is not None
    assert response["plan_name"] == PLAN
    assert response["connections"] == 0
    assert response["text"] == notify.call_args.args[1]


def test_visualize_failure_raises_when_a_response_is_requested() -> None:
    call = SimpleNamespace(data={"format": "text"}, return_response=True)
    with (
        patch.object(plan_actions, "_resolve_visualize_live_source", AsyncMock(return_value=None)),
        pytest.raises(HomeAssistantError),
    ):
        asyncio.run(plan_actions.handle_function_plan_visualize(MagicMock(), call))
