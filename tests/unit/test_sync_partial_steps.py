"""Sync steps besides Web-IO writes that leave a run partial (button.py): (t) plan wiring, (u) cleanup, (v) cancel."""

import asyncio
from collections.abc import Callable, Coroutine
import datetime
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio.button import (
    _NOTE_ACTIVATED,
    _NOTE_NOT_ACTIVATED,
    ComexioSyncButton,
    _activation_note,
    _mark_cancelled,
)
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    IO_PLAN_KIND_IO,
    SOURCE_CATEGORIES,
    WebioClass,
)

MARKER = SOURCE_CATEGORIES[WebioClass.MARKER]


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(failed_writes=[], skipped_classes=[], skipped_stages=[], api=MagicMock())


def _button(**coordinator: object) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(  # type: ignore[assignment]
        **{"cancel_sync": False, "unplanned_source_command_ids": AsyncMock(return_value=([], [], [])), **coordinator}
    )
    button.server_id = "iosrv1"
    return button


def test_plan_left_stopped_is_a_failed_step() -> None:
    ctx = _ctx()
    assert _activation_note(ctx, "HA - Marker [1-100]", True) == _NOTE_ACTIVATED
    assert not ctx.failed_writes
    assert _activation_note(ctx, "HA - Marker [1-100]", False) == _NOTE_NOT_ACTIVATED
    assert ctx.failed_writes == ["function plan 'HA - Marker [1-100]': not activated"]


def test_wiring_errors_and_unresolved_plans_are_failed_steps() -> None:
    # (t): a cluster plan that could not be resolved and pairs that could not be wired
    # used to show up only as a warning line, the sensor stayed "idle".
    button = _button(
        resolve_marker_clusters=AsyncMock(return_value=({5: [1, 2]}, set(), ["HA - Marker [101-200]"])),
        api=SimpleNamespace(fub_data={"5": {"Name": "HA - Marker [1-100]"}}),
    )
    ctx = _ctx()
    add = AsyncMock(return_value=("line", [1], ["M2: element not created"]))
    with patch.object(ComexioSyncButton, "_add_pairs_to_plan", add):
        _lines, added, errors = asyncio.run(button._wire_source_clusters(ctx, MARKER, [1, 2], {}))
    assert (added, errors) == (1, 2)
    assert ctx.failed_writes == [
        "function plan 'HA - Marker [101-200]': not resolved/created",
        "function plan 'HA - Marker [1-100]': 1 error(s)",
    ]


def test_clean_wiring_leaves_no_failed_step() -> None:
    button = _button(
        resolve_marker_clusters=AsyncMock(return_value=({5: [1]}, set(), [])),
        api=SimpleNamespace(fub_data={"5": {"Name": "HA - Marker [1-100]"}}),
    )
    ctx = _ctx()
    with patch.object(ComexioSyncButton, "_add_pairs_to_plan", AsyncMock(return_value=("line", [1], []))):
        asyncio.run(button._wire_source_clusters(ctx, MARKER, [1], {}))
    assert not ctx.failed_writes


def test_cleanup_failures_are_failed_steps() -> None:
    # (u): failed Web-IO deletions and plans left stopped/not stoppable made the sensor read "idle".
    button = _button()
    ctx = _ctx()
    with (
        patch.object(ComexioSyncButton, "_delete_source_entities", MagicMock(return_value=1)),
        patch.object(
            ComexioSyncButton,
            "_cleanup_function_plan_plans",
            AsyncMock(return_value=(2, ["c1"], [("Plan A", 3)], [("Plan B", 4)], [])),
        ),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(0, 1))),
        patch.object(ComexioSyncButton, "_notify_stopped_plans", MagicMock(return_value=[])),
    ):
        asyncio.run(button._cleanup_entities_for_category(ctx, MARKER, [7], "12"))
    assert ctx.failed_writes == [
        f"cleanup {MARKER.label}: 1 Web-IO command deletion(s)",
        "function plan 'Plan A': left stopped after cleanup",
        "function plan 'Plan B': not cleaned up (could not be stopped)",
    ]


def test_user_cancel_makes_the_run_partial() -> None:
    # (v): a cancelled run without a failed write read "idle" although it stopped halfway.
    ctx = _ctx()
    msg = _mark_cancelled(ctx, "Results: 1 added.")
    assert "Sync cancelled by user" in msg.splitlines()[0]
    assert msg.endswith("Results: 1 added.")
    assert ctx.failed_writes == ["cancelled by user"]


def test_cleanup_action_publishes_its_failures() -> None:
    # (u): the cleanup action returns before the sync's own hand-over of failed_writes.
    button = _button(ignored_ids_for=MagicMock(return_value={7}), sync_failed_writes=[], sync_progress_text="x")

    async def _cleanup(_self, ctx, *_args):
        ctx.failed_writes.append("cleanup Marker: 1 Web-IO command deletion(s)")
        return ["⚠️ 1 WebIO deletions failed"]

    ctx = _ctx()
    with patch.object(ComexioSyncButton, "_cleanup_entities_for_category", _cleanup):
        asyncio.run(button._handle_cleanup_entities(ctx, [("marker", 7)], {}, "n", False))
    assert button.coordinator.sync_failed_writes == ["cleanup Marker: 1 Web-IO command deletion(s)"]
    assert button.coordinator.sync_progress_text == "⚠️ 1 WebIO deletions failed"


def test_failed_restart_without_new_pairs_is_a_failed_step() -> None:
    # Review: a plan stopped for wiring whose pairs all failed was restarted without checking the result.
    button = _button()
    ctx = _ctx()
    ctx.update_status = MagicMock()
    ctx.api.function_plan_run_fup = AsyncMock(return_value=False)
    note = asyncio.run(button._finalize_plan_after_pairs(ctx, 5, "HA - Marker [1-100]", [], False, True))
    assert note == _NOTE_NOT_ACTIVATED
    assert ctx.failed_writes == ["function plan 'HA - Marker [1-100]': not activated"]


def test_trigger_orphans_left_behind_are_a_failed_step() -> None:
    button = _button(
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 9}}),
        api=SimpleNamespace(fub_data={"9": {"Name": FUNCTION_PLAN_TRIGGER_PLAN_NAME}}),
        async_function_plan_change_backup=AsyncMock(),
    )
    ctx = _ctx()
    ctx.api.function_plan_remove_trigger_pairs = AsyncMock(return_value=(0, False))
    line = asyncio.run(button._remove_trigger_pairs(ctx, [3]))
    assert "orphans not removed" in line
    assert ctx.failed_writes == [
        f"function plan '{FUNCTION_PLAN_TRIGGER_PLAN_NAME}': orphaned trigger pairs not removed"
    ]


def test_standalone_wiring_message_headline_follows_the_failures() -> None:
    build = ComexioSyncButton._build_function_plan_add_missing_message
    assert "update finished**" in build([], datetime.timedelta(seconds=3))
    msg = build(["line"], datetime.timedelta(seconds=3), ["function plan 'P': 1 error(s)"])
    assert "update finished with errors**" in msg
    assert "1 sync step(s) failed: function plan 'P': 1 error(s)" in msg


class _PlanApi:
    """api of one plan 5 whose cached Active flag follows stop_fup like ComexioAPI's (None: plan not cached)."""

    def __init__(self, active: int | None = 1) -> None:
        self.fub_data = {"5": {"Name": "P", "Active": active}}
        self.function_plan_stop_fup = AsyncMock(side_effect=self._stop)
        self.function_plan_run_fup = AsyncMock(return_value=True)

    def _stop(self, fub_id: int) -> bool:
        if self.fub_data[str(fub_id)]["Active"] is not None:
            self.fub_data[str(fub_id)]["Active"] = 0
        return True

    def get_fub_active(self, fub_id: int) -> bool | None:
        active = self.fub_data[str(fub_id)]["Active"]
        return None if active is None else bool(active)


def _write_raising(
    button: ComexioSyncButton,
    api: _PlanApi,
    is_fresh: bool,
    *,
    restarted: bool = False,
    error: BaseException | None = None,
) -> Any:
    """A pair write that stops plan 5 and raises error (after starting it again itself when restarted)."""

    async def _write() -> None:
        async with button._plan_restarted_on_error(api, 5, is_fresh):
            await api.function_plan_stop_fup(5)
            if restarted:
                api.fub_data["5"]["Active"] = 1
            raise error or ValueError("write failed")

    return _write()


@pytest.mark.parametrize(
    ("active", "is_fresh", "starts"),
    [(1, False, 1), (0, False, 0), (0, True, 1), (None, False, 1)],
    ids=["ran-before", "was-off", "fresh", "not-cached"],
)
def test_a_write_raising_after_the_stop_starts_the_plan_as_its_finalization_would(
    active: int | None, is_fresh: bool, starts: int
) -> None:
    api = _PlanApi(active)
    write = _write_raising(_button(), api, is_fresh)

    with pytest.raises(ValueError, match="write failed"):
        asyncio.run(write)

    assert api.function_plan_run_fup.await_count == starts


def test_a_cancelled_write_starts_the_plan_again_and_stays_cancelled() -> None:
    api = _PlanApi()
    write = _write_raising(_button(), api, False, error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(write)

    api.function_plan_run_fup.assert_awaited_once_with(5)


def test_a_plan_the_write_started_again_itself_is_not_started_twice() -> None:
    api = _PlanApi()
    write = _write_raising(_button(), api, False, restarted=True)

    with pytest.raises(ValueError, match="write failed"):
        asyncio.run(write)

    api.function_plan_run_fup.assert_not_awaited()


@pytest.mark.parametrize(
    ("start", "logged"),
    [
        ({"return_value": True}, "started again after its pair write failed (ValueError("),
        (
            {"side_effect": RuntimeError("Comexio down")},
            "stays stopped: its start after a failed pair write (ValueError(",
        ),
        (
            {"return_value": False},
            "stays stopped: Comexio did not confirm its start after a failed pair write (ValueError(",
        ),
    ],
    ids=["start-ok", "start-raises", "start-unconfirmed"],
)
def test_the_start_after_a_failed_write_keeps_the_write_error(
    start: dict[str, Any], logged: str, caplog: pytest.LogCaptureFixture
) -> None:
    api = _PlanApi()
    api.function_plan_run_fup = AsyncMock(**start)
    write = _write_raising(_button(), api, False)

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="write failed"):
        asyncio.run(write)

    assert f"fub=5 {logged}" in caplog.text


def _marker_site(api: _PlanApi, created: set[int]) -> tuple[ComexioSyncButton, dict, Callable[..., Coroutine]]:
    button = _button(resolve_marker_clusters=AsyncMock(return_value=({5: [1]}, created, [])), api=api)
    return button, {}, lambda ctx: button._wire_source_clusters(ctx, MARKER, [1], {})


def _io_site(api: _PlanApi, created: set[int]) -> tuple[ComexioSyncButton, dict, Callable[..., Coroutine]]:
    button = _button(resolve_io_clusters=AsyncMock(return_value=({"A": (5, (0, 0))}, created, {})), api=api, data={})
    return button, {}, lambda ctx: button._wire_io_kind_clusters(ctx, IO_PLAN_KIND_IO, {"A": ["I1"]}, {})


def _knx_site(api: _PlanApi, created: set[int]) -> tuple[ComexioSyncButton, dict, Callable[..., Coroutine]]:
    button = _button(resolve_knx_clusters=AsyncMock(return_value=({5: [2]}, created, [])), api=api)
    stubs = {
        "_knx_audit_items": AsyncMock(return_value=([{"ref_id": "2"}], [], None, [])),
        "_prestage_knx_webio": AsyncMock(return_value=(SimpleNamespace(allocated=[]), [])),
    }
    return button, stubs, lambda ctx: button._wire_knx_full(ctx, [], [], refresh_audit=True)


# (active before the write, plan created in this run, starts after the failed write)
_RESTART_CASES = [(1, False, 1), (0, True, 1), (0, False, 0)]
_RESTART_IDS = ["ran-before", "fresh", "was-off"]


@pytest.mark.parametrize(("active", "fresh", "starts"), _RESTART_CASES, ids=_RESTART_IDS)
@pytest.mark.parametrize(
    ("site", "writer"),
    [(_marker_site, "_add_pairs_to_plan"), (_io_site, "_add_io_pairs_to_plan"), (_knx_site, "_wire_knx_cluster")],
    ids=["marker", "io", "knx"],
)
def test_every_cluster_write_starts_its_plan_again_when_it_raises(
    site: Callable, writer: str, active: int, fresh: bool, starts: int
) -> None:
    api = _PlanApi(active)
    button, stubs, run = site(api, {5} if fresh else set())
    ctx = _ctx()
    ctx.api, ctx.action = api, "full_sync"

    async def _stop_and_fail(*_args: object, **_kwargs: object) -> None:
        await api.function_plan_stop_fup(5)
        raise ValueError("write failed")

    with patch.multiple(ComexioSyncButton, **stubs, **{writer: _stop_and_fail}):
        wiring = run(ctx)
        with pytest.raises(ValueError, match="write failed"):
            asyncio.run(wiring)

    assert api.function_plan_run_fup.await_count == starts


@pytest.mark.parametrize(
    "error", [ValueError("leg failed"), asyncio.CancelledError()], ids=["leg-raises", "leg-cancelled"]
)
def test_a_knx_leg_raising_starts_the_plan_once(error: BaseException) -> None:
    # _wire_knx_cluster no longer starts the plan itself: the caller's _plan_restarted_on_error does, once.
    api = _PlanApi()
    button, stubs, run = _knx_site(api, set())
    button.coordinator.async_function_plan_change_backup = AsyncMock()
    ctx = _ctx()
    ctx.api, ctx.action = api, "full_sync"
    stubs |= {
        "_check_plan_rename_mismatch": MagicMock(return_value=None),
        "_wire_knx_leg_bridge": AsyncMock(side_effect=error),
    }

    with patch.multiple(ComexioSyncButton, **stubs):
        wiring = run(ctx)
        with pytest.raises(type(error)):
            asyncio.run(wiring)

    api.function_plan_stop_fup.assert_awaited_once_with(5)
    api.function_plan_run_fup.assert_awaited_once_with(5)


@pytest.mark.parametrize(("active", "fresh", "starts"), _RESTART_CASES, ids=_RESTART_IDS)
def test_a_trigger_write_starts_its_plan_again_when_it_raises(active: int, fresh: bool, starts: int) -> None:
    api = _PlanApi(active)
    api.flanke_ref_id = lambda: 7
    api.function_plan_add_trigger_pairs = AsyncMock(side_effect=ValueError("write failed"))
    button = _button(
        resolve_trigger_plan=AsyncMock(return_value=(5, fresh)), async_function_plan_change_backup=AsyncMock(), api=api
    )
    ctx = _ctx()
    ctx.api = api
    wiring = button._add_trigger_pairs(ctx, [50])

    with pytest.raises(ValueError, match="write failed"):
        asyncio.run(wiring)

    assert api.function_plan_run_fup.await_count == starts
