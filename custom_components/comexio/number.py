# Version: 0.7.5
import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    MARKER_INTERVAL_MAX_VALUE,
    MARKER_TYPE_INTERVAL,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    MarkerKind,
)
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity, ComexioKnxEntity, ComexioMarkerEntity

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
            if marker["type"] == "analog"
            and int(marker["id"]) not in ignored_ids
            and marker.get("kind") == MarkerKind.NORMAL
        )

    # Analog KNX objects (blind implementation, see project_knx_objects memory) — opt-in, default OFF
    # DPT3.x composite members (the step-code half of a Dimmer/Blinds pair) are skipped here —
    # cover.py/light.py expose the pair as one composite entity instead (see
    # project_knx_write_path_design memory, "Punkt 4, Hälfte (b)").
    if conf.get("import_knx", False):
        ignored_knx = coordinator.ignored_knx_ids
        entities.extend(
            ComexioKnxNumber(coordinator, coordinator.server_id, knx)
            for knx in coordinator.data.get("knx", [])
            if knx["type"] == "analog"
            and int(knx["id"]) not in ignored_knx
            and knx.get("kind") == MarkerKind.NORMAL
            and knx.get("knx_composite") is None
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
                # Comexio has no configurable value range for markers (see
                # WEBIO_MARKER_ANALOG_MIN/MAX docstring) — don't clamp to the 0-100 default.
                self._attr_native_min_value = float(WEBIO_MARKER_ANALOG_MIN)
                self._attr_native_max_value = float(WEBIO_MARKER_ANALOG_MAX)
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
            _LOGGER.debug("Could not convert %s value '%s' to float for %s", self._source_label, val, self._marker_id)
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the value via API and update local cache."""
        if not await self._async_source_write(value):
            raise HomeAssistantError(f"Failed to set value {value} for {self._source_label} {self._marker_id}")
        self._source_cache_update(value)
        self.async_write_ha_state()


class ComexioKnxNumber(ComexioKnxEntity, ComexioMarkerNumber):
    """An analog Comexio KNX object as a Number (blind implementation, see project_knx_objects memory)."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, knx: dict[str, Any]) -> None:
        super().__init__(coordinator, server_id, knx)

        # The KNX DPT's own value range (resolved by api._resolve_knx_dpt, see
        # project_knx_write_path_design memory) takes precedence over
        # ComexioMarkerNumber.__init__'s name-guessing heuristic above — Comexio's own
        # $IOTypesBinary catalog carries no usable min/max for KNX object types (min=max=0
        # placeholder), so the real range only comes from resolving the K-element's DPT.
        # api._process_knx always sets dpt_min/dpt_max/dpt_unit together from one
        # KNX_DPT_ANALOG_RANGES tuple (or none of them) — this guard is defensive only,
        # against a future refactor that separates those three keys.
        dpt_min, dpt_max = knx.get("dpt_min"), knx.get("dpt_max")
        if dpt_min is not None and dpt_max is not None:
            self._attr_native_min_value = float(dpt_min)
            self._attr_native_max_value = float(dpt_max)
            # dpt_step is always set together with dpt_min/dpt_max (see api._process_knx /
            # KNX_DPT_ANALOG_RANGES) — without this, every KNX number silently kept
            # ComexioMarkerNumber's hardcoded 0.1 step regardless of the DPT's actual
            # resolution (found live 2026-09-20: a 2-octet counter DPT showed a 0.1 step that
            # doesn't exist in its real 1-count resolution).
            if (dpt_step := knx.get("dpt_step")) is not None:
                self._attr_native_step = float(dpt_step)
            # The DPT is authoritative for the unit too, precisely when it HAS none: a
            # unitless DPT (e.g. DPT3.008's 0-7 step code on a "Rollo ..."-named object)
            # must not be left wearing the name heuristic's "%"/device_class guess above,
            # or the displayed unit contradicts the DPT-derived range (found in review
            # 2026-09-20 against this repo's own K7="Rollo 1" DPT3.008 test object). Same
            # reasoning for device_class: only set it from KNX_DPT_DEVICE_CLASS (e.g.
            # DPT9.001 -> temperature), never from the heuristic's guess — a DPT without a
            # mapped device class (like DPT3.008's step value) stays plain None, not "%".
            self._attr_native_unit_of_measurement = knx.get("dpt_unit") or None
            self._attr_device_class = None
            if dpt_device_class := knx.get("dpt_device_class"):
                try:
                    self._attr_device_class = NumberDeviceClass(dpt_device_class)
                except ValueError:
                    # Defensive only: dpt_device_class always comes from KNX_DPT_DEVICE_CLASS,
                    # whose values are all valid NumberDeviceClass members today — this guards
                    # against a future typo there taking down the whole number platform (a bad
                    # value here would otherwise raise out of __init__, before async_add_entities
                    # ever runs for the markers/IOs built alongside this KNX entity).
                    _LOGGER.debug(
                        "KNX item %s: dpt_device_class '%s' is not a valid NumberDeviceClass, ignoring",
                        self._marker_id,
                        dpt_device_class,
                    )
            self._attr_icon = "mdi:knx"


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
