"""The managed-plans problem sensor reports deleted watched plans as well as stopped ones."""

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio.binary_sensor import ComexioManagedPlansProblemSensor


def _sensor(stopped: dict[int, str] | None, vanished: dict[int, str] | None) -> ComexioManagedPlansProblemSensor:
    sensor = object.__new__(ComexioManagedPlansProblemSensor)
    watchdog = SimpleNamespace(
        stopped=stopped,
        vanished=vanished,
        last_check=None,
        suspended=set(),
        is_user_plan=lambda _fub_id: True,
    )
    sensor.coordinator = SimpleNamespace(plan_watchdog=watchdog)  # type: ignore[assignment]
    return sensor


@pytest.mark.parametrize(
    ("stopped", "vanished", "expected"),
    [
        (None, None, None),  # nothing checked yet
        ({}, None, False),
        ({}, {}, False),
        ({19: "Test1"}, {}, True),
        ({}, {47: "Test4"}, True),  # (Sourcery) a deleted watched plan left the sensor off
    ],
)
def test_the_problem_sensor_counts_deleted_plans(
    stopped: dict[int, str] | None, vanished: dict[int, str] | None, expected: bool | None
) -> None:
    assert _sensor(stopped, vanished).is_on is expected


def test_the_problem_sensor_lists_deleted_plans() -> None:
    attrs: dict[str, Any] = _sensor({}, {47: "Test4"}).extra_state_attributes
    assert attrs["vanished_plans"] == [{"fub_id": 47, "name": "Test4"}]
    assert attrs["stopped_plans"] == []
