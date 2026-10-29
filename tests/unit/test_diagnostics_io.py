"""The diagnostics report an entity only for an IO HA really makes one of (#196)."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio import diagnostics as diagnostics_module
from custom_components.comexio.const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, QUICKBUS_IO_DATA_KEY
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.diagnostics import (
    SKIPPED_OFFLINE_EXTENSION,
    _io_diagnostics,
    _quickbus_io_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.comexio.unsupported_ios import REASON_MEASUREMENT_OUTPUT


def _io(**fields: Any) -> dict[str, Any]:
    return {"ext_name": "EXT-1", "identifier": "I1", "is_binary": True, "is_input": True, **fields}


# An analog output metering kWh: no entity of any platform.
METER_OUTPUT = {"is_binary": False, "is_input": False, "unit": "kWh"}


@pytest.mark.parametrize(
    ("fields", "include_offline", "platform", "skipped"),
    [
        ({}, False, "binary_sensor", None),
        ({"offline": True}, False, None, SKIPPED_OFFLINE_EXTENSION),
        ({"offline": True}, True, "binary_sensor", None),
        (METER_OUTPUT, True, None, REASON_MEASUREMENT_OUTPUT),
        # Offline and unsupported: the type reason, which a later include_offline would not lift.
        ({**METER_OUTPUT, "offline": True}, False, None, REASON_MEASUREMENT_OUTPUT),
    ],
)
def test_an_io_is_reported_with_the_entity_ha_makes_of_it(
    fields: dict[str, Any], include_offline: bool, platform: str | None, skipped: str | None
) -> None:
    diagnostics = _io_diagnostics(_io(**fields), include_offline)

    assert (diagnostics["platform"], diagnostics["skipped"]) == (platform, skipped)


def _quickbus_io(**fields: Any) -> dict[str, Any]:
    return _io(id="204", quickbus={"device_id": 2, "name": "MT Bad"}, **fields)


@pytest.mark.parametrize(
    ("fields", "include_offline", "platform", "skipped"),
    [
        # Not an input: a physical IO would be a number or a switch, a Quickbus IO is read-only.
        ({"is_binary": False, "is_input": False}, False, "sensor", None),
        ({"is_input": False}, False, "binary_sensor", None),
        ({}, False, "binary_sensor", None),
        ({"offline": True}, False, None, SKIPPED_OFFLINE_EXTENSION),
        ({"offline": True}, True, "binary_sensor", None),
    ],
)
def test_a_quickbus_io_is_reported_with_its_read_only_entity(
    fields: dict[str, Any], include_offline: bool, platform: str | None, skipped: str | None
) -> None:
    diagnostics = _quickbus_io_diagnostics(_quickbus_io(**fields), include_offline)

    assert (diagnostics["platform"], diagnostics["skipped"]) == (platform, skipped)
    assert (diagnostics["id"], diagnostics["quickbus_device_id"]) == ("204", "2")
    # The device name is text of the installation: it stays out.
    assert "MT Bad" not in str(diagnostics)


def _hass_with_offline_io() -> SimpleNamespace:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.data = {
        "io": [_io(offline=True)],
        # Analog and no input: a sensor only by the Quickbus rule, unlike the physical IO.
        QUICKBUS_IO_DATA_KEY: [_quickbus_io(offline=True, is_binary=False, is_input=False)],
    }
    coordinator.api = SimpleNamespace(comexio_version="11.0.2", io_types={}, io_input_types={})
    coordinator.unsupported_ios = SimpleNamespace(raw_read_at=None, last_error_type=None, extensions={}, raw_ios=[])
    return SimpleNamespace(data={DOMAIN: {"entry": coordinator}})


@pytest.mark.parametrize(
    ("data", "options", "platform", "quickbus_platform"),
    [
        ({}, {}, None, None),
        ({CONF_INCLUDE_OFFLINE_EXTENSIONS: True}, {}, "binary_sensor", "sensor"),
        ({}, {CONF_INCLUDE_OFFLINE_EXTENSIONS: True}, "binary_sensor", "sensor"),
        # The options win over the entry data, as on the entity platforms.
        ({CONF_INCLUDE_OFFLINE_EXTENSIONS: True}, {CONF_INCLUDE_OFFLINE_EXTENSIONS: False}, None, None),
    ],
)
def test_the_diagnostics_read_include_offline_like_the_entity_platforms(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, Any],
    options: dict[str, Any],
    platform: str | None,
    quickbus_platform: str | None,
) -> None:
    async def _version(_hass: Any) -> str:
        return "0.30.0"

    monkeypatch.setattr(diagnostics_module, "async_integration_version", _version)
    entry = SimpleNamespace(entry_id="entry", data=data, options=options)

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(_hass_with_offline_io(), entry))  # type: ignore[arg-type]

    assert [io["platform"] for io in diagnostics["home_assistant"]["ios"]] == [platform]
    assert [io["platform"] for io in diagnostics["home_assistant"]["quickbus_ios"]] == [quickbus_platform]
    assert "MT Bad" not in str(diagnostics)
