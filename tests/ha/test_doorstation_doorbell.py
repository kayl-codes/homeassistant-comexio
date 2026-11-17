"""Doorstation doorbell events: entity, device, ringing on a push and what survives a page without Doorstation."""

from typing import Any

from aiocomexio.doorstation import Doorstation
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN

from .conftest import SERVER_ID, device_by_identifier

BELL_1_ENTITY_ID = f"event.{SERVER_ID}_doorstation2_di1_doorbell"
BELL_2_ENTITY_ID = f"event.{SERVER_ID}_doorstation2_di2_doorbell"
BELL_1_UNIQUE_ID = f"comexio_{SERVER_ID}_doorstation2_di1_doorbell"
EXTENSION = "UD 1"


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    """A Doorstation on extension 2 ("UD 1" of the synthetic config) whose bell buttons DI1 and DI2 are active."""
    extension = api_returns["get_raw_config"]["FubModules"]["1"]["2"]
    extension["extension"]["Identifier"] = "1000-2000-4001"  # a serial number: the extension is online
    inoutput = extension["inoutput"]
    for io_id, identifier in ((22, "DI1"), (23, "DI2")):
        inoutput[str(io_id)] = {
            "Id": io_id,
            "Identifier": identifier,
            "Description": f"Klingel {identifier}",
            "InOutputTypeId": 1,
            "Value": "0",
            "Active": True,
        }
    api_returns["get_doorstations"] = ({2: Doorstation(ext_id=2, camera_id=7, name=EXTENSION)}, None)
    return api_returns


@pytest.fixture
def entry(mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    """The fixture's extension "UD 1" reports offline, so its IOs only exist with offline extensions included."""
    return MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={CONF_INCLUDE_OFFLINE_EXTENSIONS: True},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _bring_extension_online(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """The synthetic extension reports offline, which makes its entities unavailable: bring it back."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.offline_extensions = set()
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    return coordinator


def _events(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "event"]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_every_bell_button_gets_a_doorbell_event_on_the_extension_device(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)

    events = _events(hass, entry)
    assert {e.entity_id for e in events} == {BELL_1_ENTITY_ID, BELL_2_ENTITY_ID}
    bell = er.async_get(hass).async_get(BELL_1_ENTITY_ID)
    assert bell is not None
    assert bell.unique_id == BELL_1_UNIQUE_ID
    assert bell.original_device_class == "doorbell"
    device = device_by_identifier(hass, entry.entry_id, {(DOMAIN, f"{SERVER_ID}_ud 1")})
    assert bell.device_id == device.id


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_that_reports_the_button_down_rings(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    coordinator = await _bring_extension_online(hass, entry)
    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN  # available, nothing rang yet

    coordinator.update_io_by_name(EXTENSION, "DI1", 1)
    await hass.async_block_till_done()

    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.attributes["event_type"] == "ring"
    other = hass.states.get(BELL_2_ENTITY_ID)
    assert other is not None
    assert other.state == STATE_UNKNOWN  # the other button did not ring


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize(("value", "pushed"), [(0, True), (1, False)])
async def test_a_button_up_a_poll_or_an_own_write_does_not_ring(
    hass: HomeAssistant, entry: MockConfigEntry, value: int, pushed: bool
) -> None:
    await _setup(hass, entry)
    coordinator = await _bring_extension_online(hass, entry)

    coordinator.update_io_by_name(EXTENSION, "DI1", value, pushed=pushed)
    await hass.async_block_till_done()

    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_poll_that_reads_the_button_down_does_not_ring(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    coordinator = await _bring_extension_online(hass, entry)
    coordinator.api.get_io_live_states.return_value = {"22": "1"}

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.io_states["22"] == 1.0  # the poll did read the button down ...
    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN  # ... and it did not ring


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_events_appear_with_the_poll_that_first_reports_the_doorstation(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)
    await _setup(hass, entry)
    assert _events(hass, entry) == []

    # The page now reports the Doorstation: the next poll has to carry it through the tracker to the events.
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # api_returns is read when the mock API is built, so the new answer goes onto the mock itself.
    coordinator.api.get_doorstations.return_value = ({2: Doorstation(ext_id=2, camera_id=7, name=EXTENSION)}, None)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert {e.entity_id for e in _events(hass, entry)} == {BELL_1_ENTITY_ID, BELL_2_ENTITY_ID}


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_rings_although_the_last_poll_failed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The push proves Comexio reachable: the failed poll's unavailable state must not swallow the press."""
    await _setup(hass, entry)
    coordinator = await _bring_extension_online(hass, entry)
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    coordinator.update_io_by_name(EXTENSION, "DI1", 1)
    await hass.async_block_till_done()

    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    assert state.attributes["event_type"] == "ring"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_renamed_extension_going_offline_makes_its_bells_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The offline set names the extension as it is called now, not as it was at setup."""
    await _setup(hass, entry)
    coordinator = await _bring_extension_online(hass, entry)
    for io in coordinator.data["io"]:
        if str(io["ext_id"]) == "2":
            io["ext_name"] = "UD RENAMED"
    coordinator.offline_extensions = {"UD RENAMED"}

    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_installation_without_imported_ios_gets_no_doorbell_event(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={CONF_INCLUDE_OFFLINE_EXTENSIONS: True, "import_ios": False},
    )

    await _setup(hass, entry)

    assert _events(hass, entry) == []


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_installation_without_doorstation_gets_no_doorbell_event(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)

    await _setup(hass, entry)

    assert _events(hass, entry) == []


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_page_without_doorstation_keeps_the_registered_events_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    api_returns["get_doorstations"] = ({}, None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The orphan cleanup must not delete them: a Doorstation page that fails to show is no reason to lose the entity.
    assert {e.entity_id for e in _events(hass, entry)} == {BELL_1_ENTITY_ID, BELL_2_ENTITY_ID}
    state = hass.states.get(BELL_1_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
