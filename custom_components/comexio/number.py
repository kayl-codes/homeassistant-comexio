# Version: 0.7.5
import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, MARKER_INTERVAL_MAX_VALUE, MARKER_TYPE_INTERVAL
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity, ComexioMarkerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio numbers (analog markers and analog writable outputs)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}

    entities: list = []

    if conf.get("import_markers", True):
        ignored_ids = coordinator.ignored_marker_ids
        entities.extend(
            ComexioMarkerNumber(coordinator, coordinator.server_id, marker)
            for marker in coordinator.data.get("markers", [])
            if marker["type"] == "analog" and int(marker["id"]) not in ignored_ids and not marker.get("read_only")
        )

    if conf.get("import_ios", True):
        include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
        entities.extend(
            ComexioIONumber(coordinator, coordinator.server_id, io)
            for io in coordinator.data.get("io", [])
            if not io.get("is_binary") and not io.get("is_input", True) and (not io.get("offline") or include_offline)
        )

    async_add_entities(entities)


class ComexioMarkerNumber(ComexioMarkerEntity, NumberEntity):
    """Representation of an analog Comexio Marker as a Number."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, marker)

        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 0.1
        self._attr_mode = NumberMode.AUTO

        # Intelligence: Detect Interval / Timer Markers
        if marker.get("type_raw") == MARKER_TYPE_INTERVAL:
            self._attr_icon = "mdi:timer-outline"
            self._attr_native_max_value = MARKER_INTERVAL_MAX_VALUE
            self._attr_native_step = 1.0
        else:
            name_lower = marker["name"].lower()

            # Use precomputed cover keywords from coordinator
            is_cover = any(x in name_lower for x in coordinator.cover_keywords)

            if "%" in name_lower or is_cover or "dimmer" in name_lower:
                self._attr_native_unit_of_measurement = PERCENTAGE
                self._attr_native_max_value = 100.0
                self._attr_icon = "mdi:window-shutter" if is_cover else "mdi:percent"
            elif any(x in name_lower for x in ["soll", "temp", "setpoint"]):
                self._attr_device_class = NumberDeviceClass.TEMPERATURE
                self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
                self._attr_native_max_value = 50.0
            else:
                self._attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> float | None:
        """Return the current value from coordinator cache."""
        val = self.coordinator.marker_states.get(self._marker_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            _LOGGER.debug("Could not convert marker value '%s' to float for %s", val, self._marker_id)
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the value via API and update local cache."""
        if not await self.coordinator.api.set_value("marker", self._marker_id, value):
            raise HomeAssistantError(f"Failed to set value {value} for marker {self._marker_id}")
        self.coordinator.update_marker(self._marker_id, value)
        self.async_write_ha_state()


class ComexioIONumber(ComexioIOEntity, NumberEntity):
    """Representation of an analog writable Comexio IO output (dimmer, analog out)."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, io)
        self._identifier = io["identifier"]
        self._attr_mode = NumberMode.AUTO
        self._attr_native_min_value = float(io.get("min", 0))
        _raw_max = io.get("max")
        self._attr_native_max_value = float(_raw_max) if _raw_max is not None else 100.0
        self._attr_native_step = 0.1

        unit = io.get("unit", "")
        if unit:
            self._attr_native_unit_of_measurement = unit
        if unit == "%":
            self._attr_icon = "mdi:brightness-percent"
        elif unit in ("W", "V", "A"):
            self._attr_icon = "mdi:gauge"
        else:
            self._attr_icon = "mdi:tune"

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.io_states.get(self._io_id)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Write value to Comexio IO output via API."""
        if not await self.coordinator.api.set_value("io", self._io_id, value, self._ext_name, self._identifier):
            raise HomeAssistantError(f"Failed to set value {value} for IO {self._ext_name} {self._identifier}")
        self.coordinator.update_io_by_name(self._ext_name, self._identifier, value)
        self.async_write_ha_state()
