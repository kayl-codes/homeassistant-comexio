# Version: 0.1.0
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN, fw_update_signal
from .coordinator import ComexioCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the extension firmware update entities (BASE + one per known extension)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    conf = {**entry.data, **entry.options}

    include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
    # "BASE" is not a separate physical extension — it's Comexio's own name for the IO-Server's
    # own built-in IOs, i.e. the same unit ComexioBaseFirmwareUpdate already represents. Creating
    # a second entity for it would collide on unique_id (both lower-case to "..._base_...").
    ext_names = sorted(
        {
            io["ext_name"]
            for io in coordinator.data.get("io", [])
            if (not io.get("offline") or include_offline) and io["ext_name"].upper() != "BASE"
        }
    )
    entities: list[UpdateEntity] = [ComexioBaseFirmwareUpdate(coordinator, coordinator.server_id)]
    entities.extend(
        ComexioExtensionFirmwareUpdate(coordinator, coordinator.server_id, ext_name) for ext_name in ext_names
    )

    async_add_entities(entities)


class ComexioFirmwareUpdateBase(UpdateEntity):
    """Shared base for the extension firmware entities.

    Deliberately NOT a CoordinatorEntity: firmware data changes only after the version-gated
    nightly check (see coordinator._async_firmware_check_tick), which fires a dedicated
    dispatcher signal — coordinator-wide refreshes are irrelevant here.

    Read-only on purpose: Comexio's install call hasn't been captured yet, so no
    UpdateEntityFeature.INSTALL is declared. FIRMWARE device class matches the data (module
    firmware versions, not the separate comexio_version software string tracked elsewhere).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_name = "Firmware"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, catalog_name: str) -> None:
        self.coordinator = coordinator
        self.server_id = server_id
        self._catalog_name = catalog_name

    @property
    def _entry(self) -> dict[str, Any] | None:
        return self.coordinator.extension_firmware.get(self._catalog_name)

    @property
    def installed_version(self) -> str | None:
        entry = self._entry
        return entry.get("version") if entry else None

    @property
    def latest_version(self) -> str | None:
        entry = self._entry
        if not entry:
            return None
        # Comexio reports "-" (not "-1" or null) when no update target is known — treat that
        # as "up to date" rather than surfacing the literal dash as a version string.
        update = entry.get("update")
        return entry.get("version") if update in (None, "-") else update

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, fw_update_signal(self.server_id), self._handle_fw_update)
        )

    @callback
    def _handle_fw_update(self) -> None:
        self.async_write_ha_state()


class ComexioBaseFirmwareUpdate(ComexioFirmwareUpdateBase):
    """Firmware status of the IO-Server base module itself."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator, server_id, catalog_name="BASE")
        self._attr_unique_id = f"comexio_{server_id}_base_firmware_update"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }


class ComexioExtensionFirmwareUpdate(ComexioFirmwareUpdateBase):
    """Firmware status of one physical extension module."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, ext_name: str) -> None:
        super().__init__(coordinator, server_id, catalog_name=ext_name)
        self._ext_name = ext_name
        self._attr_unique_id = f"comexio_{server_id}_{ext_name}_firmware_update".lower()

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_{self._ext_name}".lower())},
            "name": f"{self.coordinator.server_id} {self._ext_name}",
            "manufacturer": "Comexio",
            "model": "Extension Module",
            "via_device": (DOMAIN, self.coordinator.server_id),
        }
