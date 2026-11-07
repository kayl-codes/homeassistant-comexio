"""System info readings: failure tolerance, steady boot time, time server problem, the poll tick."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import SystemInfo, TimeServerSettings
import pytest

from custom_components.comexio import (
    SERVER_LEVEL_UNIQUE_ID_SUFFIXES,
    binary_sensor,
    coordinator as coordinator_module,
    sensor,
)
from custom_components.comexio.api import SystemInfoReading
from custom_components.comexio.binary_sensor import ComexioTimeServerProblemSensor
from custom_components.comexio.const import (
    SYSTEM_BOOT_TIME_TOLERANCE_SEC,
    SYSTEM_INFO_FAIL_STREAK_THRESHOLD,
    UPTIME_POLL_INTERVAL_SEC,
    system_info_signal,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.sensor import ComexioFreeStorageSensor, ComexioLastBootSensor
from custom_components.comexio.system_info import PART_SYSTEM_INFO, PART_UPTIME, SystemInfoState, stable_boot_time

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
UPTIME = timedelta(days=12, hours=21)
TIME_SERVER = TimeServerSettings(enabled=True, server="pool.ntp.org", update_interval=3600)
INFO = SystemInfo(free_storage_percent=42.5, time_server=TIME_SERVER)


def test_advertised_limits() -> None:
    """The documented cadence and tolerances, pinned on their own: the tests below derive from the constants."""
    assert UPTIME_POLL_INTERVAL_SEC == 300
    assert SYSTEM_INFO_FAIL_STREAK_THRESHOLD == 2
    assert SYSTEM_BOOT_TIME_TOLERANCE_SEC == 60


def _apply(state: SystemInfoState, info: SystemInfo | None, uptime: timedelta | None, now: datetime) -> list[str]:
    """One poll's results: the storage fetch of the config poll, then the uptime tick."""
    return state.apply_storage(info) + state.apply_uptime(uptime, now)


def _state_with_readings() -> SystemInfoState:
    state = SystemInfoState()
    assert _apply(state, INFO, UPTIME, NOW) == []
    return state


def test_apply_takes_the_readings() -> None:
    state = _state_with_readings()

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_one_failed_poll_keeps_the_last_readings() -> None:
    state = _state_with_readings()

    assert _apply(state, None, None, NOW + timedelta(minutes=5)) == []

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_lasting_failure_drops_the_readings_and_reports_it_once() -> None:
    state = _state_with_readings()
    results = [_apply(state, None, None, NOW) for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1)]

    assert results == [[]] * (SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1) + [[PART_SYSTEM_INFO, PART_UPTIME], []]
    assert state.free_storage_percent is None
    assert state.time_server is None
    assert state.boot_time is None


def test_failure_streaks_count_each_part_on_its_own() -> None:
    # The dashboard failing must not drop the boot time, nor the uptime failing the free storage.
    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD):
        _apply(state, None, UPTIME, NOW)

    assert state.free_storage_percent is None
    assert state.boot_time == NOW - UPTIME

    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD):
        _apply(state, INFO, None, NOW)

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.boot_time is None


def test_a_success_resets_the_failure_streak() -> None:
    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1):
        _apply(state, None, None, NOW)
    _apply(state, INFO, UPTIME, NOW)
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1):
        _apply(state, None, None, NOW)

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.boot_time == NOW - UPTIME


def test_boot_time_holds_steady_within_the_tolerance() -> None:
    known = NOW - UPTIME
    drift = timedelta(seconds=SYSTEM_BOOT_TIME_TOLERANCE_SEC)

    assert stable_boot_time(known, known + drift) == known
    assert stable_boot_time(known, known - drift) == known


def test_boot_time_moves_just_outside_the_tolerance() -> None:
    known = NOW - UPTIME
    estimate = known + timedelta(seconds=SYSTEM_BOOT_TIME_TOLERANCE_SEC + 1)

    assert stable_boot_time(known, estimate) == estimate


def test_boot_time_drift_keeps_it_and_a_reboot_becomes_the_new_baseline() -> None:
    state = SystemInfoState()
    # The uptime grows by 299-301 s between polls 300 s apart: request latency, one-second resolution.
    for poll, uptime_s in enumerate((1000, 1299, 1601, 1900)):
        _apply(state, INFO, timedelta(seconds=uptime_s), NOW + timedelta(seconds=300 * poll))
    assert state.boot_time == NOW - timedelta(seconds=1000)

    rebooted = NOW + timedelta(seconds=1500)
    _apply(state, INFO, timedelta(seconds=10), rebooted)
    _apply(state, INFO, timedelta(seconds=311), rebooted + timedelta(seconds=300))

    assert state.boot_time == rebooted - timedelta(seconds=10)


def test_boot_time_follows_an_estimate_beyond_the_tolerance() -> None:
    """Without an earlier uptime (first poll after a restart of HA) only the tolerance tells a reboot."""
    known = NOW - UPTIME
    state = SystemInfoState(boot_time=known)

    _apply(state, INFO, timedelta(minutes=3), NOW)

    assert state.boot_time == NOW - timedelta(minutes=3)


def test_a_reboot_within_the_tolerance_is_seen_by_the_uptime_going_down() -> None:
    """Review (Sourcery, #204): a reboot under SYSTEM_BOOT_TIME_TOLERANCE_SEC after the last one moves the boot time."""
    state = SystemInfoState()
    _apply(state, INFO, timedelta(seconds=10), NOW)
    rebooted_at = NOW + timedelta(seconds=SYSTEM_BOOT_TIME_TOLERANCE_SEC // 2)
    uptime = timedelta(seconds=5)

    _apply(state, INFO, uptime, rebooted_at + uptime)

    assert state.boot_time == rebooted_at


def test_an_unchanged_uptime_is_no_reboot() -> None:
    state = SystemInfoState()
    _apply(state, INFO, timedelta(hours=1), NOW)

    # Read again within the tolerance (a quick retry): an equal uptime keeps the boot time, it is no reboot.
    _apply(state, INFO, timedelta(hours=1), NOW + timedelta(seconds=SYSTEM_BOOT_TIME_TOLERANCE_SEC // 2))

    assert state.boot_time == NOW - timedelta(hours=1)


def test_a_reboot_is_seen_across_a_failed_uptime_poll() -> None:
    state = SystemInfoState()
    _apply(state, INFO, timedelta(seconds=10), NOW)
    _apply(state, INFO, None, NOW + timedelta(seconds=UPTIME_POLL_INTERVAL_SEC))
    rebooted_at = NOW + timedelta(seconds=SYSTEM_BOOT_TIME_TOLERANCE_SEC // 2)
    uptime = timedelta(seconds=5)

    _apply(state, INFO, uptime, rebooted_at + uptime)

    assert state.boot_time == rebooted_at


def test_boot_time_without_a_known_one_is_the_estimate() -> None:
    assert stable_boot_time(None, NOW) == NOW


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        (TIME_SERVER, False),
        (TimeServerSettings(enabled=False, server="pool.ntp.org", update_interval=3600), True),
        (TimeServerSettings(enabled=True, server="", update_interval=3600), True),
        (TimeServerSettings(enabled=True, server="pool.ntp.org", update_interval=None), False),
        (None, None),
    ],
)
def test_time_server_problem(settings: TimeServerSettings | None, problem: bool | None) -> None:
    assert SystemInfoState(time_server=settings).time_server_problem is problem


def _coordinator(info: SystemInfo | None, uptime: timedelta | None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.hass = MagicMock()
    coordinator.server_id = "srv"
    coordinator.api = MagicMock()
    coordinator.api.get_system_info = AsyncMock(
        return_value=SystemInfoReading(info, uptime, None if info else "HTTP 500", None if uptime else "timed out")
    )
    coordinator.system_info = SystemInfoState()
    coordinator.unloading = False
    coordinator._system_info_task = None
    return coordinator


def test_tick_fetches_only_the_uptime_and_notifies_the_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: NOW)
    coordinator = _coordinator(INFO, UPTIME)

    asyncio.run(coordinator._async_system_info_tick())

    coordinator.api.get_system_info.assert_awaited_once_with(storage=False)
    assert coordinator.system_info.boot_time == NOW - UPTIME
    assert coordinator.system_info.free_storage_percent is None
    sent.assert_called_once_with(coordinator.hass, system_info_signal("srv"))


def test_config_poll_refresh_fetches_only_storage_and_time_server(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(INFO, UPTIME)

    asyncio.run(coordinator._async_refresh_system_info_storage())

    coordinator.api.get_system_info.assert_awaited_once_with(uptime=False)
    assert coordinator.system_info.free_storage_percent == pytest.approx(42.5)
    assert coordinator.system_info.time_server == TIME_SERVER
    assert coordinator.system_info.boot_time is None
    sent.assert_called_once_with(coordinator.hass, system_info_signal("srv"))


def test_a_failed_storage_fetch_does_not_touch_the_uptime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator(None, None)
    coordinator.system_info.apply_uptime(UPTIME, NOW)

    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD):
        asyncio.run(coordinator._async_refresh_system_info_storage())

    assert coordinator.system_info.free_storage_percent is None
    assert coordinator.system_info.boot_time == NOW - UPTIME


INFO_DROPPED = "free storage and time server reset to unknown: HTTP 500"
UPTIME_DROPPED = "last boot reset to unknown: timed out"
SYSTEM_INFO_ENTITY_CLASSES = [ComexioFreeStorageSensor, ComexioLastBootSensor, ComexioTimeServerProblemSensor]


def _poll_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "System info poll failed" in r.getMessage()]


@pytest.mark.parametrize(
    ("tick", "info", "uptime", "expected"),
    [
        ("uptime", None, None, "last boot reset to unknown: timed out"),
        ("storage", None, None, "free storage and time server reset to unknown: HTTP 500"),
    ],
)
def test_each_fetch_warns_once_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tick: str,
    info: SystemInfo | None,
    uptime: timedelta | None,
    expected: str,
) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(info, uptime)
    run = coordinator._async_system_info_tick if tick == "uptime" else coordinator._async_refresh_system_info_storage

    with caplog.at_level(logging.WARNING):
        for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1):
            asyncio.run(run())

    prefix = f"[srv] System info poll failed {SYSTEM_INFO_FAIL_STREAK_THRESHOLD} times in a row, "
    assert _poll_warnings(caplog) == [prefix + expected]
    # Every fetch notifies, so the entities show "unknown" right when the readings drop.
    assert sent.call_count == SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1


def test_start_poll_ticks_now_and_on_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    track = MagicMock(return_value="cancel")
    monkeypatch.setattr(coordinator_module, "async_track_time_interval", track)
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator(INFO, UPTIME)
    coordinator.hass.async_create_task = MagicMock(side_effect=lambda coro, name: asyncio.run(coro))

    assert coordinator.async_start_system_info_poll() == "cancel"

    coordinator.hass.async_create_task.assert_called_once()
    assert coordinator.hass.async_create_task.call_args.kwargs["name"] == "comexio_srv_system_info_tick"
    # Review (Sourcery, #204): the task started right away is the poll itself.
    coordinator.api.get_system_info.assert_awaited_once()
    assert coordinator.system_info.boot_time is not None  # the uptime tick, not the storage fetch
    track.assert_called_once_with(
        coordinator.hass, coordinator._async_schedule_system_info_tick, timedelta(seconds=UPTIME_POLL_INTERVAL_SEC)
    )


def _running_poll(coordinator: ComexioCoordinator) -> asyncio.Event:
    """Let the coordinator's polls run on the current loop, each one blocked until the returned event is set."""
    release = asyncio.Event()
    reading = SystemInfoReading(INFO, UPTIME)

    async def blocked(**_kwargs: bool) -> SystemInfoReading:
        await release.wait()
        return reading

    coordinator.api.get_system_info = AsyncMock(side_effect=blocked)
    loop = asyncio.get_running_loop()
    coordinator.hass.async_create_task = MagicMock(side_effect=lambda coro, name: loop.create_task(coro, name=name))
    return release


def test_a_tick_while_a_poll_runs_starts_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #204): overlapping polls could apply an older poll's readings after a newer one's."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator(INFO, UPTIME)

    async def scenario() -> None:
        release = _running_poll(coordinator)
        coordinator._async_schedule_system_info_tick()
        await asyncio.sleep(0)
        coordinator._async_schedule_system_info_tick()
        assert coordinator.api.get_system_info.await_count == 1
        release.set()
        assert coordinator._system_info_task is not None
        await coordinator._system_info_task
        coordinator._async_schedule_system_info_tick()  # the poll is done: the next tick polls again
        await coordinator._system_info_task

    asyncio.run(scenario())

    assert coordinator.api.get_system_info.await_count == 2


def test_no_poll_starts_while_the_entry_unloads() -> None:
    coordinator = _coordinator(INFO, UPTIME)
    coordinator.unloading = True

    coordinator._async_schedule_system_info_tick()

    coordinator.hass.async_create_task.assert_not_called()


def _coordinator_with_a_pending_preview(
    monkeypatch: pytest.MonkeyPatch, base_shutdown: AsyncMock
) -> ComexioCoordinator:
    coordinator = _coordinator(INFO, UPTIME)
    for helper in ("_stop_connection_poll", "_cancel_preview_card_sweep", "_disarm_preview_cache"):
        monkeypatch.setattr(coordinator, helper, MagicMock(), raising=False)
    coordinator._knx_dpt_reload_cancel = None
    coordinator._preview_refresh_cancel = None
    coordinator._plan_preview_rendered = True
    coordinator._plan_preview_store_closed = False
    coordinator.last_plan_preview = None
    coordinator.last_plan_preview_svg = None
    coordinator._plan_preview_store = MagicMock(async_save=AsyncMock())
    monkeypatch.setattr(coordinator_module.DataUpdateCoordinator, "async_shutdown", base_shutdown)
    return coordinator


def test_a_failing_preview_save_skips_nothing_of_the_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The save runs last, so it cannot leave a poll running on the API session async_unload_entry closes."""
    base_shutdown = AsyncMock()
    coordinator = _coordinator_with_a_pending_preview(monkeypatch, base_shutdown)
    seen: list[tuple[bool, int]] = []

    async def scenario() -> None:
        poll = asyncio.create_task(asyncio.sleep(60))
        coordinator._system_info_task = poll

        async def failing_save(_data: object) -> None:
            seen.append((poll.cancelled(), base_shutdown.await_count))
            raise RuntimeError("store failed")

        coordinator._plan_preview_store.async_save = failing_save
        await coordinator.async_shutdown()

    run = scenario()
    with pytest.raises(RuntimeError, match="store failed"):
        asyncio.run(run)

    assert seen == [(True, 1)]


def test_a_failing_base_shutdown_still_saves_the_pending_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator_with_a_pending_preview(monkeypatch, AsyncMock(side_effect=RuntimeError("base failed")))

    shutdown = coordinator.async_shutdown()
    with pytest.raises(RuntimeError, match="base failed"):
        asyncio.run(shutdown)

    coordinator._plan_preview_store.async_save.assert_awaited_once()
    assert coordinator._plan_preview_store_closed is True


def test_shutdown_cancels_a_running_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #204): a poll in flight must not outlive the API session async_unload_entry closes."""
    coordinator = _coordinator(INFO, UPTIME)
    for helper in ("_stop_connection_poll", "_cancel_preview_card_sweep", "_disarm_preview_cache"):
        monkeypatch.setattr(coordinator, helper, MagicMock(), raising=False)
    coordinator._knx_dpt_reload_cancel = None
    coordinator._preview_refresh_cancel = None
    coordinator._plan_preview_rendered = False
    coordinator._plan_preview_store_closed = False
    monkeypatch.setattr(coordinator_module.DataUpdateCoordinator, "async_shutdown", AsyncMock())

    async def scenario() -> asyncio.Task[None]:
        _running_poll(coordinator)
        coordinator._async_schedule_system_info_tick()
        await asyncio.sleep(0)
        await coordinator.async_shutdown()
        task = coordinator._system_info_task
        assert task is not None
        # Review (Sourcery, #204): stopped by the time async_shutdown returns, before the session is closed.
        assert task.done()
        return task

    assert asyncio.run(scenario()).cancelled()


@pytest.mark.parametrize(
    ("module", "entity_cls"),
    [
        (sensor, ComexioFreeStorageSensor),
        (sensor, ComexioLastBootSensor),
        (binary_sensor, ComexioTimeServerProblemSensor),
    ],
)
def test_entities_listen_to_the_system_info_signal(
    monkeypatch: pytest.MonkeyPatch, module: object, entity_cls: type
) -> None:
    connect = MagicMock(return_value="disconnect")
    monkeypatch.setattr(module, "async_dispatcher_connect", connect)
    entity = entity_cls(_coordinator(None, None), "srv")
    entity.hass = MagicMock()
    entity.async_on_remove = MagicMock()

    asyncio.run(entity.async_added_to_hass())

    connect.assert_called_once_with(entity.hass, system_info_signal("srv"), entity.async_write_ha_state)
    entity.async_on_remove.assert_called_once_with("disconnect")


@pytest.mark.parametrize(
    "entity_cls", [ComexioFreeStorageSensor, ComexioLastBootSensor, ComexioTimeServerProblemSensor]
)
def test_orphan_cleanup_keeps_the_entities(entity_cls: type) -> None:
    unique_id = entity_cls(_coordinator(None, None), "srv").unique_id

    assert unique_id.removeprefix("comexio_srv_") in SERVER_LEVEL_UNIQUE_ID_SUFFIXES


def test_entities_read_the_coordinator_state() -> None:
    coordinator = _coordinator(None, None)
    _apply(coordinator.system_info, INFO, UPTIME, NOW)
    problem = ComexioTimeServerProblemSensor(coordinator, "srv")

    assert ComexioFreeStorageSensor(coordinator, "srv").native_value == pytest.approx(42.5)
    assert ComexioLastBootSensor(coordinator, "srv").native_value == NOW - UPTIME
    assert problem.is_on is False
    assert problem.extra_state_attributes == {"enabled": True, "server": "pool.ntp.org", "update_interval": 3600}


def test_entity_unique_ids() -> None:
    coordinator = _coordinator(None, None)

    assert ComexioFreeStorageSensor(coordinator, "srv").unique_id == "comexio_srv_free_storage_sensor"
    assert ComexioLastBootSensor(coordinator, "srv").unique_id == "comexio_srv_last_boot_sensor"
    assert ComexioTimeServerProblemSensor(coordinator, "srv").unique_id == "comexio_srv_time_server_problem"


def test_time_server_problem_without_settings_has_no_attributes() -> None:
    problem = ComexioTimeServerProblemSensor(_coordinator(None, None), "srv")

    assert problem.is_on is None
    assert problem.extra_state_attributes == {}
