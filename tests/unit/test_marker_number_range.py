"""Marker numbers (number.py): the value range each name heuristic gives the slider."""

from types import SimpleNamespace
from typing import Any

from homeassistant.components.number import NumberDeviceClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
import pytest

from custom_components.comexio.const import (
    MARKER_INTERVAL_MAX_VALUE,
    MARKER_TEMPERATURE_MAX_VALUE,
    MARKER_TEMPERATURE_MIN_VALUE,
    MARKER_TYPE_INTERVAL,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
)
from custom_components.comexio.number import ComexioKnxNumber, ComexioMarkerNumber

COORDINATOR = SimpleNamespace(cover_keywords=["rollo"])


def _number(name: str, type_raw: int = 0) -> ComexioMarkerNumber:
    marker = {"id": 7, "name": name, "ha_name": f"M7 {name}", "type_raw": type_raw}
    return ComexioMarkerNumber(COORDINATOR, "iosrv1", marker)  # type: ignore[arg-type]


def _knx_number(name: str, **dpt: Any) -> ComexioKnxNumber:
    knx = {"id": 3, "name": name, "ha_name": f"K3 {name}", "type_raw": 0, **dpt}
    return ComexioKnxNumber(COORDINATOR, "iosrv1", knx)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["Soll Wohnen", "Temp Korrektur", "SETPOINT Bad"])
def test_temperature_marker_accepts_negative_values(name: str) -> None:
    """#179: a temperature marker was capped at 0-50 °C, so a negative correction offset was not settable."""
    number = _number(name)

    assert number.device_class == NumberDeviceClass.TEMPERATURE
    assert number.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert number.native_step == 0.1
    assert number.native_min_value == MARKER_TEMPERATURE_MIN_VALUE == -273.15
    assert number.native_max_value == MARKER_TEMPERATURE_MAX_VALUE == 10_000.0


@pytest.mark.parametrize("name", ["Helligkeit %", "Rollo Küche", "Dimmer Flur", "Soll %", "Temp Dimmer"])
def test_percentage_marker_keeps_0_to_100(name: str) -> None:
    """A percentage name wins over a temperature keyword in the same name."""
    number = _number(name)

    assert (number.native_min_value, number.native_max_value) == (0.0, 100.0)
    assert number.native_unit_of_measurement == PERCENTAGE
    assert number.device_class is None


def test_interval_marker_keeps_its_own_ceiling() -> None:
    number = _number("Nachlauf Temp", type_raw=MARKER_TYPE_INTERVAL)

    assert (number.native_min_value, number.native_max_value) == (0.0, MARKER_INTERVAL_MAX_VALUE)
    assert number.device_class is None


def test_other_marker_uses_the_full_webio_range() -> None:
    number = _number("Zähler")

    assert number.native_min_value == float(WEBIO_MARKER_ANALOG_MIN)
    assert number.native_max_value == float(WEBIO_MARKER_ANALOG_MAX)
    assert number.device_class is None


def test_knx_dpt_range_overrides_the_temperature_name_heuristic() -> None:
    number = _knx_number("Soll Bad", dpt_min=0, dpt_max=7, dpt_step=1, dpt_unit=None)

    assert (number.native_min_value, number.native_max_value) == (0.0, 7.0)
    assert number.native_unit_of_measurement is None
    assert number.device_class is None


def test_knx_temperature_dpt_keeps_its_own_range() -> None:
    number = _knx_number(
        "Soll Bad", dpt_min=-273, dpt_max=670760, dpt_step=0.01, dpt_unit="°C", dpt_device_class="temperature"
    )

    assert (number.native_min_value, number.native_max_value) == (-273.0, 670760.0)
    assert number.device_class == NumberDeviceClass.TEMPERATURE


def test_knx_without_dpt_falls_back_to_the_temperature_range() -> None:
    number = _knx_number("Soll Bad")

    assert (number.native_min_value, number.native_max_value) == (
        MARKER_TEMPERATURE_MIN_VALUE,
        MARKER_TEMPERATURE_MAX_VALUE,
    )
