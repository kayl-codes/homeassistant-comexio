"""1-Wire sensors: entities from the config, states from the status poll, kept through the setup cleanup."""

from typing import Any

from aiocomexio.onewire import OneWireStatus
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import OneWireReading
from custom_components.comexio.const import DOMAIN, onewire_problem_unique_id, onewire_unique_id
from tests.common import load_json_fixture

from .conftest import SERVER_ID

# One analog (Binary 2) and one digital (Binary 1) sensor in the 1-Wire group "12" of $FubModules.
_ONEWIRE_GROUP = {
    "1": {"Id": 1, "Prefix": "OT", "Name": "Kitchen", "HwId": "28-1", "Binary": 2, "Active": 1},
    "2": {"Id": 2, "Prefix": "OIB", "Name": "Door", "HwId": "29-1", "Binary": 1, "Active": 1},
}
_ENTITIES = {
    ("sensor", onewire_unique_id(SERVER_ID, "OT1")),
    ("binary_sensor", onewire_unique_id(SERVER_ID, "OIB2")),
    ("binary_sensor", onewire_problem_unique_id(SERVER_ID)),
}


def _status(address: str, *, online: bool = True, value: float | None = 1.0) -> OneWireStatus:
    return OneWireStatus(address=address, device_id="28-1", name="", online=online, visible=True, value=value)


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    config = load_json_fixture("config_basic.json")
    config["FubModules"]["12"] = _ONEWIRE_GROUP
    return {
        **api_returns,
        "get_raw_config": config,
        "get_onewire_status": OneWireReading(
            {"OT1": _status("OT1", value=21.5), "OIB2": _status("OIB2", online=False)}
        ),
    }


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


@pytest.mark.usefixtures("mock_comexio_api")
async def test_configured_sensors_become_entities_and_survive_the_cleanup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The setup cleanup must keep every 1-Wire entity on its platform, through a reload too."""
    await _setup(hass, mock_config_entry)
    entity_ids = {key: _entity_id(hass, *key) for key in _ENTITIES}
    assert None not in entity_ids.values()

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert {key: _entity_id(hass, *key) for key in _ENTITIES} == entity_ids
    assert hass.states.get(entity_ids["sensor", onewire_unique_id(SERVER_ID, "OT1")]).state == "21.5"
    # A read error: the digital sensor is unavailable, and the problem sensor names it.
    door = entity_ids["binary_sensor", onewire_unique_id(SERVER_ID, "OIB2")]
    assert hass.states.get(door).state == STATE_UNAVAILABLE
    problem = hass.states.get(entity_ids["binary_sensor", onewire_problem_unique_id(SERVER_ID)])
    assert problem.state == STATE_ON
    assert problem.attributes["read_error"] == ["OIB2"]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_sensors_gone_from_the_config_lose_their_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    await _setup(hass, mock_config_entry)
    assert _entity_id(hass, "sensor", onewire_unique_id(SERVER_ID, "OT1")) is not None

    # The config every API instance returns, also one the reload builds anew.
    api_returns["get_raw_config"]["FubModules"]["12"] = {}
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert {key: _entity_id(hass, *key) for key in _ENTITIES} == dict.fromkeys(_ENTITIES)
