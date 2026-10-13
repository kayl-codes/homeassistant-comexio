"""A reload waits for a running function plan restore; a failed restore never leaves its progress notice behind."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.const import CONF_FUNCTION_PLAN_PLAN_MAP, DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.repairs import ComexioRepairFlow
from custom_components.comexio.services import _context, backup, connect, marker_actions, plan_actions


def _coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.in_sync = False
    coordinator.sync_error = False
    coordinator.sync_failed_writes = []
    coordinator.sync_progress_text = "Done"
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    coordinator._sync_lock = asyncio.Lock()
    coordinator._restore_lock = asyncio.Lock()
    coordinator.config_entry = SimpleNamespace(entry_id="e1")
    coordinator.hass = MagicMock()
    coordinator.hass.data = {DOMAIN: {"e1": coordinator}}
    coordinator.hass.config_entries.async_reload = AsyncMock()
    return coordinator


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_a_reload_waits_for_a_running_restore_to_finish() -> None:
    coordinator = _coordinator()
    reload = coordinator.hass.config_entries.async_reload

    async def run() -> None:
        async with coordinator._restore_lock:
            task = asyncio.create_task(coordinator.async_reload_entry("options changed"))
            await _settle()
            reload.assert_not_awaited()
        await task
        reload.assert_awaited_once_with("e1")

    asyncio.run(run())


def test_the_sync_reload_waits_for_a_running_restore_and_keeps_in_sync_meanwhile() -> None:
    """Seen live 2026-10-13: the sync's closing reload closed the session under a copy-restore."""
    coordinator = _coordinator()
    coordinator.in_sync = True
    reset_and_reload = AsyncMock()
    button = SimpleNamespace(coordinator=coordinator, _reset_sync_state_and_reload=reset_and_reload)

    async def run() -> None:
        await coordinator._sync_lock.acquire()
        async with coordinator._restore_lock:
            task = asyncio.create_task(ComexioSyncButton._finalize_sync(button))  # type: ignore[arg-type]
            await _settle()
            reset_and_reload.assert_not_awaited()
            assert coordinator.sync_progress_text.startswith("Waiting for the running function plan restore")
            coordinator.async_update_listeners.assert_called_once()
            # Still a sync: the restore's own reload request is skipped, the sync's covers it.
            await coordinator.async_reload_entry("function plan restore")
            coordinator.hass.config_entries.async_reload.assert_not_awaited()
        await task
        reset_and_reload.assert_awaited_once()
        assert not coordinator._sync_lock.locked()

    asyncio.run(run())


def test_an_unexpected_restore_error_replaces_the_progress_notice() -> None:
    coordinator = _coordinator()
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    coordinator._auto_backup_pauses = 0
    coordinator._auto_backup_pause_generation = 0

    hass, call = MagicMock(), SimpleNamespace(data={})
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", AsyncMock(side_effect=RuntimeError("Session is closed"))),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        restore = backup._handle_function_plan_restore(hass, call)
        with pytest.raises(RuntimeError):
            asyncio.run(restore)

    create.assert_called_once()
    assert create.call_args.kwargs["notification_id"] == "comexio_function_plan_restore_cx1"
    assert "RuntimeError" in create.call_args.args[1]
    assert not coordinator._restore_lock.locked()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (asyncio.CancelledError(), "The restore was cancelled"),
        (RuntimeError("401, url='http://cx/admin/?token=s3cret'"), "RuntimeError — details in the"),
    ],
)
def test_a_cancelled_or_failed_restore_replaces_its_progress_notice(error: BaseException, expected: str) -> None:
    """(Review) a cancel skips 'except Exception'; an error's repr can carry request headers and cookies."""
    coordinator = _coordinator()
    coordinator._auto_backup_pauses = 0
    coordinator._auto_backup_pause_generation = 0
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", AsyncMock(side_effect=error)),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        restore = backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={}))
        with pytest.raises(type(error)):
            asyncio.run(restore)

    assert expected in create.call_args.args[1]
    # (Review) the message itself can carry the request URL — the log has it, not the notification.
    assert "s3cret" not in create.call_args.args[1]
    assert create.call_args.kwargs["notification_id"] == "comexio_function_plan_restore_cx1"
    assert not coordinator._restore_lock.locked()
    assert coordinator._auto_backup_pauses == 0


def test_a_failing_service_description_refresh_keeps_the_restores_result() -> None:
    """(Review) the refresh used to run inside the guard: its error replaced an OK result with 'failed'."""
    coordinator = _coordinator()
    coordinator._auto_backup_pauses = 0
    coordinator._auto_backup_pause_generation = 0
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup.persistent_notification, "async_create") as create,
        patch.object(backup, "_refresh_service_descriptions", AsyncMock(side_effect=OSError("disk full"))),
    ):

        async def restore_posting_its_result(hass: object, *_: object) -> None:
            backup.persistent_notification.async_create(hass, "Restore: **OK**", notification_id="n")

        with patch.object(backup, "_run_function_plan_restore", restore_posting_its_result):
            restore = backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={}))
            with pytest.raises(OSError):
                asyncio.run(restore)

    create.assert_called_once()
    assert create.call_args.args[1] == "Restore: **OK**"


def test_a_range_check_does_not_start_while_a_restore_runs() -> None:
    """(Review) a restore refuses to start during a range check; the other way round must hold too."""
    coordinator = _coordinator()
    coordinator.data = {"webio_commands": {}}

    async def run() -> dict[str, int]:
        async with coordinator._restore_lock:
            return await coordinator.async_force_webio_range_check()

    result = asyncio.run(run())
    assert result["skipped"] == 1
    assert not coordinator._sync_lock.locked()


def test_the_restores_own_reload_runs_after_it_instead_of_deadlocking() -> None:
    """Awaiting the reload under _restore_lock would wait for the restore itself."""
    coordinator = _coordinator()
    coordinator.function_plan_backup = SimpleNamespace(async_rekey_fub_id=AsyncMock())
    coordinator.async_repoint_function_plan_fub_id = AsyncMock(return_value=["select.plan"])  # type: ignore[method-assign]
    hass = coordinator.hass
    scheduled: list[asyncio.Task] = []
    hass.async_create_task = lambda coro, **_: scheduled.append(asyncio.ensure_future(coro))
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
    )
    snapshot = {"plan_name": "Kitch", "elements": {}, "connections": {}}

    async def run() -> None:
        async with coordinator._restore_lock:
            await asyncio.wait_for(backup._restore_plan_as_new(hass, coordinator, api, 42, snapshot, "auto", 0), 2)
            hass.config_entries.async_reload.assert_not_awaited()
        await asyncio.gather(*scheduled)
        hass.config_entries.async_reload.assert_awaited_once_with("e1")

    with patch.object(backup.persistent_notification, "async_create"):
        asyncio.run(run())


@pytest.mark.parametrize(
    "handler",
    [
        plan_actions.handle_function_plan_sort,
        plan_actions.handle_function_plan_stop,
        plan_actions.handle_function_plan_activate,
        connect.handle_function_plan_connect,
        marker_actions.handle_marker_delete,
    ],
)
def test_plan_editing_services_refuse_while_a_restore_runs(handler: Any) -> None:
    """(Review) an edit during a restore mixed into the restored plan, or the restore overwrote it."""
    coordinator = _coordinator()
    coordinator.api = MagicMock()
    call = SimpleNamespace(data={"fub_id": "1", "marker_id": "5", "confirm": True})

    async def run() -> None:
        async with coordinator._restore_lock:
            await handler(coordinator.hass, call)

    with (
        patch.object(_context, "_resolve_coordinator", return_value=coordinator),
        patch.object(_context.persistent_notification, "async_create") as create,
        patch.object(marker_actions, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
    ):
        asyncio.run(run())

    assert create.call_args.args[1] == _context.RESTORE_RUNNING_MSG
    assert coordinator.api.mock_calls == []


def test_a_failing_promotion_keeps_the_restore_result() -> None:
    """(Review) the plan is restored by then: a store error must not report the restore as failed."""
    coordinator = SimpleNamespace(
        function_plan_backup=SimpleNamespace(async_mark_restored=AsyncMock(side_effect=OSError("disk full")))
    )
    promoted = asyncio.run(backup._async_promote_restored(coordinator, "auto", 1, "Lights", {}))
    assert promoted == "OSError"


def test_a_restore_as_new_whose_lineage_could_not_move_reports_the_built_plan() -> None:
    """(Review) the plan is built by then: reported PARTIAL with the failed bookkeeping, not as failed."""
    coordinator = _coordinator()
    coordinator.function_plan_backup = SimpleNamespace(async_rekey_fub_id=AsyncMock(side_effect=OSError("disk full")))
    coordinator.async_repoint_function_plan_fub_id = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
    )
    snapshot = {"plan_name": "Kitch", "elements": {}, "connections": {}}

    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))

    assert create.call_args.kwargs["title"] == "Function Plan Restore — PARTIAL"
    assert "not moved from old ID 42 to new ID 50 (OSError" in create.call_args.args[1]


def test_a_restore_as_new_whose_references_could_not_move_still_moves_its_backups() -> None:
    """(Review) one try block skipped the other step and named the wrong one as failed."""
    coordinator = _coordinator()
    coordinator.function_plan_backup = SimpleNamespace(async_rekey_fub_id=AsyncMock())
    coordinator.async_repoint_function_plan_fub_id = AsyncMock(side_effect=OSError("x"))  # type: ignore[method-assign]
    coordinator.hass.async_create_task = MagicMock()
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
    )
    snapshot = {"plan_name": "Kitch", "elements": {}, "connections": {}}

    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))

    coordinator.function_plan_backup.async_rekey_fub_id.assert_awaited_once_with(42, 50, "Kitch")
    message = create.call_args.args[1]
    assert "Backup lineage: moved from old ID 42 to new ID 50." in message
    assert f"References: {backup.ICON_WARNING} not updated — they still point at old ID 42 (OSError" in message
    assert create.call_args.kwargs["title"] == "Function Plan Restore — PARTIAL"
    coordinator.hass.async_create_task.assert_not_called()


def test_a_free_restore_lock_lets_a_plan_edit_through_and_visualize_never_waits() -> None:
    coordinator = _coordinator()
    with patch.object(_context.persistent_notification, "async_create") as create:
        assert _context.refuse_while_restoring(coordinator.hass, coordinator, "T") is False
    create.assert_not_called()

    resolver = AsyncMock(return_value=None)
    with patch.object(plan_actions, "_resolve_function_plan_context", resolver):
        asyncio.run(plan_actions._resolve_visualize_live_source(MagicMock(), SimpleNamespace(data={}), "T"))
    assert resolver.call_args.kwargs["edits_plan"] is False


def test_the_bus_load_cascade_stops_while_a_restore_runs() -> None:
    """(Review) a stop during a restore could leave a plan the restore just wrote stopped."""
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={CONF_FUNCTION_PLAN_PLAN_MAP: {"A": 1, "B": 2}})
    coordinator._watchdog_lock = asyncio.Lock()
    coordinator.api = SimpleNamespace(function_plan_stop_fup=AsyncMock(), function_plan_run_fup=AsyncMock())
    coordinator._async_finish_watchdog_cascade = AsyncMock()  # type: ignore[method-assign]

    async def run() -> None:
        async with coordinator._restore_lock:
            await coordinator._async_bus_load_cascade_restart(50.0)

    asyncio.run(run())
    coordinator.api.function_plan_stop_fup.assert_not_awaited()
    coordinator.api.function_plan_run_fup.assert_not_awaited()


def test_the_reload_after_an_uninstall_cleanup_waits_for_a_running_restore() -> None:
    coordinator = _coordinator()
    coordinator.async_uninstall_cleanup = AsyncMock(return_value={})  # type: ignore[method-assign]
    reload = coordinator.hass.config_entries.async_reload
    flow = SimpleNamespace(hass=coordinator.hass)

    async def run() -> None:
        async with coordinator._restore_lock:
            task = asyncio.create_task(
                ComexioRepairFlow._async_cleanup_then_reload(
                    flow,  # type: ignore[arg-type]
                    coordinator,
                    SimpleNamespace(entry_id="e1"),
                    "all",
                    "n1",
                    False,
                    None,
                )
            )
            # Past the cleanup's 0.5 s pause before its reload.
            await asyncio.sleep(0.7)
            reload.assert_not_awaited()
        await task
        reload.assert_awaited_once_with("e1")

    asyncio.run(run())


def test_a_failed_sync_and_the_restores_reload_reload_only_once() -> None:
    """A failed sync clears in_sync before its closing reload; the restore's queued reload must not follow it."""
    coordinator = _coordinator()
    reload = coordinator.hass.config_entries.async_reload

    async def sync_reload() -> None:
        await reload("e1")
        coordinator.hass.data[DOMAIN]["e1"] = SimpleNamespace(in_sync=False)  # the reload's new coordinator

    button = SimpleNamespace(coordinator=coordinator, _reset_sync_state_and_reload=sync_reload)

    async def run() -> bool:
        await coordinator._sync_lock.acquire()
        async with coordinator._restore_lock:
            finalize = asyncio.create_task(ComexioSyncButton._finalize_sync(button))  # type: ignore[arg-type]
            restore_reload = asyncio.create_task(coordinator.async_reload_entry("function plan restore"))
            await _settle()
        await finalize
        return await restore_reload

    assert asyncio.run(run()) is True  # covered by the sync's reload, not a failure
    assert reload.await_count == 1


def test_a_failed_reload_after_a_restore_is_reported() -> None:
    coordinator = SimpleNamespace(
        server_id="cx1", async_reload_entry=AsyncMock(side_effect=RuntimeError("entry not loaded"))
    )
    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._async_reload_after_restore(MagicMock(), coordinator))  # type: ignore[arg-type]
    create.assert_called_once()
    assert "Reload the Comexio integration manually" in create.call_args.args[1]


def test_a_reload_home_assistant_reports_as_failed_is_reported_too() -> None:
    """(Review) config_entries.async_reload returns False instead of raising for a failed setup."""
    coordinator = _coordinator()
    coordinator.hass.config_entries.async_reload = AsyncMock(return_value=False)
    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._async_reload_after_restore(MagicMock(), coordinator))
    create.assert_called_once()

    coordinator.hass.config_entries.async_reload = AsyncMock(return_value=True)
    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._async_reload_after_restore(MagicMock(), coordinator))
    create.assert_not_called()


@pytest.mark.parametrize("busy", ["sync_lock", "in_sync"])
def test_a_restore_does_not_start_while_a_sync_or_cleanup_runs(busy: str) -> None:
    """(Review) a sync or uninstall cleanup writes into or deletes the plans a restore rewrites."""
    coordinator = _coordinator()
    coordinator.in_sync = busy == "in_sync"
    run_restore = AsyncMock()

    async def run() -> None:
        if busy == "sync_lock":
            await coordinator._sync_lock.acquire()
        await backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={}))

    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", run_restore),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        asyncio.run(run())

    run_restore.assert_not_awaited()
    assert "uninstall cleanup" in create.call_args.args[1]


def test_a_sync_does_not_start_while_a_restore_runs() -> None:
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", data={}, options={})
    button = SimpleNamespace(coordinator=coordinator, hass=coordinator.hass, server_id="cx1")

    async def run() -> None:
        async with coordinator._restore_lock:
            with patch("custom_components.comexio.button.persistent_notification.async_create") as create:
                await ComexioSyncButton.async_handle_press(button)  # type: ignore[arg-type]
            assert "function plan restore" in create.call_args.args[1]
        assert not coordinator._sync_lock.locked()

    asyncio.run(run())


def test_an_uninstall_cleanup_does_not_start_while_a_restore_runs() -> None:
    coordinator = _coordinator()
    flow = SimpleNamespace(hass=coordinator.hass, _reraise_cleanup_issue=MagicMock())

    async def run() -> bool:
        async with coordinator._restore_lock:
            with patch("custom_components.comexio.repairs._post_result_notification"):
                return ComexioRepairFlow._cleanup_blocked_by_sync(flow, coordinator, "all", "n1", True)  # type: ignore[arg-type]

    assert asyncio.run(run()) is True
    flow._reraise_cleanup_issue.assert_called_once_with(coordinator, "all")


@pytest.mark.parametrize(
    ("in_sync", "sync_error", "failed_writes"), [(False, False, []), (True, True, []), (True, False, ["m1"])]
)
def test_the_waiting_text_never_replaces_a_failed_syncs_report(
    in_sync: bool, sync_error: bool, failed_writes: list[str]
) -> None:
    coordinator = _coordinator()
    coordinator.in_sync = in_sync
    coordinator.sync_error = sync_error
    coordinator.sync_failed_writes = failed_writes
    coordinator.sync_progress_text = "Sync failed: 1 write"

    async def run() -> None:
        async with coordinator._restore_lock:
            task = asyncio.create_task(coordinator.async_reload_entry("options changed"))
            await _settle()
        await task

    asyncio.run(run())
    assert coordinator.sync_progress_text == "Sync failed: 1 write"
    coordinator.async_update_listeners.assert_not_called()


def test_a_skipped_reload_counts_as_done_unless_the_entry_is_not_loaded() -> None:
    """(Review) a restore's reload must not report a skip as a failure, nor a missing entry as a reload."""
    coordinator = _coordinator()
    coordinator.in_sync = True
    assert asyncio.run(coordinator.async_reload_entry("x")) is True

    coordinator.in_sync = False
    coordinator.hass.data[DOMAIN] = {}
    assert asyncio.run(coordinator.async_reload_entry("x")) is False
    coordinator.hass.config_entries.async_reload.assert_not_awaited()
