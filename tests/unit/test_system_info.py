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
    SYSTEM_INFO_POLL_INTERVAL_SEC,
    system_info_signal,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.sensor import ComexioFreeStorageSensor, ComexioLastBootSensor
from custom_components.comexio.system_info import PART_SYSTEM_INFO, PART_UPTIME, SystemInfoState, stable_boot_time

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
UPTIME = timedelta(days=12, hours=21)
TIME_SERVER = TimeServerSettings(enabled=True, server="pool.ntp.org", update_interval=3600)
INFO = SystemInfo(free_storage_percent=42.5, time_server=TIME_SERVER)


def _state_with_readings() -> SystemInfoState:
    state = SystemInfoState()
    assert state.apply(INFO, UPTIME, NOW) == []
    return state


def test_apply_takes_the_readings() -> None:
    state = _state_with_readings()

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_one_failed_poll_keeps_the_last_readings() -> None:
    state = _state_with_readings()

    assert state.apply(None, None, NOW + timedelta(minutes=5)) == []

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_lasting_failure_drops_the_readings_and_reports_it_once() -> None:
    state = _state_with_readings()
    results = [state.apply(None, None, NOW) for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1)]

    assert results == [[]] * (SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1) + [[PART_SYSTEM_INFO, PART_UPTIME], []]
    assert state.free_storage_percent is None
    assert state.time_server is None
    assert state.boot_time is None


def test_failure_streaks_count_each_part_on_its_own() -> None:
    # The dashboard failing must not drop the boot time, nor the uptime failing the free storage.
    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD):
        state.apply(None, UPTIME, NOW)

    assert state.free_storage_percent is None
    assert state.boot_time == NOW - UPTIME

    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD):
        state.apply(INFO, None, NOW)

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.boot_time is None


def test_a_success_resets_the_failure_streak() -> None:
    state = _state_with_readings()
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1):
        state.apply(None, None, NOW)
    state.apply(INFO, UPTIME, NOW)
    for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1):
        state.apply(None, None, NOW)

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
        state.apply(INFO, timedelta(seconds=uptime_s), NOW + timedelta(seconds=300 * poll))
    assert state.boot_time == NOW - timedelta(seconds=1000)

    rebooted = NOW + timedelta(seconds=1500)
    state.apply(INFO, timedelta(seconds=10), rebooted)
    state.apply(INFO, timedelta(seconds=311), rebooted + timedelta(seconds=300))

    assert state.boot_time == rebooted - timedelta(seconds=10)


def test_boot_time_follows_a_reboot() -> None:
    known = NOW - UPTIME
    state = SystemInfoState(boot_time=known)

    state.apply(INFO, timedelta(minutes=3), NOW)

    assert state.boot_time == NOW - timedelta(minutes=3)


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
    return coordinator


def test_tick_stores_the_readings_and_notifies_the_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(INFO, UPTIME)

    asyncio.run(coordinator._async_system_info_tick())

    assert coordinator.system_info.free_storage_percent == pytest.approx(42.5)
    assert coordinator.system_info.boot_time is not None
    sent.assert_called_once_with(coordinator.hass, system_info_signal("srv"))


INFO_DROPPED = "free storage and time server reset to unknown: HTTP 500"
UPTIME_DROPPED = "last boot reset to unknown: timed out"
SYSTEM_INFO_ENTITY_CLASSES = [ComexioFreeStorageSensor, ComexioLastBootSensor, ComexioTimeServerProblemSensor]


def _poll_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "System info poll failed" in r.getMessage()]


@pytest.mark.parametrize(
    ("info", "uptime", "expected"),
    [
        (
            None,
            None,
            ["free storage and time server reset to unknown: HTTP 500", "last boot reset to unknown: timed out"],
        ),
        (INFO, None, ["last boot reset to unknown: timed out"]),
        (None, UPTIME, ["free storage and time server reset to unknown: HTTP 500"]),
    ],
)
def test_tick_warns_once_per_part_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    info: SystemInfo | None,
    uptime: timedelta | None,
    expected: list[str],
) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(info, uptime)

    with caplog.at_level(logging.WARNING):
        for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1):
            asyncio.run(coordinator._async_system_info_tick())

    prefix = f"[srv] System info poll failed {SYSTEM_INFO_FAIL_STREAK_THRESHOLD} times in a row, "
    assert _poll_warnings(caplog) == [prefix + line for line in expected]
    # The part that kept answering keeps its reading.
    assert (coordinator.system_info.free_storage_percent is None) is (info is None)
    assert (coordinator.system_info.boot_time is None) is (uptime is None)
    # Every tick notifies, so the entities show "unknown" right when the readings drop.
    assert sent.call_count == SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1


def test_start_poll_ticks_now_and_on_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    track = MagicMock(return_value="cancel")
    monkeypatch.setattr(coordinator_module, "async_track_time_interval", track)
    coordinator = _coordinator(INFO, UPTIME)
    coordinator.hass.async_create_task = MagicMock(side_effect=lambda coro, name: coro.close())

    assert coordinator.async_start_system_info_poll() == "cancel"

    coordinator.hass.async_create_task.assert_called_once()
    assert coordinator.hass.async_create_task.call_args.kwargs["name"] == "comexio_srv_system_info_initial_tick"
    track.assert_called_once_with(
        coordinator.hass, coordinator._async_system_info_tick, timedelta(seconds=SYSTEM_INFO_POLL_INTERVAL_SEC)
    )


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
    coordinator.system_info.apply(INFO, UPTIME, NOW)
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
