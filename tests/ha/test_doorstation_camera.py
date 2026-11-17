"""Doorstation cameras: entity, device, image and what survives a page that shows no Doorstation."""

from typing import Any

from aiocomexio.doorstation import Doorstation
from homeassistant.components.camera import async_get_image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN

from .conftest import SERVER_ID, device_by_identifier

FRAME = b"\xff\xd8frame\xff\xd9"
CAMERA_UNIQUE_ID = f"comexio_{SERVER_ID}_doorstation2_camera"
CAMERA_ENTITY_ID = f"camera.{SERVER_ID}_doorstation2_camera"


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    """One Doorstation on extension 2 ("UD 1" of the synthetic config) whose camera answers with a frame."""
    api_returns["get_doorstations"] = ({2: Doorstation(ext_id=2, camera_id=7, name="UD 1")}, None)
    api_returns["get_doorstation_snapshot"] = (FRAME, None)
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


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_camera_sits_on_the_extension_device_and_serves_the_frame(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)

    registry = er.async_get(hass)
    camera = registry.async_get(CAMERA_ENTITY_ID)
    assert camera is not None
    assert camera.unique_id == CAMERA_UNIQUE_ID
    device = device_by_identifier(hass, entry.entry_id, {(DOMAIN, f"{SERVER_ID}_ud 1")})
    assert camera.device_id == device.id
    assert device.name == f"{SERVER_ID} EXT - UD 1"
    # The synthetic extension reports offline (no image then, see the next test): bring it back online.
    hass.data[DOMAIN][entry.entry_id].offline_extensions = set()
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_camera_of_an_offline_extension_serves_no_image(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "UD 1" in coordinator.offline_extensions

    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, CAMERA_ENTITY_ID)

    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_installation_without_doorstation_gets_no_camera(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)

    await _setup(hass, entry)

    registry = er.async_get(hass)
    assert not [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "camera"]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_page_without_doorstation_keeps_the_registered_camera_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    api_returns["get_doorstations"] = ({}, None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(CAMERA_ENTITY_ID) is not None
    state = hass.states.get(CAMERA_ENTITY_ID)
    assert state is None or state.state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_doorstation_without_any_extension_name_gets_no_camera(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({9: Doorstation(ext_id=9, camera_id=3, name=None)}, None)

    await _setup(hass, entry)

    registry = er.async_get(hass)
    assert not [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "camera"]
