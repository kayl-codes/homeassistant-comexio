"""A reload waits for a running function plan restore; a failed restore never leaves its progress notice behind."""

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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
    coordinator._plan_edits_running = 0
    coordinator._plan_edits_done = asyncio.Event()
    coordinator._plan_edits_done.set()
    coordinator.unloading = False
    coordinator._restored_as_new = {}
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

    async def post_progress_then_fail(*_args: Any) -> None:
        # (Review) as the restore paths do: the progress notice is up when the error hits.
        backup.persistent_notification.async_create(
            hass, "Restoring…", title="P", notification_id=backup._restore_notification_id(coordinator)
        )
        raise RuntimeError("Session is closed")

    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", AsyncMock(side_effect=post_progress_then_fail)),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        restore = backup._handle_function_plan_restore(hass, call)
        with pytest.raises(RuntimeError):
            asyncio.run(restore)

    assert [c.args[1] for c in create.call_args_list][0] == "Restoring…"
    assert {c.kwargs["notification_id"] for c in create.call_args_list} == {"comexio_function_plan_restore_cx1"}
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
        assert _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T") is False
    create.assert_not_called()

    resolver = AsyncMock(return_value=None)
    with patch.object(plan_actions, "_resolve_function_plan_context", resolver):
        asyncio.run(plan_actions._resolve_visualize_live_source(MagicMock(), SimpleNamespace(data={}), "T"))
    assert resolver.call_args.kwargs["edits_plan"] is False


def test_the_real_resolver_lets_a_read_only_call_through_while_a_restore_runs() -> None:
    """(Review) with the resolver mocked, edits_plan=False being ignored went unnoticed."""
    coordinator = _coordinator()
    coordinator.api = SimpleNamespace(fub_data={"1": {"Name": "Lights"}}, login=AsyncMock(return_value=True))
    call = SimpleNamespace(data={"fub_id": "1"})

    async def run() -> tuple[Any, ...] | None:
        async with coordinator._restore_lock:
            return await _context._resolve_function_plan_context(coordinator.hass, call, "T", edits_plan=False)

    with (
        patch.object(_context, "_resolve_coordinator", return_value=coordinator),
        patch.object(_context.persistent_notification, "async_create") as create,
    ):
        ctx = asyncio.run(run())
    assert ctx == (coordinator, coordinator.api, 1)
    create.assert_not_called()


def test_no_restore_or_plan_edit_starts_while_the_entry_unloads() -> None:
    """(Sourcery) One starting after the unload's wait would lose the API session mid-way."""
    coordinator = _coordinator()
    coordinator.unloading = True
    run_restore = AsyncMock()
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", run_restore),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        asyncio.run(backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={})))
        run_restore.assert_not_awaited()
        assert _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T") is True
    assert create.call_args.args[1] == _context.SYNC_RUNNING_MSG


def test_a_restore_as_new_tells_the_coordinator_it_built_the_plan() -> None:
    """A Restore from a watchdog push judges its outcome by this, not by a plan of that name."""
    coordinator = _coordinator()
    coordinator.function_plan_backup = SimpleNamespace(async_rekey_fub_id=AsyncMock())
    coordinator.async_repoint_function_plan_fub_id = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=AsyncMock(return_value=True),
    )
    snapshot = {"plan_name": "Kitch", "elements": {}, "connections": {}}
    with patch.object(backup.persistent_notification, "async_create"):
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))
    assert coordinator._restored_as_new == {"Kitch": (50, True)}
    coordinator.async_repoint_function_plan_fub_id.assert_awaited_once_with("Kitch", 42, 50, old_id_still_live=False)

    # (Sourcery) built, but not completely: restored, and the push says so
    api.function_plan_rebuild_plan_from_snapshot.return_value = ({}, 0, ["block 3 not recreated"])
    with patch.object(backup.persistent_notification, "async_create"):
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))
    assert coordinator._restored_as_new == {"Kitch": (50, False)}

    # (Review) built completely, but its references did not move: PARTIAL as well
    api.function_plan_rebuild_plan_from_snapshot.return_value = ({}, 0, [])
    coordinator.async_repoint_function_plan_fub_id.side_effect = RuntimeError("options")
    with patch.object(backup.persistent_notification, "async_create"):
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))
    assert coordinator._restored_as_new == {"Kitch": (50, False)}

    coordinator._restored_as_new = {}
    api.function_plan_rebuild_plan_from_snapshot.side_effect = TimeoutError
    with patch.object(backup.persistent_notification, "async_create"):
        asyncio.run(backup._restore_plan_as_new(coordinator.hass, coordinator, api, 42, snapshot, "auto", 0))
    assert coordinator._restored_as_new == {}  # half built: not restored


def test_a_restore_does_not_start_while_a_plan_edit_runs() -> None:
    """(Review) checked only before its writes, an edit could still run into a restore started meanwhile."""
    coordinator = _coordinator()
    run_restore = AsyncMock()
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", run_restore),
        patch.object(backup.persistent_notification, "async_create") as create,
        coordinator.plan_edit(),
    ):
        asyncio.run(backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={})))

    run_restore.assert_not_awaited()
    assert "function plan edit" in create.call_args.args[1]
    assert create.call_args.kwargs["notification_id"] == "comexio_function_plan_restore_blocked_cx1"
    assert not coordinator.plan_edits_running


class _WriteSeen(Exception):
    """Stops a handler at its first plan write, after recording whether the write ran marked."""


@pytest.mark.parametrize(
    "handler", [plan_actions.handle_function_plan_stop, plan_actions.handle_function_plan_activate]
)
def test_plan_run_and_stop_write_as_a_marked_plan_edit(handler: Any) -> None:
    coordinator = _coordinator()
    marked: list[bool] = []

    async def write(_fub_id: int) -> bool:
        marked.append(coordinator.plan_edits_running)
        return True

    coordinator.api = SimpleNamespace(
        fub_data={"1": {"Name": "Lights"}},
        login=AsyncMock(return_value=True),
        function_plan_stop_fup=write,
        function_plan_run_fup=write,
    )
    coordinator.async_plan_transition = lambda *_a: contextlib.nullcontext()  # type: ignore[method-assign]
    with (
        patch.object(_context, "_resolve_coordinator", return_value=coordinator),
        patch.object(plan_actions, "_report_run_stop_result"),
    ):
        asyncio.run(handler(coordinator.hass, SimpleNamespace(data={"fub_id": "1"})))
    assert marked == [True]
    assert not coordinator.plan_edits_running


def test_a_marker_delete_writes_as_a_marked_plan_edit() -> None:
    """(Review) checked at delete_marker itself, not at the eligibility read before it."""
    coordinator = _coordinator()
    marked: list[bool] = []

    async def delete_marker(_marker_id: int) -> bool:
        marked.append(coordinator.plan_edits_running)
        raise _WriteSeen

    api = SimpleNamespace(
        get_marker_delete_eligibility=AsyncMock(return_value=([5], [], {5}, None)), delete_marker=delete_marker
    )
    call = SimpleNamespace(data={"marker_id": "5", "confirm": True})
    delete = marker_actions.handle_marker_delete(coordinator.hass, call)
    with (
        patch.object(marker_actions, "_async_get_service_context", AsyncMock(return_value=(coordinator, api, None))),
        patch.object(marker_actions, "_ensure_comexio_login", AsyncMock(return_value=True)),
        pytest.raises(_WriteSeen),
    ):
        asyncio.run(delete)
    assert marked == [True]
    assert not coordinator.plan_edits_running


def test_a_connect_writes_as_a_marked_plan_edit() -> None:
    coordinator = _coordinator()
    coordinator.data = {"markers": [{"id": "2"}], "webio_commands": {}}
    coordinator.config_entry = SimpleNamespace(entry_id="e1", data={}, options={})
    marked: list[bool] = []

    async def stop_plan(_fub_id: int) -> None:
        marked.append(coordinator.plan_edits_running)
        raise _WriteSeen

    # (Review) checked at the plan stop, the first write to the plan — the change backup before it is no plan write.
    coordinator.async_function_plan_change_backup = AsyncMock()  # type: ignore[method-assign]
    api = SimpleNamespace(
        fub_data={"1": {"Active": True}}, login=AsyncMock(return_value=True), function_plan_stop_fup=stop_plan
    )
    connect_call = connect.handle_function_plan_connect(coordinator.hass, SimpleNamespace(data={"marker_id": "2"}))
    with (
        patch.object(connect, "_resolve_function_plan", return_value=(coordinator, api, 1)),
        pytest.raises(_WriteSeen),
    ):
        asyncio.run(connect_call)
    assert marked == [True]
    assert not coordinator.plan_edits_running


def test_the_bus_load_cascade_restarts_plans_as_a_marked_plan_edit() -> None:
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={CONF_FUNCTION_PLAN_PLAN_MAP: {"A": 1}})
    coordinator._watchdog_lock = asyncio.Lock()
    coordinator.bus_workload = None
    marked: list[bool] = []

    async def write(_fub_id: int) -> bool:
        marked.append(coordinator.plan_edits_running)
        return True

    coordinator.api = SimpleNamespace(function_plan_stop_fup=write, function_plan_run_fup=write)
    coordinator._async_finish_watchdog_cascade = AsyncMock()  # type: ignore[method-assign]
    with patch("custom_components.comexio.coordinator.asyncio.sleep", AsyncMock()):
        asyncio.run(coordinator._async_bus_load_cascade_restart(50.0))
    assert marked == [True, True]
    assert not coordinator.plan_edits_running
    coordinator._async_finish_watchdog_cascade.assert_awaited_once_with(50.0, None, 1)


@pytest.mark.parametrize(
    ("status", "promoted", "expected"),
    [("OK", True, "OK"), ("OK", None, "OK"), ("OK", "OSError", "PARTIAL"), ("FAILED", "OSError", "FAILED")],
)
def test_a_restore_whose_snapshot_could_not_be_marked_restored_is_partial(
    status: str, promoted: bool | str | None, expected: str
) -> None:
    """(Review) the result said OK while its own message named the backup-history step as failed."""
    assert backup._status_after_promotion(status, promoted) == expected


def test_a_network_error_never_puts_its_message_into_a_restore_notification() -> None:
    """(Review) aiohttp's message carries the request URL, which every notification viewer would see."""
    coordinator = _coordinator()
    error = aiohttp.ClientError("401, url='http://cx/admin/?token=s3cret'")
    coordinator.async_function_plan_change_backup = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    api = SimpleNamespace(fub_data={"1": {"Name": "Lights"}}, get_raw_config=AsyncMock(side_effect=error))

    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._restore_plan_in_place(coordinator.hass, coordinator, api, 1, {}, "auto", 0, None))
    assert "ClientError — details in the Home Assistant log" in create.call_args.args[1]
    assert "s3cret" not in create.call_args.args[1]

    _entry, why = asyncio.run(backup._fetch_live_plan(api, 1, as_copy=False))
    assert why is not None
    assert "ClientError" in why
    assert "s3cret" not in why


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


@pytest.mark.parametrize("busy", ["sync_lock", "in_sync"])
def test_a_plan_edit_does_not_start_while_a_sync_or_cleanup_runs(busy: str) -> None:
    """(Review) a sync, range check or cleanup writes into or deletes the plans an edit changes."""
    coordinator = _coordinator()
    coordinator.in_sync = busy == "in_sync"

    async def run() -> bool:
        if busy == "sync_lock":
            await coordinator._sync_lock.acquire()
        return _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T")

    with patch.object(_context.persistent_notification, "async_create") as create:
        assert asyncio.run(run()) is True
    assert create.call_args.args[1] == _context.SYNC_RUNNING_MSG


def test_a_sync_does_not_start_while_a_plan_edit_runs() -> None:
    """(Review) a sort or connect meanwhile could be overwritten by the sync's plan writes."""
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", data={}, options={})
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = coordinator
    button.hass = coordinator.hass
    button.server_id = "cx1"

    async def run() -> None:
        with (
            coordinator.plan_edit(),
            patch("custom_components.comexio.button.persistent_notification.async_create") as create,
        ):
            await button.async_handle_press()
        assert "function plan edit" in create.call_args.args[1]
        assert not coordinator._sync_lock.locked()

    asyncio.run(run())


def test_an_uninstall_cleanup_does_not_start_while_a_plan_edit_runs() -> None:
    coordinator = _coordinator()
    flow = SimpleNamespace(hass=coordinator.hass, _reraise_cleanup_issue=MagicMock())
    with coordinator.plan_edit(), patch("custom_components.comexio.repairs._post_result_notification") as post:
        assert ComexioRepairFlow._cleanup_blocked_by_sync(flow, coordinator, "all", "n1", True) is True  # type: ignore[arg-type]
    assert "function plan edit" in post.call_args.args[2]


@pytest.mark.parametrize("busy", ["sync", "plan_edit", "unloading"])
def test_the_bus_load_cascade_stops_while_a_sync_or_plan_edit_runs(busy: str) -> None:
    """(Review) a cascade stopping a plan a sort or connect is changing leaves it stopped or mixed."""
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={CONF_FUNCTION_PLAN_PLAN_MAP: {"A": 1}})
    coordinator._watchdog_lock = asyncio.Lock()
    coordinator.api = SimpleNamespace(function_plan_stop_fup=AsyncMock(), function_plan_run_fup=AsyncMock())
    coordinator._async_finish_watchdog_cascade = AsyncMock()  # type: ignore[method-assign]

    async def run() -> None:
        async with contextlib.AsyncExitStack() as stack:
            if busy == "sync":
                await stack.enter_async_context(coordinator._sync_lock)
            elif busy == "unloading":
                coordinator.unloading = True  # the unload closes the session the cascade would stop plans through
            else:
                stack.enter_context(coordinator.plan_edit())
            await coordinator._async_bus_load_cascade_restart(50.0)

    asyncio.run(run())
    coordinator.api.function_plan_stop_fup.assert_not_awaited()
    coordinator._async_finish_watchdog_cascade.assert_not_awaited()


def test_a_plan_edit_does_not_start_while_another_plan_edit_runs() -> None:
    """(Review) two edits of one plan load the same topology, and the later save undoes the other."""
    coordinator = _coordinator()
    with coordinator.plan_edit(), patch.object(_context.persistent_notification, "async_create") as create:
        assert _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T") is True
    assert create.call_args.args[1] == _context.PLAN_EDIT_RUNNING_MSG
    with patch.object(_context.persistent_notification, "async_create") as create:
        assert _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T") is False
    create.assert_not_called()


def test_a_reload_waits_for_the_last_of_overlapping_plan_edits() -> None:
    coordinator = _coordinator()
    reload = coordinator.hass.config_entries.async_reload

    async def run() -> None:
        first, second = coordinator.plan_edit(), coordinator.plan_edit()
        first.__enter__()
        second.__enter__()
        task = asyncio.create_task(coordinator.async_reload_entry("options changed"))
        first.__exit__(None, None, None)
        await _settle()
        reload.assert_not_awaited()
        second.__exit__(None, None, None)
        await task
        reload.assert_awaited_once_with("e1")

    asyncio.run(run())


def test_a_reload_waits_for_a_running_plan_edit_and_keeps_new_ones_out() -> None:
    """(Review) a reload closes the API session under the requests a sort or connect still makes."""
    coordinator = _coordinator()
    reload = coordinator.hass.config_entries.async_reload

    async def run() -> None:
        with coordinator.plan_edit():
            task = asyncio.create_task(coordinator.async_reload_entry("options changed"))
            await _settle()
            reload.assert_not_awaited()
            # The reload holds _sync_lock while it waits: a new edit refuses instead of starting.
            with patch.object(_context.persistent_notification, "async_create") as create:
                assert _context.refuse_while_plans_busy(coordinator.hass, coordinator, "T") is True
            assert create.call_args.args[1] == _context.SYNC_RUNNING_MSG
        await task
        reload.assert_awaited_once_with("e1")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("handler", "data"),
    [
        (backup._handle_function_plan_delete_backups, {"confirm": True}),
        (backup._handle_function_plan_purge_orphaned_backups, {"confirm": True}),
    ],
)
def test_backups_are_not_deleted_while_a_restore_runs(handler: Any, data: dict[str, Any]) -> None:
    """(Review) a restore moving or marking the deleted backups could write them back."""
    coordinator = _coordinator()
    coordinator.function_plan_backup = SimpleNamespace(
        async_delete_all_backups=AsyncMock(), async_purge_orphaned=AsyncMock()
    )
    coordinator.config_entry = SimpleNamespace(entry_id="e1", data={}, options={})
    coordinator.live_plan_list = MagicMock(return_value={1: "A"})  # type: ignore[method-assign]

    async def run() -> None:
        async with coordinator._restore_lock:
            await handler(coordinator.hass, SimpleNamespace(data=data))

    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup.persistent_notification, "async_create") as create,
    ):
        asyncio.run(run())

    coordinator.function_plan_backup.async_delete_all_backups.assert_not_awaited()
    coordinator.function_plan_backup.async_purge_orphaned.assert_not_awaited()
    assert "restore is running" in create.call_args.args[1]


def test_a_backup_deletion_runs_as_a_marked_plan_edit() -> None:
    coordinator = _coordinator()
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    marked: list[bool] = []

    async def delete_all() -> int:
        marked.append(coordinator.plan_edits_running)
        return 0

    coordinator.function_plan_backup = SimpleNamespace(async_delete_all_backups=delete_all)
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_refresh_service_descriptions", AsyncMock()),
        patch.object(backup, "delete_all_orphaned_backup_issues"),
        patch.object(backup.persistent_notification, "async_create"),
    ):
        asyncio.run(
            backup._handle_function_plan_delete_backups(coordinator.hass, SimpleNamespace(data={"confirm": True}))
        )
    assert marked == [True]
    assert not coordinator.plan_edits_running


def test_a_sync_whose_reload_did_not_load_the_integration_again_is_reported() -> None:
    """(Review) config_entries.async_reload returns False instead of raising for a failed setup."""
    coordinator = _coordinator()
    coordinator.config_entry = SimpleNamespace(entry_id="e1", data={}, options={})
    coordinator.hass.config_entries.async_reload = AsyncMock(return_value=False)
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = coordinator
    button.hass = coordinator.hass
    button.server_id = "cx1"
    button.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

    with patch("custom_components.comexio.button.persistent_notification.async_create") as create:
        asyncio.run(button._reset_sync_state_and_reload())
    assert create.call_args.kwargs["notification_id"] == "comexio_sync_reload_cx1"

    coordinator.hass.config_entries.async_reload = AsyncMock(return_value=True)
    with patch("custom_components.comexio.button.persistent_notification.async_create") as create:
        asyncio.run(button._reset_sync_state_and_reload())
    create.assert_not_called()


def test_a_cleanup_whose_reload_did_not_load_the_integration_again_is_reported() -> None:
    coordinator = _coordinator()
    coordinator.async_uninstall_cleanup = AsyncMock(return_value={})  # type: ignore[method-assign]
    coordinator.hass.config_entries.async_reload = AsyncMock(return_value=False)
    flow = SimpleNamespace(hass=coordinator.hass)
    with patch("custom_components.comexio.repairs.persistent_notification.async_create") as create:
        asyncio.run(
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
    assert create.call_args.kwargs["notification_id"] == "comexio_cleanup_reload_cx1"


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
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = coordinator
    button.hass = coordinator.hass
    button.server_id = "cx1"

    async def run() -> None:
        async with coordinator._restore_lock:
            with patch("custom_components.comexio.button.persistent_notification.async_create") as create:
                await button.async_handle_press()
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
