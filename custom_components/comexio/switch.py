# Version: 0.7.5
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio switches (digital markers and digital outputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}
    entities = []

    # 1. Digital Markers
    if conf.get("import_markers", True):
        # Only markers explicitly marked as digital in api.py
        entities.extend(
            ComexioMarkerSwitch(coordinator, coordinator.server_id, marker)
            for marker in coordinator.data.get("markers", [])
            if marker["type"] == "digital"
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


class ComexioMarkerSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a digital Comexio Marker as a Switch."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        """Initialize the marker switch."""
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}".lower()
        self._attr_name = marker["ha_name"]
        self._attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_markers")},
            "name": f"{self.coordinator.server_id} Markers",
            "manufacturer": "Comexio",
            "model": "Marker Group",
            "via_device": (DOMAIN, self.coordinator.server_id),
        }

    @property
    def is_on(self) -> bool:
        """Return true if the digital marker is active."""
        val = self.coordinator.marker_states.get(self._marker_id, 0)
        return float(val) >= 1.0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the marker on."""
        if not await self.coordinator.api.set_value("marker", self._marker_id, 1):
            raise HomeAssistantError(f"Failed to turn on marker {self._marker_id}")

        self.coordinator.update_marker(self._marker_id, 1)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the marker off."""
        if not await self.coordinator.api.set_value("marker", self._marker_id, 0):
            raise HomeAssistantError(f"Failed to turn off marker {self._marker_id}")

        self.coordinator.update_marker(self._marker_id, 0)
        self.async_write_ha_state()


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
