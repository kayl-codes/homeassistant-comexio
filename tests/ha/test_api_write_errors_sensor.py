"""The diagnostic sensor "API write errors" (#290) in a real setup: registration, live updates, unload.

How a write ends up in the counters (set_value, retry, rescue) is pinned by tests/unit/test_api_write_errors.py and
test_api_write_retry.py; ComexioAPI.set_value is a stub here, so the tests report to the entry's real counters.
"""

from datetime import datetime

from aiocomexio import ComexioConnectionError
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN

from .conftest import SERVER_ID, device_by_identifier

UNIQUE_ID = f"comexio_{SERVER_ID}_api_write_errors"
ENTITY_ID = f"sensor.{SERVER_ID}_api_write_errors"
FRIENDLY_NAME = f"{SERVER_ID} API write errors"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant) -> State:
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    return state


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_sensor_is_registered_as_a_diagnostic_of_the_hub(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    entity = er.async_get(hass).async_get(ENTITY_ID)
    assert entity is not None
    assert (entity.unique_id, entity.config_entry_id) == (UNIQUE_ID, mock_config_entry.entry_id)
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    hub = device_by_identifier(hass, mock_config_entry.entry_id, {(DOMAIN, SERVER_ID)})
    assert entity.device_id == hub.id

    state = _state(hass)
    assert state.state == "0"  # nothing failed yet
    assert state.attributes["friendly_name"] == FRIENDLY_NAME
    assert state.attributes["retries"] == 0
    assert state.attributes["recovered_by_retry"] == 0
    assert state.attributes["last_failures"] == []


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_sensor_follows_the_counters_without_waiting_for_a_poll(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, mock_config_entry)
    (api,) = mock_comexio_api
    dropped = ComexioConnectionError("dropped")
    dropped.__cause__ = ConnectionResetError()

    api.write_stats.record_retry()
    state = _state(hass)
    assert (state.state, state.attributes["retries"]) == ("0", 1)  # a retry alone is no failed write

    api.write_stats.record_recovered()
    state = _state(hass)
    assert (state.state, state.attributes["recovered_by_retry"]) == ("0", 1)

    api.write_stats.record_failure("marker 5", dropped)
    state = _state(hass)
    assert state.state == "1"
    (failure,) = state.attributes["last_failures"]
    assert (failure["target"], failure["error"]) == ("marker 5", "ComexioConnectionError (ConnectionResetError)")
    assert datetime.fromisoformat(failure["at"]).tzinfo is not None  # the time of the failure, as an ISO string


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_sensor_keeps_the_latest_failures_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, mock_config_entry)
    (api,) = mock_comexio_api

    for marker in range(1, 6):
        api.write_stats.record_failure(f"marker {marker}", ComexioConnectionError("dropped"))

    state = _state(hass)
    assert state.state == "5"  # every failed write counts ...
    targets = [failure["target"] for failure in state.attributes["last_failures"]]
    assert targets == ["marker 3", "marker 4", "marker 5"]  # ... the latest three are named, oldest first


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_unload_removes_the_sensors_listener_from_the_counters(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, mock_config_entry)
    (api,) = mock_comexio_api
    assert api.write_stats._listeners  # the sensor listens while it lives

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # The counters outlive the entity (a write still under way reports into them, and ApiWriteStats swallows a
    # failing listener): only the removal of the listener keeps them from calling a removed entity.
    assert api.write_stats._listeners == []
