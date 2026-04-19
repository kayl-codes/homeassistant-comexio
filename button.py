# Version: 0.3.3
import logging
import asyncio
import time
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from homeassistant.helpers.network import get_url
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Comexio button platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Register the button entity
    async_add_entities([ComexioSyncButton(coordinator, coordinator.server_id)])

class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button for automated Web-IO lifecycle management."""

    def __init__(self, coordinator, server_id):
        """Initialize the button."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        
        # Internal unique ID for HA
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_btn"
        
        # Forced Entity ID to match your schema
        self.entity_id = f"button.comexio_{server_id}_webio_sync"
        
        # Localization and UI appearance
        self._attr_translation_key = "webio_sync"
        self._attr_icon = "mdi:cloud-upload"

    async def _wait_for_condition(self, check_func, name, should_exist, timeout=30):
        """Polls the check function until the desired state (exists or not) is reached."""
        start = time.time()
        _LOGGER.debug("[%s] Waiting for condition (should_exist: %s) for '%s'", self.server_id, should_exist, name)
        
        while time.time() - start < timeout:
            res = await check_func(name)
            # Handle possible tuple return from get_web_io_base_info
            exists = res is not None
            if exists == should_exist:
                _LOGGER.debug("[%s] Condition met after %d seconds", self.server_id, int(time.time() - start))
                return True
            await asyncio.sleep(3)
            
        _LOGGER.warning("[%s] Timeout waiting for condition (should_exist: %s) for '%s'", self.server_id, should_exist, name)
        return False

    async def async_press(self) -> None:
        """Handle the button press to start the sync process."""
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        notif_id = f"comexio_sync_{self.server_id}"
        
        # Determine language for persistent notification
        is_de = self.hass.config.language == "de"
        msg_start = f"Sync for '{webio_name}' started. Please wait..." if not is_de else f"Sync für '{webio_name}' gestartet. Bitte warten..."
        
        persistent_notification.async_create(
            self.hass, msg_start, title="Comexio Sync", notification_id=notif_id
        )

        try:
            # Auto-detect HA address
            ha_url = get_url(self.hass, require_ssl=False, prefer_external=False, allow_internal=True)
            ha_address = ha_url.replace("http://", "").replace("https://", "").rstrip("/")
            
            # --- 1. CLEANUP DEVICE ---
            dev_id = await api.get_web_io_device_info(webio_name)
            if dev_id:
                _LOGGER.info("[%s] Removing existing device %s", self.server_id, dev_id)
                await api.delete_web_io_device(dev_id)
                await self._wait_for_condition(api.get_web_io_device_info, webio_name, False)

            # --- 2. CLEANUP CLASS ---
            base_info = await api.get_web_io_base_info(webio_name)
            if base_info:
                base_id, deletable = base_info
                if deletable:
                    _LOGGER.info("[%s] Removing existing class %s", self.server_id, base_id)
                    await api.delete_web_io_base(base_id)
                    await self._wait_for_condition(api.get_web_io_base_info, webio_name, False)
                else:
                    raise Exception(f"Class '{webio_name}' is in use by another logic!")

            # --- 3. UPLOAD & RE-CREATE ---
            web_io_json = api.generate_web_io_json(self.server_id, webio_name, self.coordinator.data)
            success, result = await api.upload_web_io(self.server_id, webio_name, web_io_json)
            
            if success:
                new_base_id = result
                await asyncio.sleep(2) # Breathing time for Comexio
                await api.create_web_io_device(webio_name, new_base_id, ha_address)
                
                # VERIFY SUCCESS via the new HTML-tab check
                if await self._wait_for_condition(api.get_web_io_device_info, webio_name, True):
                    msg_ok = f"Sync successful! Device '{webio_name}' created at {ha_address}." if not is_de else f"Sync erfolgreich! Gerät '{webio_name}' wurde unter {ha_address} neu angelegt."
                    persistent_notification.async_create(self.hass, msg_ok, title="Success", notification_id=notif_id)
                else:
                    raise Exception("Sync timeout during verification.")
            else:
                raise Exception(f"Upload failed: {result}")

        except Exception as e:
            _LOGGER.exception("[%s] Sync failed", self.server_id)
            persistent_notification.async_create(
                self.hass, f"Error: {e}", title="Sync Failed", notification_id=notif_id
            )
