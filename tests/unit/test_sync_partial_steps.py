"""Sync steps besides Web-IO writes that leave a run partial (button.py): (t) plan wiring, (u) cleanup, (v) cancel."""

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    SOURCE_CATEGORIES,
    WebioClass,
)

MARKER = SOURCE_CATEGORIES[WebioClass.MARKER]


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(failed_writes=[], api=MagicMock())


def _button(**coordinator: object) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(cancel_sync=False, **coordinator)  # type: ignore[assignment]
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
            AsyncMock(return_value=(2, ["c1"], [("Plan A", 3)], [("Plan B", 4)])),
        ),
        patch.object(ComexioSyncButton, "_delete_webio_commands", AsyncMock(return_value=(0, 1))),
        patch.object(ComexioSyncButton, "_notify_stopped_plans", MagicMock(return_value=[])),
    ):
        asyncio.run(button._cleanup_entities_for_category(ctx, MARKER, [7], "12", None))
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
