# Version: 0.8.3
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

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, bus_load_signal
from .coordinator import ComexioCoordinator
from .entity import ComexioIOEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Comexio binary sensors (digital inputs)."""
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
