# Version: 0.2.0
from datetime import timedelta
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import webhook, persistent_notification
import logging

from .const import (
    DOMAIN, CONF_HOST, CONF_USERNAME, CONF_PASSWORD, 
    CONF_SERVER_ID, CONF_API_USERNAME, CONF_API_PASSWORD
)
from .api import ComexioAPI
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Comexio from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    server_id = entry.data[CONF_SERVER_ID]

    # Initialize API
    api = ComexioAPI(
        hass,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        api_user=entry.data.get(CONF_API_USERNAME),
        api_pass=entry.data.get(CONF_API_PASSWORD),
    )

    await api.login()

    # Initialize Coordinator
    coordinator = ComexioCoordinator(hass, api)
    coordinator.server_id = server_id
    coordinator.config_entry = entry
    
    # Set custom update interval from config
    interval = entry.data.get("scan_interval", 15)
    coordinator.update_interval = timedelta(minutes=interval)
    
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch", "number", "button"])

    # ---------------------------
    # Service: Smart Web-IO Sync
    # ---------------------------
    async def handle_generate_web_io(call: ServiceCall):
        do_upload = call.data.get("upload", False)
        try:
            web_io_json = api.generate_web_io_json(server_id, coordinator.data)
            
            if not do_upload:
                persistent_notification.async_create(
                    hass, f"```json\n{web_io_json}\n```",
                    title=f"Comexio Preview ({server_id})"
                )
                return

            # Smart Upload Logic
            base_id, deletable = await api.get_web_io_base_info(server_id)
            if base_id:
                if deletable:
                    await api.delete_web_io_base(base_id)
                else:
                    persistent_notification.async_create(
                        hass, "Klasse in Benutzung! Bitte manuell pflegen.", 
                        title="Sync Blockiert"
                    )
                    return

            success, new_id = await api.upload_web_io(server_id, web_io_json)
            msg = f"Sync erfolgreich! ID: {new_id}" if success else f"Fehler: {new_id}"
            persistent_notification.async_create(hass, msg, title="Comexio Sync")

        except Exception as e:
            _LOGGER.exception("Service Error: %s", e)

    hass.services.async_register(DOMAIN, "generate_web_io", handle_generate_web_io)

    # ---------------------------
    # Webhook Setup
    # ---------------------------
    webhook_id = f"comexio_{server_id}"
    async def handle_webhook(hass, webhook_id, request):
        try:
            data = await request.json()
            val = data.get("value")
            if data.get("type") == "io":
                coordinator.update_io_by_name(data.get("ext"), data.get("io"), val)
            else:
                coordinator.update_marker(data.get("id"), val)
        except Exception as e:
            _LOGGER.error("Webhook Error: %s", e)

    try:
        webhook.async_register(hass, DOMAIN, f"Comexio {server_id}", webhook_id, handle_webhook)
    except ValueError: pass

    hass.data[DOMAIN][entry.entry_id + "_webhook"] = webhook_id
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload and cleanup."""
    webhook_id = hass.data[DOMAIN].get(entry.entry_id + "_webhook")
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "switch", "number", "button"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(entry.entry_id + "_webhook", None)
    return unload_ok
