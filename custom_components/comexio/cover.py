import logging
from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    KNX_DPT3_COVER_DIRECTION_DOWN,
    KNX_DPT3_COVER_DIRECTION_UP,
    KNX_DPT3_STEPCODE_BREAK,
    KNX_DPT3_STEPCODE_MOVE,
)
from .entity import ComexioKnxDpt3Entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio Blinds (KNX DPT3.008 direction+stepcode pairs) as Covers."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}
    if not conf.get("import_knx", False):
        return

    ignored_knx = coordinator.ignored_knx_ids
    knx_items = coordinator.data.get("knx", [])
    knx_by_id = {item["id"]: item for item in knx_items}

    entities = []
    for item in knx_items:
        composite = item.get("knx_composite")
        if not composite or composite["role"] != "direction" or composite["domain"] != "cover":
            continue
        stepcode_item = knx_by_id.get(composite["partner_id"])
        if stepcode_item is None:
            continue
        if int(item["id"]) in ignored_knx or int(stepcode_item["id"]) in ignored_knx:
            _LOGGER.debug(
                "Hiding DPT3.008 composite K%s/K%s: one of the two K-elements is on the KNX ignore list",
                item["id"],
                stepcode_item["id"],
            )
            continue
        entities.append(ComexioKnxCover(coordinator, coordinator.server_id, item, stepcode_item))

    async_add_entities(entities)


class ComexioKnxCover(ComexioKnxDpt3Entity, CoverEntity):
    """A DPT3.008 (Blinds) K-element pair as a Cover.

    DPT3.008 carries no absolute position, only relative up/down move + break telegrams —
    the same limitation a plain switch-actuated blind (no travel-time position tracking)
    has. is_closed therefore always stays None (unknown) rather than fabricating a state
    with no ground truth to back it.
    """

    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    _attr_is_closed = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Move up: write direction=Up, then a non-zero step code to start moving."""
        if not await self._async_write_direction(KNX_DPT3_COVER_DIRECTION_UP) or not await self._async_write_stepcode(
            KNX_DPT3_STEPCODE_MOVE
        ):
            raise HomeAssistantError(f"Failed to open cover K{self._direction_id}/K{self._stepcode_id}")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Move down: write direction=Down, then a non-zero step code to start moving."""
        if not await self._async_write_direction(KNX_DPT3_COVER_DIRECTION_DOWN) or not await self._async_write_stepcode(
            KNX_DPT3_STEPCODE_MOVE
        ):
            raise HomeAssistantError(f"Failed to close cover K{self._direction_id}/K{self._stepcode_id}")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Break: a step code of 0 stops movement regardless of the direction bit's value."""
        if not await self._async_write_stepcode(KNX_DPT3_STEPCODE_BREAK):
            raise HomeAssistantError(f"Failed to stop cover K{self._direction_id}/K{self._stepcode_id}")
