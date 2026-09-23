import asyncio
import logging
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    KNX_DPT3_LIGHT_DIRECTION_DECREASE,
    KNX_DPT3_LIGHT_DIRECTION_INCREASE,
    KNX_DPT3_LIGHT_FULL_RANGE_SECONDS,
    KNX_DPT3_STEPCODE_BREAK,
    KNX_DPT3_STEPCODE_MOVE,
)
from .coordinator import ComexioCoordinator
from .entity import ComexioKnxDpt3Entity

_LOGGER = logging.getLogger(__name__)

_MAX_BRIGHTNESS = 255


# HA calls this with `await` — the async signature is a platform-setup contract requirement
# regardless of body (python:S7503 false positive).
async def async_setup_entry(  # NOSONAR
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Comexio Dimmers (KNX DPT3.007 direction+stepcode pairs) as Lights."""
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
        if not composite or composite["role"] != "direction" or composite["domain"] != "light":
            continue
        stepcode_item = knx_by_id.get(composite["partner_id"])
        if stepcode_item is None:
            continue
        if int(item["id"]) in ignored_knx or int(stepcode_item["id"]) in ignored_knx:
            _LOGGER.debug(
                "Hiding DPT3.007 composite K%s/K%s: one of the two K-elements is on the KNX ignore list",
                item["id"],
                stepcode_item["id"],
            )
            continue
        entities.append(ComexioKnxLight(coordinator, coordinator.server_id, item, stepcode_item))

    async_add_entities(entities)


class ComexioKnxLight(ComexioKnxDpt3Entity, LightEntity, RestoreEntity):
    """A DPT3.007 (Dimmer) K-element pair as a Light with a best-effort brightness slider.

    DPT3.007 has no absolute value at all — only relative increase/decrease move telegrams,
    the same "hold to dim" gesture a real dimmer switch sends. Brightness is therefore only
    ever an internal HA-side estimate: this entity assumes KNX_DPT3_LIGHT_FULL_RANGE_SECONDS
    to travel the full 0..255 range, holds a move telegram for the proportional fraction of
    that time, then sends a break telegram — and restores its last estimate across HA
    restarts via RestoreEntity since there is no real bus feedback to re-derive it from. Any
    change made directly at the KNX actuator (a physical switch, ETS, ...) is invisible to
    this estimate and will drift until the next full-range move (user-accepted trade-off,
    see project_knx_write_path_design memory, "Punkt 4, Hälfte (b)").
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: ComexioCoordinator,
        server_id: str,
        direction_item: dict[str, Any],
        stepcode_item: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, server_id, direction_item, stepcode_item)
        self._attr_brightness: int | None = None
        self._attr_is_on = False
        self._move_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Restore the last brightness estimate — see class docstring for why."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        raw_brightness = last_state.attributes.get(ATTR_BRIGHTNESS)
        if raw_brightness is None:
            return
        try:
            self._attr_brightness = int(raw_brightness)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "K%s/K%s: could not restore brightness from last state '%s', ignoring",
                self._direction_id,
                self._stepcode_id,
                raw_brightness,
            )
            return
        self._attr_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Move towards the requested (or last-known/full) brightness — see _async_move_to."""
        target = kwargs.get(ATTR_BRIGHTNESS)
        if target is None:
            target = self._attr_brightness or _MAX_BRIGHTNESS
        await self._async_move_to(target)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Move all the way down — DPT3.007 has no dedicated 'off' telegram."""
        await self._async_move_to(0)

    async def _async_move_to(self, target: int) -> None:
        """Hold a move telegram proportional to the estimated brightness delta, then break.

        The break write runs in a finally so a cancelled sleep (HA shutdown, config-entry
        reload/unload, a cancelled service call) still attempts to stop the actuator instead
        of leaving it ramping with no stop signal ever sent (found in review 2026-09-20). The
        brightness estimate is updated to target regardless of whether the break write itself
        succeeded — the actuator physically held the move for the full duration either way, so
        holding the estimate back would only make the eventual drift worse; a failed break is
        still surfaced via HomeAssistantError, just after the state update instead of instead
        of it.
        """
        async with self._move_lock:
            current = self._attr_brightness or 0
            delta = target - current
            stop_ok = True
            if delta != 0:
                direction = KNX_DPT3_LIGHT_DIRECTION_INCREASE if delta > 0 else KNX_DPT3_LIGHT_DIRECTION_DECREASE
                duration = abs(delta) / _MAX_BRIGHTNESS * KNX_DPT3_LIGHT_FULL_RANGE_SECONDS
                if not await self._async_write_direction(direction) or not await self._async_write_stepcode(
                    KNX_DPT3_STEPCODE_MOVE
                ):
                    raise HomeAssistantError(f"Failed to dim K{self._direction_id}/K{self._stepcode_id}")
                try:
                    await asyncio.sleep(duration)
                finally:
                    stop_ok = await self._async_write_stepcode(KNX_DPT3_STEPCODE_BREAK)

            self._attr_brightness = target
            self._attr_is_on = target > 0
            self.async_write_ha_state()
            if not stop_ok:
                raise HomeAssistantError(
                    f"Stop telegram failed after dimming K{self._direction_id}/K{self._stepcode_id} "
                    "— the actuator may still be moving"
                )
