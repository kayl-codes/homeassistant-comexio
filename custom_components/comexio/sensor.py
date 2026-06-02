# Version: 0.7.5
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator

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
    "%": SensorDeviceClass.HUMIDITY,  # Often used for humidity in Comexio
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio sensors based on dynamic type mapping."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}

    entities = []
    if conf.get("import_ios", True):
        # Only analog values (is_binary=False) are created as sensors
        entities.extend(
            ComexioIOSensor(coordinator, coordinator.server_id, io)
            for io in coordinator.data.get("io", [])
            if not io.get("is_binary")
        )

    # Add the system sync status sensor
    entities.append(ComexioSyncStatusSensor(coordinator, coordinator.server_id))

    async_add_entities(entities)


class ComexioIOSensor(CoordinatorEntity, SensorEntity):
    """Representation of an analog Comexio Input/Output."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._io_id = io["id"]
        self._ext_name = io["ext_name"]

        # Stable Unique ID for the HA database
        self._attr_unique_id = f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
        self._attr_name = io["ha_name"]

        # State class 'measurement' enables long-term statistics and graphs
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Dynamic unit and device class mapping from Comexio type list
        unit = io.get("unit", "")
        self._attr_native_unit_of_measurement = unit

        # Assign Device Class based on the unit provided by Comexio
        if unit in UNIT_TO_DEVICE_CLASS:
            self._attr_device_class = UNIT_TO_DEVICE_CLASS[unit]

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_{self._ext_name}".lower())},
            "name": self._ext_name,
            "manufacturer": "Comexio",
            "model": "Extension Module",
            "via_device": (DOMAIN, self.coordinator.server_id),
        }

    @property
    def native_value(self) -> float | str | None:
        """Return the current value from coordinator cache."""
        return self.coordinator.io_states.get(self._io_id)


class ComexioSyncStatusSensor(CoordinatorEntity, SensorEntity):
    """Representation of the integration's sync status."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_status_sensor"
        self._attr_translation_key = "sync_status"
        self._attr_icon = "mdi:cloud-sync"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def native_value(self) -> str:
        if getattr(self.coordinator, "in_sync", False):
            return "syncing"
        else:
            return "error" if getattr(self.coordinator, "sync_error", False) else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {"progress_details": getattr(self.coordinator, "sync_progress_text", "Idle")}
        if getattr(self.coordinator, "sync_progress_pct", None) is not None:
            attrs["progress"] = self.coordinator.sync_progress_pct
        if getattr(self.coordinator, "sync_current_step", None) is not None:
            attrs["current_step"] = self.coordinator.sync_current_step
        return attrs
