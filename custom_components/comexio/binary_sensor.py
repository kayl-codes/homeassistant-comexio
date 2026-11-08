# Version: 0.8.3
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    ONEWIRE_DATA_KEY,
    MarkerKind,
    bus_load_signal,
    offline_io_skipped,
    onewire_problem_unique_id,
    onewire_problems,
    plan_watch_signal,
    system_info_signal,
)
from .coordinator import ComexioCoordinator
from .entity import (
    ComexioIOEntity,
    ComexioKnxEntity,
    ComexioMarkerEntity,
    ComexioOneWireEntity,
    ComexioQuickbusIOEntity,
    ComexioStableEntityIdMixin,
    function_plan_device_info,
    hub_device_info,
    onewire_device_info,
    onewire_sensors_for_platform,
    quickbus_ios_for_platform,
)

_LOGGER = logging.getLogger(__name__)

# The last state of a digital 1-Wire sensor as the reading it seeds after a restart; "unknown"/"unavailable" seed none.
_RESTORED_READINGS = {STATE_ON: 1.0, STATE_OFF: 0.0}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio binary sensors (digital inputs, read-only digital markers)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}

    entities: list[BinarySensorEntity] = []

    if conf.get("import_ios", True):
        include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
        entities.extend(
            ComexioBinarySensor(coordinator, coordinator.server_id, io)
            for io in coordinator.data.get("io", [])
            if io.get("is_binary") and io.get("is_input", True) and not offline_io_skipped(io, include_offline)
        )

    # Quickbus IOs (multitouch gestures, motion detectors, ...) are read-only, outputs included
    entities.extend(
        ComexioQuickbusBinarySensor(coordinator, coordinator.server_id, io)
        for io in quickbus_ios_for_platform(coordinator, conf, "binary_sensor")
    )

    if conf.get("import_markers", True):
        ignored_ids = coordinator.ignored_marker_ids
        entities.extend(
            ComexioMarkerBinarySensor(coordinator, coordinator.server_id, marker)
            for marker in coordinator.data.get("markers", [])
            if marker["type"] == "digital"
            and marker.get("kind") == MarkerKind.READ_ONLY
            and int(marker["id"]) not in ignored_ids
        )

    # Read-only ("[RO]") digital KNX objects (blind, see project_knx_objects memory) — opt-in, default OFF
    # DPT3.x composite members are skipped here — cover.py/light.py expose the pair as one
    # composite entity instead (see project_knx_write_path_design memory, "Punkt 4, Hälfte (b)").
    if conf.get("import_knx", False):
        ignored_knx = coordinator.ignored_knx_ids
        entities.extend(
            ComexioKnxBinarySensor(coordinator, coordinator.server_id, knx)
            for knx in coordinator.data.get("knx", [])
            if knx["type"] == "digital"
            and knx.get("kind") == MarkerKind.READ_ONLY
            and int(knx["id"]) not in ignored_knx
            and knx.get("knx_composite") is None
        )

    entities.extend(_onewire_entities(coordinator))
    entities.extend(
        (
            ComexioSdCardSensor(coordinator, coordinator.server_id),
            ComexioManagedPlansProblemSensor(coordinator, coordinator.server_id),
            ComexioTimeServerProblemSensor(coordinator, coordinator.server_id),
        )
    )

    async_add_entities(entities)


def _onewire_entities(coordinator: ComexioCoordinator) -> list[BinarySensorEntity]:
    """The digital 1-Wire sensors' entities and, with the first 1-Wire sensor, the 1-Wire problem sensor.

    The data carries only the imported sensors (import_onewire); same rule as the cleanup's _onewire_entity_uids.
    """
    if not coordinator.data.get(ONEWIRE_DATA_KEY):
        return []
    entities: list[BinarySensorEntity] = [
        ComexioOneWireBinarySensor(coordinator, s) for s in onewire_sensors_for_platform(coordinator, "binary_sensor")
    ]
    entities.append(ComexioOneWireProblemSensor(coordinator))
    return entities


class ComexioBinarySensor(ComexioIOEntity, BinarySensorEntity):
    """Representation of a Comexio Digital Input."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, io)
        name_lower = io["name"].lower()
        if any(x in name_lower for x in ["bewegung", "presence", "präsenz"]):
            self._attr_device_class = BinarySensorDeviceClass.MOTION
        elif any(x in name_lower for x in ["fenster", "window"]):
            self._attr_device_class = BinarySensorDeviceClass.WINDOW
        elif any(x in name_lower for x in ["tür", "door"]):
            self._attr_device_class = BinarySensorDeviceClass.DOOR

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is active, None ("unknown") while no real value is known (#175)."""
        # Convert Comexio numeric states (1.0/0.0) to boolean
        value = self.coordinator.io_states.get(self._io_id)
        if value is None:
            return None
        try:
            return float(value) > 0
        except (ValueError, TypeError):
            _LOGGER.debug("IO %s has a non-numeric value %r, shown as unknown", self._io_id, value)
            return None


class ComexioQuickbusBinarySensor(ComexioQuickbusIOEntity, ComexioBinarySensor):
    """A digital IO of a Quickbus device (gesture, button, motion), read-only."""


class ComexioMarkerBinarySensor(ComexioMarkerEntity, BinarySensorEntity):
    """Representation of a read-only ("[RO]"-suffixed) digital Comexio Marker.

    Same unique_id as ComexioMarkerSwitch would use for a normal digital marker — HA's
    stale-platform cleanup (__init__.py) removes the writable switch entity if a marker
    is renamed to add/drop the [RO] suffix.
    """

    @property
    def is_on(self) -> bool | None:
        """True if the digital source is active, None ("unknown") while no value is known.

        A KNX object has no value until its first webhook, a marker none after a setup from the saved config
        (#250): an 'off' then would be made up rather than known.
        """
        val = self._source_value
        return None if val is None else float(val or 0) >= 1.0


class ComexioKnxBinarySensor(ComexioKnxEntity, ComexioMarkerBinarySensor):
    """A read-only ("[RO]") digital Comexio KNX object (blind implementation, see project_knx_objects memory)."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, knx: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, knx)
        # DPT-derived device_class (KNX_DPT_DIGITAL_DEVICE_CLASS, see const.py) — mirrors
        # ComexioKnxNumber's dpt_device_class handling for analog KNX items (number.py). Only
        # meaningful here: SwitchDeviceClass (ComexioKnxSwitch) has no matching values, so a
        # digital KNX item only gets a semantic device_class once it's "[RO]" and lands here.
        if dpt_device_class := knx.get("dpt_device_class"):
            try:
                self._attr_device_class = BinarySensorDeviceClass(dpt_device_class)
            except ValueError:
                # Defensive only: dpt_device_class always comes from KNX_DPT_DIGITAL_DEVICE_CLASS,
                # whose values are all valid BinarySensorDeviceClass members today — guards
                # against a future typo there taking down the whole binary_sensor platform.
                _LOGGER.debug(
                    "KNX item %s: dpt_device_class '%s' is not a valid BinarySensorDeviceClass, ignoring",
                    self._marker_id,
                    dpt_device_class,
                )


class ComexioOneWireBinarySensor(ComexioOneWireEntity, BinarySensorEntity, RestoreEntity):
    """A digital 1-Wire sensor, read-only, its reading pushed by Web-IO.

    Comexio pushes on change only, so after a restart the last state seeds the reading (restore_onewire_value).
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and (value := _RESTORED_READINGS.get(last.state)) is not None:
            self.coordinator.restore_onewire_value(self._address, value)

    @property
    def is_on(self) -> bool | None:
        value = self._reading
        return None if value is None else value != 0


class ComexioOneWireProblemSensor(ComexioStableEntityIdMixin, CoordinatorEntity, BinarySensorEntity):
    """On while a configured 1-Wire sensor has a read error, is not found or is missing from the 1-Wire monitor.

    The status comes with the config poll; unknown while it could not be fetched.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "onewire_problem"

    def __init__(self, coordinator: ComexioCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = onewire_problem_unique_id(coordinator.server_id)

    @property
    def device_info(self) -> DeviceInfo:
        return onewire_device_info(self.coordinator)

    def _problems(self) -> dict[str, list[str]] | None:
        if (statuses := self.coordinator.onewire_status) is None:
            return None
        return onewire_problems(statuses, (s["address"] for s in self.coordinator.data.get(ONEWIRE_DATA_KEY, [])))

    @property
    def is_on(self) -> bool | None:
        problems = self._problems()
        return None if problems is None else any(problems.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._problems() or {}


class ComexioManagedPlansProblemSensor(ComexioStableEntityIdMixin, CoordinatorEntity, BinarySensorEntity):
    """On while a watched function plan (HA-managed, or a user plan picked in the options) does not run.

    See plan_watchdog.

    Unknown until the watchdog's first check right after setup (coordinator.async_initial_plan_watch),
    unavailable while the plan run states cannot be read.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "managed_plans_problem"
    # Changes with every check (about once a minute, via plan_watch_signal): kept out of the recorder history.
    _unrecorded_attributes = frozenset({"last_check"})

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{server_id}_managed_plans_problem"

    @property
    def available(self) -> bool:
        # While the run states cannot be read the watchdog is blind: "off" would claim all plans run.
        return super().available and self.coordinator.plan_run_states_available

    @property
    def is_on(self) -> bool | None:
        watchdog = self.coordinator.plan_watchdog
        if watchdog.stopped is None:
            return None
        # A watched plan deleted in Comexio is a problem too: it has its own repair and push.
        return bool(watchdog.stopped) or bool(watchdog.vanished)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        watchdog = self.coordinator.plan_watchdog
        stopped = watchdog.stopped or {}
        return {
            "last_check": watchdog.last_check,
            "vanished_plans": [{"fub_id": fub_id, "name": name} for fub_id, name in sorted(watchdog.vanished or ())],
            "stopped_plans": [
                {
                    "fub_id": fub_id,
                    "name": name,
                    "user_plan": watchdog.is_user_plan(fub_id),
                    "auto_start_suspended": fub_id in watchdog.suspended,
                }
                for fub_id, name in sorted(stopped.items())
            ],
        }

    @property
    def device_info(self) -> DeviceInfo:
        return function_plan_device_info(self.coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, plan_watch_signal(self.coordinator.server_id), self._handle_coordinator_update
            )
        )


class ComexioTimeServerProblemSensor(ComexioStableEntityIdMixin, BinarySensorEntity):
    """On while Comexio does not set its clock from a time server (disabled, or no server entered).

    A clock that drifts or jumps breaks time-based logic (timers, schedules) on the server. Fed by
    the system info poll via system_info_signal, like the free storage sensor (sensor.py). Off says
    only that a server is configured, not that Comexio reaches it — the dashboard shows no sync state.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "time_server_problem"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_time_server_problem"

    @property
    def device_info(self) -> DeviceInfo:
        return hub_device_info(self.coordinator)

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.system_info.time_server_problem

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        settings = self.coordinator.system_info.time_server
        if settings is None:
            return {}
        return {"enabled": settings.enabled, "server": settings.server, "update_interval": settings.update_interval}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, system_info_signal(self.server_id), self.async_write_ha_state)
        )


class ComexioSdCardSensor(BinarySensorEntity):
    """Whether the Comexio server currently reports an SD card as present.

    Polled together with the bus workload (same admin endpoint, same fast dispatcher
    signal) — see ComexioBusLoadSensor in sensor.py for the reasoning against
    CoordinatorEntity here. No device_class: HA has none for "storage media present",
    and PROBLEM would wrongly imply "off" (no card) is always an error state.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "SD Card Present"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_sd_card_sensor"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.bus_sd_card

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, bus_load_signal(self.server_id), self._handle_bus_load_update)
        )

    @callback
    def _handle_bus_load_update(self) -> None:
        self.async_write_ha_state()
