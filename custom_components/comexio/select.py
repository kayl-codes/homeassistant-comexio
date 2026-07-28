# Version: 0.1.0
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanSelectEntity(coordinator)])


class ComexioPlanSelectEntity(CoordinatorEntity, SelectEntity):
    """Select entity listing available Logikplan plans for use in service calls."""

    _attr_has_entity_name = True
    _attr_name = "Logikplan Plan"
    _attr_icon = "mdi:file-tree-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ComexioCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{coordinator.server_id}_logikplan_plan_selector"
        self._selected: str | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def options(self) -> list[str]:
        # Plan names aren't unique in Comexio (only fub_id is) — the ID suffix keeps
        # options unambiguous and lets _resolve_fub_id() parse it back out directly.
        fub_data = getattr(self.coordinator.api, "_fub_data", {})
        return sorted(
            (f"{fub.get('Name') or f'Plan {fid}'} (ID {fid})" for fid, fub in fub_data.items()),
            key=str.lower,
        )

    @property
    def current_option(self) -> str | None:
        opts = self.options
        if self._selected in opts:
            return self._selected
        return opts[0] if opts else None

    async def async_select_option(self, option: str) -> None:
        self._selected = option
        self.async_write_ha_state()
