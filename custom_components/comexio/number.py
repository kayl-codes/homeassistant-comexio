# Version: 0.7.3
import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS, DOMAIN, MARKER_INTERVAL_MAX_VALUE, MARKER_TYPE_INTERVAL
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio numbers (analog markers)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    if not entry.data.get("import_markers", True):
        return

    entities = [
        ComexioMarkerNumber(coordinator, coordinator.server_id, marker)
        for marker in coordinator.data.get("markers", [])
        if marker["type"] == "analog"
    ]

    async_add_entities(entities)


class ComexioMarkerNumber(CoordinatorEntity, NumberEntity):
    """Representation of an analog Comexio Marker as a Number."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])

        # Unique ID for the database
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}".lower()
        # Display name
        self._attr_name = marker["ha_name"]

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

            # Fetch custom cover keywords from config
            conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
            kw_str = str(conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS))
            cover_kw = [kw.strip().lower() for kw in kw_str.split(",") if kw.strip()]
            is_cover = any(x in name_lower for x in cover_kw)

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
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

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
        # Update coordinator cache immediately for responsive UI
        self.coordinator.update_marker(self._marker_id, value)
        self.async_write_ha_state()
