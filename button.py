# Version: 0.2.3
import logging
import asyncio
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from homeassistant.helpers.network import get_url # WICHTIG
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioSyncButton(coordinator, coordinator.server_id)])

class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, server_id):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_btn"
        self.entity_id = f"button.comexio_{server_id}_webio_sync"
        self._attr_name = "Web-IO Smart Sync"
        self._attr_icon = "mdi:cloud-upload"

    async def async_press(self) -> None:
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        
        # Dynamische IP Ermittlung
        try:
            ha_url = get_url(self.hass, require_ssl=False, prefer_external=False)
            ha_address = ha_url.replace("http://", "").replace("https://", "").rstrip("/")
            _LOGGER.debug("[%s] Auto-detected HA Address: %s", self.server_id, ha_address)
        except Exception:
            ha_address = "homeassistant.local:8123"

        try:
            # 1. Gerät löschen
            dev_id = await api.get_web_io_device_info(webio_name)
            if dev_id:
                await api.delete_web_io_device(dev_id)
                await asyncio.sleep(1)

            # 2. Klasse löschen
            base_id, deletable = await api.get_web_io_base_info(webio_name)
            if base_id and deletable:
                await api.delete_web_io_base(base_id)
                await asyncio.sleep(1)

            # 3. Neu anlegen
            web_io_json = api.generate_web_io_json(self.server_id, webio_name, self.coordinator.data)
            success, info = await api.upload_web_io(self.server_id, webio_name, web_io_json)
            
            if success:
                await api.create_web_io_device(webio_name, info, ha_address)
                msg = f"Vollständiger Sync abgeschlossen! Gerät '{webio_name}' wurde mit {ha_address} neu erstellt."
            else:
                msg = f"Fehler beim Upload: {info}"

            persistent_notification.async_create(self.hass, msg, title="Comexio Sync")
        except Exception as e:
            _LOGGER.exception("Sync error: %s", e)
