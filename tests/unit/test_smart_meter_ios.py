"""Smart Meter readings and read-only inputs from parse_config to their entity (#249).

Comexio's type table marks the Smart Meter's readings (E in Wh, S in VA, Q in var) and inputs like
PIR and Panic as outputs ($IOInputTypes input false), and the Smart Meter's Q1-Q3 look like relays.
aiocomexio reads IntIsOutput 0 as read-only, so they become sensors and binary sensors instead of a
writable switch or the #196 Repair; an IO row without the flag still falls back to that Repair.
"""

from types import SimpleNamespace
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.sensor import ComexioIOSensor
from custom_components.comexio.unsupported_ios import (
    REASON_MEASUREMENT_OUTPUT,
    io_has_entity,
    io_platform,
    unsupported_io_reason,
)
from tests.common import load_json_fixture

# Type tables as Comexio sends them: every reading and the panel inputs are marked "not an input",
# the counter's range is decimal text (OS 11.0.2), the power factor's unit a blank.
IO_TYPES = {
    "2": {"binary": True, "min": 0, "max": 1, "unit": "1/0"},
    "81": {"binary": False, "min": "-2147483648", "max": "2147483647", "unit": "Wh"},
    "82": {"binary": False, "min": 0, "max": 100000, "unit": "VA"},
    "83": {"binary": False, "min": -100000, "max": 100000, "unit": "var"},
    "84": {"binary": False, "min": -1, "max": 1, "unit": " "},
    "140": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
IO_INPUT_TYPES = {type_id: {"input": False} for type_id in IO_TYPES}


@pytest.fixture
def ios(comexio_api: ComexioAPI) -> dict[tuple[str, str], dict[str, Any]]:
    """parse_config() IOs of config_smart_meter.json by (extension, identifier)."""
    comexio_api.io_types = IO_TYPES
    comexio_api.io_input_types = IO_INPUT_TYPES
    result = comexio_api.parse_config(load_json_fixture("config_smart_meter.json"))
    return {(io["ext_name"], io["identifier"]): io for io in result["io"]}


@pytest.mark.parametrize(
    ("key", "platform"),
    [
        (("vSM1", "E1"), "sensor"),
        (("vSM1", "S1"), "sensor"),
        # Q1 of a Smart Meter is reactive power, not a relay.
        (("vSM1", "Q1"), "sensor"),
        (("vSM1", "PF1"), "sensor"),
        # PIR and Panic were writable switches before #249.
        (("vTP1", "PIR1"), "binary_sensor"),
        (("vTP1", "PANIC1"), "binary_sensor"),
        # IntIsOutput 1 leaves a relay writable.
        (("BASE", "Q1"), "switch"),
    ],
)
def test_an_io_comexio_never_writes_gets_a_read_only_entity(
    ios: dict[tuple[str, str], dict[str, Any]], key: tuple[str, str], platform: str
) -> None:
    io = ios[key]

    # The sensor, binary_sensor and switch setups filter on these two flags themselves.
    assert io["is_input"] is (platform != "switch")
    assert io["is_binary"] is (platform != "sensor")
    assert unsupported_io_reason(io) is None
    assert io_platform(io) == platform
    # Only an IO with an entity gets a Web-IO command, so its value is pushed.
    assert io_has_entity(io, include_offline=False)


def test_a_reading_without_the_flag_still_gets_the_repair(ios: dict[tuple[str, str], dict[str, Any]]) -> None:
    """A firmware whose IO rows carry no IntIsOutput keeps the #196 fallback: no entity, a Repair."""
    io = ios["vSM2", "E1"]

    assert unsupported_io_reason(io) == REASON_MEASUREMENT_OUTPUT
    assert io_platform(io) is None
    assert not io_has_entity(io, include_offline=False)


@pytest.mark.parametrize(
    ("identifier", "device_class", "state_class", "unit"),
    [
        ("E1", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL, "Wh"),
        ("S1", SensorDeviceClass.APPARENT_POWER, SensorStateClass.MEASUREMENT, "VA"),
        ("Q1", SensorDeviceClass.REACTIVE_POWER, SensorStateClass.MEASUREMENT, "var"),
        ("PF1", None, SensorStateClass.MEASUREMENT, ""),
    ],
)
def test_a_smart_meter_reading_becomes_a_sensor_of_its_kind(
    ios: dict[tuple[str, str], dict[str, Any]],
    identifier: str,
    device_class: SensorDeviceClass | None,
    state_class: SensorStateClass,
    unit: str,
) -> None:
    coordinator = SimpleNamespace(server_id="iosrv1", offline_extensions=set())
    sensor = ComexioIOSensor(coordinator, "iosrv1", ios["vSM1", identifier])  # type: ignore[arg-type]

    assert sensor.device_class == device_class
    assert sensor.state_class == state_class
    assert sensor.native_unit_of_measurement == unit
