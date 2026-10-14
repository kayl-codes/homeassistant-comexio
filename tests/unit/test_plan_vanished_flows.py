"""A deleted watched plan's repair, HA's own unload waiting for a restore, purge of recent orphans, pick repointing."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import (
    async_unload_entry,
    coordinator as coordinator_module,
    repairs,
)
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_FUB_ID,
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DOMAIN,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.plan_watchdog import vanished_plan_issue_id
from custom_components.comexio.repairs import ComexioRepairFlow
from custom_components.comexio.services import backup


@pytest.fixture(autouse=True)
def fast_unload_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "FUNCTION_PLAN_UNLOAD_POLL_SEC", 0.001)


def _coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.in_sync = False
    coordinator._restore_lock = asyncio.Lock()
    coordinator._restores_held_for_reload = False
    coordinator._plan_edits_running = 0
    coordinator._plan_edits_done = asyncio.Event()
    coordinator._plan_edits_done.set()
    return coordinator


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.001)


# (ck) HA's own reload (UI) must not close the session under a running restore.


def test_an_unload_waits_for_a_running_restore(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator()
    caplog.set_level(logging.INFO)

    async def run() -> None:
        async with coordinator._restore_lock:
            task = asyncio.create_task(coordinator.async_wait_before_unload())
            await _settle()
            assert not task.done()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())
    assert "Unload waits for the running function plan restore" in caplog.text


def test_an_unload_waits_for_a_running_plan_edit() -> None:
    coordinator = _coordinator()
    coordinator._plan_edits_running = 1
    coordinator._plan_edits_done.clear()

    async def run() -> None:
        task = asyncio.create_task(coordinator.async_wait_before_unload())
        await _settle()
        assert not task.done()
        coordinator._plan_edits_running = 0
        coordinator._plan_edits_done.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_the_reload_holding_the_restores_off_unloads_without_waiting() -> None:
    """async_reload_entry holds the lock itself: waiting for it would deadlock its own unload."""
    coordinator = _coordinator()

    async def run() -> None:
        async with coordinator.restores_held_off():
            await asyncio.wait_for(coordinator.async_wait_before_unload(), 1)
        assert coordinator._restores_held_for_reload is False
        with pytest.raises(RuntimeError):
            async with coordinator.restores_held_off():
                raise RuntimeError
        assert coordinator._restores_held_for_reload is False

    asyncio.run(run())


def test_an_unload_behind_a_queued_reload_goes_ahead_once_the_restore_ended() -> None:
    """(Review) Queueing for the lock behind async_reload_entry's reload deadlocked until the timeout.

    That reload gets the lock next and waits for HA's setup lock, which the unload's own reload holds.
    """
    coordinator = _coordinator()
    setup_lock_free = asyncio.Event()

    async def queued_reload() -> None:
        async with coordinator.restores_held_off():
            await setup_lock_free.wait()  # HA's setup lock, held by the reload running the unload below

    async def run() -> None:
        await coordinator._restore_lock.acquire()  # the restore
        reload = asyncio.create_task(queued_reload())
        await _settle()
        unload = asyncio.create_task(coordinator.async_wait_before_unload())
        await _settle()
        coordinator._restore_lock.release()  # restore over: the queued reload takes the lock
        await asyncio.wait_for(unload, 1)
        assert not reload.done()
        setup_lock_free.set()
        await reload

    asyncio.run(run())


def test_an_unload_gives_up_waiting_after_the_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(coordinator_module, "FUNCTION_PLAN_UNLOAD_WAIT_SEC", 0.05)
    coordinator = _coordinator()

    async def run() -> None:
        async with coordinator._restore_lock:
            await asyncio.wait_for(coordinator.async_wait_before_unload(), 1)
            assert coordinator._restore_lock.locked()

    asyncio.run(run())
    assert "Unloading although a function plan restore or edit still runs" in caplog.text


def test_unload_entry_waits_before_it_unloads_anything() -> None:
    order: list[str] = []
    running = SimpleNamespace(
        api=object(), async_wait_before_unload=AsyncMock(side_effect=lambda: order.append("wait"))
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": running}}
    hass.config_entries.async_unload_platforms = AsyncMock(side_effect=lambda *_: order.append("unload") or False)
    assert asyncio.run(async_unload_entry(hass, SimpleNamespace(entry_id="e1"))) is False
    assert order == ["wait", "unload"]


def test_unload_entry_of_an_entry_that_never_set_up_does_not_wait() -> None:
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    assert asyncio.run(async_unload_entry(hass, SimpleNamespace(entry_id="e1"))) is False


# (cn) purge: include_recent deletes orphans of any age, the default keeps the retention.


def _purge(data: dict[str, Any], purged: list[dict[str, Any]]) -> tuple[SimpleNamespace, MagicMock]:
    manager = SimpleNamespace(async_purge_orphaned=AsyncMock(return_value=purged))
    coordinator = SimpleNamespace(
        server_id="cx1",
        live_plan_list=lambda: {"1": {"Name": "A"}},
        function_plan_backup=manager,
        config_entry=SimpleNamespace(options={}),
        async_update_listeners=MagicMock(),
        _restore_lock=asyncio.Lock(),
        plan_edit=MagicMock(),
    )
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_refresh_service_descriptions", AsyncMock()),
        patch.object(backup, "delete_orphaned_backup_issue"),
        patch.object(backup, "retention_cutoff", return_value="RETENTION"),
        patch.object(backup.dt_util, "utcnow", return_value="NOW"),
        patch.object(backup.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(backup._handle_function_plan_purge_orphaned_backups(MagicMock(), SimpleNamespace(data=data)))
    return manager, notify


@pytest.mark.parametrize(
    ("include_recent", "cutoff", "age"),
    [
        (False, "RETENTION", f"older than {DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS} month(s)"),
        (True, "NOW", "of any age"),
    ],
)
@pytest.mark.parametrize("purged", [[], [{"fub_id": 47, "plan_name": "Test4", "removed": 3}]])
def test_purge_cutoff_follows_include_recent(
    include_recent: bool, cutoff: str, age: str, purged: list[dict[str, Any]]
) -> None:
    data = {"confirm": True} | ({"include_recent": True} if include_recent else {})
    manager, notify = _purge(data, purged)
    assert manager.async_purge_orphaned.await_args.kwargs == {"cutoff": cutoff}
    assert age in notify.call_args.args[1]


# (cl) The repair of a watched user plan deleted in Comexio.

ISSUE_DATA = {"entry_id": "e1", "plan_name": "Test4", "fub_id": "47"}


def _vanished_flow(vanished: dict[int, str] | None, *, backup_field: str | None = "47:auto:0:Test4") -> tuple:
    coordinator = SimpleNamespace(
        server_id="cx1",
        plan_watchdog=SimpleNamespace(vanished=vanished),
        async_unwatch_user_plan=AsyncMock(return_value=True),
        async_newest_backup_field=AsyncMock(return_value=backup_field),
        async_restore_vanished_plan=MagicMock(return_value="restore-coroutine"),
    )
    flow = ComexioRepairFlow(vanished_plan_issue_id("cx1", 47), dict(ISSUE_DATA))
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        data={DOMAIN: {"e1": coordinator}},
        config=SimpleNamespace(language="en"),
        async_create_background_task=MagicMock(),
    )
    return flow, coordinator


def _step(flow: ComexioRepairFlow, user_input: dict[str, Any] | None) -> tuple[dict[str, Any], MagicMock]:
    with patch.object(repairs.ir, "async_delete_issue") as delete_issue:
        # init routes by the issue id and shows the form; the answer comes back to the step itself.
        step = flow.async_step_init if user_input is None else flow.async_step_function_plan_vanished
        result = asyncio.run(step(user_input))
    return result, delete_issue


def test_the_vanished_repair_offers_restore_and_unwatch() -> None:
    flow, _ = _vanished_flow({47: "Test4"})
    result, _ = _step(flow, None)
    assert result["step_id"] == "function_plan_vanished"
    assert result["description_placeholders"] == {"plan_name": "Test4", "fub_id": "47"}


def test_the_vanished_repair_restores_in_the_background() -> None:
    flow, coordinator = _vanished_flow({47: "Test4"})
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["type"] == "create_entry"
    coordinator.async_restore_vanished_plan.assert_called_once_with(47, "Test4")
    flow.hass.async_create_background_task.assert_called_once()
    assert flow.hass.async_create_background_task.call_args.args == ("restore-coroutine",)
    delete_issue.assert_not_called()  # the next check removes it once the plan is back


def test_the_vanished_repair_without_a_backup_restores_nothing() -> None:
    flow, coordinator = _vanished_flow({47: "Test4"}, backup_field=None)
    result, _ = _step(flow, {"action": "restore"})
    assert result["reason"] == "no_backup"
    coordinator.async_restore_vanished_plan.assert_not_called()
    flow.hass.async_create_background_task.assert_not_called()


def test_the_vanished_repair_can_stop_watching() -> None:
    flow, coordinator = _vanished_flow({47: "Test4"})
    result, _ = _step(flow, {"action": "unwatch"})
    assert result["type"] == "create_entry"
    coordinator.async_unwatch_user_plan.assert_awaited_once_with(47, "Test4")


def test_a_vanished_repair_whose_plan_is_back_is_removed() -> None:
    flow, coordinator = _vanished_flow({})
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["reason"] == "plan_not_missing"
    delete_issue.assert_called_once_with(flow.hass, DOMAIN, vanished_plan_issue_id("cx1", 47))
    coordinator.async_unwatch_user_plan.assert_not_awaited()
    coordinator.async_restore_vanished_plan.assert_not_called()


def test_a_vanished_repair_before_the_first_check_stays() -> None:
    """(Review) Not checked since setup is unknown, not "no longer missing"."""
    flow, coordinator = _vanished_flow(None)
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["reason"] == "check_pending"
    delete_issue.assert_not_called()
    coordinator.async_restore_vanished_plan.assert_not_called()


def test_a_vanished_repair_of_a_removed_entry_aborts() -> None:
    flow, _ = _vanished_flow({47: "Test4"})
    flow.hass.data = {DOMAIN: {}}
    result, _ = _step(flow, {"action": "restore"})
    assert result["reason"] == "entry_not_found"


# (cm) A restore-as-new moves the watchdog pick with the plan.


def test_a_restore_as_new_moves_the_watchdog_pick_in_the_options() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4", "19:Other"], CONF_FUNCTION_PLAN_FUB_ID: "19"}
    coordinator.config_entry = SimpleNamespace(options=options)
    coordinator.request_options_update_without_reload = MagicMock()  # type: ignore[method-assign]
    updated = asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 47, 52))
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["52:Test4", "19:Other"]
    assert new_options[CONF_FUNCTION_PLAN_FUB_ID] == "19"
    assert updated == ["function plan watchdog pick 'Test4' → fub_id 52"]

    coordinator.request_options_update_without_reload.reset_mock()
    assert asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 48, 52)) == []
    coordinator.request_options_update_without_reload.assert_not_called()
