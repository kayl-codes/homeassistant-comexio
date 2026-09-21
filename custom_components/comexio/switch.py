# Version: 0.7.5
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, MarkerKind
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity, ComexioKnxEntity, ComexioMarkerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio switches (digital markers and digital outputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}
    entities = []

    # 1. Digital Markers (skip ignored markers)
    if conf.get("import_markers", True):
        ignored_ids = coordinator.ignored_marker_ids
        entities.extend(
            ComexioMarkerSwitch(coordinator, coordinator.server_id, marker)
            for marker in coordinator.data.get("markers", [])
            if marker["type"] == "digital"
            and int(marker["id"]) not in ignored_ids
            and marker.get("kind") == MarkerKind.NORMAL
        )

    # 1b. Digital KNX objects (blind implementation, see project_knx_objects memory) — opt-in, default OFF
    # DPT3.x composite members (the direction/control-bit half of a Dimmer/Blinds pair) are
    # skipped here — cover.py/light.py expose the pair as one composite entity instead (see
    # project_knx_write_path_design memory, "Punkt 4, Hälfte (b)").
    if conf.get("import_knx", False):
        ignored_knx = coordinator.ignored_knx_ids
        entities.extend(
            ComexioKnxSwitch(coordinator, coordinator.server_id, knx)
            for knx in coordinator.data.get("knx", [])
            if knx["type"] == "digital"
            and int(knx["id"]) not in ignored_knx
            and knx.get("kind") == MarkerKind.NORMAL
            and knx.get("knx_composite") is None
        )

    # 2. Digital Outputs (Relays) — binary and writable (not an input)
    if conf.get("import_ios", True):
        include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
        entities.extend(
            ComexioIOSwitch(coordinator, coordinator.server_id, io)
            for io in coordinator.data.get("io", [])
            if io.get("is_binary") and not io.get("is_input", True) and (not io.get("offline") or include_offline)
        )

    async_add_entities(entities)


class ComexioMarkerSwitch(ComexioMarkerEntity, SwitchEntity):
    """Representation of a digital Comexio Marker as a Switch."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        """Initialize the marker switch."""
        super().__init__(coordinator, server_id, marker)
        self._attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool:
        """Return true if the digital source is active."""
        return float(self._source_value or 0) >= 1.0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the source on."""
        if not await self._async_source_write(1):
            raise HomeAssistantError(f"Failed to turn on {self._source_label} {self._marker_id}")

        self._source_cache_update(1)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the source off."""
        if not await self._async_source_write(0):
            raise HomeAssistantError(f"Failed to turn off {self._source_label} {self._marker_id}")

        self._source_cache_update(0)
        self.async_write_ha_state()


class ComexioKnxSwitch(ComexioKnxEntity, ComexioMarkerSwitch):
    """A digital Comexio KNX object as a Switch (blind implementation, see project_knx_objects memory)."""

    @property
    def is_on(self) -> bool | None:
        """None until the first webhook. Unlike markers, KNX objects have no authoritative
        poll path, so a pre-webhook value would be a fabricated 'off' rather than a known state.
        """
        val = self._source_value
        return None if val is None else float(val or 0) >= 1.0


class ComexioIOSwitch(ComexioIOEntity, SwitchEntity):
    """Representation of a Comexio Digital Output (Relay) as a Switch."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, io)
        self._identifier = io["identifier"]
        self._attr_device_class = SwitchDeviceClass.OUTLET

    @property
    def is_on(self) -> bool:
        """Return true if the relay is active."""
        val = self.coordinator.io_states.get(self._io_id, 0)
        return float(val) >= 1.0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the relay on via API."""
        if not await self.coordinator.api.set_value("io", self._io_id, 1, self._ext_name, self._identifier):
            raise HomeAssistantError(f"Failed to turn on IO {self._ext_name} {self._identifier}")

        self.coordinator.update_io_by_name(self._ext_name, self._identifier, 1)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the relay off via API."""
        if not await self.coordinator.api.set_value("io", self._io_id, 0, self._ext_name, self._identifier):
            raise HomeAssistantError(f"Failed to turn off IO {self._ext_name} {self._identifier}")

        self.coordinator.update_io_by_name(self._ext_name, self._identifier, 0)
        self.async_write_ha_state()
