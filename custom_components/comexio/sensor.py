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

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity

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
        include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
        entities.extend(
            ComexioIOSensor(coordinator, coordinator.server_id, io)
            for io in coordinator.data.get("io", [])
            if not io.get("is_binary") and (not io.get("offline") or include_offline)
        )

    entities.extend(
        [
            ComexioSyncStatusSensor(coordinator, coordinator.server_id),
            ComexioOfflineExtensionsSensor(coordinator, coordinator.server_id),
        ]
    )

    async_add_entities(entities)


class ComexioIOSensor(ComexioIOEntity, SensorEntity):
    """Representation of an analog Comexio Input/Output."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, io)
        self._attr_state_class = SensorStateClass.MEASUREMENT
        unit = io.get("unit", "")
        self._attr_native_unit_of_measurement = unit
        if unit in UNIT_TO_DEVICE_CLASS:
            self._attr_device_class = UNIT_TO_DEVICE_CLASS[unit]

    @property
    def state_class(self) -> SensorStateClass | None:
        """Suppress long-term statistics while extension is offline to avoid unit-mismatch warnings."""
        if self._ext_name in self.coordinator.offline_extensions:
            return None
        return self._attr_state_class

    @property
    def native_value(self) -> float | str | None:
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
            "name": self.coordinator.server_id,
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


class ComexioOfflineExtensionsSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor listing extension modules currently offline."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-disconnect"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_offline_extensions_sensor"
        self._attr_translation_key = "offline_extensions"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def native_value(self) -> int:
        return len(self.coordinator.offline_extensions)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"extensions": sorted(self.coordinator.offline_extensions)}
