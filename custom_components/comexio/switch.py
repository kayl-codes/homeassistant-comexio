# Version: 0.6.0
from typing import Any
import re
import logging
from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
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
    """Set up Comexio switches (digital markers and digital outputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    # 1. Digital Markers
    if entry.data.get("import_markers", True):
        for marker in coordinator.data.get("markers", []):
            # Only markers explicitly marked as digital in api.py
            if marker["type"] == "digital":
                entities.append(ComexioMarkerSwitch(coordinator, coordinator.server_id, marker))

    # 2. Digital Outputs (Relays)
    if entry.data.get("import_ios", True):
        for io in coordinator.data.get("io", []):
            identifier = io.get("identifier", "")
            # Filter: Must be binary AND a real output (Identifier starts with Q followed by digits)
            # This regex specifically excludes "QI" (Power/Current inputs)
            if io.get("is_binary") and re.match(r"^Q\d+$", identifier):
                entities.append(ComexioIOSwitch(coordinator, coordinator.server_id, io))

    async_add_entities(entities)

class ComexioMarkerSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a digital Comexio Marker as a Switch."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        """Initialize the marker switch."""
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}".lower()
        self._attr_name = marker["ha_name"]
        self._attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def device_info(self) -> dict[str, Any]:
        """Link entity to the parent Comexio server device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }
    
    @property
    def is_on(self) -> bool:
        """Return true if the digital marker is active."""
        val = self.coordinator.marker_states.get(self._marker_id, 0)
        return float(val) >= 1.0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the marker on."""
        if await self.coordinator.api.set_value("marker", self._marker_id, 1):
            self.coordinator.update_marker(self._marker_id, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the marker off."""
        if await self.coordinator.api.set_value("marker", self._marker_id, 0):
            self.coordinator.update_marker(self._marker_id, 0)

class ComexioIOSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Comexio Digital Output (Relay) as a Switch."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        """Initialize the relay switch."""
        super().__init__(coordinator)
        self._io_id = str(io["id"])
        self._ext_name = io["ext_name"]
        self._identifier = io["identifier"]
        
        self._attr_unique_id = f"comexio_{server_id}_{self._ext_name}_{self._identifier}".lower()
        self._attr_name = io["ha_name"]
        # Real outputs are classified as OUTLET by default
        self._attr_device_class = SwitchDeviceClass.OUTLET

    @property
    def device_info(self) -> dict[str, Any]:
        """Link entity to the parent Comexio server device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def is_on(self) -> bool:
        """Return true if the relay is active."""
        val = self.coordinator.io_states.get(self._io_id, 0)
        return float(val) >= 1.0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the relay on via API."""
        if await self.coordinator.api.set_value("io", self._io_id, 1, self._ext_name, self._identifier):
            self.coordinator.update_io_by_name(self._ext_name, self._identifier, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the relay off via API."""
        if await self.coordinator.api.set_value("io", self._io_id, 0, self._ext_name, self._identifier):
            self.coordinator.update_io_by_name(self._ext_name, self._identifier, 0)
