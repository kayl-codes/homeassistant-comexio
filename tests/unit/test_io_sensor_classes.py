"""Analog IO sensors: which device class and state class a Comexio unit gives (#249 Smart Meter)."""

from types import SimpleNamespace

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
import pytest

from custom_components.comexio.sensor import ComexioIOSensor

COORDINATOR = SimpleNamespace(server_id="iosrv1", offline_extensions=set())


def _io_sensor(unit: str, coordinator: SimpleNamespace = COORDINATOR) -> ComexioIOSensor:
    io = {"id": "81", "ext_name": "vSM1", "identifier": "E1", "ha_name": "E1 Energy", "unit": unit}
    return ComexioIOSensor(coordinator, "iosrv1", io)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("unit", "device_class", "state_class"),
    [
        # A meter counter can run backwards (net energy), so "total", not "total_increasing".
        ("Wh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL),
        ("kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL),
        ("VA", SensorDeviceClass.APPARENT_POWER, SensorStateClass.MEASUREMENT),
        ("var", SensorDeviceClass.REACTIVE_POWER, SensorStateClass.MEASUREMENT),
        ("W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
        ("V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT),
        ("A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT),
        ("Hz", SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT),
        # Power factor (unit " " in Comexio, "" after aiocomexio) and phase angle have no device class.
        ("", None, SensorStateClass.MEASUREMENT),
        ("°", None, SensorStateClass.MEASUREMENT),
    ],
)
def test_io_sensor_unit_gives_device_and_state_class(
    unit: str, device_class: SensorDeviceClass | None, state_class: SensorStateClass
) -> None:
    sensor = _io_sensor(unit)

    assert sensor.device_class == device_class
    assert sensor.state_class == state_class
    assert sensor.native_unit_of_measurement == unit


@pytest.mark.parametrize("unit", ["Wh", "kWh", "VA", "var"])
def test_an_offline_extension_suppresses_the_state_class_of_a_meter_reading(unit: str) -> None:
    """No long-term statistics while the extension is offline, energy's "total" included."""
    sensor = _io_sensor(unit, SimpleNamespace(server_id="iosrv1", offline_extensions={"vSM1"}))

    assert sensor.state_class is None
