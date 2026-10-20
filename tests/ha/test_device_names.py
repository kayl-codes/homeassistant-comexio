"""Sub-device names tagged by kind (EXT - / QB - ) — display only, ids stay on the technical address."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN

from .conftest import SERVER_ID


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def entry_with_ud1(mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    """The fixture's extension "UD 1" reports offline, so it only gets entities with offline extensions included."""
    return MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={CONF_INCLUDE_OFFLINE_EXTENSIONS: True},
    )


def _device_of(hass: HomeAssistant, entity_id: str) -> dr.DeviceEntry:
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None, entity_id
    return dr.async_get(hass).async_get(entity.device_id)


@pytest.mark.usefixtures("mock_comexio_api")
async def test_firmware_and_io_entities_share_the_tagged_device_and_keep_their_ids(
    hass: HomeAssistant, entry_with_ud1: MockConfigEntry
) -> None:
    await _setup(hass, entry_with_ud1)

    # HA would derive "update.iosrv1_ext_ud_1_firmware" from the tagged device name.
    extension = _device_of(hass, "update.iosrv1_ud_1_firmware")
    base = _device_of(hass, "update.iosrv1_base_firmware")
    assert extension.name == f"{SERVER_ID} EXT - UD 1"
    assert base.name == f"{SERVER_ID} BASE"
    io_devices = {
        entity.device_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry_with_ud1.entry_id)
        if entity.domain != "update" and entity.unique_id.startswith(f"comexio_{SERVER_ID}_ud 1_")
    }
    assert io_devices == {extension.id}
    assert er.async_get(hass).async_get("sensor.iosrv1_ud_1_qi1") is not None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_marker_device_keeps_its_plain_name(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_markers")})
    assert device.name == f"{SERVER_ID} Markers"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_extension_rename_moves_the_device_to_the_tagged_new_name(
    hass: HomeAssistant, entry_with_ud1: MockConfigEntry
) -> None:
    await _setup(hass, entry_with_ud1)
    dev_reg = dr.async_get(hass)
    old = dev_reg.async_get_device(identifiers={(DOMAIN, f"{SERVER_ID}_ud 1")})
    coordinator = hass.data[DOMAIN][entry_with_ud1.entry_id]

    await coordinator._migrate_extension_entities("UD 1", "UD\n9")

    renamed = dev_reg.async_get(old.id)
    assert renamed.identifiers == {(DOMAIN, f"{SERVER_ID}_ud\n9")}
    assert renamed.name == f"{SERVER_ID} EXT - UD 9"
