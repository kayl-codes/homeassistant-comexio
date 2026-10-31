"""Analog IO output numbers: the step HA offers for a write (#253)."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio.number import ComexioIONumber

COORDINATOR = SimpleNamespace()


def _io_number(**io: Any) -> ComexioIONumber:
    item = {"id": "12", "ext_name": "LD1", "identifier": "Q1", "ha_name": "Q1 Dimmerausgang 1", **io}
    return ComexioIONumber(COORDINATOR, "iosrv1", item)  # type: ignore[arg-type]


def test_percentage_output_steps_in_whole_percent() -> None:
    """#253: Comexio rounds a written percentage to a whole number (12.1 → 12), so a 0.1 step offered
    values the dimmer never takes and showed every value with a decimal ("2.0 %")."""
    number = _io_number(unit="%", min=0, max=100)

    assert number.native_step == 1.0
    assert number.native_unit_of_measurement == "%"
    assert number.icon == "mdi:brightness-percent"


def _write(number: ComexioIONumber, value: float) -> tuple[AsyncMock, MagicMock]:
    set_value = AsyncMock(return_value=True)
    update = MagicMock()
    number.coordinator = SimpleNamespace(api=SimpleNamespace(set_value=set_value), update_io_by_name=update)
    number.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    asyncio.run(number.async_set_native_value(value))
    return set_value, update


@pytest.mark.parametrize(("value", "sent"), [(12.1, 12.0), (25.6, 26.0), (2.5, 3.0), (99.5, 100.0), (40.0, 40.0)])
def test_percentage_write_sends_the_whole_number(value: float, sent: float) -> None:
    """#253: a service call can still pass a fraction, which Comexio rounds: the integration sends and
    keeps the whole number, so HA's state matches the output at once instead of after the push."""
    set_value, update = _write(_io_number(unit="%", min=0, max=100), value)

    set_value.assert_awaited_once_with("io", "12", sent, "LD1", "Q1")
    update.assert_called_once_with("LD1", "Q1", sent)


def test_fine_step_write_keeps_the_fraction() -> None:
    set_value, update = _write(_io_number(unit="V", min=0, max=10), 4.25)

    set_value.assert_awaited_once_with("io", "12", 4.25, "LD1", "Q1")
    update.assert_called_once_with("LD1", "Q1", 4.25)


@pytest.mark.parametrize(
    ("unit", "bounds"),
    [
        ("V", (0, 10)),
        ("A", (0, 10)),
        ("", (0, 100)),
        ("%", (0.5, 100)),
        ("%", (0, 99.5)),
    ],
    ids=["volt", "ampere", "no-unit", "percent-fractional-min", "percent-fractional-max"],
)
def test_other_outputs_keep_the_fine_step(unit: str, bounds: tuple[float, float]) -> None:
    """Only a percentage with whole bounds gets step 1: a 0-10 V output needs its decimals, and a step of 1
    from a fractional minimum would put the whole percents themselves off the frontend's step grid."""
    number = _io_number(unit=unit, min=bounds[0], max=bounds[1])

    assert number.native_step == 0.1


def test_percentage_output_without_a_type_range_steps_in_whole_percent() -> None:
    """No usable type range (#196) falls back to Comexio's whole-numbered value range: still step 1."""
    number = _io_number(unit="%", min=None, max=None)

    assert number.native_step == 1.0


@pytest.mark.parametrize(
    "io",
    [
        {"unit": "%", "min": 0, "max": 100},
        {"unit": "%", "min": None, "max": None},
        {"unit": "V", "min": 0, "max": 10},
        {"unit": "%", "min": 0.5, "max": 100},
    ],
    ids=["percent", "percent-no-range", "volt", "percent-fractional-min"],
)
def test_minimum_lies_on_the_step_grid(io: dict[str, Any]) -> None:
    """The frontend's number input counts its steps from the minimum (see test_marker_number_range)."""
    number = _io_number(**io)

    assert Decimal(str(number.native_min_value)) % Decimal(str(number.native_step)) == 0
