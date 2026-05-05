# Version: 0.6.0
import logging
from homeassistant.components.number import NumberEntity, NumberMode, NumberDeviceClass
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
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

    def __init__(self, coordinator, server_id, marker):
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        
        # Einzigartige ID für die Datenbank
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}_num"
        # Anzeigename
        self._attr_name = marker["name"]

        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 0.1
        self._attr_mode = NumberMode.AUTO

        # Intelligence: Detect Temperature Setpoints
        if marker.get("type_raw") == 3:
            self._attr_icon = "mdi:timer-outline"
        else:
            name_lower = marker["name"].lower()
            if any(x in name_lower for x in ["soll", "temp", "setpoint"]):
                self._attr_device_class = NumberDeviceClass.TEMPERATURE
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
                self._attr_native_max_value = 50.0
            else:
                self._attr_icon = "mdi:gauge"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }
    
    @property
    def native_value(self):
        """Return the current value from coordinator cache."""
        val = self.coordinator.marker_states.get(self._marker_id, 0)
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    async def async_set_native_value(self, value: float):
        """Update the value via API and update local cache."""
        if await self.coordinator.api.set_value("marker", self._marker_id, value):
            # Update coordinator cache immediately for responsive UI
            self.coordinator.update_marker(self._marker_id, value)
