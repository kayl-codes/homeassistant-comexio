"""The auto backup holds back during a restore; a lasting bulk-load outage or left-out plan is reported once."""

import asyncio
from collections.abc import Callable
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioError
from aiocomexio.function_plan import plan_payload_has_elements
import pytest

from custom_components.comexio import coordinator as coordinator_module
from custom_components.comexio.const import (
    FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD,
    FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD,
)
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
    coordinator._bulk_load_pending = False
    coordinator._plan_omit_streaks = {}
    coordinator._omitted_plans_reported = ""
    coordinator._function_plan_snapshot_from_bulk = False
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
    assert coordinator.function_plan_backup.async_auto_backup.await_args.args[0] == PLANS


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
        dismiss.assert_any_call(coordinator.hass, NOTIFICATION_ID)
        assert caplog.text.count("bulk load works again") == 1

        # The next outage is reported again.
        _failing_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD)
        assert create.call_count == 2


def test_a_load_that_works_after_a_reload_still_dismisses_the_notification() -> None:
    """The notification outlives a reload, the fail streak does not: a working load always dismisses it."""
    coordinator = _coordinator()  # fresh after the reload: streak 0
    with patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss:
        _cycle(coordinator)
    dismiss.assert_any_call(coordinator.hass, NOTIFICATION_ID)


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


def test_a_restore_starting_inside_the_auto_backup_is_seen_before_the_history_changes() -> None:
    held_back_answers: list[bool] = []

    async def auto_backup(*_: object, held_back: Callable[[], bool], **__: object) -> list:
        held_back_answers.append(held_back())
        with coordinator.pause_auto_backup():  # the store awaits, a restore starts
            held_back_answers.append(held_back())
        held_back_answers.append(held_back())  # ... and has ended again before the history changes
        return []

    coordinator = _coordinator()
    coordinator.function_plan_backup.async_auto_backup = AsyncMock(side_effect=auto_backup)
    _cycle(coordinator)
    assert held_back_answers == [False, True, True]
    # The cycle goes on without a changed plan to show.
    assert coordinator.last_changed_plans == []
    coordinator.function_plan_backup.async_backfill_block_keys.assert_awaited_once()


def _timed_out_cycles(coordinator: ComexioCoordinator, count: int) -> None:
    async def run() -> None:
        for _ in range(count):
            await coordinator._async_function_plan_backup_cycle()

    with patch.object(coordinator_module, "FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC", 0.01):
        asyncio.run(run())


async def _hang(*_: object, **__: object) -> dict:
    await asyncio.sleep(1)
    return dict(PLANS)


def test_bulk_loads_that_time_out_count_as_failed_loads() -> None:
    coordinator = _coordinator(AsyncMock(side_effect=_hang))
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        _timed_out_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD)
    create.assert_called_once()
    assert "(last error: timed out after 0.01s)" in create.call_args.args[1]
    assert coordinator._bulk_load_pending is False


def test_a_cycle_timing_out_after_the_load_is_no_failed_load(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator()
    coordinator.function_plan_backup.async_auto_backup = AsyncMock(side_effect=_hang)
    with (
        patch.object(coordinator_module.persistent_notification, "async_create") as create,
        caplog.at_level(logging.WARNING),
    ):
        _timed_out_cycles(coordinator, FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD)
    # Every cycle got past the load and timed out inside the auto backup.
    assert (
        coordinator.function_plan_backup.async_auto_backup.await_count == FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD
    )
    assert caplog.text.count("backup cycle aborted") == FUNCTION_PLAN_BULK_LOAD_FAIL_NOTIFY_THRESHOLD
    create.assert_not_called()
    assert coordinator._bulk_load_fail_streak == 0


OMITTED_NOTIFICATION_ID = "comexio_function_plan_omitted_cx1"


def _omitting_coordinator() -> ComexioCoordinator:
    """The poll lists plans 7 and 8, the bulk load delivers only 7 without an error (e.g. 8 malformed)."""
    coordinator = _coordinator()
    coordinator.api.fub_data = {"7": {"Name": "Pumps"}, "8": {"Name": "Broken"}}
    coordinator.api.function_plan_name = lambda fub_id: coordinator.api.fub_data[str(fub_id)]["Name"]
    return coordinator


def _omitted_creates(create: MagicMock) -> list:
    return [c for c in create.call_args_list if c.kwargs["notification_id"] == OMITTED_NOTIFICATION_ID]


def test_a_plan_the_bulk_load_keeps_leaving_out_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    """(ce): a working load that skips a listed plan backs up the others; the skipped one is named after N cycles."""
    coordinator = _omitting_coordinator()
    with (
        patch.object(coordinator_module.persistent_notification, "async_create") as create,
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
    ):
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1):
            _cycle(coordinator)
        assert not _omitted_creates(create)

        _cycle(coordinator)
        _cycle(coordinator)
        assert len(_omitted_creates(create)) == 1  # no repeat per poll
        assert "'Broken' (8)" in create.call_args.args[1]
        assert "'Pumps'" not in create.call_args.args[1]
        # The working load is no outage: plan 7 is still backed up, the fail streak stays at 0.
        backed_up = [c.args[0] for c in coordinator.function_plan_backup.async_auto_backup.await_args_list]
        assert backed_up == [PLANS] * (FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD + 1)
        assert coordinator._bulk_load_fail_streak == 0

        dismiss.reset_mock()
        coordinator.api.function_plan_load_all_plans = AsyncMock(return_value={**PLANS, 8: {"elements": {}}})
        with caplog.at_level(logging.INFO):
            _cycle(coordinator)
            _cycle(coordinator)
        dismiss.assert_any_call(coordinator.hass, OMITTED_NOTIFICATION_ID)
        assert caplog.text.count("leaves no listed plan out anymore") == 1
        assert coordinator._plan_omit_streaks == {}


@pytest.mark.parametrize(
    "broken",
    [{"connections": []}, {"elements": None, "connections": []}, "garbage"],
    ids=["no-key", "null", "no-object"],
)
def test_a_plan_without_an_elements_collection_is_left_out_not_backed_up_empty(broken: object) -> None:
    """Review: loaded leniently, a broken entry became an empty plan — saved empty, never reported as left out."""
    # Comexio sends an empty plan as "elements": [] (checked live) — only plan 8 lacks the collection.
    bulk_answer = {7: {"elements": [], "connections": []}, 8: broken}

    async def load(strict: bool = False, raise_errors: bool = False) -> dict:
        # aiocomexio's own check, so the fake cannot drift from what strict drops.
        return {fub_id: plan for fub_id, plan in bulk_answer.items() if not strict or plan_payload_has_elements(plan)}

    coordinator = _omitting_coordinator()
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=load)
    _cycle(coordinator)
    assert coordinator._plan_omit_streaks == {8: 1}
    assert list(coordinator.function_plan_backup.async_auto_backup.await_args.args[0]) == [7]


def test_a_failed_bulk_load_neither_counts_nor_clears_a_left_out_plan() -> None:
    """A failed load says nothing about single plans: the streak of plan 8 goes on where it was."""
    coordinator = _omitting_coordinator()
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1):
            _cycle(coordinator)
        working_load = coordinator.api.function_plan_load_all_plans
        _failing_cycles(coordinator, 1)
        assert not _omitted_creates(create)
        assert coordinator._plan_omit_streaks == {8: FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1}

        coordinator.api.function_plan_load_all_plans = working_load
        _cycle(coordinator)
    assert len(_omitted_creates(create)) == 1


def test_a_plan_deleted_during_the_bulk_load_is_not_left_out() -> None:
    """The load asked for plan 8, a poll meanwhile found it deleted: nothing was skipped."""
    coordinator = _omitting_coordinator()

    async def load(**_: object) -> dict:
        del coordinator.api.fub_data["8"]  # what the next poll's parse_config does
        return dict(PLANS)

    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=load)
    _cycle(coordinator)
    assert coordinator._plan_omit_streaks == {}


def test_a_new_set_of_left_out_plans_updates_the_notification() -> None:
    """Plan 9 joins plan 8: the notification names both instead of keeping the old list."""
    coordinator = _omitting_coordinator()
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD):
            _cycle(coordinator)
        coordinator.api.fub_data["9"] = {"Name": "Lights"}
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD):
            _cycle(coordinator)
    creates = _omitted_creates(create)
    assert len(creates) == 2
    assert "'Broken' (8), 'Lights' (9)" in creates[-1].args[1]


def test_a_plan_delivered_in_between_starts_its_count_afresh() -> None:
    """Only loads in a row count: one that delivers plan 8 resets its streak."""
    coordinator = _omitting_coordinator()
    omitting_load = coordinator.api.function_plan_load_all_plans
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1):
            _cycle(coordinator)
        coordinator.api.function_plan_load_all_plans = AsyncMock(return_value={**PLANS, 8: {"elements": {}}})
        _cycle(coordinator)
        coordinator.api.function_plan_load_all_plans = omitting_load
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1):
            _cycle(coordinator)
    assert not _omitted_creates(create)
    assert coordinator._plan_omit_streaks == {8: FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1}


def test_a_plan_loading_again_leaves_the_others_in_the_notification(caplog: pytest.LogCaptureFixture) -> None:
    """Plan 9 loads again, plan 8 not: the notification is replaced by one naming only plan 8."""
    coordinator = _omitting_coordinator()
    coordinator.api.fub_data["9"] = {"Name": "Lights"}
    with (
        patch.object(coordinator_module.persistent_notification, "async_create") as create,
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
    ):
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD):
            _cycle(coordinator)
        dismiss.reset_mock()  # the cycles below the threshold dismiss as nothing is reported yet
        coordinator.api.function_plan_load_all_plans = AsyncMock(return_value={**PLANS, 9: {"elements": {}}})
        with caplog.at_level(logging.INFO):
            _cycle(coordinator)
    creates = _omitted_creates(create)
    assert len(creates) == 2
    assert "'Broken' (8)" in creates[-1].args[1]
    assert "'Lights'" not in creates[-1].args[1]
    assert (coordinator.hass, OMITTED_NOTIFICATION_ID) not in [c.args for c in dismiss.call_args_list]
    assert "leaves no listed plan out anymore" not in caplog.text


def test_a_working_load_after_a_reload_dismisses_a_left_out_notification(caplog: pytest.LogCaptureFixture) -> None:
    """The notification outlives a reload, the streaks do not: a load leaving nothing out always dismisses it."""
    coordinator = _coordinator()  # fresh after the reload: nothing reported
    with (
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
        caplog.at_level(logging.INFO),
    ):
        _cycle(coordinator)
    dismiss.assert_any_call(coordinator.hass, OMITTED_NOTIFICATION_ID)
    assert "leaves no listed plan out anymore" not in caplog.text  # nothing was reported in this run


def test_a_plan_still_left_out_after_a_reload_is_named_again_once_its_count_is_back() -> None:
    """After a reload the counts start afresh, so the notification may name a plan that loads again: it goes."""
    coordinator = _omitting_coordinator()  # fresh after the reload: nothing reported
    with (
        patch.object(coordinator_module.persistent_notification, "async_create") as create,
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
    ):
        _cycle(coordinator)
        dismiss.assert_any_call(coordinator.hass, OMITTED_NOTIFICATION_ID)
        assert not _omitted_creates(create)  # not before the count is back

        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD - 1):
            _cycle(coordinator)
    assert len(_omitted_creates(create)) == 1
    assert "'Broken' (8)" in create.call_args.args[1]


def test_a_left_out_plan_renamed_meanwhile_updates_the_notification() -> None:
    coordinator = _omitting_coordinator()
    with patch.object(coordinator_module.persistent_notification, "async_create") as create:
        for _ in range(FUNCTION_PLAN_OMITTED_NOTIFY_THRESHOLD):
            _cycle(coordinator)
        coordinator.api.fub_data["8"]["Name"] = "Broken 2"
        _cycle(coordinator)
    assert len(_omitted_creates(create)) == 2
    assert "'Broken 2' (8)" in _omitted_creates(create)[-1].args[1]


def test_a_left_out_plan_counts_before_the_first_poll() -> None:
    """Without a poll's plan list there is nothing to tell deleted plans by: every plan asked for counts."""
    coordinator = _omitting_coordinator()
    coordinator.scraped_plan_ids = None
    _cycle(coordinator)
    assert coordinator._plan_omit_streaks == {8: 1}
