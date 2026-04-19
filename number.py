# Version: 0.1.1
import logging
from homeassistant.components.number import NumberEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, MARKER_TYPE_ANALOG

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Comexio numbers (analog markers)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Checkbox Filter
    if not entry.data.get("import_markers", True):
        return

    entities = []
    for marker in coordinator.data.get("markers", []):
        if marker["type"] == MARKER_TYPE_ANALOG:
            entities.append(ComexioMarkerNumber(coordinator, coordinator.server_id, marker))

    async_add_entities(entities)

class ComexioMarkerNumber(CoordinatorEntity, NumberEntity):
    """Representation of an analog Comexio Marker as a Number."""

    def __init__(self, coordinator, server_id, marker):
        super().__init__(coordinator)
        self.marker = marker
        self.server_id = server_id
        
        self._attr_unique_id = f"comexio_{server_id}_m{marker['id']}_num"
        self.entity_id = f"number.comexio_{server_id}_m{marker['id']}"
        self._attr_name = marker["name"]
        
        # Standard range for markers
        self._attr_native_min = 0
        self._attr_native_max = 1000000  # High limit to be safe for all analog types
        self._attr_native_step = 0.1
        self._attr_icon = "mdi:gauge"

    @property
    def native_value(self):
        """Return value cleaned by API logic."""
        val = self.coordinator.marker_states.get(self.marker["id"], 0)
        return float(val)

    async def async_set_native_value(self, value):
        """Send value via API."""
        if await self.coordinator.api.set_value("marker", self.marker["id"], value):
            self.coordinator.marker_states[self.marker["id"]] = value
            self.async_write_ha_state()
