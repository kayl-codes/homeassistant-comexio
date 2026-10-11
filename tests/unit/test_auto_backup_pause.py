"""The auto backup holds back during an in-place restore, and a lasting bulk-load outage is reported once."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioError
import pytest

from custom_components.comexio import coordinator as coordinator_module
from custom_components.comexio.const import FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.services import backup

PLANS = {7: {"elements": {}, "connections": {}}}
NOTIFICATION_ID = "comexio_function_plan_bulk_load_cx1"


def _coordinator(load: AsyncMock | None = None) -> ComexioCoordinator:
    """A coordinator whose backup cycle loads plan 7 and stores its auto backup."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator.hass = MagicMock()
    coordinator._function_plan_backup_lock = asyncio.Lock()
    coordinator._auto_backup_pauses = 0
    coordinator._auto_backup_pause_generation = 0
    coordinator._bulk_load_fail_streak = 0
    coordinator.scraped_plan_ids = {7}
    coordinator.api = SimpleNamespace(
        function_plan_load_all_plans=load or AsyncMock(return_value=dict(PLANS)),
        fub_data={"7": {"Name": "Pumps"}},
        comexio_version="11.1.4",
        block_settings={},
    )
    coordinator.function_plan_plans = {}
    coordinator._drop_plans_deleted_meanwhile = lambda plans: plans  # type: ignore[method-assign]
    coordinator._plans_added_meanwhile = lambda requested: {}  # type: ignore[method-assign]
    coordinator.reference_monitor = MagicMock()
    coordinator._async_recheck_after_snapshot_update = AsyncMock()  # type: ignore[method-assign]
    coordinator._current_plan_format = lambda fub_data: {}  # type: ignore[method-assign]
    coordinator.function_plan_label_maps = lambda: ({}, {}, {})  # type: ignore[method-assign]
    coordinator.function_plan_backup = SimpleNamespace(
        async_auto_backup=AsyncMock(return_value=[]),
        async_backfill_paper_metadata=AsyncMock(),
        async_backfill_block_keys=AsyncMock(),
    )
    coordinator._async_refresh_service_descriptions = AsyncMock()  # type: ignore[method-assign]
    coordinator._async_audit_orphaned_backups = AsyncMock()  # type: ignore[method-assign]
    coordinator._async_finish_cycle_without_plans = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    return coordinator


def _cycle(coordinator: ComexioCoordinator) -> None:
    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())


def test_a_cycle_backs_up_the_loaded_plans() -> None:
    """Baseline for the tests below: without a restore the cycle stores the auto backup."""
    coordinator = _coordinator()
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


def test_a_cycle_during_a_restore_loads_and_backs_up_nothing() -> None:
    coordinator = _coordinator()
    coordinator.last_changed_plans = [{"fub_id": 7}]
    coordinator._bulk_load_fail_streak = 2
    with coordinator.pause_auto_backup():
        _cycle(coordinator)
    coordinator.api.function_plan_load_all_plans.assert_not_called()
    coordinator.function_plan_backup.async_auto_backup.assert_not_called()
    # Nothing judged: the last result stays shown, and the failure streak goes on where it was.
    assert coordinator.last_changed_plans == [{"fub_id": 7}]
    assert coordinator._bulk_load_fail_streak == 2


def test_a_restore_still_running_when_the_bulk_load_returns_skips_the_auto_backup() -> None:
    pause = None

    async def load(**_: object) -> dict:
        nonlocal pause
        pause = coordinator.pause_auto_backup()
        pause.__enter__()  # a restore starts during the load and is still running afterwards
        return dict(PLANS)

    coordinator = _coordinator(AsyncMock(side_effect=load))
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_not_called()
    assert pause is not None
    pause.__exit__(None, None, None)
    assert coordinator._auto_backup_pauses == 0


def test_overlapping_restores_hold_the_auto_backup_back_until_both_end() -> None:
    coordinator = _coordinator()
    with coordinator.pause_auto_backup():
        with coordinator.pause_auto_backup():
            pass
        _cycle(coordinator)
        coordinator.function_plan_backup.async_auto_backup.assert_not_called()
    assert coordinator._auto_backup_pauses == 0
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


def test_a_restore_during_the_bulk_load_skips_this_cycles_auto_backup() -> None:
    """(cd): the load may hold the restored plan; stored now it would duplicate the promoted snapshot."""

    async def load(**_: object) -> dict:
        with coordinator.pause_auto_backup():  # a restore starts and ends while the load runs
            pass
        return dict(PLANS)

    coordinator = _coordinator(AsyncMock(side_effect=load))
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_not_called()
    # The rest of the cycle still runs, and the next cycle backs up again.
    coordinator.function_plan_backup.async_backfill_block_keys.assert_awaited_once()
    coordinator.api.function_plan_load_all_plans = AsyncMock(return_value=dict(PLANS))
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


def test_a_failed_restore_resumes_the_auto_backup() -> None:
    coordinator = _coordinator()
    with pytest.raises(RuntimeError), coordinator.pause_auto_backup():
        raise RuntimeError("restore failed")
    _cycle(coordinator)
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


@pytest.mark.parametrize("fails", [False, True], ids=["restored", "restore-failed"])
def test_the_restore_service_pauses_the_auto_backup_for_every_restore_path(fails: bool) -> None:
    """In place, as a new plan and as a copy all run inside _run_function_plan_restore."""
    coordinator = _coordinator()
    coordinator._restore_lock = asyncio.Lock()
    pauses_during_restore: list[int] = []

    async def restore(*_: object) -> None:
        pauses_during_restore.append(coordinator._auto_backup_pauses)
        if fails:
            raise RuntimeError("connection lost")

    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_run_function_plan_restore", AsyncMock(side_effect=restore)),
        pytest.raises(RuntimeError) if fails else contextlib.nullcontext(),
    ):
        asyncio.run(backup._handle_function_plan_restore(MagicMock(), SimpleNamespace(data={})))

    assert pauses_during_restore == [1]
    assert coordinator._auto_backup_pauses == 0


def _failing_cycles(coordinator: ComexioCoordinator, count: int) -> None:
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=ComexioError("server busy"))
    for _ in range(count):
        _cycle(coordinator)


def test_a_lasting_bulk_load_outage_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    """(bs): failing loads stop the auto backups; one notification after N cycles, no repeat per poll."""
    coordinator = _coordinator()
    with (
        patch.object(coordinator_module.persistent_notification, "async_create") as create,
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
    ):
        _failing_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD - 1)
        create.assert_not_called()

        _failing_cycles(coordinator, 3)
        create.assert_called_once()
        assert create.call_args.kwargs["notification_id"] == NOTIFICATION_ID
        assert "server busy" in create.call_args.args[1]
        dismiss.assert_not_called()

        # A load that works again takes the notification away and logs the recovery once.
        coordinator.api.function_plan_load_all_plans = AsyncMock(return_value=dict(PLANS))
        with caplog.at_level(logging.INFO):
            _cycle(coordinator)
            _cycle(coordinator)
        dismiss.assert_called_with(coordinator.hass, NOTIFICATION_ID)
        assert caplog.text.count("bulk load works again") == 1

        # The next outage is reported again.
        _failing_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD)
        assert create.call_count == 2


def test_a_load_that_works_after_a_reload_still_dismisses_the_notification() -> None:
    """The notification outlives a reload, the fail streak does not: a working load always dismisses it."""
    coordinator = _coordinator()  # fresh after the reload: streak 0
    with patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss:
        _cycle(coordinator)
    dismiss.assert_called_once_with(coordinator.hass, NOTIFICATION_ID)


def test_an_empty_error_message_shows_the_error_type() -> None:
    coordinator = _coordinator(AsyncMock(side_effect=ComexioError("")))
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD):
            _cycle(coordinator)
    assert "(last error: ComexioError)" in create.call_args.args[1]
    assert f"{FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD} times in a row" in create.call_args.args[1]


def test_a_short_bulk_load_outage_is_not_reported() -> None:
    coordinator = _coordinator()
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        _failing_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD - 1)
        coordinator.api.function_plan_load_all_plans = AsyncMock(return_value=dict(PLANS))
        _cycle(coordinator)
        _failing_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD - 1)
    create.assert_not_called()


def test_an_unexpected_bulk_load_error_counts_too() -> None:
    coordinator = _coordinator(AsyncMock(side_effect=OSError("reset")))
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD):
            _cycle(coordinator)
    create.assert_called_once()
    assert "unexpected OSError" in create.call_args.args[1]
