"""Quickbus devices (#86): read-only entities on their own device, live values via the webhook by IO Id."""

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.comexio import entity
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN, extension_firmware_update_unique_id
from custom_components.comexio.entity import extension_device_identifier, update_quickbus_devices
from tests.common import load_json_fixture

from .conftest import SERVER_ID

WEBHOOK_URL = f"/api/webhook/comexio_{SERVER_ID}"

# IO type tables for config_quickbus.json: 134 = Multitouch gesture, 126 = motion (PIR), 12 = temperature,
# 111 = LED colour channel.
_QUICKBUS_IO_TYPES = {
    "1": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "12": {"binary": False, "min": 0, "max": 100, "unit": "°C"},
    "111": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "126": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "134": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
_QUICKBUS_IO_INPUT_TYPES = {"1": {"input": True}, "12": {"input": True}, "111": {"input": True}}


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    return {**api_returns, "get_raw_config": load_json_fixture("config_quickbus.json")}


@pytest.fixture
def api_attributes() -> dict[str, Any]:
    return {"io_types": _QUICKBUS_IO_TYPES, "io_input_types": _QUICKBUS_IO_INPUT_TYPES}


def _entries(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_quickbus_ios_become_read_only_entities_on_their_own_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    ent_reg = er.async_get(hass)
    gesture = ent_reg.async_get("binary_sensor.iosrv1_qb201")
    assert gesture is not None
    assert gesture.unique_id == "comexio_iosrv1_qb201"
    # An output (R) and an analog input stay read-only sensors.
    assert ent_reg.async_get("sensor.iosrv1_qb204") is not None
    assert ent_reg.async_get("sensor.iosrv1_qb203") is not None
    quickbus_domains = {e.domain for e in _entries(hass, mock_config_entry) if "_qb" in e.entity_id}
    assert quickbus_domains == {"binary_sensor", "sensor"}
    device = dr.async_get(hass).async_get(gesture.device_id)
    assert device.name == f"{SERVER_ID} QB - MT Flur"
    assert (DOMAIN, f"{SERVER_ID}_qb1") in device.identifiers
    host = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")})
    assert host.name == f"{SERVER_ID} BASE"
    # Same identifier G1 on two devices: two entities, two devices.
    other = ent_reg.async_get("binary_sensor.iosrv1_qb211")
    assert other is not None
    assert other.device_id != gesture.device_id


@pytest.mark.usefixtures("mock_comexio_api")
async def test_quickbus_webhook_updates_only_the_pushed_io(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client_no_auth()

    response = await client.post(WEBHOOK_URL, json={"type": "quickbus", "id": "211", "value": 1})
    await hass.async_block_till_done()

    assert response.status == 200
    assert hass.states.get("binary_sensor.iosrv1_qb211").state == "on"
    assert hass.states.get("binary_sensor.iosrv1_qb201").state == "off"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_quickbus_webhook_without_id_is_logged_and_dropped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client_no_auth()
    before = {state.entity_id: state.state for state in hass.states.async_all()}

    await client.post(WEBHOOK_URL, json={"type": "quickbus", "value": 1})
    await hass.async_block_till_done()

    assert "Webhook Quickbus event missing id" in caplog.text
    assert {state.entity_id: state.state for state in hass.states.async_all()} == before


@pytest.mark.usefixtures("mock_comexio_api")
async def test_import_quickbus_off_creates_no_quickbus_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={"import_quickbus": False},
    )
    await _setup(hass, entry)

    entity_ids = [e.entity_id for e in _entries(hass, entry)]
    assert not [entity_id for entity_id in entity_ids if "_qb" in entity_id]
    # The IOs of the host extension itself are unaffected.
    assert "binary_sensor.iosrv1_base_i1" in entity_ids


@pytest.mark.usefixtures("mock_comexio_api")
async def test_switching_import_quickbus_off_removes_the_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The setup's orphan cleanup drops Quickbus entities once their import is switched off."""
    await _setup(hass, mock_config_entry)
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("binary_sensor.iosrv1_qb201") is not None

    hass.config_entries.async_update_entry(mock_config_entry, options={"import_quickbus": False})
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert ent_reg.async_get("binary_sensor.iosrv1_qb201") is None


async def test_quickbus_entities_follow_their_host_going_offline_without_plain_ios(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """import_ios off: only the Quickbus IOs can tell the poll that their host extension went offline."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={"import_ios": False},
    )
    await _setup(hass, entry)
    assert hass.states.get("binary_sensor.iosrv1_qb201").state == "off"

    offline_config = load_json_fixture("config_quickbus.json")
    for module in offline_config["FubModules"]["1"].values():
        module["extension"]["Identifier"] = ""  # no serial number: the extension is offline
    mock_comexio_api[0].get_raw_config.return_value = offline_config
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.offline_extensions == {"BASE", "Multibridge A6"}
    assert hass.states.get("binary_sensor.iosrv1_qb201").state == "unavailable"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_quickbus_devices_are_connected_via_their_host_extension(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Already after the first setup a Quickbus device links to its host extension's device."""
    await _setup(hass, mock_config_entry)

    dev_reg = dr.async_get(hass)
    flur = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb1")})
    diele = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb3")})
    assert flur.via_device_id == dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")}).id
    # Multibridge A6 carries only Quickbus IOs: its firmware entity still gives it a device to hang under.
    multibridge = dev_reg.async_get_device(
        identifiers={(DOMAIN, extension_device_identifier(SERVER_ID, "Multibridge A6"))}
    )
    assert diele.via_device_id == multibridge.id


@pytest.mark.usefixtures("mock_comexio_api")
async def test_update_moves_a_quickbus_device_from_the_hub_to_its_host(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A Quickbus device registered before its host (parallel platform setup) is moved under the host."""
    await _setup(hass, mock_config_entry)
    dev_reg = dr.async_get(hass)
    flur = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb1")})
    hub = dev_reg.async_get_device(identifiers={(DOMAIN, SERVER_ID)})
    diele = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb3")})
    dev_reg.async_update_device(flur.id, via_device_id=hub.id)
    dev_reg.async_update_device(diele.id, via_device_id=hub.id)

    update_quickbus_devices(hass.data[DOMAIN][mock_config_entry.entry_id])

    base = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")})
    assert dev_reg.async_get(flur.id).via_device_id == base.id
    multibridge = dev_reg.async_get_device(
        identifiers={(DOMAIN, extension_device_identifier(SERVER_ID, "Multibridge A6"))}
    )
    assert dev_reg.async_get(diele.id).via_device_id == multibridge.id

    # A host without a device leaves the Quickbus device where it is.
    dev_reg.async_update_device(diele.id, via_device_id=hub.id)
    dev_reg.async_remove_device(multibridge.id)
    update_quickbus_devices(hass.data[DOMAIN][mock_config_entry.entry_id])
    assert dev_reg.async_get(diele.id).via_device_id == hub.id


@pytest.mark.usefixtures("mock_comexio_api")
async def test_setup_updates_the_quickbus_devices_once_their_hosts_are_registered(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    dev_reg = dr.async_get(hass)
    real_update = entity.update_quickbus_devices

    def update_after_all_platforms(coordinator: Any) -> None:
        assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb1")}) is not None
        assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")}) is not None
        real_update(coordinator)

    with patch.object(entity, "update_quickbus_devices", side_effect=update_after_all_platforms) as update:
        await _setup(hass, mock_config_entry)
        update.assert_called_once_with(hass.data[DOMAIN][mock_config_entry.entry_id])
        flur = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb1")})
        assert flur.via_device_id == dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")}).id

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        coordinator.async_update_listeners()  # a webhook push: same data, no new poll
        update.assert_called_once()
        coordinator.async_set_updated_data({**coordinator.data})  # a poll: new data
        assert update.call_count == 2

        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        coordinator.async_set_updated_data({**coordinator.data})
        assert update.call_count == 2  # unsubscribed on unload


async def test_a_quickbus_device_renamed_in_comexio_is_renamed_on_the_next_poll(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """HA applies device_info only when an entity is added: the poll renames the registered device."""
    await _setup(hass, mock_config_entry)
    dev_reg = dr.async_get(hass)
    flur = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_qb1")})
    assert flur.name == f"{SERVER_ID} QB - MT Flur"
    dev_reg.async_update_device(flur.id, name_by_user="Mein Flur")

    renamed_config = load_json_fixture("config_quickbus.json")
    for module in renamed_config["FubModules"]["1"].values():
        if "1" in module.get("lowcan", {}):
            module["lowcan"]["1"]["Name"] = "MT Eingang"
    mock_comexio_api[0].get_raw_config.return_value = renamed_config
    await hass.data[DOMAIN][mock_config_entry.entry_id].async_refresh()
    await hass.async_block_till_done()

    flur = dev_reg.async_get(flur.id)
    assert flur.name == f"{SERVER_ID} QB - MT Eingang"
    assert flur.name_by_user == "Mein Flur"  # the name set in HA stays
    assert flur.via_device_id == dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_base")}).id


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_quickbus_only_extension_keeps_its_firmware_entity_through_the_cleanup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Multibridge A6 hosts only Quickbus IOs: its firmware entity exists and survives the setup cleanup."""
    await _setup(hass, mock_config_entry)
    ent_reg = er.async_get(hass)
    unique_id = extension_firmware_update_unique_id(SERVER_ID, "Multibridge A6")
    entity_id = ent_reg.async_get_entity_id("update", DOMAIN, unique_id)
    assert entity_id is not None

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert ent_reg.async_get_entity_id("update", DOMAIN, unique_id) == entity_id
    assert hass.states.get(entity_id) is not None
