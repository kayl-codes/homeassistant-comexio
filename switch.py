# Version: 0.1.1
import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, MARKER_TYPE_DIGITAL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Comexio switches (digital markers)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Checkbox Filter
    if not entry.data.get("import_markers", True):
        _LOGGER.debug("Skipping marker import as per configuration")
        return

    entities = []
    for marker in coordinator.data.get("markers", []):
        if marker["type"] == MARKER_TYPE_DIGITAL:
            entities.append(ComexioMarkerSwitch(coordinator, coordinator.server_id, marker))

    async_add_entities(entities)

class ComexioMarkerSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a digital Comexio Marker as a Switch."""

    def __init__(self, coordinator, server_id, marker):
        super().__init__(coordinator)
        self.marker = marker
        self.server_id = server_id
        
        self._attr_unique_id = f"comexio_{server_id}_m{marker['id']}_sw"
        self.entity_id = f"switch.comexio_{server_id}_m{marker['id']}"
        self._attr_name = marker["name"]
        self._attr_icon = "mdi:toggle-switch"

    @property
    def is_on(self):
        """Return true if marker state is 1."""
        val = self.coordinator.marker_states.get(self.marker["id"], 0)
        return float(val) >= 1.0

    async def async_turn_on(self, **kwargs):
        """Send ON command via API."""
        if await self.coordinator.api.set_value("marker", self.marker["id"], 1):
            self.coordinator.marker_states[self.marker["id"]] = 1
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Send OFF command via API."""
        if await self.coordinator.api.set_value("marker", self.marker["id"], 0):
            self.coordinator.marker_states[self.marker["id"]] = 0
            self.async_write_ha_state()
