# Version: 0.7.5
import contextlib
from datetime import timedelta
import logging

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .api import ComexioAPI
from .const import CONF_API_PASSWORD, CONF_API_USERNAME, CONF_HOST, CONF_PASSWORD, CONF_SERVER_ID, CONF_USERNAME, DOMAIN
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

    # Initialize Coordinator (passes config_entry to base class, sets api.config_entry and server_id)
    coordinator = ComexioCoordinator(hass, api, entry)

    # Set custom update interval from config
    conf = {**entry.data, **entry.options}
    interval = conf.get("scan_interval", 15)
    coordinator.update_interval = timedelta(minutes=interval)

    try:
        if not await api.login():
            _LOGGER.error("Comexio login rejected for %s — check credentials", entry.data[CONF_HOST])
            raise ConfigEntryAuthFailed
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Close the session on any setup failure to prevent leaks on retry (ConfigEntryNotReady)
        await api.close()
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # ---------------------------
    # Explicit Device Registration
    # ---------------------------
    # Registers the device early to prevent frontend crashes during the first setup.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, server_id)},
        name=f"Comexio {server_id}",
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
                ext = data.get("ext")
                io_id = data.get("io")
                if not ext or not io_id:
                    _LOGGER.warning("Webhook IO event missing ext/io: %s", data)
                    return
                coordinator.update_io_by_name(ext, io_id, val)
            else:
                marker_id = data.get("id")
                if marker_id is None:
                    _LOGGER.warning("Webhook marker event missing id: %s", data)
                    return
                coordinator.update_marker(marker_id, val)
        except Exception as e:
            _LOGGER.exception("Webhook Error: %s", e)

    with contextlib.suppress(ValueError):
        webhook.async_register(hass, DOMAIN, f"Comexio {server_id}", webhook_id, handle_webhook)

    entry.async_on_unload(entry.add_update_listener(update_listener))
    hass.data[DOMAIN][f"{entry.entry_id}_webhook"] = webhook_id

    # ---------------------------
    # Entity Cleanup
    # ---------------------------
    ent_reg = er.async_get(hass)

    # Get all IDs of objects currently recognized by the coordinator
    active_unique_ids = set()
    for m in coordinator.data.get("markers", []):
        active_unique_ids.add(f"comexio_{server_id}_m{m['id']}".lower())

    for io in coordinator.data.get("io", []):
        active_unique_ids.add(f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower())

    # Add buttons (these are always active)
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_start_btn")
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_cancel_btn")
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_status_sensor")

    # Delete anything from the registry that is not in active_unique_ids
    _LOGGER.debug("Comexio Cleanup: protecting %d active unique IDs", len(active_unique_ids))
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        # _LOGGER.debug(
        #     "Comexio Cleanup: checking entity %s (Unique ID: %s)",
        #     entity_entry.entity_id,
        #     entity_entry.unique_id
        # )
        if entity_entry.unique_id not in active_unique_ids:
            _LOGGER.info(
                "Cleaning up orphaned entity: %s (Unique ID: %s)", entity_entry.entity_id, entity_entry.unique_id
            )
            ent_reg.async_remove(entity_entry.entity_id)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    # R2: The sync button sets this flag before writing audit_ignored to options,
    # so we skip the listener-triggered reload and let the explicit reload in
    # async_handle_press be the single reload after a sync.
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and getattr(coordinator, "_skip_next_listener_reload", False):
        coordinator._skip_next_listener_reload = False
        _LOGGER.debug(
            "[%s] update_listener: skipping reload (explicit reload pending after sync)", coordinator.server_id
        )
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload and cleanup."""
    if webhook_id := hass.data[DOMAIN].get(f"{entry.entry_id}_webhook"):
        webhook.async_unregister(hass, webhook_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator and hasattr(coordinator, "api"):
            await coordinator.api.close()
        hass.data[DOMAIN].pop(f"{entry.entry_id}_webhook", None)

        # Remove global services when the last entry is unloaded
        if not hass.config_entries.async_entries(DOMAIN):
            hass.services.async_remove(DOMAIN, "generate_web_io")

    return unload_ok
