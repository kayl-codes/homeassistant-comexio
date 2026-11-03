"""A digital marker or KNX object shows unknown until its value is known, never a made-up off (#250)."""

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio import binary_sensor, switch


@pytest.mark.parametrize(
    "entity_class",
    [
        switch.ComexioMarkerSwitch,
        switch.ComexioKnxSwitch,
        binary_sensor.ComexioMarkerBinarySensor,
        binary_sensor.ComexioKnxBinarySensor,
    ],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (0, False), (1, True), ("0", False), ("1", True), (0.5, False), (1.0, True)],
)
def test_a_digital_source_is_unknown_until_its_value_is_known(
    entity_class: type, value: Any, expected: bool | None
) -> None:
    # A marker has no value after a setup from the saved config, a KNX object none until its first webhook.
    entity = SimpleNamespace(_source_value=value)

    assert entity_class.is_on.fget(entity) is expected
