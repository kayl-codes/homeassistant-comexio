# Version: 0.7.5
from datetime import datetime
import logging
from typing import Any
from urllib.parse import quote

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    MARKER_TYPE_INTERVAL,
    PLAN_RUN_STATE_RUNNING,
    PLAN_RUN_STATE_STOPPED,
    PLAN_RUN_STATES,
    PLAN_TRANSITION_STARTING,
    PLAN_TRANSITION_STOPPING,
    SYNC_STATE_ERROR,
    SYNC_STATE_IDLE,
    SYNC_STATE_PARTIAL,
    SYNC_STATE_SYNCING,
    SYNC_STATES,
    MarkerKind,
    bus_load_signal,
    function_plan_ids,
    function_plan_run_state_unique_id,
)
from .coordinator import ComexioCoordinator
from .entity import (
    ComexioIOEntity,
    ComexioKnxEntity,
    ComexioMarkerEntity,
    ComexioStableEntityIdMixin,
    function_plan_device_info,
)

_LOGGER = logging.getLogger(__name__)

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

    if conf.get("import_markers", True):
        ignored_ids = coordinator.ignored_marker_ids
        entities.extend(
            ComexioMarkerSensor(coordinator, coordinator.server_id, marker)
            for marker in coordinator.data.get("markers", [])
            if marker["type"] == "analog"
            and marker.get("kind") == MarkerKind.READ_ONLY
            and int(marker["id"]) not in ignored_ids
        )

    # Read-only ("[RO]") analog KNX objects (blind implementation, see project_knx_objects memory) — opt-in, default OFF
    # DPT3.x composite members are skipped here — cover.py/light.py expose the pair as one
    # composite entity instead (see project_knx_write_path_design memory, "Punkt 4, Hälfte (b)").
    if conf.get("import_knx", False):
        ignored_knx = coordinator.ignored_knx_ids
        entities.extend(
            ComexioKnxSensor(coordinator, coordinator.server_id, knx)
            for knx in coordinator.data.get("knx", [])
            if knx["type"] == "analog"
            and knx.get("kind") == MarkerKind.READ_ONLY
            and int(knx["id"]) not in ignored_knx
            and knx.get("knx_composite") is None
        )

    entities.extend(
        [
            ComexioSyncStatusSensor(coordinator, coordinator.server_id),
            ComexioOfflineExtensionsSensor(coordinator, coordinator.server_id),
            ComexioFunctionPlanBackupSensor(coordinator, coordinator.server_id),
            ComexioVersionSensor(coordinator, coordinator.server_id),
            ComexioPlanChangedSensor(coordinator, coordinator.server_id),
            ComexioBusLoadSensor(coordinator, coordinator.server_id),
            ComexioPlanPreviewSensor(coordinator, coordinator.server_id),
            ComexioExtensionCountSensor(coordinator, coordinator.server_id),
            ComexioActiveMarkerCountSensor(coordinator, coordinator.server_id),
            ComexioFunctionPlanCountSensor(coordinator, coordinator.server_id),
            ComexioWebioCommandCountSensor(coordinator, coordinator.server_id),
            ComexioWatchdogEventSensor(coordinator, coordinator.server_id),
        ]
    )

    async_add_entities(entities)

    sync_plan_sensors = _PlanRunStateSensorSync(hass, coordinator, async_add_entities)
    sync_plan_sensors()
    entry.async_on_unload(coordinator.async_add_listener(sync_plan_sensors))


class _PlanRunStateSensorSync:
    """Coordinator listener: a run-state sensor per new plan, removal of those of deleted plans."""

    def __init__(
        self, hass: HomeAssistant, coordinator: ComexioCoordinator, async_add_entities: AddEntitiesCallback
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._known_plans: set[int] = set()
        # The plan_scrape_generation last pruned against: every full poll that read $Fubs prunes
        # once. fub_data alone proves nothing — a failed scrape elsewhere (e.g. a config reload
        # during a sync) leaves it empty while the plans still exist.
        self._pruned_generation = coordinator.plan_scrape_generation - 1

    @callback
    def __call__(self) -> None:
        coordinator = self._coordinator
        current = function_plan_ids(coordinator.api.fub_data)
        if new := current - self._known_plans:
            self._known_plans.update(new)
            self._async_add_entities(
                ComexioFunctionPlanRunStateSensor(coordinator, coordinator.server_id, fub_id) for fub_id in sorted(new)
            )
        scraped = coordinator.scraped_plan_ids
        if scraped is None or self._pruned_generation == coordinator.plan_scrape_generation:
            return
        self._pruned_generation = coordinator.plan_scrape_generation
        ent_reg = er.async_get(self._hass)
        # A plan HA created after the scrape (create_fup during a poll) is in fub_data, not in scraped.
        for fub_id in sorted(self._known_plans - scraped - current):
            self._known_plans.discard(fub_id)
            uid = function_plan_run_state_unique_id(coordinator.server_id, fub_id)
            if entity_id := ent_reg.async_get_entity_id("sensor", DOMAIN, uid):
                _LOGGER.info("Removing %s: function plan %s no longer exists in Comexio", entity_id, fub_id)
                ent_reg.async_remove(entity_id)


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


class ComexioMarkerSensor(ComexioMarkerEntity, SensorEntity):
    """Representation of a read-only ("[RO]"-suffixed) analog Comexio Marker.

    Same unique_id as ComexioMarkerNumber would use for a normal analog marker — HA's
    stale-platform cleanup (__init__.py) removes the writable number entity if a marker
    is renamed to add/drop the [RO] suffix.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, marker)

        if marker.get("type_raw") == MARKER_TYPE_INTERVAL:
            self._attr_icon = "mdi:timer-outline"
        else:
            name_lower = marker["name"].lower()
            is_cover = any(x in name_lower for x in coordinator.cover_keywords)

            if "%" in name_lower or is_cover or "dimmer" in name_lower:
                self._attr_native_unit_of_measurement = PERCENTAGE
                self._attr_icon = "mdi:window-shutter" if is_cover else "mdi:percent"
            elif any(x in name_lower for x in ["soll", "temp", "setpoint"]):
                self._attr_device_class = SensorDeviceClass.TEMPERATURE
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            else:
                self._attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> float | None:
        """Return the current value from coordinator cache."""
        val = self._source_value
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


class ComexioKnxSensor(ComexioKnxEntity, ComexioMarkerSensor):
    """A read-only ("[RO]") analog Comexio KNX object (blind implementation, see project_knx_objects memory)."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, knx: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, knx)

        # Same DPT-over-name-heuristic precedence as ComexioKnxNumber (see its own comment,
        # number.py) — a read-only KNX object must not inherit ComexioMarkerSensor's
        # "soll/temp/setpoint" name guess when the resolved DPT says otherwise (or says
        # nothing at all, e.g. an unresolved/unmapped DPT keeps the name heuristic instead).
        if knx.get("dpt_min") is not None and knx.get("dpt_max") is not None:
            self._attr_native_unit_of_measurement = knx.get("dpt_unit") or None
            self._attr_device_class = None
            if dpt_device_class := knx.get("dpt_device_class"):
                try:
                    self._attr_device_class = SensorDeviceClass(dpt_device_class)
                except ValueError:
                    # Defensive only — see ComexioKnxNumber's identical guard for why.
                    _LOGGER.debug(
                        "KNX item %s: dpt_device_class '%s' is not a valid SensorDeviceClass, ignoring",
                        self._marker_id,
                        dpt_device_class,
                    )
            self._attr_icon = "mdi:knx"


ATTR_FAILED_WRITES = "failed_writes"
ATTR_PROGRESS_DETAILS = "progress_details"


class ComexioSyncStatusSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of the integration's sync status."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SYNC_STATES

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._restore_last_outcome(await self.async_get_last_state())

    def _restore_last_outcome(self, last_state: State | None) -> None:
        """Carry an aborted or partial sync's outcome over a reload or HA restart.

        Every sync ends with a reload, which builds a new coordinator whose sync_error and
        sync_failed_writes start empty — without this the sensor read "idle" right after every
        failed sync. A coordinator that already has an outcome of its own keeps it.
        """
        coordinator = self.coordinator
        if last_state is None or coordinator.in_sync or coordinator.sync_error or coordinator.sync_failed_writes:
            return
        failed = last_state.attributes.get(ATTR_FAILED_WRITES)
        if isinstance(failed, str):
            failed = [failed]
        restored = [str(name) for name in failed] if isinstance(failed, list) else []
        if last_state.state == SYNC_STATE_ERROR:
            coordinator.sync_error = True
            coordinator.sync_failed_writes = restored
        elif last_state.state == SYNC_STATE_PARTIAL:
            if not restored:
                # The list is always written with the state; this only keeps a damaged restore
                # entry from silently turning "partial" into "idle".
                _LOGGER.warning("Restored a partial sync state without its failed_writes list")
                restored = ["(names not restored)"]
            coordinator.sync_failed_writes = restored
        else:
            return
        details = last_state.attributes.get(ATTR_PROGRESS_DETAILS)
        if isinstance(details, str):
            coordinator.sync_progress_text = details

    @property
    def native_value(self) -> str:
        if getattr(self.coordinator, "in_sync", False):
            return SYNC_STATE_SYNCING
        if getattr(self.coordinator, "sync_error", False):
            return SYNC_STATE_ERROR
        return SYNC_STATE_PARTIAL if getattr(self.coordinator, "sync_failed_writes", None) else SYNC_STATE_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {ATTR_PROGRESS_DETAILS: getattr(self.coordinator, "sync_progress_text", "Idle")}
        if failed_writes := getattr(self.coordinator, "sync_failed_writes", None):
            attrs[ATTR_FAILED_WRITES] = list(failed_writes)
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
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

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
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

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

    Sustained-rise/overload detection lives in the Bus-Load-Watchdog
    (coordinator._evaluate_bus_load_watchdog); this sensor just exposes the raw reading.
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


class ComexioExtensionCountSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor: number of Comexio extension modules known to HA (online + offline)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:expansion-card"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_extension_count_sensor"
        self._attr_translation_key = "extension_count"

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
        return len(self.coordinator.data.get("extensions", {}))


class ComexioActiveMarkerCountSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor: number of active (non-ignored) markers known to HA."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:map-marker-multiple"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_active_marker_count_sensor"
        self._attr_translation_key = "active_marker_count"

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
        ignored = self.coordinator.ignored_marker_ids
        return sum(int(m["id"]) not in ignored for m in self.coordinator.data.get("markers", []))


class ComexioFunctionPlanCountSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor: total number of function plans on the Comexio server (managed + others)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:sitemap"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_function_plan_count_sensor"
        self._attr_translation_key = "function_plan_count"

    @property
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.api.fub_data)


class ComexioWebioCommandCountSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor: number of Web-IO commands HA has registered on the Comexio server."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_webio_command_count_sensor"
        self._attr_translation_key = "webio_command_count"

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
        return len(self.coordinator.data.get("webio_commands", {}))


class ComexioWatchdogEventSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor exposing the most recent Bus-Load-Watchdog event, if any.

    Ties the statistics sensors together with the watchdog for exactly the diagnosis
    context a Repair/Issue investigation needs: last trigger time, culprit plan, outcome.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:pulse"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_watchdog_event_sensor"
        self._attr_translation_key = "watchdog_event"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def _last_event(self) -> dict[str, Any] | None:
        history = self.coordinator.watchdog_history
        return history[-1] if history else None

    @property
    def native_value(self) -> datetime | None:
        event = self._last_event
        return dt_util.parse_datetime(event["timestamp"]) if event else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not (event := self._last_event):
            return {}
        return {
            "trigger_type": event.get("trigger_type"),
            "culprit_plan": event.get("culprit_plan"),
            "outcome": event.get("outcome"),
        }


class ComexioPlanPreviewSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor showing the last generated Function Plan preview (SVG) as entity_picture.

    Fed by coordinator.async_generate_plan_preview, called either from the Preview button
    (live plan) or a function_plan_visualize service call with format=svg (live or a stored
    backup snapshot) — both paths update the same coordinator.last_plan_preview, so this
    sensor always reflects whatever was last generated, regardless of the trigger.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:image-outline"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_plan_preview_sensor"
        self._attr_translation_key = "plan_preview"

    @property
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

    @property
    def entity_picture(self) -> str | None:
        preview = self.coordinator.last_plan_preview
        if not preview:
            return None
        # Cache-buster: the SVG file is overwritten in place on every new preview, so the
        # frontend needs a changing query param to notice the update.
        cache_buster = quote(str(preview.get("generated_at", "")), safe="")
        return f"/local/comexio_{self.coordinator.server_id}_plan_preview.svg?v={cache_buster}"

    @property
    def native_value(self) -> str | None:
        preview = self.coordinator.last_plan_preview
        return preview.get("plan_name") if preview else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        preview = self.coordinator.last_plan_preview or {}
        return {
            "source": preview.get("source"),
            "generated_at": preview.get("generated_at"),
        }


_PLAN_RUN_STATE_ICONS = {
    PLAN_RUN_STATE_RUNNING: "mdi:play-circle-outline",
    PLAN_RUN_STATE_STOPPED: "mdi:stop-circle-outline",
    PLAN_TRANSITION_STARTING: "mdi:progress-clock",
    PLAN_TRANSITION_STOPPING: "mdi:progress-clock",
}


class ComexioFunctionPlanRunStateSensor(ComexioStableEntityIdMixin, CoordinatorEntity, SensorEntity):
    """Whether one function plan runs in Comexio — on the function plan sub-device.

    Reads the plan's Active flag from api.fub_data: a full poll that decodes $Fubs refreshes it
    (one that cannot keeps the cached plans and their flags), the run-state poll in between (see
    coordinator.async_start_plan_run_state_poll), and HA's own start/stop right away. While HA
    itself starts or stops the plan, the state is starting/stopping (coordinator.async_plan_transition).
    The id carries only the plan id; the plan name is the display name alone.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PLAN_RUN_STATES
    _attr_translation_key = "function_plan_run_state"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, fub_id: int) -> None:
        super().__init__(coordinator)
        self._fub_id = fub_id
        self._attr_unique_id = function_plan_run_state_unique_id(server_id, fub_id)

    @property
    def _fub(self) -> dict[str, Any] | None:
        fub = self.coordinator.api.fub_data.get(str(self._fub_id))
        return fub if isinstance(fub, dict) else None

    @property
    def name(self) -> str:
        fub = self._fub
        # The device is already called "<server> # Function plans", so the plan name alone reads well.
        return (fub or {}).get("Name") or f"Plan {self._fub_id}"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.plan_run_state_available(self._fub_id) and self._fub is not None

    @property
    def native_value(self) -> str | None:
        if transition := self.coordinator.plan_transition(self._fub_id):
            return transition
        active = self.coordinator.api.get_fub_active(self._fub_id) if self._fub is not None else None
        if active is None:
            return None
        return PLAN_RUN_STATE_RUNNING if active else PLAN_RUN_STATE_STOPPED

    @property
    def icon(self) -> str:
        return _PLAN_RUN_STATE_ICONS.get(self.native_value, "mdi:help-circle-outline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"fub_id": self._fub_id}

    @property
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)
