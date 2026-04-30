# Version: 0.3.0
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfPower,
    UnitOfElectricCurrent,
    UnitOfTemperature,
    UnitOfElectricPotential,
    UnitOfFrequency,
    PERCENTAGE,
    LIGHT_LUX,
    UnitOfPressure,
    UnitOfSpeed,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

# Mapping Comexio units to HA Device Classes
UNIT_TO_DEVICE_CLASS = {
    "W": SensorDeviceClass.POWER,
    "A": SensorDeviceClass.CURRENT,
    "°C": SensorDeviceClass.TEMPERATURE,
    "V": SensorDeviceClass.VOLTAGE,
    "Hz": SensorDeviceClass.FREQUENCY,
    "lx": SensorDeviceClass.ILLUMINANCE,
    "Pa": SensorDeviceClass.PRESSURE,
    "m/s": SensorDeviceClass.WIND_SPEED,
    "km/h": SensorDeviceClass.WIND_SPEED,
    "%": SensorDeviceClass.HUMIDITY, # Often used for humidity in Comexio
}

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Comexio sensors based on dynamic type mapping."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    if not entry.data.get("import_ios", True):
        return

    entities = []
    for io in coordinator.data["io"]:
        # Only analog values (is_binary=False) are created as sensors
        if not io.get("is_binary"):
            entities.append(ComexioIOSensor(coordinator, coordinator.server_id, io))

    async_add_entities(entities)

class ComexioIOSensor(CoordinatorEntity, SensorEntity):
    """Representation of an analog Comexio Input/Output."""

    def __init__(self, coordinator, server_id, io):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._io_id = io["id"]
        
        # Stable Unique ID for the HA database
        self._attr_unique_id = f"{server_id}_{self._io_id}_io_sensor"
        # Name used for initial entity_id generation
        self._attr_name = io['name']
        
        # State class 'measurement' enables long-term statistics and graphs
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Dynamic unit and device class mapping from Comexio type list
        unit = io.get("unit", "")
        self._attr_native_unit_of_measurement = unit
        
        # Assign Device Class based on the unit provided by Comexio
        if unit in UNIT_TO_DEVICE_CLASS:
            self._attr_device_class = UNIT_TO_DEVICE_CLASS[unit]

    @property
    def device_info(self):
        """Link entity to the parent Comexio server device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def native_value(self):
        """Return the current value from coordinator cache."""
        return self.coordinator.io_states.get(self._io_id)
