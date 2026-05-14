# Version: 0.6.0
from typing import Any
import logging
from homeassistant.components.number import NumberEntity, NumberMode, NumberDeviceClass
from homeassistant.const import UnitOfTemperature, PERCENTAGE
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Comexio numbers (analog markers)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    if not entry.data.get("import_markers", True):
        return

    entities = []
    for marker in coordinator.data.get("markers", []):
        # We only use 'analog' markers for the number platform
        if marker["type"] == "analog":
            entities.append(ComexioMarkerNumber(coordinator, coordinator.server_id, marker))

    async_add_entities(entities)

class ComexioMarkerNumber(CoordinatorEntity, NumberEntity):
    """Representation of an analog Comexio Marker as a Number."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        
        # Unique ID for the database
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}".lower()
        # Display name
        self._attr_name = marker["ha_name"]

        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 0.1
        self._attr_mode = NumberMode.AUTO

        # Intelligence: Detect Temperature Setpoints
        if marker.get("type_raw") == 3:
            self._attr_icon = "mdi:timer-outline"
        else:
            name_lower = marker["name"].lower()
            if "%" in name_lower or any(x in name_lower for x in ["rollo", "jalousie", "blind", "dimmer"]):
                self._attr_native_unit_of_measurement = PERCENTAGE
                self._attr_native_max_value = 100.0
                self._attr_icon = "mdi:window-shutter" if any(x in name_lower for x in ["rollo", "jalousie", "blind"]) else "mdi:percent"
            elif any(x in name_lower for x in ["soll", "temp", "setpoint"]):
                self._attr_device_class = NumberDeviceClass.TEMPERATURE
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
                self._attr_native_max_value = 50.0
            else:
                self._attr_icon = "mdi:gauge"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }
    
    @property
    def native_value(self) -> float:
        """Return the current value from coordinator cache."""
        val = self.coordinator.marker_states.get(self._marker_id, 0)
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    async def async_set_native_value(self, value: float) -> None:
        """Update the value via API and update local cache."""
        if await self.coordinator.api.set_value("marker", self._marker_id, value):
            # Update coordinator cache immediately for responsive UI
            self.coordinator.update_marker(self._marker_id, value)
