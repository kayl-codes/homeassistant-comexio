# Version: 0.1.3
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Check if IOs should be imported
    if not entry.data.get("import_ios", True):
        return

    entities = []
    for io in coordinator.data["io"]:
        entities.append(ComexioIOSensor(coordinator, coordinator.server_id, io))

    async_add_entities(entities)

class ComexioIOSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, server_id, io):
        super().__init__(coordinator)
        self.io = io
        clean_ext = io['ext_name'].lower().replace(" ", "_")
        clean_ident = io['identifier'].lower()
        self.entity_id = f"sensor.comexio_{server_id}_{clean_ext}_{clean_ident}"
        self._attr_name = io['name']
        self._attr_unique_id = f"{server_id}_{io['id']}_io_sensor"

    @property
    def native_value(self):
        return self.coordinator.io_states.get(self.io["id"])
