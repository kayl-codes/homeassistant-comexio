# Version: 0.2.1
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Comexio sync button."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioSyncButton(coordinator, coordinator.server_id)])

class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button to trigger the Web-IO Smart Sync with notifications."""

    def __init__(self, coordinator, server_id):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_btn"
        self.entity_id = f"button.comexio_{server_id}_webio_sync"
        self._attr_name = "Web-IO Smart Sync"
        self._attr_icon = "mdi:cloud-upload"

    async def async_press(self) -> None:
        """Handle the button press: Delete device, delete class, upload fresh, create device."""
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        # In a real setup, we might need a way to determine the HA IP, for now we use a placeholder 
        # or you can hardcode your HA address here:
        ha_address = "homeassistant.local:8123" 

        try:
            # 1. Check for existing DEVICE first (needs to go first so CLASS becomes deletable)
            dev_id = await api.get_web_io_device_info(webio_name)
            if dev_id:
                _LOGGER.info("Device instance exists. Deleting device %s...", dev_id)
                await api.delete_web_io_device(dev_id)

            # 2. Check for existing CLASS
            base_id, deletable = await api.get_web_io_base_info(webio_name)
            if base_id and deletable:
                _LOGGER.info("Device class exists and is unassigned. Deleting class %s...", base_id)
                await api.delete_web_io_base(base_id)
            elif base_id and not deletable:
                persistent_notification.async_create(
                    self.hass, f"Klasse '{webio_name}' wird noch von anderen Geräten benutzt!", title="Sync Error"
                )
                return

            # 3. Re-Upload Class
            parsed_data = self.coordinator.data
            web_io_json = api.generate_web_io_json(self.server_id, webio_name, parsed_data)
            success, new_base_id = await api.upload_web_io(self.server_id, webio_name, web_io_json)
            
            if success:
                # 4. Create fresh Device Instance
                await api.create_web_io_device(webio_name, new_base_id, ha_address)
                msg = f"Vollständiger Sync abgeschlossen! Gerät '{webio_name}' ist bereit."
            else:
                msg = f"Fehler beim Upload der Klasse: {new_base_id}"

            persistent_notification.async_create(self.hass, msg, title="Comexio Sync")

        except Exception as e:
            _LOGGER.exception("Sync error: %s", e)
