"""The plan preview renders a block's extended view from the right block settings (#146 follow-up)."""

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN

# A block with an auto-hidden input; element 408 switches its extended view on ("autohide": "1").
CATALOG = {"fub_base": {"77": {"in": ["a", "b"], "in_hide": ["b"], "out": ["q"], "out_hide": []}}}
ELEMENTS = {"408": {"reference": {"type": "5", "ref_id": "77"}, "position_x": 10, "position_y": 10}}
EXPANDED = {"408": {"autohide": "1"}}


async def _rendered_ref_id(hass: HomeAssistant, entry: MockConfigEntry, source: str, block_settings: Any) -> str:
    """The catalog key element 408 was rendered with for one preview call."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    with (
        patch.object(coordinator.function_plan_catalog, "async_get_catalog", AsyncMock(return_value=CATALOG)),
        patch("custom_components.comexio.coordinator.render_plan_svg", return_value="<svg/>") as render,
    ):
        await coordinator.async_generate_plan_preview(
            1, "Test1", ELEMENTS, {}, source, label_metadata=None, block_settings=block_settings
        )
    render_elements, _connections, render_catalog = render.call_args.args[:3]
    ref_id = render_elements["408"]["reference"]["ref_id"]
    assert ref_id in render_catalog["fub_base"]
    return ref_id


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def test_a_live_preview_uses_the_polled_block_settings(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    mock_comexio_api[0].block_settings = EXPANDED

    assert await _rendered_ref_id(hass, loaded_entry, "live", block_settings=None) == "77:expanded"


async def test_a_snapshot_preview_uses_the_stored_block_settings(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The live table switches the extended view on, the backup's does not: the backup wins."""
    mock_comexio_api[0].block_settings = EXPANDED

    assert await _rendered_ref_id(hass, loaded_entry, "snapshot:auto:0", block_settings={}) == "77"
    assert await _rendered_ref_id(hass, loaded_entry, "snapshot:auto:1", block_settings=EXPANDED) == "77:expanded"
