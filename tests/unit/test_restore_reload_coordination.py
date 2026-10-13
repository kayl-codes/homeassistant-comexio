"""A reload waits for a running function plan restore; a failed restore never leaves its progress notice behind."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.repairs import ComexioRepairFlow
from custom_components.comexio.services import backup


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

    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", AsyncMock(side_effect=RuntimeError("Session is closed"))),
        patch.object(backup.persistent_notification, "async_create") as create,
        pytest.raises(RuntimeError),
    ):
        asyncio.run(backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={})))

    create.assert_called_once()
    assert create.call_args.kwargs["notification_id"] == "comexio_function_plan_restore_cx1"
    assert "Session is closed" in create.call_args.args[1]
    assert not coordinator._restore_lock.locked()


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

    async def run() -> None:
        await coordinator._sync_lock.acquire()
        async with coordinator._restore_lock:
            finalize = asyncio.create_task(ComexioSyncButton._finalize_sync(button))  # type: ignore[arg-type]
            restore_reload = asyncio.create_task(coordinator.async_reload_entry("function plan restore"))
            await _settle()
        await asyncio.gather(finalize, restore_reload)

    asyncio.run(run())
    assert reload.await_count == 1


def test_a_failed_reload_after_a_restore_is_reported() -> None:
    coordinator = SimpleNamespace(
        server_id="cx1", async_reload_entry=AsyncMock(side_effect=RuntimeError("entry not loaded"))
    )
    with patch.object(backup.persistent_notification, "async_create") as create:
        asyncio.run(backup._async_reload_after_restore(MagicMock(), coordinator))  # type: ignore[arg-type]
    create.assert_called_once()
    assert "Reload the Comexio integration manually" in create.call_args.args[1]
