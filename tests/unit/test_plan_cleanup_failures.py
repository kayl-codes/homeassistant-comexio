"""Plan cleanup steps that did not complete count as a failed sync step: (y) coordinator, (z) button, (ag) visualize."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import button as button_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import ComexioSyncButton, _failed_writes_note
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    ICON_CHECK,
    ICON_INFO,
    IO_PLAN_KIND_QUICKBUS,
    PLAN_FAILURE_NOT_RESOLVED,
    PLAN_FAILURE_OFF_CANVAS,
    QUICKBUS_IO_DATA_KEY,
    SOURCE_CATEGORIES,
    WebioClass,
)
from custom_components.comexio.coordinator import (
    COMMAND_NOT_CHECKED,
    PLAN_CMD_NOT_FOUND,
    PLAN_DELETE_FAILED,
    PLAN_ENTRY_UNREADABLE,
    PLAN_LIST_NAME,
    PLAN_LIST_NOT_READ,
    PLAN_LOAD_FAILED,
    PLAN_WEBIO_ID_UNREADABLE,
    ComexioCoordinator,
    SourceCommandCheck,
    plan_cleanup_outcome,
)
from custom_components.comexio.services import plan_actions

PLAN = "HA - Marker [1-100]"
TRIGGER_FUB_ID = 39


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
    coordinator.scraped_plan_ids = None
    coordinator.config_entry = SimpleNamespace(  # type: ignore[assignment]
        options={CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: TRIGGER_FUB_ID}}
    )
    return coordinator


def _plan_name(fub_id: int) -> str:
    return {5: PLAN, TRIGGER_FUB_ID: FUNCTION_PLAN_TRIGGER_PLAN_NAME}.get(fub_id, f"Plan {fub_id}")


def _api(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(function_plan_name=_plan_name, **{"fub_data": {}, **kwargs})


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


def test_unwire_reports_a_web_io_command_it_cannot_resolve() -> None:
    # (an): the command stays in Comexio after its wiring was removed — only logged before.
    api = _api(
        function_plan_load_elements=AsyncMock(return_value={"elements": {}, "connections": {}}),
        _find_webio_wiring=MagicMock(return_value=[11, 12]),
        _delete_plan_elements_and_restart=AsyncMock(
            return_value=_delete_result(deleted_elem_count=4, webio_cmd_ids=[40, 41])
        ),
    )
    coordinator = _coordinator(api)
    coordinator.data = {"webio_commands": {"a": {"webIoId": 40, "cmdId": 900}}}
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True):
        result = asyncio.run(coordinator.unwire_webio_commands([40, 41], preferred_fub_id=5))
    assert result["cmd_ids"] == [900]
    assert result["failures"] == [(PLAN, f"{PLAN_CMD_NOT_FOUND} (webIoId 41)")]


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
    assert result["inactive_fub_ids"] == []


NOTHING_DELETED = {
    "deleted_elem_count": 0,
    "cmd_ids": [],
    "stopped_plan": None,
    "stop_failure": None,
    "failures": [],
    "touched": False,
    "was_running": True,
}


def _ctx(api: Any = None) -> SimpleNamespace:
    return SimpleNamespace(failed_writes=[], api=api or MagicMock())


def _button(**coordinator: Any) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    defaults = {
        "cancel_sync": False,
        "webio_id_to_cmd_id": dict,
        "delete_plan_elements_for_commands": AsyncMock(return_value=dict(NOTHING_DELETED)),
        "deletable_source_command_ids": AsyncMock(return_value=SourceCommandCheck([], [], [], [])),
    }
    button.coordinator = SimpleNamespace(**(defaults | coordinator))  # type: ignore[assignment]
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
    assert (resort, removed) == ({5: True}, 4)
    assert ctx.failed_writes == [
        "function plan 'P8': left stopped after cleanup",
        "function plan 'P6': not cleaned up (could not be stopped)",
        "function plan 'P7': x",
    ]


def test_plan_that_was_not_running_is_resorted_as_inactive() -> None:
    # (am) review: a stopped plan is cleaned up now, so it reaches the re-sort — which must not start it.
    # Plan 5 was running and the unwire could not restart it; the dangling cleanup then finds it
    # stopped. That must not turn it into "was not running" — the re-sort has to restart it.
    button = _button(
        unwire_webio_commands=AsyncMock(
            return_value={
                "touched_fub_ids": [5, 6],
                "inactive_fub_ids": [6],
                "deleted_elem_count": 4,
                "cmd_ids": [],
                "stopped_plans": [(PLAN, 5)],
                "stop_failures": [],
                "failures": [],
            }
        ),
        delete_dangling_plan_elements=AsyncMock(
            return_value={
                "touched_fub_ids": [5, 6],
                "inactive_fub_ids": [5, 6],
                "deleted_elem_count": 1,
                "stopped_plans": [],
                "stop_failures": [],
                "failures": [],
            }
        ),
    )
    ctx = _ctx()
    tasks = [{"type": "delete", "item": {"webIoId": 40}}]
    resort, _ = asyncio.run(button._cleanup_delta_debris(ctx, WebioClass.MARKER, tasks, [{"ref_id": "7"}]))
    assert resort == {5: True, 6: False}
    assert not ctx.failed_writes


def test_unwire_marks_a_plan_that_was_not_running() -> None:
    api = _api(
        function_plan_load_elements=AsyncMock(return_value={"elements": {}, "connections": {}}),
        _find_webio_wiring=MagicMock(return_value=[11, 12]),
        _delete_plan_elements_and_restart=AsyncMock(
            return_value=_delete_result(deleted_elem_count=2, plan_stopped=False, was_running=False)
        ),
    )
    coordinator = _coordinator(api)
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True):
        result = asyncio.run(coordinator.unwire_webio_commands([40], preferred_fub_id=5))
    assert result["touched_fub_ids"] == [5]
    assert result["inactive_fub_ids"] == [5]
    assert result["stopped_plans"] == []
    assert result["failures"] == []


def test_sort_of_an_inactive_plan_does_not_start_it() -> None:
    ctx = _ctx(SimpleNamespace(function_plan_run_fup=AsyncMock(return_value=True)))
    note = _sort(_button(), ctx, {"success": True, "activated": False, "duration": 1.0}, was_active=False)
    assert note == ", sorted in 1.0s"
    assert not ctx.failed_writes
    ctx.api.function_plan_run_fup.assert_not_awaited()


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


@pytest.mark.parametrize("reason", ["QB3 no longer fit its 1 columns", "No elements in plan."])
def test_skipped_sort_is_a_failed_step_with_its_reason(reason: str) -> None:
    ctx = _ctx(SimpleNamespace(function_plan_run_fup=AsyncMock(return_value=True)))
    sort_res = {"success": False, "skipped": reason, "activated": False}

    note = _sort(_button(), ctx, sort_res, was_active=True)

    assert "sort skipped" in note
    assert ctx.failed_writes == [f"function plan '{PLAN}': sort skipped — {reason}"]
    ctx.api.function_plan_run_fup.assert_awaited_once_with(5)  # the skip stopped nothing, but keeps it running


def test_skipped_sort_of_an_inactive_plan_leaves_it_stopped() -> None:
    ctx = _ctx(SimpleNamespace(function_plan_run_fup=AsyncMock(return_value=True)))
    sort_res = {"success": False, "skipped": "No elements in plan.", "activated": False}

    _sort(_button(), ctx, sort_res, was_active=False)

    assert ctx.failed_writes == [f"function plan '{PLAN}': sort skipped — No elements in plan."]
    ctx.api.function_plan_run_fup.assert_not_awaited()


@pytest.mark.parametrize(("sort_line", "skipped"), [("QB plan sort skipped", "QB plan sort skipped"), ("", None)])
@pytest.mark.parametrize("notify", [True, False])
def test_sort_that_places_nothing_returns_why_it_skipped(sort_line: str, skipped: str | None, notify: bool) -> None:
    # None stays reserved for a plan that could not be loaded; _sort_checked reports the reason.
    api = SimpleNamespace(fub_data={}, function_plan_load_elements=AsyncMock(return_value={"elements": [1]}))
    with (
        patch.object(plan_actions, "_sort_row_step", return_value=50),
        patch.object(plan_actions, "_sort_canvas_bounds", return_value=("A3", 1230, 20, 3)),
        patch.object(plan_actions, "_sort_compute_positions", return_value=([], 0, 0, sort_line, [], None)),
        patch.object(plan_actions.persistent_notification, "async_create") as create,
    ):
        result = asyncio.run(plan_actions.async_sort_function_plan(MagicMock(), MagicMock(), api, 5, notify=notify))

    expected = skipped or "No elements in plan."
    assert result == {"success": False, "skipped": expected, "activated": False}
    assert create.call_count == int(notify)


@pytest.mark.parametrize("reason", [PLAN_FAILURE_OFF_CANVAS, PLAN_FAILURE_NOT_RESOLVED])
def test_unwired_cluster_member_is_a_failed_step_with_its_reason(reason: str) -> None:
    button = _button(
        resolve_io_clusters=AsyncMock(return_value=({}, set(), {"2": reason})),
        data={QUICKBUS_IO_DATA_KEY: [{"quickbus": {"device_id": 2, "name": "MT1"}}]},
    )
    ctx = _ctx()

    summary, added, errors = asyncio.run(
        button._wire_io_kind_clusters(ctx, IO_PLAN_KIND_QUICKBUS, {"2": ["G1"]}, {})  # type: ignore[arg-type]
    )

    # The device as the user knows it, not its bare id.
    label = IO_PLAN_KIND_QUICKBUS.label
    assert ctx.failed_writes == [f"{label} cluster plan for 'QB2 MT1': {reason}"]
    assert len(summary) == 1
    assert reason in summary[0]
    assert "'QB2 MT1'" in summary[0]
    assert (added, errors) == (0, 1)


def test_no_cluster_plan_at_all_is_one_failed_step() -> None:
    button = _button(resolve_io_clusters=AsyncMock(return_value=({}, set(), {})))
    ctx = _ctx()

    summary, added, errors = asyncio.run(
        button._wire_io_kind_clusters(ctx, IO_PLAN_KIND_QUICKBUS, {"QB2": ["G1"]}, {})  # type: ignore[arg-type]
    )

    assert ctx.failed_writes == [f"{IO_PLAN_KIND_QUICKBUS.label} cluster plan: none available"]
    assert len(summary) == 1
    assert (added, errors) == (0, 1)


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
    result = asyncio.run(button._cleanup_function_plan_plans(ctx, [7], category))
    assert result == (0, [], [], [], [])
    assert ctx.failed_writes == [f"function plan '{PLAN}': {PLAN_LOAD_FAILED}"]
    # A degenerate payload must count as a failed load, not as a plan the source left.
    ctx.api.function_plan_load_elements.assert_awaited_once_with(5, strict=True)


def test_cleanup_reports_a_plan_the_scan_could_not_load() -> None:
    # (u)/(bi): with the cable pulled the scan finds no plan at all — that must not read as clean.
    async def _resolve(_ids: list[int], _ref_type: int, load_failures: list[int]) -> dict[int, list[int]]:
        load_failures.append(5)
        return {}

    button = _button(
        resolve_source_cleanup_plans=_resolve,
        plan_load_failures=lambda ids: [(PLAN, PLAN_LOAD_FAILED)] if ids == [5] else [],
    )
    ctx = _ctx()
    result = asyncio.run(button._cleanup_function_plan_plans(ctx, [7], SOURCE_CATEGORIES[WebioClass.MARKER]))
    assert result == (0, [], [], [], [])
    assert ctx.failed_writes == [f"function plan '{PLAN}': {PLAN_LOAD_FAILED}"]


def _marker_element(marker_id: int) -> dict[str, Any]:
    return {"reference": {"type": 2, "ref_id": marker_id}}


def test_cleanup_finds_every_managed_plan_holding_the_source() -> None:
    # (bi): the cleanup took the plan selector's plan instead of the plan the marker sits in,
    # found nothing there and still reported success. Here the marker sits in its cluster
    # plan 36, in a stray duplicate in plan 37 and in the user's own plan 5 (never touched).
    # (bj): the trigger plan is left to the trigger-pair audit, which removes the [TRIG]
    # source together with its Flanke — the generic cleanup would keep it and re-flag it.
    plans = {
        5: {"elements": {"1": _marker_element(253)}},
        36: {"elements": {"1460": _marker_element(253), "1470": _marker_element(254)}},
        37: {"elements": {"2": _marker_element(253)}},
        TRIGGER_FUB_ID: {"elements": {"4": _marker_element(253)}},
        42: {"elements": {"3": _marker_element(7)}},
    }

    memos: list[Any] = []

    async def _load(load_failures: list[int] | None = None, *, memo: dict[int, dict] | None = None) -> dict[int, Any]:
        memos.append(memo)
        if load_failures is not None:
            load_failures.append(38)  # a managed plan that could not be loaded
        return plans

    coordinator = _coordinator(_api(_find_source_element_id=ComexioAPI._find_source_element_id))
    coordinator._load_function_plan_check_data = _load  # type: ignore[method-assign]
    coordinator._audit_plan_memo = {}  # an open audit block must not hand the cleanup its plans
    load_failures: list[int] = []
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", lambda _self, fub_id: fub_id != 5):
        result = asyncio.run(coordinator.resolve_source_cleanup_plans([253, 254], load_failures=load_failures))
    assert result == {36: [253, 254], 37: [253]}
    assert load_failures == [38]
    assert memos == [None]  # the cleanup loads live


def test_cleanup_scans_a_plan_behind_a_stale_trigger_mapping() -> None:
    # The mapping still names fub 39, but Comexio reused the id for another managed plan: the
    # trigger audit skips it (name mismatch), so the ignored-source scan must not skip it too.
    coordinator = _coordinator(
        SimpleNamespace(
            function_plan_name=lambda fub_id: "HA - Marker [200-300]",
            fub_data={},
            _find_source_element_id=ComexioAPI._find_source_element_id,
        )
    )
    coordinator._load_function_plan_check_data = AsyncMock(  # type: ignore[method-assign]
        return_value={TRIGGER_FUB_ID: {"elements": {"4": _marker_element(253)}}}
    )
    with patch.object(ComexioCoordinator, "_is_managed_function_plan", return_value=True):
        result = asyncio.run(coordinator.resolve_source_cleanup_plans([253]))
    assert result == {TRIGGER_FUB_ID: [253]}


def _plan_cleanup_button(plan_data: dict[str, Any], **coordinator: Any) -> tuple[ComexioSyncButton, SimpleNamespace]:
    button = _button(
        resolve_source_cleanup_plans=AsyncMock(return_value={5: [7]}),
        plan_load_failures=MagicMock(return_value=[]),
        async_function_plan_change_backup=AsyncMock(),
        **coordinator,
    )
    ctx = _ctx(
        _api(
            function_plan_load_elements=AsyncMock(return_value=plan_data),
            _find_source_element_ids=ComexioAPI._find_source_element_ids,
            _source_element_wiring=ComexioAPI._source_element_wiring,
        )
    )
    return button, ctx


def _conn(source: str, *targets: str) -> dict[str, Any]:
    return {"input": {"FubElementId": source}, "output": [{"FubElementId": target} for target in targets]}


def _web_io_element(webio_id: Any) -> dict[str, Any]:
    return {"reference": {"type": 10, "ref_id": webio_id}}


LOGIC_BLOCK = {"reference": {"type": 5, "ref_id": 1}}
MIXED_PLAN = {
    "elements": {"11": _marker_element(7), "12": LOGIC_BLOCK, "13": _web_io_element(40)},
    "connections": {"0": _conn("11", "12", "13")},
}

KEPT_LINE = (
    f"⚠️ M7 stays in Function Plan '{PLAN}' (ID 5) — it is wired to other plan logic there, "
    "or its plan data could not be read (see the log); the cleanup leaves it in place"
)
GONE_LINE = f"💡 M7 is no longer in Function Plan '{PLAN}' (ID 5) — nothing to remove there"


@pytest.mark.parametrize(
    ("plan_data", "selected"),
    [
        # (bj): a source that fed a WebIO AND other logic lost its element — and with it that logic.
        (MIXED_PLAN, ([13], [40], [7], [])),
        # (bj): only the first element of a source was cleaned up, a stray duplicate stayed wired.
        (
            {
                "elements": {
                    "11": _marker_element(7),
                    "13": _web_io_element(40),
                    "21": _marker_element(7),
                    "23": _web_io_element("41"),
                },
                "connections": {"0": _conn("11", "13"), "1": _conn("21", "23")},
            },
            ([13, 11, 23, 21], [40, 41], [], []),
        ),
        ({"elements": {"11": _marker_element(7)}, "connections": {}}, ([11], [], [], [])),
        # Wired into the source counts as logic too.
        (
            {"elements": {"11": _marker_element(7), "12": LOGIC_BLOCK}, "connections": {"0": _conn("12", "11")}},
            ([], [], [7], []),
        ),
        # A WebIO element without a readable webIoId cannot be resolved to its command: it stays.
        (
            {
                "elements": {"11": _marker_element(7), "13": _web_io_element("x")},
                "connections": {"0": _conn("11", "13")},
            },
            ([], [], [7], []),
        ),
        # A WebIO element another connection uses too is shared plan logic: it stays with its command.
        (
            {
                "elements": {"11": _marker_element(7), "12": LOGIC_BLOCK, "13": _web_io_element(40)},
                "connections": {"0": _conn("11", "13"), "1": _conn("13", "12")},
            },
            ([], [], [7], []),
        ),
        (
            {
                "elements": {"11": _marker_element(7), "12": _marker_element(8), "13": _web_io_element(40)},
                "connections": {"0": _conn("11", "13"), "1": _conn("12", "13")},
            },
            ([], [], [7], []),
        ),
        # A connection whose ends cannot be read may touch the source or its WebIO: everything stays.
        *(
            (
                {
                    "elements": {"11": _marker_element(7), "13": _web_io_element(40)},
                    "connections": {"0": _conn("11", "13"), "1": broken},
                },
                ([], [], [7], []),
            )
            for broken in (
                "broken",
                {"input": None, "output": [{"FubElementId": "13"}]},
                {"input": {}, "output": [{"FubElementId": "13"}]},
                {"input": {"FubElementId": None}, "output": [{"FubElementId": "13"}]},
                {"input": {"FubElementId": "11"}, "output": [{"IOPos": 0}]},
                {"input": {"FubElementId": "11"}, "output": ["x"]},
                {"input": {"FubElementId": "11"}, "output": 5},
                {"input": {"FubElementId": "11"}},
                {"input": {"FubElementId": "11"}, "output": None},
            )
        ),
        # An empty target list feeds nothing: a readable connection, nothing to keep.
        (
            {
                "elements": {"11": _marker_element(7), "13": _web_io_element(40)},
                "connections": {"0": _conn("11", "13"), "1": {"input": {"FubElementId": "11"}, "output": []}},
            },
            ([13, 11], [40], [], []),
        ),
        # An element id that is not a number cannot be deleted by id: that element stays, the rest goes.
        (
            {
                "elements": {"a1": _marker_element(7), "13": _web_io_element(40)},
                "connections": {"0": _conn("a1", "13")},
            },
            ([13], [40], [7], []),
        ),
        (
            {
                "elements": {"11": _marker_element(7), "w3": _web_io_element(40)},
                "connections": {"0": _conn("11", "w3")},
            },
            ([], [], [7], []),
        ),
        # int() would read these as element 11 / 13 — another element's id: they stay as well.
        (
            {
                "elements": {"011": _marker_element(7), "13": _web_io_element(40)},
                "connections": {"0": _conn("011", "13")},
            },
            ([13], [40], [7], []),
        ),
        (
            {
                "elements": {"11": _marker_element(7), "١٣": _web_io_element(40)},
                "connections": {"0": _conn("11", "١٣")},
            },
            ([], [], [7], []),
        ),
        # Comexio may serialize the targets as a dict: read the same as the list.
        (
            {
                "elements": {"11": _marker_element(7), "13": _web_io_element(40)},
                "connections": {"0": {"input": {"FubElementId": "11"}, "output": {"0": {"FubElementId": "13"}}}},
            },
            ([13, 11], [40], [], []),
        ),
        ({"elements": {"11": _marker_element(8)}, "connections": {}}, ([], [], [], [7])),
    ],
    ids=[
        "web-io-and-logic",
        "stray-duplicate",
        "unwired",
        "fed-by-logic",
        "unreadable-web-io-id",
        "web-io-feeds-logic",
        "web-io-fed-twice",
        "connection-not-a-dict",
        "connection-start-none",
        "connection-start-empty",
        "connection-start-id-none",
        "connection-target-without-id",
        "connection-target-not-a-dict",
        "connection-targets-not-a-list",
        "connection-targets-missing",
        "connection-targets-none",
        "connection-without-targets",
        "source-element-id-not-a-number",
        "web-io-element-id-not-a-number",
        "source-element-id-zero-padded",
        "web-io-element-id-non-ascii-digits",
        "targets-as-dict",
        "gone",
    ],
)
def test_cleanup_selects_every_element_of_the_source(plan_data: dict[str, Any], selected: tuple) -> None:
    assert ComexioSyncButton._select_plan_cleanup(ComexioAPI, plan_data, [7]) == selected


def test_an_unreadable_connection_is_named_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # Otherwise the kept source reads as plan logic, and the corrupt wire shows up nowhere.
    plan = {"elements": {"11": _marker_element(7)}, "connections": {"c9": {"input": None, "output": []}}}
    with caplog.at_level(logging.WARNING):
        assert ComexioSyncButton._select_plan_cleanup(ComexioAPI, plan, [7]) == ([], [], [7], [])
    assert "connection c9 has an unreadable end" in caplog.text


@pytest.mark.parametrize(
    ("plan_data", "line"),
    [
        (MIXED_PLAN, KEPT_LINE),
        # An element id the delete request cannot carry: reported as kept instead of raising out of the cleanup.
        ({"elements": {"a1": _marker_element(7)}, "connections": {}}, KEPT_LINE),
        ({"elements": {}, "connections": {}}, GONE_LINE),
    ],
    ids=["kept-for-other-logic", "kept-for-an-unreadable-element-id", "gone-from-plan"],
)
def test_cleanup_reports_what_a_plan_kept_or_no_longer_holds(plan_data: dict[str, Any], line: str) -> None:
    # Neither is a failure (gone: the scan may read the cached snapshot), but neither is a silent 0.
    button, ctx = _plan_cleanup_button(plan_data)
    result = asyncio.run(button._cleanup_function_plan_plans(ctx, [7], SOURCE_CATEGORIES[WebioClass.MARKER]))
    assert result == (0, [], [], [], [line])
    assert not ctx.failed_writes


def test_cleanup_deletes_every_plan_element_in_one_pass_and_folds_the_outcome() -> None:
    plan_data = {
        "elements": {"11": _marker_element(7), "13": _web_io_element(40), "21": _marker_element(7)},
        "connections": {"0": _conn("11", "13")},
    }
    delete = AsyncMock(
        return_value={
            **NOTHING_DELETED,
            "deleted_elem_count": 3,
            "cmd_ids": [900],
            "stopped_plan": (PLAN, 5),
            "failures": [(PLAN, PLAN_CMD_NOT_FOUND)],
        }
    )
    cmd_map = {"40": 900}
    button, ctx = _plan_cleanup_button(
        plan_data, delete_plan_elements_for_commands=delete, webio_id_to_cmd_id=lambda: cmd_map
    )

    result = asyncio.run(button._cleanup_function_plan_plans(ctx, [7], SOURCE_CATEGORIES[WebioClass.MARKER]))

    delete.assert_awaited_once_with(5, [13, 11, 21], [40], cmd_map)
    assert result == (3, [900], [(PLAN, 5)], [], [])
    assert ctx.failed_writes == [f"function plan '{PLAN}': {PLAN_CMD_NOT_FOUND}"]


@pytest.mark.parametrize(
    ("deletable", "deleted", "failed"),
    [
        # (bj): a command of the ignored source without a plan element stayed behind as an orphan.
        (([900, 901], [], [], []), [900, 901], []),
        # A plan that could not be checked keeps the commands, the unwired one included, and is named.
        (([], [(PLAN, PLAN_LOAD_FAILED)], [], []), [], [f"function plan '{PLAN}': {PLAN_LOAD_FAILED}"]),
        # No poll has read the plan list yet: nothing counts as checked.
        (
            ([], [(PLAN_LIST_NAME, PLAN_LIST_NOT_READ)], [], []),
            [],
            [f"function plan '{PLAN_LIST_NAME}': {PLAN_LIST_NOT_READ}"],
        ),
        # A command without a readable webIoId cannot be checked against the plans: kept and named.
        (
            ([900], [], ["HA M7 kaputt"], []),
            [900],
            [
                f"cleanup {SOURCE_CATEGORIES[WebioClass.MARKER].label}: Web-IO command 'HA M7 kaputt' "
                f"{COMMAND_NOT_CHECKED}"
            ],
        ),
        # An unwired command the last poll does not list cannot be checked either.
        (
            ([], [], ["cmdId 999"], []),
            [],
            [f"cleanup {SOURCE_CATEGORIES[WebioClass.MARKER].label}: Web-IO command 'cmdId 999' {COMMAND_NOT_CHECKED}"],
        ),
    ],
    ids=["unplanned", "plan-unchecked", "plan-list-not-read", "command-unreadable", "command-not-listed"],
)
def test_cleanup_deletes_the_commands_no_plan_element_holds(
    deletable: tuple, deleted: list[int], failed: list[str]
) -> None:
    deletable_mock = AsyncMock(return_value=SourceCommandCheck(*deletable))
    button = _button(deletable_source_command_ids=deletable_mock)
    ctx = _ctx()
    marker = SOURCE_CATEGORIES[WebioClass.MARKER]
    with (
        patch.object(ComexioSyncButton, "_delete_source_entities", MagicMock(return_value=0)),
        patch.object(ComexioSyncButton, "_cleanup_function_plan_plans", AsyncMock(return_value=(1, [900], [], [], []))),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(len(deleted), 0))) as delete,
    ):
        asyncio.run(button._cleanup_entities_for_category(ctx, marker, [7], "12"))

    deletable_mock.assert_awaited_once_with(marker, [7], [900])
    delete.assert_awaited_once_with(ctx.api, "12", deleted, marker)
    assert ctx.failed_writes == failed


EMPTY_PLAN: dict[str, Any] = {"elements": {}, "connections": {}}


def _command_coordinator(
    plans: dict[int, Any],
    *,
    bulk: dict[int, Any] | None = None,
    markers: list[dict[str, Any]] | None = None,
    listed: set[int] | None = None,
) -> ComexioCoordinator:
    """plans: the single-plan loads; bulk: the bulk answer; listed: Comexio's plan list, read fresh.

    The list names plan 6 and every plan of plans and bulk unless given; api.comexio_fubs is what the
    fresh config fetch returns as $Fubs. The cached plan list is a stale one naming plan 6 only, and a
    stale snapshot of each plan sits in the cache: neither may count.
    """

    async def _load(fub_id: int, strict: bool = False) -> Any:
        assert strict
        return plans.get(fub_id)

    async def _load_all(strict: bool = False) -> dict[int, Any]:
        assert strict
        return dict(bulk or {})

    if listed is None:
        listed = {6, *plans, *(bulk or {})}
    stale = {"elements": {"1": _web_io_element(41)}, "connections": {}}
    api = _api(
        function_plan_load_elements=AsyncMock(side_effect=_load),
        function_plan_load_all_plans=AsyncMock(side_effect=_load_all),
        fub_data={"6": {"Name": "Plan 6"}},
        comexio_fubs={str(fub_id): {"Name": f"Plan {fub_id}"} for fub_id in listed},
    )
    api.get_raw_config = AsyncMock(side_effect=lambda: {"Fubs": api.comexio_fubs})
    api._replace_fub_data = MagicMock(side_effect=lambda fubs: setattr(api, "fub_data", fubs))
    coordinator = _coordinator(api, dict.fromkeys([6, 9, 12], stale))
    coordinator.scraped_plan_ids = {6}  # the plan list the last poll read
    coordinator.data = {
        "markers": markers or [{"id": 7, "name": "M7 Licht"}, {"id": 8, "name": "M8 Rollo"}],
        "webio_commands": {
            "HA M7 Licht": {"cmdId": 900, "webIoId": 40, "webioClass": "marker"},
            "HA M7 Licht alt": {"cmdId": 901, "webIoId": "41", "webioClass": "marker"},  # renamed since
            "HA M7 Licht (2)": {"cmdId": 902, "webIoId": 42, "webioClass": "marker"},  # wired in a plan
            "HA M8 Rollo": {"cmdId": 903, "webIoId": 43, "webioClass": "marker"},  # another source
            "HA M7 KNX": {"cmdId": 904, "webIoId": 44, "webioClass": "knx"},  # another class
            "HA M7 kaputt": {"cmdId": 905, "webIoId": None, "webioClass": "marker"},  # unreadable
        },
    }
    return coordinator


def _deletable(coordinator: ComexioCoordinator, source_ids: list[int], unwired: list[int]) -> SourceCommandCheck:
    return asyncio.run(
        coordinator.deletable_source_command_ids(SOURCE_CATEGORIES[WebioClass.MARKER], source_ids, unwired)
    )


def _loaded_plans(coordinator: ComexioCoordinator) -> list[int]:
    return [call.args[0] for call in coordinator.api.function_plan_load_elements.await_args_list]


def test_commands_no_plan_holds_are_picked_for_deletion() -> None:
    # 900 was unwired by the plan cleanup, 901 was never wired; 902 still sits in plan 6.
    coordinator = _command_coordinator({6: {"elements": {"1": _web_io_element(42)}, "connections": {}}, 9: EMPTY_PLAN})
    assert _deletable(coordinator, [7], [900]) == ([900, 901], [], ["HA M7 kaputt"], ["HA M7 Licht (2)"])


def test_a_plan_that_cannot_be_loaded_keeps_every_command() -> None:
    # The unwired command too: plan 6 may hold another element of it.
    coordinator = _command_coordinator({9: EMPTY_PLAN})  # plan 6 fails
    assert _deletable(coordinator, [7], [900]) == ([], [("Plan 6", PLAN_LOAD_FAILED)], ["HA M7 kaputt"], [])


def test_a_source_without_commands_loads_no_plan() -> None:
    coordinator = _command_coordinator({})
    assert _deletable(coordinator, [10], []) == ([], [], [], [])
    coordinator.api.function_plan_load_all_plans.assert_not_awaited()


def test_a_command_placed_only_in_a_users_own_plan_is_kept() -> None:
    # A user's own plan may use the command too, the unwired one included (the plan cleanup searches
    # the managed plans only): the fresh bulk answer counts, no single load needed.
    plan_6 = {"elements": {"1": _web_io_element(42)}, "connections": {}}
    plan_12 = {"elements": {"1": _web_io_element("41"), "2": _web_io_element(40)}, "connections": {}}
    coordinator = _command_coordinator({}, bulk={6: plan_6, 12: plan_12})
    assert _deletable(coordinator, [7], [900]) == (
        [],
        [],
        ["HA M7 kaputt"],
        ["HA M7 Licht", "HA M7 Licht alt", "HA M7 Licht (2)"],
    )
    assert _loaded_plans(coordinator) == []


def test_the_cached_snapshot_never_counts() -> None:
    # The snapshot may predate the plan cleanup or a user's edit: a fresh read says 41 is gone.
    coordinator = _command_coordinator({6: EMPTY_PLAN}, bulk={12: EMPTY_PLAN})
    assert _deletable(coordinator, [7], [900]) == ([900, 901, 902], [], ["HA M7 kaputt"], [])


def test_a_listed_plan_the_bulk_answer_lacks_is_loaded_on_its_own() -> None:
    # Plan 12 has no single-load answer either: it cannot be checked, so nothing is deleted.
    coordinator = _command_coordinator({6: EMPTY_PLAN}, bulk={9: EMPTY_PLAN}, listed={6, 9, 12})
    assert _deletable(coordinator, [7], [900]) == ([], [("Plan 12", PLAN_LOAD_FAILED)], ["HA M7 kaputt"], [])
    assert _loaded_plans(coordinator) == [6, 12]


def test_a_listed_plan_the_bulk_answer_lacks_counts() -> None:
    # The bulk answer lacks plan 6, the list names it: it is loaded on its own and its element counts.
    coordinator = _command_coordinator(
        {6: {"elements": {"1": _web_io_element(42)}, "connections": {}}}, bulk={9: EMPTY_PLAN}
    )
    assert _deletable(coordinator, [7], [900]) == ([900, 901], [], ["HA M7 kaputt"], ["HA M7 Licht (2)"])
    assert _loaded_plans(coordinator) == [6]


@pytest.mark.parametrize("listed", [{9}, set()], ids=["other-plan-listed", "no-plan-left"])
def test_a_managed_plan_the_plan_list_lacks_is_gone(listed: set[int]) -> None:
    # Plan 6 sits in the plan map, but Comexio deleted it: the list a poll read does not name it.
    coordinator = _command_coordinator({}, bulk=dict.fromkeys(listed, EMPTY_PLAN), listed=listed)
    coordinator._function_plan_check_fub_ids = lambda: {6}  # type: ignore[method-assign]
    assert _deletable(coordinator, [7], [900]) == ([900, 901, 902], [], ["HA M7 kaputt"], [])
    assert _loaded_plans(coordinator) == []


@pytest.mark.parametrize(
    "fetch",
    [
        AsyncMock(side_effect=TimeoutError),
        AsyncMock(return_value={}),
        AsyncMock(return_value={"FubModules": {}, "Fubs": None}),
    ],
    ids=["unreachable", "scrape-failed", "fubs-not-decoded"],
)
def test_a_plan_list_that_cannot_be_read_keeps_every_command(fetch: AsyncMock) -> None:
    # The cached list may lack a user's plan that holds a command: nothing counts as checked.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.api.get_raw_config = fetch
    assert _deletable(coordinator, [7], [900]) == ([], [(PLAN_LIST_NAME, PLAN_LIST_NOT_READ)], ["HA M7 kaputt"], [])
    coordinator.api.function_plan_load_all_plans.assert_not_awaited()
    coordinator.api._replace_fub_data.assert_not_called()


def test_a_plan_list_no_poll_has_read_yet_is_read_fresh() -> None:
    # Right after setup no poll has read the plan list: the fresh read alone decides.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.scraped_plan_ids = None
    assert _deletable(coordinator, [7], [900]) == ([900, 901, 902], [], ["HA M7 kaputt"], [])


def test_an_unreadable_plan_list_entry_keeps_every_command() -> None:
    # The entry may be a plan that holds the command, and it cannot even be loaded.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.api.comexio_fubs = {"x7": {"Name": "Broken"}}
    assert _deletable(coordinator, [7], [900]) == ([], [("x7", PLAN_ENTRY_UNREADABLE)], ["HA M7 kaputt"], [])


def test_only_web_io_elements_count_for_placed_commands() -> None:
    # A broken marker element says nothing about the commands: it must not block their deletion.
    plan = {"elements": {"1": {"reference": {"type": 2, "ref_id": "x"}}, "2": _web_io_element(42)}, "connections": {}}
    assert ComexioAPI.plan_webio_ids(plan) == ({42}, False)
    coordinator = _command_coordinator({6: plan})
    assert _deletable(coordinator, [7], [900]) == ([900, 901], [], ["HA M7 kaputt"], ["HA M7 Licht (2)"])


def test_an_unreadable_command_is_reported_even_without_a_command_to_check() -> None:
    coordinator = _command_coordinator({})
    for name in ("HA M7 Licht", "HA M7 Licht alt", "HA M7 Licht (2)"):
        del coordinator.data["webio_commands"][name]
    assert _deletable(coordinator, [7], []) == ([], [], ["HA M7 kaputt"], [])
    coordinator.api.function_plan_load_all_plans.assert_not_awaited()


def test_a_matching_command_without_a_cmd_id_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    # Nothing can delete it, but leaving it unnamed would read as a complete cleanup.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.data["webio_commands"]["HA M7 ohne Id"] = {"webIoId": 45, "webioClass": "marker"}
    with caplog.at_level(logging.WARNING):
        assert _deletable(coordinator, [7], [900]) == ([900, 901, 902], [], ["HA M7 kaputt", "HA M7 ohne Id"], [])
    assert "'HA M7 ohne Id' has no readable cmdId" in caplog.text
    assert "'HA M7 kaputt' has no readable webIoId" in caplog.text


@pytest.mark.parametrize(("ref_id", "matches"), [(7, True), ("7", True), (7.0, True), (7.9, False), ("7.9", False)])
def test_a_fractional_reference_id_matches_no_source(ref_id: Any, matches: bool) -> None:
    # int() would truncate 7.9 into marker 7 and hand its element to the cleanup.
    elements = {"11": {"reference": {"type": 2, "ref_id": ref_id}}}
    assert ComexioAPI._find_source_element_ids(elements, 7) == (["11"] if matches else [])


def test_a_boolean_reference_id_matches_no_source() -> None:
    assert ComexioAPI._find_source_element_ids({"11": {"reference": {"type": 2, "ref_id": True}}}, 1) == []


def test_a_plan_with_an_unreadable_web_io_element_keeps_every_command() -> None:
    # That element may stand for any of the commands.
    coordinator = _command_coordinator({6: {"elements": {"1": _web_io_element("x")}, "connections": {}}})
    assert _deletable(coordinator, [7], [900]) == ([], [("Plan 6", PLAN_WEBIO_ID_UNREADABLE)], ["HA M7 kaputt"], [])


def test_a_command_named_after_another_source_is_never_matched() -> None:
    # Marker 9 carries the name "M7 Licht alt": that command is its own, not marker 7's renamed leftover.
    markers = [{"id": 7, "name": "M7 Licht"}, {"id": 9, "name": "M7 Licht alt"}]
    coordinator = _command_coordinator({6: EMPTY_PLAN}, markers=markers)
    assert _deletable(coordinator, [7], [900]) == ([900, 902], [], ["HA M7 kaputt"], [])


def test_an_unwired_command_counts_whatever_its_name() -> None:
    # The plan cleanup found 903 wired to marker 7's element: its name does not decide.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    assert _deletable(coordinator, [7], [903]) == ([900, 901, 902, 903], [], ["HA M7 kaputt"], [])


def test_an_unwired_command_counts_whatever_its_class() -> None:
    # 904 is a KNX command by its class, but the plan cleanup unwired it from marker 7's element.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    assert _deletable(coordinator, [7], [904]) == ([900, 901, 902, 904], [], ["HA M7 kaputt"], [])


def test_an_unwired_command_the_last_poll_lacks_is_kept_and_named() -> None:
    # It cannot be checked against the plans; the next audit shows it again if it is still there.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    assert _deletable(coordinator, [7], [999]) == ([900, 901, 902], [], ["HA M7 kaputt", "cmdId 999"], [])


def test_only_unwired_commands_the_last_poll_lacks_load_no_plan() -> None:
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.data["webio_commands"] = {}
    assert _deletable(coordinator, [7], [999, 950]) == ([], [], ["cmdId 950", "cmdId 999"], [])
    coordinator.api.function_plan_load_all_plans.assert_not_awaited()


def test_an_unwired_command_without_a_readable_web_io_id_is_kept() -> None:
    # 903 belongs to marker 8 by its name: only the unwired path brings it in, and it cannot be checked.
    coordinator = _command_coordinator({6: EMPTY_PLAN})
    coordinator.data["webio_commands"]["HA M8 Rollo"]["webIoId"] = None
    assert _deletable(coordinator, [7], [903]) == ([900, 901, 902], [], ["HA M8 Rollo", "HA M7 kaputt"], [])


def test_a_plan_created_since_the_poll_is_checked() -> None:
    # The cached list the last poll read names plan 6 only; the user has since created plan 15 in
    # Comexio and placed command 900 there. The fresh plan list names it: 900 stays.
    plan_15 = {"elements": {"1": _web_io_element(40)}, "connections": {}}
    coordinator = _command_coordinator({6: EMPTY_PLAN, 15: plan_15})
    assert coordinator.api.fub_data.keys() == {"6"}
    assert _deletable(coordinator, [7], [900]) == ([901, 902], [], ["HA M7 kaputt"], ["HA M7 Licht"])
    assert _loaded_plans(coordinator) == [6, 15]
    coordinator.api.get_raw_config.assert_awaited_once()


def _run_cleanup_action(button: ComexioSyncButton, plan_result: Any) -> tuple[SimpleNamespace, MagicMock]:
    ctx = _ctx()
    with (
        patch.object(ComexioSyncButton, "_delete_source_entities", MagicMock(return_value=0)),
        patch.object(ComexioSyncButton, "_cleanup_function_plan_plans", plan_result),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(0, 0))),
        patch.object(button_module, "_post_result_notification") as notify,
    ):
        asyncio.run(button._handle_cleanup_entities(ctx, [("marker", 7)], {}, "n1", True))
    return ctx, notify


def test_cleanup_result_names_a_plan_it_could_not_load() -> None:
    # (u)/(bi): with the cable pulled the result read "Nothing to clean up" under an "Incomplete" title.
    async def _plan_load_fails(_self: Any, ctx: SimpleNamespace, *_args: Any) -> tuple:
        ctx.failed_writes.append(f"function plan '{PLAN}': {PLAN_LOAD_FAILED}")
        return 0, [], [], [], []

    button = _button(ignored_ids_for=MagicMock(return_value={7}), sync_failed_writes=[], sync_progress_text="")
    _ctx_, notify = _run_cleanup_action(button, _plan_load_fails)
    msg, title = notify.call_args.args[2:4]
    assert "Incomplete" in title
    assert f"function plan '{PLAN}': {PLAN_LOAD_FAILED}" in msg
    assert "Nothing to clean up" not in msg


def test_cleanup_result_shows_a_kept_source() -> None:
    button = _button(ignored_ids_for=MagicMock(return_value={7}), sync_failed_writes=[], sync_progress_text="")
    ctx, notify = _run_cleanup_action(button, AsyncMock(return_value=(0, [], [], [], [KEPT_LINE])))
    msg, title = notify.call_args.args[2:4]
    assert "Incomplete" not in title
    assert KEPT_LINE in msg.split("\n")
    assert "Nothing to clean up" not in msg
    assert not ctx.failed_writes


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


def test_visualize_svg_returns_the_preview_url() -> None:
    coordinator = SimpleNamespace(
        async_generate_plan_preview=AsyncMock(return_value="/local/comexio/plan_5.svg"),
        snapshot_block_settings_for_source=MagicMock(return_value=None),
        snapshot_paper_format_for_source=MagicMock(return_value=None),
        preview_follow_lock=asyncio.Lock(),
        preview_follow_generation=0,
    )
    source = (coordinator, MagicMock(), 5, PLAN, {}, {}, "live", None)
    call = SimpleNamespace(data={"format": "svg"}, return_response=True)
    with (
        patch.object(plan_actions, "_resolve_visualize_live_source", AsyncMock(return_value=source)),
        patch.object(plan_actions.persistent_notification, "async_create"),
    ):
        response = asyncio.run(plan_actions.handle_function_plan_visualize(MagicMock(), call))
    assert response == {"plan_name": PLAN, "url": "/local/comexio/plan_5.svg"}


def _visualize_api(live_fubs: dict[str, Any] | list | None) -> SimpleNamespace:
    fub_data = {"5": {"Name": PLAN}}
    raw = {} if live_fubs is None else {"Fubs": live_fubs}
    return SimpleNamespace(
        fub_data=fub_data,
        get_raw_config=AsyncMock(return_value=raw),
        update_fub_cache_entry=lambda fid, info: fub_data.__setitem__(str(fid), info),
        function_plan_load_elements=AsyncMock(return_value={"elements": {}, "connections": {}}),
    )


def _resolve_live(api: SimpleNamespace, fub_id: int) -> tuple[Any, MagicMock]:
    with (
        patch.object(
            plan_actions, "_resolve_function_plan_context", AsyncMock(return_value=(MagicMock(), api, fub_id))
        ),
        patch.object(plan_actions.persistent_notification, "async_create") as notify,
    ):
        source = asyncio.run(plan_actions._resolve_visualize_live_source(MagicMock(), MagicMock(), "Error"))
    return source, notify


def test_visualize_rejects_an_unknown_plan() -> None:
    # (ao): Comexio answers an unknown fub_id with an empty plan, which was rendered as a valid one.
    api = _visualize_api({"5": {"Name": PLAN}})
    source, notify = _resolve_live(api, 99999)
    assert source is None
    assert notify.call_args.args[1] == "Plan 99999 does not exist."
    api.function_plan_load_elements.assert_not_awaited()


def test_visualize_looks_up_a_plan_newer_than_the_cached_list() -> None:
    api = _visualize_api({"5": {"Name": PLAN}, "7": {"Name": "New plan"}})
    source, notify = _resolve_live(api, 7)
    assert source is not None
    assert source[3] == "New plan"
    notify.assert_not_called()


def test_visualize_says_when_the_plan_list_could_not_be_read() -> None:
    source, notify = _resolve_live(_visualize_api(None), 7)
    assert source is None
    assert notify.call_args.args[1] == "Plan 7 could not be checked (plan list not readable)."


def test_visualize_rejects_a_plan_when_the_server_has_none() -> None:
    # PHP encodes an empty plan list as [] — .get() on it used to crash the service.
    source, notify = _resolve_live(_visualize_api([]), 7)
    assert source is None
    assert notify.call_args.args[1] == "Plan 7 does not exist."


def test_visualize_failure_raises_when_a_response_is_requested() -> None:
    call = SimpleNamespace(data={"format": "text"}, return_response=True)
    hass = MagicMock()
    with patch.object(plan_actions, "_resolve_visualize_live_source", AsyncMock(return_value=None)):
        visualize = plan_actions.handle_function_plan_visualize(hass, call)
        with pytest.raises(HomeAssistantError):
            asyncio.run(visualize)


def test_cleanup_handles_each_duplicate_by_its_own_wiring() -> None:
    # One element of the source feeds logic (kept), its duplicate only a WebIO (removed).
    plan_data = {
        "elements": {"11": _marker_element(7), "12": LOGIC_BLOCK, "21": _marker_element(7), "23": _web_io_element(40)},
        "connections": {"0": _conn("11", "12"), "1": _conn("21", "23")},
    }
    assert ComexioSyncButton._select_plan_cleanup(ComexioAPI, plan_data, [7]) == ([23, 21], [40], [7], [])


def test_cleanup_selects_knx_elements_by_their_own_reference_type() -> None:
    # Marker 7 and KNX object 7 share the id: the KNX cleanup must leave the marker element alone.
    plan_data = {
        "elements": {"11": _marker_element(7), "21": {"reference": {"type": "11", "ref_id": "7"}}},
        "connections": {},
    }
    assert ComexioSyncButton._select_plan_cleanup(ComexioAPI, plan_data, [7], ref_type=11) == ([21], [], [], [])


def _delete_coordinator(result: dict[str, Any]) -> ComexioCoordinator:
    return _coordinator(_api(_delete_plan_elements_and_restart=AsyncMock(return_value=result)))


def test_deleting_no_element_leaves_the_plan_untouched() -> None:
    coordinator = _delete_coordinator({})
    result = asyncio.run(coordinator.delete_plan_elements_for_commands(5, [], [40], {"40": 900}))
    assert result == NOTHING_DELETED
    coordinator.api._delete_plan_elements_and_restart.assert_not_awaited()


def test_deleting_elements_resolves_only_the_unwired_commands() -> None:
    coordinator = _delete_coordinator(
        {"plan_name": PLAN, "deleted_elem_count": 3, "webio_cmd_ids": [40, 41], "was_running": False}
    )
    result = asyncio.run(coordinator.delete_plan_elements_for_commands(5, [13, 11, 13], [40, 41], {"40": 900}))

    coordinator.api._delete_plan_elements_and_restart.assert_awaited_once_with(5, [13, 11], [40, 41], PLAN)
    assert result == {
        **NOTHING_DELETED,
        "deleted_elem_count": 3,
        "cmd_ids": [900],
        "failures": [(PLAN, f"{PLAN_CMD_NOT_FOUND} (webIoId 41)")],
        "touched": True,
        "was_running": False,
    }


@pytest.mark.parametrize(
    "unplanned",
    [([], [(PLAN, PLAN_LOAD_FAILED)], [], []), ([], [], ["HA M7 kaputt"], []), ([], [], [], ["HA M7 Licht"])],
    ids=["plan-unchecked", "command-unreadable", "command-in-use"],
)
def test_a_command_check_that_kept_commands_is_never_nothing_to_clean_up(unplanned: tuple) -> None:
    button = _button(deletable_source_command_ids=AsyncMock(return_value=SourceCommandCheck(*unplanned)))
    with (
        patch.object(ComexioSyncButton, "_delete_source_entities", MagicMock(return_value=0)),
        patch.object(ComexioSyncButton, "_cleanup_function_plan_plans", AsyncMock(return_value=(0, [], [], [], []))),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(0, 0))),
    ):
        lines = asyncio.run(
            button._cleanup_entities_for_category(_ctx(), SOURCE_CATEGORIES[WebioClass.MARKER], [7], "12")
        )
    assert f"{ICON_CHECK} 0 WebIO commands deleted" in lines
    assert not [line for line in lines if line.startswith("Nothing to clean up")]


def test_a_command_a_plan_still_uses_is_named_in_the_result() -> None:
    check = SourceCommandCheck([], [], [], ["HA M7 Licht", "HA M7 Licht alt"])
    button = _button(deletable_source_command_ids=AsyncMock(return_value=check))
    ctx = _ctx()
    with (
        patch.object(ComexioSyncButton, "_delete_source_entities", MagicMock(return_value=0)),
        patch.object(ComexioSyncButton, "_cleanup_function_plan_plans", AsyncMock(return_value=(1, [900], [], [], []))),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(0, 0))) as delete,
    ):
        lines = asyncio.run(button._cleanup_entities_for_category(ctx, SOURCE_CATEGORIES[WebioClass.MARKER], [7], "12"))
    stays = "stays — a function plan still uses its WebIO element"
    assert f"{ICON_INFO} Web-IO command 'HA M7 Licht' {stays}" in lines
    assert f"{ICON_INFO} Web-IO command 'HA M7 Licht alt' {stays}" in lines
    assert delete.await_args.args[2] == []  # the unwired 900 is still in use: not deleted
    assert ctx.failed_writes == []
