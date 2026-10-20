"""System info readings: failure tolerance, steady boot time, time server problem, the poll tick."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import SystemInfo, TimeServerSettings
import pytest

from custom_components.comexio import coordinator as coordinator_module
from custom_components.comexio.binary_sensor import ComexioTimeServerProblemSensor
from custom_components.comexio.const import (
    SYSTEM_BOOT_TIME_TOLERANCE_SEC,
    SYSTEM_INFO_FAIL_STREAK_THRESHOLD,
    system_info_signal,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.sensor import ComexioFreeStorageSensor, ComexioLastBootSensor
from custom_components.comexio.system_info import SystemInfoState, stable_boot_time

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
UPTIME = timedelta(days=12, hours=21)
TIME_SERVER = TimeServerSettings(enabled=True, server="pool.ntp.org", update_interval=3600)
INFO = SystemInfo(free_storage_percent=42.5, time_server=TIME_SERVER)


def _state_with_readings() -> SystemInfoState:
    state = SystemInfoState()
    assert state.apply(INFO, UPTIME, NOW) is False
    return state


def test_apply_takes_the_readings() -> None:
    state = _state_with_readings()

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_one_failed_poll_keeps_the_last_readings() -> None:
    state = _state_with_readings()

    assert state.apply(None, None, NOW + timedelta(minutes=5)) is False

    assert state.free_storage_percent == pytest.approx(42.5)
    assert state.time_server == TIME_SERVER
    assert state.boot_time == NOW - UPTIME


def test_lasting_failure_drops_the_readings_and_reports_it_once() -> None:
    state = _state_with_readings()
    results = [state.apply(None, None, NOW) for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1)]

    assert results == [False] * (SYSTEM_INFO_FAIL_STREAK_THRESHOLD - 1) + [True, False]
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
    coordinator.api.get_system_info = AsyncMock(return_value=(info, uptime))
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


def test_tick_warns_once_when_the_readings_turn_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(None, None)

    with caplog.at_level(logging.WARNING):
        for _ in range(SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1):
            asyncio.run(coordinator._async_system_info_tick())

    assert [r.getMessage() for r in caplog.records if "System info poll failed" in r.getMessage()] == [
        f"[srv] System info poll failed {SYSTEM_INFO_FAIL_STREAK_THRESHOLD} times in a row; "
        "the affected diagnostics are reset to unknown"
    ]
    # Every tick notifies, so the entities show "unknown" right when the readings drop.
    assert sent.call_count == SYSTEM_INFO_FAIL_STREAK_THRESHOLD + 1


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
