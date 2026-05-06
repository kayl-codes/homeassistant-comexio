# Version: 0.6.0
from typing import Any
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .coordinator import ComexioCoordinator

async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Comexio binary sensors (digital inputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    if not entry.data.get("import_ios", True):
        return

    entities = []
    for io in coordinator.data["io"]:
        # 1. Must be a binary type according to Comexio $ioTypes
        # 2. Must NOT be an output (Q) to avoid duplication with switch.py
        if io.get("is_binary") and not io["identifier"].startswith("Q"):
            entities.append(ComexioBinarySensor(coordinator, coordinator.server_id, io))

    async_add_entities(entities)

class ComexioBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Comexio Digital Input."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._io_id = io["id"]
        
        # Unique ID as the stable anchor in HA
        self._attr_unique_id = f"comexio_{server_id}_{self._io_id}_io_binary_sensor"
        self._attr_name = io['name']

        # Intelligence: Automatic Device Class assignment based on name
        name_lower = io['name'].lower()
        if any(x in name_lower for x in ["bewegung", "presence", "präsenz"]):
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif any(x in name_lower for x in ["fenster", "window"]):
            self._attr_device_class = BinarySensorDeviceClass.WINDOW
        elif any(x in name_lower for x in ["tür", "door"]):
            self._attr_device_class = BinarySensorDeviceClass.DOOR

    @property
    def device_info(self) -> dict[str, Any]:
        """Link entity to the parent Comexio server device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is active."""
        # Convert Comexio numeric states (1.0/0.0) to boolean
        value = self.coordinator.io_states.get(self._io_id, 0)
        try:
            return float(value) > 0
        except (ValueError, TypeError):
            return False
