"""A deleted watched plan's repair, HA's own unload waiting for a sync or restore, orphan purge, pick moves."""

import asyncio
import contextlib
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
from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_FUB_ID,
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DOMAIN,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager, PlanBackupsNotMovedError
from custom_components.comexio.plan_watchdog import unnamed_plan_label, vanished_plan_issue_id
from custom_components.comexio.repairs import (
    ACTION_RESTORE,
    ACTION_TAKE_OVER_RENAME,
    ACTION_UNWATCH,
    ACTION_WATCH_HOLDER,
    ComexioRepairFlow,
)
from custom_components.comexio.services import backup
from custom_components.comexio.services._context import SYNC_RUNNING_MSG, refuse_while_plans_busy


@pytest.fixture(autouse=True)
def fast_unload_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "FUNCTION_PLAN_UNLOAD_POLL_SEC", 0.001)


def _coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.in_sync = False
    coordinator._sync_lock = asyncio.Lock()
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
    assert "Unload waits for the running sync or function plan restore" in caplog.text


@pytest.mark.parametrize("held_by", ["in_sync", "sync_lock"])
def test_an_unload_waits_for_a_running_sync_until_its_closing_reload(held_by: str) -> None:
    """(Sourcery) HA's own reload closed the API session under a running sync."""
    coordinator = _coordinator()

    async def run() -> None:
        await coordinator._sync_lock.acquire()
        coordinator.in_sync = held_by == "in_sync"
        if held_by == "in_sync":
            coordinator._sync_lock.release()
        unload = asyncio.create_task(coordinator.async_wait_before_unload())
        await _settle()
        assert not unload.done()
        async with coordinator.restores_held_off():  # the sync's closing reload
            await asyncio.wait_for(unload, 1)

    asyncio.run(run())


def test_an_unload_after_the_sync_ended_goes_ahead() -> None:
    coordinator = _coordinator()

    async def run() -> None:
        coordinator.in_sync = True
        unload = asyncio.create_task(coordinator.async_wait_before_unload())
        await _settle()
        coordinator.in_sync = False
        await asyncio.wait_for(unload, 1)

    asyncio.run(run())


def test_no_sync_starts_while_the_entry_unloads() -> None:
    """A sync started after the unload's wait would lose the API session mid-way."""
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(
        _sync_lock=asyncio.Lock(), _restore_lock=asyncio.Lock(), plan_edits_running=False, unloading=True
    )
    button.server_id = "cx1"
    button._notify_sync_blocked = MagicMock()  # type: ignore[method-assign]
    assert asyncio.run(button._async_acquire_sync_lock(True)) is False
    assert "the integration is reloading" in button._notify_sync_blocked.call_args.args[1]
    assert not button.coordinator._sync_lock.locked()


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
        held_during_failure: list[bool] = []

        async def failing_reload() -> None:
            async with coordinator.restores_held_off():
                held_during_failure.append(coordinator._restores_held_for_reload)
                raise RuntimeError

        with pytest.raises(RuntimeError):
            await failing_reload()
        # The RuntimeError came from inside the hold, not from entering it.
        assert held_during_failure == [True]
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
    assert "Unloading although a sync, function plan restore or plan edit still runs" in caplog.text


def test_unload_entry_waits_before_it_unloads_anything() -> None:
    order: list[str] = []
    running = SimpleNamespace(
        api=object(), unloading=True, async_wait_before_unload=AsyncMock(side_effect=lambda: order.append("wait"))
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": running}}
    hass.config_entries.async_unload_platforms = AsyncMock(side_effect=lambda *_: order.append("unload") or False)
    assert asyncio.run(async_unload_entry(hass, SimpleNamespace(entry_id="e1"))) is False
    assert order == ["wait", "unload"]
    assert running.unloading is False  # the entry stays loaded: restores and plan edits start again


@pytest.mark.parametrize("raised_by", ["wait", "unload"])
def test_an_unload_that_raises_lets_restores_and_plan_edits_start_again(raised_by: str) -> None:
    """(Review) The entry stays loaded: a stuck unloading flag would refuse every restore as "sync running"."""
    running = SimpleNamespace(api=object(), unloading=True, async_wait_before_unload=AsyncMock())
    if raised_by == "wait":  # (Sourcery) e.g. the unload task cancelled while it waits
        running.async_wait_before_unload.side_effect = RuntimeError("boom")
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": running}}
    hass.config_entries.async_unload_platforms = AsyncMock(side_effect=RuntimeError("boom"))
    unload = async_unload_entry(hass, SimpleNamespace(entry_id="e1"))
    with pytest.raises(RuntimeError):
        asyncio.run(unload)
    assert running.unloading is False
    assert hass.config_entries.async_unload_platforms.await_count == int(raised_by == "unload")


@pytest.mark.parametrize("held_for_reload", [False, True])
def test_an_unload_lets_no_restore_or_plan_edit_start_after_its_wait(held_for_reload: bool) -> None:
    """(Sourcery) Work starting between the wait and the session's close would lose the session."""
    coordinator = _coordinator()
    coordinator.unloading = False
    coordinator._sync_lock = asyncio.Lock()
    coordinator._restores_held_for_reload = held_for_reload
    with patch("custom_components.comexio.services._context.persistent_notification.async_create") as create:
        assert refuse_while_plans_busy(MagicMock(), coordinator, "T") is False
        asyncio.run(coordinator.async_wait_before_unload())
        assert coordinator.unloading is True
        # (Sourcery) the entry points' own guard refuses now: plan edits, the restore service, plan starts
        assert refuse_while_plans_busy(MagicMock(), coordinator, "T") is True
    assert create.call_args.args[1] == SYNC_RUNNING_MSG


@pytest.mark.parametrize("unload_ok", [True, False])
def test_an_unload_closes_the_watchdog_only_once_the_platforms_unloaded(unload_ok: bool) -> None:
    """(Sourcery, #187) The reloaded entry's watchdog takes the pushes over; a failed unload keeps them, and the
    work the unload waited for still sent its own."""
    order: list[str] = []
    running = SimpleNamespace(
        api=MagicMock(),
        unloading=True,
        async_wait_before_unload=AsyncMock(side_effect=lambda: order.append("wait")),
        async_shutdown=AsyncMock(),
        plan_watchdog=SimpleNamespace(close=lambda: order.append("close")),
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": running}}
    hass.config_entries.async_unload_platforms = AsyncMock(side_effect=lambda *_: order.append("unload") or unload_ok)
    assert asyncio.run(async_unload_entry(hass, SimpleNamespace(entry_id="e1"))) is unload_ok
    assert order == (["wait", "unload", "close"] if unload_ok else ["wait", "unload"])


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
        unloading=False,
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


def test_purge_reads_string_flags_as_ha_does() -> None:
    """(Sourcery) bool("false") is True: "false" purged the young orphans, and an unconfirmed purge ran."""
    manager, _notify = _purge({"confirm": "true", "include_recent": "false"}, [])
    assert manager.async_purge_orphaned.await_args.kwargs == {"cutoff": "RETENTION"}
    manager, notify = _purge({"confirm": "false", "include_recent": True}, [])
    manager.async_purge_orphaned.assert_not_awaited()
    assert "'confirm' must be enabled" in notify.call_args.args[1]


# (cl) The repair of a watched user plan deleted in Comexio.

ISSUE_DATA = {"entry_id": "e1", "plan_name": "Test4", "fub_id": "47"}


def _vanished_flow(
    vanished: set[tuple[int, str]] | None,
    *,
    backup_field: str | None = "47:auto:0:Test4",
    issue_data: dict[str, str] = ISSUE_DATA,
) -> tuple:
    coordinator = SimpleNamespace(
        server_id="cx1",
        plan_watchdog=SimpleNamespace(vanished=vanished),
        async_unwatch_user_plan=AsyncMock(return_value=True),
        async_newest_backup_field=AsyncMock(return_value=backup_field),
        async_restore_vanished_plan=MagicMock(return_value="restore-coroutine"),
        function_plan_backup=SimpleNamespace(async_load=AsyncMock()),
        plan_rename_suspected=MagicMock(return_value=False),
        async_watch_id_holder=AsyncMock(return_value=True),
        unloading=False,
        _restore_lock=asyncio.Lock(),
        plan_edit=MagicMock(side_effect=contextlib.nullcontext),
        vanished_restore_running=MagicMock(return_value=False),
    )
    issue_id = vanished_plan_issue_id("cx1", int(issue_data["fub_id"]), issue_data["plan_name"])
    flow = ComexioRepairFlow(issue_id, dict(issue_data))
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
    flow, _ = _vanished_flow({(47, "Test4")})
    result, _ = _step(flow, None)
    assert result["step_id"] == "function_plan_vanished"
    assert result["description_placeholders"] == {"plan_name": "Test4", "fub_id": "47"}
    # (Sourcery) both choices are offered, Restore preselected
    (field,) = result["data_schema"].schema
    assert field.default() == ACTION_RESTORE
    assert result["data_schema"].schema[field].config["options"] == [ACTION_RESTORE, ACTION_UNWATCH]


def test_the_vanished_repair_restores_in_the_background() -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")})
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["type"] == "create_entry"
    coordinator.async_restore_vanished_plan.assert_called_once_with(47, "Test4")
    flow.hass.async_create_background_task.assert_called_once()
    assert flow.hass.async_create_background_task.call_args.args == ("restore-coroutine",)
    delete_issue.assert_not_called()  # the next check removes it once the plan is back


def test_the_vanished_repair_without_a_backup_restores_nothing() -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")}, backup_field=None)
    result, _ = _step(flow, {"action": "restore"})
    assert result["reason"] == "no_backup"
    coordinator.async_restore_vanished_plan.assert_not_called()
    flow.hass.async_create_background_task.assert_not_called()


def test_the_vanished_repair_can_stop_watching() -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")})
    result, _ = _step(flow, {"action": "unwatch"})
    assert result["type"] == "create_entry"
    coordinator.async_unwatch_user_plan.assert_awaited_once_with(47, "Test4")


def test_a_vanished_repair_whose_plan_is_back_is_removed() -> None:
    flow, coordinator = _vanished_flow({(47, "Other")})  # another deleted plan on its ID is not it
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["reason"] == "plan_not_missing"
    delete_issue.assert_called_once_with(flow.hass, DOMAIN, vanished_plan_issue_id("cx1", 47, "Test4"))
    coordinator.async_unwatch_user_plan.assert_not_awaited()
    coordinator.async_restore_vanished_plan.assert_not_called()


def test_the_repair_of_a_plan_without_a_backup_identity_offers_only_unwatch() -> None:
    """(Sourcery) A bare-ID pick whose backups name several plans: Restore could not pick one."""
    unnamed = unnamed_plan_label(47)
    flow, _ = _vanished_flow({(47, unnamed)}, issue_data={"entry_id": "e1", "plan_name": unnamed, "fub_id": "47"})
    result, _ = _step(flow, None)
    (field,) = result["data_schema"].schema
    assert field.default() == ACTION_UNWATCH
    assert result["data_schema"].schema[field].config["options"] == [ACTION_UNWATCH]


def test_the_vanished_repair_survives_a_failing_backup_lookup(caplog: pytest.LogCaptureFixture) -> None:
    """(Sourcery) The lookup raising aborted the flow with an error; the restore reports a missing backup itself."""
    flow, coordinator = _vanished_flow({(47, "Test4")})
    coordinator.async_newest_backup_field.side_effect = OSError("store unreadable")
    result, _ = _step(flow, {"action": "restore"})
    assert result["type"] == "create_entry"
    coordinator.async_restore_vanished_plan.assert_called_once_with(47, "Test4")
    assert "store unreadable" in caplog.text


def test_a_vanished_repair_before_the_first_check_stays() -> None:
    """(Review) Not checked since setup is unknown, not "no longer missing"."""
    flow, coordinator = _vanished_flow(None)
    result, delete_issue = _step(flow, {"action": "restore"})
    assert result["reason"] == "check_pending"
    delete_issue.assert_not_called()
    coordinator.async_restore_vanished_plan.assert_not_called()


def test_a_vanished_repair_of_a_removed_entry_aborts() -> None:
    flow, _ = _vanished_flow({(47, "Test4")})
    flow.hass.data = {DOMAIN: {}}
    result, _ = _step(flow, {"action": "restore"})
    assert result["reason"] == "entry_not_found"


# (cu)/(cq) A watched plan whose ID another plan holds now: watch that plan instead, or take it over as a rename.

REPLACED_DATA = {**ISSUE_DATA, "current_name": "Test 4"}


@pytest.mark.parametrize(
    ("suspected", "expected"),
    [
        (False, [ACTION_RESTORE, ACTION_WATCH_HOLDER, ACTION_UNWATCH]),
        # (Review) no Restore: the restored copy would run beside the renamed original
        (True, [ACTION_TAKE_OVER_RENAME, ACTION_UNWATCH]),
    ],
)
def test_the_replaced_repair_offers_the_id_holder(suspected: bool, expected: list[str]) -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.plan_rename_suspected.return_value = suspected
    result, _ = _step(flow, None)
    assert result["description_placeholders"] == {"plan_name": "Test4", "fub_id": "47", "current_name": "Test 4"}
    (field,) = result["data_schema"].schema
    assert result["data_schema"].schema[field].config["options"] == expected
    assert field.default() == expected[0]  # a likely rename comes first
    coordinator.plan_rename_suspected.assert_called_once_with(47, "Test4", "Test 4")


def test_the_replaced_repair_offers_no_rename_when_the_backups_fail_to_load(caplog: pytest.LogCaptureFixture) -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.function_plan_backup.async_load.side_effect = OSError("store unreadable")
    coordinator.plan_rename_suspected.return_value = True
    result, _ = _step(flow, None)
    (field,) = result["data_schema"].schema
    assert result["data_schema"].schema[field].config["options"] == [
        ACTION_RESTORE,
        ACTION_WATCH_HOLDER,
        ACTION_UNWATCH,
    ]
    assert "no rename offered" in caplog.text


@pytest.mark.parametrize(("action", "rename"), [(ACTION_WATCH_HOLDER, False), (ACTION_TAKE_OVER_RENAME, True)])
def test_the_replaced_repair_watches_the_id_holder(action: str, rename: bool) -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.plan_rename_suspected.return_value = True
    result, _ = _step(flow, {"action": action})
    assert result["type"] == "create_entry"
    coordinator.async_watch_id_holder.assert_awaited_once_with(47, "Test4", "Test 4", rename=rename)
    coordinator.plan_edit.assert_called_once_with()  # (Review) no restore moves the same backups meanwhile
    coordinator.async_restore_vanished_plan.assert_not_called()


def test_the_replaced_repair_still_restores() -> None:
    """(Review) Restore with another plan on the ID still reaches the restore, not the holder path."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    result, _ = _step(flow, {"action": ACTION_RESTORE})
    assert result["type"] == "create_entry"
    coordinator.async_restore_vanished_plan.assert_called_once_with(47, "Test4")
    coordinator.async_watch_id_holder.assert_not_awaited()


@pytest.mark.parametrize(("unloading", "restoring"), [(True, False), (False, True)])
def test_a_rename_is_not_taken_over_while_backups_are_busy(unloading: bool, restoring: bool) -> None:
    """(Review) a restore moves the same backups; after the unload's wait a write would hit a torn-down entry."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.plan_rename_suspected.return_value = True
    coordinator.unloading = unloading

    async def submit() -> dict[str, Any]:
        if restoring:
            await coordinator._restore_lock.acquire()
        with patch.object(repairs.ir, "async_delete_issue"):
            return await flow.async_step_function_plan_vanished({"action": ACTION_TAKE_OVER_RENAME})

    result = asyncio.run(submit())
    assert result["reason"] == "backups_busy"
    coordinator.async_watch_id_holder.assert_not_awaited()


def test_watching_the_id_holder_is_refused_while_unloading() -> None:
    """(Review) after the unload's wait the options write would hit a torn-down entry."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.unloading = True
    result, _ = _step(flow, {"action": ACTION_WATCH_HOLDER})
    assert result["reason"] == "backups_busy"
    coordinator.async_watch_id_holder.assert_not_awaited()


def test_watching_the_id_holder_is_not_held_back_by_a_restore() -> None:
    """(Review) only the rename moves backups a restore moves too; watching the holder touches the options only."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)

    async def submit() -> dict[str, Any]:
        await coordinator._restore_lock.acquire()
        with patch.object(repairs.ir, "async_delete_issue"):
            return await flow.async_step_function_plan_vanished({"action": ACTION_WATCH_HOLDER})

    assert asyncio.run(submit())["type"] == "create_entry"
    coordinator.async_watch_id_holder.assert_awaited_once_with(47, "Test4", "Test 4", rename=False)


@pytest.mark.parametrize("action", [ACTION_WATCH_HOLDER, ACTION_TAKE_OVER_RENAME])
def test_the_holder_is_not_watched_while_this_plans_restore_runs(action: str) -> None:
    """(Sourcery) its Restore would build the plan anew beside the holder, its pick swapped away from it."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.plan_rename_suspected.return_value = True
    coordinator.vanished_restore_running.return_value = True
    result, _ = _step(flow, {"action": action})
    assert result["reason"] == "backups_busy"
    coordinator.vanished_restore_running.assert_called_with(47, "Test4")
    coordinator.async_watch_id_holder.assert_not_awaited()


def test_a_rename_with_unreadable_backups_is_reported_as_not_moved() -> None:
    """(Review) a load failure was reported as "content no longer matches"."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.function_plan_backup.async_load.side_effect = OSError("store unreadable")
    result, _ = _step(flow, {"action": ACTION_TAKE_OVER_RENAME})
    assert result["reason"] == "backups_not_moved"
    coordinator.async_watch_id_holder.assert_not_awaited()


def test_an_error_after_the_backups_moved_is_not_reported_as_not_moved() -> None:
    """(Review) only the backup move's failure means "nothing was changed"; anything else surfaces."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.async_watch_id_holder.side_effect = RuntimeError("listener")
    step = flow.async_step_function_plan_vanished({"action": ACTION_WATCH_HOLDER})
    with pytest.raises(RuntimeError, match="listener"):
        asyncio.run(step)


def test_a_rename_no_longer_matching_is_not_taken_over() -> None:
    """The content changed between the dialog opening and the choice: no longer recognisable as the plan."""
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    result, _ = _step(flow, {"action": ACTION_TAKE_OVER_RENAME})
    assert result["reason"] == "rename_not_confirmed"
    coordinator.async_watch_id_holder.assert_not_awaited()


@pytest.mark.parametrize(
    ("outcome", "reason"), [(False, "holder_changed"), (PlanBackupsNotMovedError(), "backups_not_moved")]
)
def test_the_replaced_repair_reports_a_holder_it_could_not_watch(outcome: Any, reason: str) -> None:
    flow, coordinator = _vanished_flow({(47, "Test4")}, issue_data=REPLACED_DATA)
    coordinator.async_watch_id_holder.side_effect = outcome if isinstance(outcome, Exception) else None
    coordinator.async_watch_id_holder.return_value = outcome
    result, _ = _step(flow, {"action": ACTION_WATCH_HOLDER})
    assert result["type"] == "abort"
    assert result["reason"] == reason


# (cm) A restore-as-new moves the watchdog pick with the plan.


def test_a_restore_as_new_moves_the_watchdog_pick_in_the_options() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4", "19:Other"], CONF_FUNCTION_PLAN_FUB_ID: "19"}
    coordinator.config_entry = SimpleNamespace(options=options)
    coordinator.request_options_update_without_reload = MagicMock()  # type: ignore[method-assign]
    coordinator.plan_watchdog = SimpleNamespace(vanished={(47, "Test4")}, async_dismiss_push=AsyncMock())
    updated = asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 47, 52))
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["52:Test4", "19:Other"]
    assert new_options[CONF_FUNCTION_PLAN_FUB_ID] == "19"
    assert updated == ["function plan watchdog pick 'Test4' → fub_id 52"]
    # (Sourcery) The reload after the restore forgets the push: the old alarm goes now.
    coordinator.plan_watchdog.async_dismiss_push.assert_awaited_once_with(47, "Test4")

    coordinator.request_options_update_without_reload.reset_mock()
    assert asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 48, 52)) == []
    coordinator.request_options_update_without_reload.assert_not_called()


def test_a_restore_as_new_leaves_the_selected_plan_with_the_plan_now_holding_the_old_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(Sourcery) The selector stored only the ID; once another plan holds it, it means that plan."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_FUB_ID: "47"})
    coordinator.request_options_update_without_reload = MagicMock()  # type: ignore[method-assign]
    coordinator.plan_watchdog = SimpleNamespace(vanished=set(), async_dismiss_push=AsyncMock())
    with caplog.at_level(logging.INFO):
        updated = asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 47, 52, old_id_still_live=True))
    assert updated == []
    coordinator.request_options_update_without_reload.assert_not_called()
    assert "Selected function plan stays on fub_id 47" in caplog.text

    updated = asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 47, 52))
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_FUB_ID] == 52
    assert updated == ["selected function plan → fub_id 52"]


@pytest.mark.parametrize("updated", [[], ["cluster plan map entry 'Test4' → fub_id 52"]])
def test_the_restore_report_names_the_selections_staying_with_the_plan_now_holding_the_old_id(
    updated: list[str],
) -> None:
    """(Review) "nothing pointed at the old ID" was false while the plan selector still held it."""
    line = backup._restored_references_line(47, updated, None, True)
    assert "nothing pointed" not in line
    assert "another plan holds old ID 47 now" in line
    assert "plan selector" in line
    assert all(entry in line for entry in updated)


def test_a_restore_as_new_writes_the_options_before_it_awaits_the_push_dismissal() -> None:
    """(Sourcery) An options save during the await was overwritten by the snapshot taken before it."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]})
    order: list[str] = []
    coordinator.request_options_update_without_reload = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _options: order.append("options")
    )

    async def dismiss(_fub_id: int, _name: str | None = None) -> None:
        order.append("dismiss")

    coordinator.plan_watchdog = SimpleNamespace(
        vanished={(47, "Test4")}, async_dismiss_push=AsyncMock(side_effect=dismiss)
    )
    asyncio.run(coordinator.async_repoint_function_plan_fub_id("Test4", 47, 52))
    assert order == ["options", "dismiss"]


def test_a_deleted_plans_restore_never_overwrites_a_plan_of_its_name_back_under_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(Sourcery) No conflict there, so the action restored in place over the plan that came back."""
    notifications = MagicMock()
    monkeypatch.setattr(backup.persistent_notification, "async_create", notifications)
    running = {(47, "Test4")}
    coordinator = SimpleNamespace(
        vanished_restore_running=lambda fub_id, name: (fub_id, name) in running,
        note_vanished_plan_back=MagicMock(),
    )
    assert backup._refuse_restore_over_returned_plan(MagicMock(), coordinator, 47, "Test4") is True
    assert "is back in Comexio" in notifications.call_args.args[1]
    # (Sourcery) The Restore that ran this learns the plan is back: no "restore failed" for it.
    coordinator.note_vanished_plan_back.assert_called_once_with(47, "Test4")
    notifications.reset_mock()
    assert backup._refuse_restore_over_returned_plan(MagicMock(), coordinator, 48, "Test4") is False
    notifications.assert_not_called()


def test_the_backup_store_names_the_plans_kept_under_an_id() -> None:
    """A bare-ID watchdog pick of a deleted plan takes the name its backups carry."""
    manager = FunctionPlanBackupManager.__new__(FunctionPlanBackupManager)
    manager._auto_data = {"47": {"Test4": [{}]}, "12": {"A": [{}]}}
    manager._change_data = {"47": {"Test4": [{}], "Old": [{}]}}
    assert manager.plan_names_for_fub_id_sync(47) == {"Test4", "Old"}
    assert manager.plan_names_for_fub_id_sync(12) == {"A"}
    assert manager.plan_names_for_fub_id_sync(99) == set()
