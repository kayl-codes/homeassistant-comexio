# Version: 0.7.5
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, bus_load_signal
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
            if not io.get("is_binary") and io.get("is_input", True) and (not io.get("offline") or include_offline)
        )

    entities.extend(
        [
            ComexioSyncStatusSensor(coordinator, coordinator.server_id),
            ComexioOfflineExtensionsSensor(coordinator, coordinator.server_id),
            ComexioFunctionPlanBackupSensor(coordinator, coordinator.server_id),
            ComexioVersionSensor(coordinator, coordinator.server_id),
            ComexioPlanChangedSensor(coordinator, coordinator.server_id),
            ComexioBusLoadSensor(coordinator, coordinator.server_id),
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
    def native_value(self) -> float | int | str | None:
        val = self.coordinator.io_states.get(self._io_id)
        if val is None:
            return None
        try:
            f = float(val)
            return int(f) if f == int(f) else f
        except (ValueError, TypeError):
            return val


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


class ComexioFunctionPlanBackupSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor summarizing stored function plan backup snapshots."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Function Plan Backups"
    _attr_icon = "mdi:backup-restore"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_function_plan_backups_sensor"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_added_to_hass(self) -> None:
        """Load the backup stores so the summary is available right after startup."""
        await super().async_added_to_hass()
        await self.coordinator.function_plan_backup.async_load()
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        summary = self.coordinator.function_plan_backup.summary()
        return summary["auto_snapshots"] + summary["change_snapshots"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.function_plan_backup.summary()


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


class ComexioVersionSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor exposing Comexio's own firmware/frontend version (e.g. "11.0.2").

    Also stamped onto the hub device's sw_version so it shows on the device info page —
    HA merges device_info fields from every entity that shares the device identifier.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_version_sensor"
        self._attr_translation_key = "comexio_version"

    @property
    def device_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }
        version = self.coordinator.api.comexio_version
        if version:
            info["sw_version"] = version
        return info

    @property
    def native_value(self) -> str | None:
        return self.coordinator.api.comexio_version


class ComexioPlanChangedSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor listing plans whose auto-backup got a new snapshot this poll cycle.

    Pure last-cycle control aid (does not accumulate across polls): right after making an
    intentional plan edit, this sensor lets you confirm that exactly the expected plan(s)
    changed and no others — a quick way to catch unexpected wiring changes in plans that
    were not meant to be touched.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:file-compare"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_plan_changed_sensor"
        self._attr_translation_key = "plan_changed"

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
        return len(self.coordinator.last_changed_plans)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"plans": self.coordinator.last_changed_plans}


class ComexioBusLoadSensor(SensorEntity):
    """Comexio internal bus/CPU workload (%), polled on its own fast cadence.

    Deliberately NOT a CoordinatorEntity: the reading changes every ~10s (see
    const.BUS_LOAD_POLL_INTERVAL_SEC) via a dedicated dispatcher signal — routing it
    through the main coordinator would notify every other entity on each tick for no
    benefit.

    Only the raw value is exposed — sustained-overload alerting (e.g. "80% for 60s") is
    left to a native HA automation (numeric_state trigger with a `for:` duration).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:chip"
    _attr_name = "Bus Workload"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_bus_load_sensor"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def native_value(self) -> int | None:
        return self.coordinator.bus_workload

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, bus_load_signal(self.server_id), self._handle_bus_load_update)
        )

    @callback
    def _handle_bus_load_update(self) -> None:
        self.async_write_ha_state()
