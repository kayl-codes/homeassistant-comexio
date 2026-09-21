# Version: 0.8.3
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, MarkerKind, bus_load_signal
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity, ComexioKnxEntity, ComexioMarkerEntity

_LOGGER = logging.getLogger(__name__)


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
            if io.get("is_binary") and io.get("is_input", True) and (not io.get("offline") or include_offline)
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

    entities.append(ComexioSdCardSensor(coordinator, coordinator.server_id))

    async_add_entities(entities)


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
    def is_on(self) -> bool:
        """Return true if the binary sensor is active."""
        # Convert Comexio numeric states (1.0/0.0) to boolean
        value = self.coordinator.io_states.get(self._io_id, 0)
        try:
            return float(value) > 0
        except (ValueError, TypeError):
            return False


class ComexioMarkerBinarySensor(ComexioMarkerEntity, BinarySensorEntity):
    """Representation of a read-only ("[RO]"-suffixed) digital Comexio Marker.

    Same unique_id as ComexioMarkerSwitch would use for a normal digital marker — HA's
    stale-platform cleanup (__init__.py) removes the writable switch entity if a marker
    is renamed to add/drop the [RO] suffix.
    """

    @property
    def is_on(self) -> bool:
        """Return true if the digital source is active."""
        return float(self._source_value or 0) >= 1.0


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

    @property
    def is_on(self) -> bool | None:
        """None until the first webhook. Unlike markers, KNX objects have no authoritative
        poll path, so a pre-webhook value would be a fabricated 'off' rather than a known state.
        """
        val = self._source_value
        return None if val is None else float(val or 0) >= 1.0


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
