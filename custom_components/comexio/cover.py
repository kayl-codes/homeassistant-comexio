import asyncio
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
from .coordinator import ComexioCoordinator
from .entity import ComexioKnxDpt3Entity, write_failed

_LOGGER = logging.getLogger(__name__)


# HA calls this with `await` — the async signature is a platform-setup contract requirement
# regardless of body (python:S7503 false positive).
async def async_setup_entry(  # NOSONAR
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Comexio Blinds (KNX DPT3.008 direction+stepcode pairs) as Covers."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}
    if not conf.get("import_knx", False):
        return

    ignored_knx = coordinator.ignored_knx_ids
    knx_items = coordinator.data.get("knx", [])
    knx_by_id = {item["id"]: item for item in knx_items}

    entities: list[CoverEntity] = []
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

    def __init__(
        self,
        coordinator: ComexioCoordinator,
        server_id: str,
        direction_item: dict[str, Any],
        stepcode_item: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, server_id, direction_item, stepcode_item)
        # One open or close at a time: two that overlap would otherwise interleave their direction and step writes
        # (each target has its own lock in set_value), and a late move could start in the other command's
        # direction. A repeated write after a dropped connection widens that gap.
        self._move_lock = asyncio.Lock()
        # A stop does not wait for that lock: the cover may still be moving from an earlier command. It counts
        # itself here instead, and an open or close issued before it gives up ahead of its move telegram.
        self._stops = 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Move up: write direction=Up, then a non-zero step code to start moving."""
        await self._async_move(KNX_DPT3_COVER_DIRECTION_UP, "open")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Move down: write direction=Down, then a non-zero step code to start moving."""
        await self._async_move(KNX_DPT3_COVER_DIRECTION_DOWN, "close")

    async def _async_move(self, direction: int, verb: str) -> None:
        """Write the direction, then the move step code, as one command; a stop issued meanwhile cancels it.

        A move step write already on its way when the stop comes holds the step marker's lock in set_value, so the
        stop telegram goes out after it. Nothing awaits between the check and that write taking the lock.
        """
        stops = self._stops  # as of the call: a stop pressed while this command waits for its turn cancels it too
        failed = f"Failed to {verb} cover K{self._direction_id}/K{self._stepcode_id}"
        async with self._move_lock:
            if self._stops == stops and not await self._async_write_direction(direction):
                raise write_failed(self.coordinator, failed)
            if self._stops != stops:
                raise HomeAssistantError(f"{failed}: a stop came first")
            # No await may come between the check above and this write taking the step marker's lock (docstring).
            if not await self._async_write_stepcode(KNX_DPT3_STEPCODE_MOVE):
                raise write_failed(self.coordinator, failed)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Break: a step code of 0 stops movement regardless of the direction bit's value.

        It does not wait for an open or close in progress (see __init__), only for a move telegram already on its
        way: that one holds the step marker's lock in set_value, and the stop has to come after it.
        """
        self._stops += 1
        if not await self._async_write_stepcode(KNX_DPT3_STEPCODE_BREAK):
            raise write_failed(self.coordinator, f"Failed to stop cover K{self._direction_id}/K{self._stepcode_id}")
