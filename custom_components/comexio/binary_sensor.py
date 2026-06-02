# Version: 0.7.5
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio binary sensors (digital inputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}

    if not conf.get("import_ios", True):
        return

    entities = [
        ComexioBinarySensor(coordinator, coordinator.server_id, io)
        for io in coordinator.data.get("io", [])
        # 1. Must be a binary type according to Comexio $ioTypes
        # 2. Must NOT be an output (Q) to avoid duplication with switch.py
        if io.get("is_binary") and not io.get("identifier", "").startswith("Q")
    ]

    async_add_entities(entities)


class ComexioBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a Comexio Digital Input."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._io_id = io["id"]
        self._ext_name = io["ext_name"]

        # Unique ID as the stable anchor in HA
        self._attr_unique_id = f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
        self._attr_name = io["ha_name"]

        # Intelligence: Automatic Device Class assignment based on name
        name_lower = io["name"].lower()
        if any(x in name_lower for x in ["bewegung", "presence", "präsenz"]):
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif any(x in name_lower for x in ["fenster", "window"]):
            self._attr_device_class = BinarySensorDeviceClass.WINDOW
        elif any(x in name_lower for x in ["tür", "door"]):
            self._attr_device_class = BinarySensorDeviceClass.DOOR

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_{self._ext_name}".lower())},
            "name": f"{self.coordinator.server_id} {self._ext_name}",
            "manufacturer": "Comexio",
            "model": "Extension Module",
            "via_device": (DOMAIN, self.coordinator.server_id),
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
