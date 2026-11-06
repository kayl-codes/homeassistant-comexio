"""The 0-10 V analog outputs AO1-AO4 become writable entities (#259).

Comexio's type table marks their types (0-10V Prozent, 1-10V Prozent, Analog 0-10V, Digital 10V) as
inputs, so before #259 they became read-only sensors. aiocomexio reads the AO identifier as an output.
"""

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.unsupported_ios import io_platform, unsupported_io_reason
from tests.common import load_json_fixture

# Type ids and ranges as the live UD1 extension sends them; every one is marked as an input.
IO_TYPES = {
    "1": {"binary": False, "min": 0, "max": 10, "unit": "V"},
    "117": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "118": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "120": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
IO_INPUT_TYPES = {type_id: {"input": True} for type_id in IO_TYPES}


@pytest.mark.parametrize(
    ("identifier", "platform"),
    [("AO1", "number"), ("AO2", "number"), ("AO3", "number"), ("AO4", "switch")],
)
def test_an_analog_output_gets_a_writable_entity(comexio_api: ComexioAPI, identifier: str, platform: str) -> None:
    comexio_api.io_types = IO_TYPES
    comexio_api.io_input_types = IO_INPUT_TYPES
    result = comexio_api.parse_config(load_json_fixture("config_analog_outputs.json"))
    io = next(io for io in result["io"] if io["identifier"] == identifier)

    assert io["is_input"] is False
    assert unsupported_io_reason(io) is None
    assert io_platform(io) == platform
