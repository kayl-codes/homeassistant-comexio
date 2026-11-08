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
    number.coordinator = SimpleNamespace(
        api=SimpleNamespace(set_value=set_value), update_io_by_name=update, io_push_count=lambda _io_id: 0
    )
    number.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    asyncio.run(number.async_set_native_value(value))
    return set_value, update


@pytest.mark.parametrize(("value", "sent"), [(12.1, 12.0), (25.6, 26.0), (2.5, 3.0), (99.5, 100.0), (40.0, 40.0)])
def test_percentage_write_sends_the_whole_number(value: float, sent: float) -> None:
    """#253: a service call can still pass a fraction, which Comexio rounds: the integration sends and
    keeps the whole number, so HA's state matches the output at once instead of after the push."""
    set_value, update = _write(_io_number(unit="%", min=0, max=100), value)

    set_value.assert_awaited_once_with("io", "12", sent, "LD1", "Q1")
    update.assert_called_once_with("LD1", "Q1", sent, pushed=False)


def test_fine_step_write_keeps_the_fraction() -> None:
    set_value, update = _write(_io_number(unit="V", min=0, max=10), 4.25)

    set_value.assert_awaited_once_with("io", "12", 4.25, "LD1", "Q1")
    update.assert_called_once_with("LD1", "Q1", 4.25, pushed=False)


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


def test_analog_output_takes_a_fraction_of_a_percent() -> None:
    """#259: the #253 rounding is the API's, an AO is written over the admin session and takes 30.6."""
    number = _io_number(identifier="AO1", unit="%", min=0, max=100)

    assert number.native_step == pytest.approx(0.1)
    set_value, update = _write(number, 30.6)

    set_value.assert_awaited_once_with("io", "12", 30.6, "LD1", "AO1")
    update.assert_called_once_with("LD1", "AO1", 30.6, pushed=False)


def test_an_io_written_over_the_admin_session_but_no_ao_keeps_the_whole_percent_step() -> None:
    """Route is not class: aiocomexio 0.9.0 sends AI*/QI* over the admin session too, they are no analog outputs."""
    number = _io_number(identifier="AI1", unit="%", min=0, max=100)

    assert number.native_step == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("identifier", "unit", "raw", "shown"),
    [
        ("AO1", "%", 30.1961, 30.2),
        ("AO2", "%", 3.91304, 3.9),
        ("AO3", "V", 6.43137, 6.4),
        ("Q1", "%", 30.1961, 30.1961),
        ("Q1", "V", 3.91304, 3.91304),
        ("AI1", "%", 30.1961, 30.1961),  # written over the admin session since aiocomexio 0.9.0, but no AO
        ("QI1", "V", 3.91304, 3.91304),
        ("AO3", "V", 1.25, 1.3),  # half up like Comexio, not round()'s half to even
        ("AO3", "V", 3.65, 3.7),
        ("AO3", "V", 123456789.25, 123456789.3),  # a large finite value still rounds
    ],
)
def test_only_an_analog_output_shows_one_decimal(identifier: str, unit: str, raw: float, shown: float) -> None:
    """#259: an AO reports its raw step, Comexio shows it with one decimal; other outputs stay exact."""
    number = _io_number(identifier=identifier, unit=unit, min=0, max=100 if unit == "%" else 10)
    number.coordinator = SimpleNamespace(io_states={"12": raw})

    assert number.native_value == pytest.approx(shown)


def test_a_push_during_the_write_is_not_replaced_by_the_requested_value() -> None:
    """#259, seen live: the push 5.4902 arrived before the write returned, and HA then showed 5.5."""
    pushes = [0]

    async def write_and_push(*_: Any) -> bool:
        pushes[0] += 1
        return True

    update = MagicMock()
    number = _io_number(identifier="AO3", unit="V", min=0, max=10)
    number.coordinator = SimpleNamespace(
        api=SimpleNamespace(set_value=write_and_push), update_io_by_name=update, io_push_count=lambda _io_id: pushes[0]
    )
    number.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

    asyncio.run(number.async_set_native_value(5.5))

    update.assert_not_called()
    number.async_write_ha_state.assert_called_once()


@pytest.mark.parametrize("raw", [float("inf"), float("-inf"), 1e30, -1e30])
def test_an_analog_output_passes_an_unroundable_value_through(raw: float) -> None:
    # Decimal cannot quantize it (non-finite, or more digits than its context holds); native_value must not raise
    # and break the entity's state.
    number = _io_number(identifier="AO3", unit="V", min=0, max=10)
    number.coordinator = SimpleNamespace(io_states={"12": raw})

    assert number.native_value == raw
