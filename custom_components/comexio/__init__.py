# Version: 0.6.0
from datetime import timedelta
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import webhook, persistent_notification
from homeassistant.helpers import entity_registry as er, device_registry as dr
import logging

from .const import (
    DOMAIN, CONF_HOST, CONF_USERNAME, CONF_PASSWORD, 
    CONF_SERVER_ID, CONF_API_USERNAME, CONF_API_PASSWORD
)
from .api import ComexioAPI
from .coordinator import ComexioCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch", "number", "button", "binary_sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Comexio from a config entry."""
    _LOGGER.info("Comexio__init__: Entered async_setup_entry")

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

    try:
        await api.login()
    except Exception as e:
        _LOGGER.error("Failed to log in to Comexio API: %s", e)
        return False

    # Initialize Coordinator
    coordinator = ComexioCoordinator(hass, api)
    coordinator.server_id = server_id
    coordinator.config_entry = entry
    api.config_entry = entry
    
    # Set custom update interval from config
    conf = {**entry.data, **entry.options}
    interval = conf.get("scan_interval", 15)
    coordinator.update_interval = timedelta(minutes=interval)
    
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as e:
        _LOGGER.error("Failed to refresh coordinator data: %s", e)
        return False

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # ---------------------------
    # Explicit Device Registration
    # ---------------------------
    # Registers the device early to prevent frontend crashes during the first setup.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, server_id)},
        name=f"Comexio Server {server_id}",
        manufacturer="Comexio",
        model="IO-Server",
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up services
    await async_setup_services(hass)

    # ---------------------------
    # Webhook Setup
    # ---------------------------
    webhook_id = f"comexio_{server_id}"
    async def handle_webhook(hass, webhook_id, request):
        try:
            try:
                data = await request.json()
            except Exception:
                _LOGGER.error("Received non-JSON payload on webhook %s", webhook_id)
                return
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

    entry.async_on_unload(entry.add_update_listener(update_listener))
    hass.data[DOMAIN][entry.entry_id + "_webhook"] = webhook_id

    # ---------------------------
    # Entity Cleanup
    # ---------------------------
    ent_reg = er.async_get(hass)
    
    # Get all IDs of objects currently recognized by the coordinator
    active_unique_ids = set()
    for m in coordinator.data.get("markers", []):
        # Match exactly what platforms like number.py or switch.py define
        if m.get("type") == "analog":
            active_unique_ids.add(f"comexio_{server_id}_m{m['id']}_num")
        else:
            active_unique_ids.add(f"comexio_{server_id}_m{m['id']}_sw")

    for io in coordinator.data.get("io", []):
        if not io.get("is_binary"):
            active_unique_ids.add(f"comexio_{server_id}_{io['id']}_io_sensor")
        elif io.get("identifier", "").startswith("Q"):
            active_unique_ids.add(f"comexio_{server_id}_{io['id']}_io_sw")
        else:
            active_unique_ids.add(f"comexio_{server_id}_{io['id']}_io_binary_sensor")
    
    # Add buttons (these are always active)
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_btn")
    active_unique_ids.add(f"comexio_{server_id}_cancel_sync_btn")
    active_unique_ids.add(f"comexio_{server_id}_sync_status")

    # Delete anything from the registry that is not in active_unique_ids
    _LOGGER.debug("Comexio Cleanup: protecting %d active unique IDs", len(active_unique_ids))
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        #_LOGGER.debug("Comexio Cleanup: checking entity %s (Unique ID: %s)", entity_entry.entity_id, entity_entry.unique_id)
        if entity_entry.unique_id not in active_unique_ids:
            _LOGGER.info("Cleaning up orphaned entity: %s (Unique ID: %s)", 
                         entity_entry.entity_id, entity_entry.unique_id)
            ent_reg.async_remove(entity_entry.entity_id)

    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload and cleanup."""
    webhook_id = hass.data[DOMAIN].get(entry.entry_id + "_webhook")
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(entry.entry_id + "_webhook", None)
    return unload_ok
