# Version: 0.8.1
import contextlib
from datetime import timedelta
import logging

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from .api import ComexioAPI
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_HOST,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import ComexioCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch", "number", "button", "binary_sensor", "select", "update"]


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

    # Restore the last checked extension-firmware snapshot before entities are created,
    # so update.* entities (update.py) don't start at Unknown after every restart.
    await coordinator.async_load_extension_firmware()

    # ---------------------------
    # Explicit Device Registration
    # ---------------------------
    # Registers the device early to prevent frontend crashes during the first setup.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, server_id)},
        name=server_id,
        manufacturer="Comexio",
        model="IO-Server",
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Extension firmware check: nightly window, version-gated (see
    # coordinator.async_start_firmware_update_check). Cancelled automatically on unload/reload.
    entry.async_on_unload(coordinator.async_start_firmware_update_check())

    # Set up services
    await async_setup_services(hass)

    # Auto-fix statistics unit mismatches (one-time migration: empty → correct unit)
    hass.async_create_task(_async_fix_statistics_units(hass, server_id))

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

    include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
    for io in coordinator.data.get("io", []):
        if not io.get("offline") or include_offline:
            active_unique_ids.add(f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower())

    # Add buttons (these are always active)
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_start_btn")
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_cancel_btn")
    active_unique_ids.add(f"comexio_{server_id}_webio_sync_status_sensor")
    active_unique_ids.add(f"comexio_{server_id}_entity_id_fix_btn")
    active_unique_ids.add(f"comexio_{server_id}_fw_check_btn")
    active_unique_ids.add(f"comexio_{server_id}_statistics_cleanup_btn")
    active_unique_ids.add(f"comexio_{server_id}_offline_extensions_sensor")
    active_unique_ids.add(f"comexio_{server_id}_logikplan_plan_selector")

    # Extension firmware updates (update.py): one entity per known extension + BASE. Built the
    # same way as the IO entities above, since extension existence is independent of the
    # firmware check ever having run — entities just stay "unknown" until the first check.
    active_unique_ids.add(f"comexio_{server_id}_base_firmware_update")
    for ext_name in {io["ext_name"] for io in coordinator.data.get("io", [])}:
        active_unique_ids.add(f"comexio_{server_id}_{ext_name}_firmware_update".lower())

    # Build expected-platform map so that entities which migrated to a different
    # HA domain (e.g. sensor → binary_sensor after firmware upgrade) get removed.
    expected_platform: dict[str, str] = {}
    for m in coordinator.data.get("markers", []):
        uid = f"comexio_{server_id}_m{m['id']}".lower()
        expected_platform[uid] = "switch" if m["type"] == "digital" else "number"
    for io in coordinator.data.get("io", []):
        if not io.get("offline") or include_offline:
            uid = f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
            is_binary = io.get("is_binary", False)
            is_input = io.get("is_input", True)
            if is_binary:
                expected_platform[uid] = "binary_sensor" if is_input else "switch"
            else:
                expected_platform[uid] = "sensor" if is_input else "number"

    # Before removing offline IO entities from the registry, snapshot their entity_ids.
    # async_detect_orphaned_statistics uses this set to exclude "temporarily orphaned"
    # statistics (extension offline but still known) from the repair issue.
    offline_unique_ids = {
        f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
        for io in coordinator.data.get("io", [])
        if io.get("offline") and not include_offline
    }
    coordinator.offline_entity_statistic_ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id in offline_unique_ids
    }

    # Delete entities that are no longer active, or that migrated to a different HA domain.
    _LOGGER.debug("Comexio Cleanup: protecting %d active unique IDs", len(active_unique_ids))
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = entity_entry.unique_id
        if uid not in active_unique_ids:
            _LOGGER.debug("Cleaning up orphaned entity: %s (Unique ID: %s)", entity_entry.entity_id, uid)
            ent_reg.async_remove(entity_entry.entity_id)
        elif uid in expected_platform and entity_entry.domain != expected_platform[uid]:
            _LOGGER.debug(
                "Removing stale-platform entity: %s (was %s, now %s)",
                entity_entry.entity_id,
                entity_entry.domain,
                expected_platform[uid],
            )
            ent_reg.async_remove(entity_entry.entity_id)

    # Remove sub-devices that have no entities left (e.g. offline extension modules).
    # Skipped when include_offline_extensions is enabled — devices stay visible as unavailable.
    if not include_offline:
        dev_reg = dr.async_get(hass)
        hub_identifier = (DOMAIN, server_id)
        # Pre-index device_ids that still have entities to avoid a registry scan per device.
        devices_with_entities = {
            e.device_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id) if e.device_id
        }
        for dev_entry in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            if hub_identifier in dev_entry.identifiers:
                continue
            if dev_entry.id not in devices_with_entities:
                _LOGGER.info("Removing empty sub-device: %s", dev_entry.name)
                dev_reg.async_remove_device(dev_entry.id)

    # Re-run orphaned-statistics check after entity cleanup so that entities removed
    # above (e.g. offline extension IOs) are immediately detected as orphaned statistics,
    # without waiting for the next scheduled poll.
    await coordinator.async_check_orphaned_statistics(conf)

    return True


def _delete_stale_statistics_issues(hass: HomeAssistant, issue_reg, server_slug: str) -> int:
    """Remove recorder repair issues for this server's statistics."""
    deleted = 0
    for domain, issue_id in issue_reg.issues:
        if domain == "recorder" and server_slug in issue_id.lower():
            ir.async_delete_issue(hass, domain, issue_id)
            deleted += 1
    return deleted


async def _async_fix_statistics_units(hass: HomeAssistant, server_id: str) -> None:
    """Fix statistics unit mismatches from firmware upgrade (empty label → correct unit).

    Values stored in the recorder are already in the correct unit — only the
    statistics_meta.unit_of_measurement label was absent before Comexio OS 11.0.2
    introduced $IOTypesBinary. async_change_statistics_unit is wrong here because it
    attempts a numeric conversion (fails for "" → "V"). We use
    async_update_statistics_metadata (label-only update, no value conversion), wait
    for the recorder to commit, then explicitly delete stale repair issues that
    validate_statistics may have already created.
    """
    import asyncio

    await asyncio.sleep(15)

    if "recorder" not in hass.config.components:
        return
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_update_statistics_metadata,
            list_statistic_ids,
        )
    except ImportError:
        _LOGGER.debug("[%s] Recorder API unavailable; skipping statistics unit fix", server_id)
        return

    try:
        instance = get_instance(hass)
        all_stats = await instance.async_add_executor_job(list_statistic_ids, hass)
    except Exception:
        _LOGGER.debug("[%s] Statistics unit fix skipped: recorder unavailable", server_id)
        return

    server_slug = server_id.lower()
    prefixes = (
        f"sensor.comexio_{server_slug}_",
        f"sensor.comexio_server_{server_slug}_",
    )

    mismatches: list[tuple[str, str]] = []
    for stat in all_stats:
        stat_id = stat["statistic_id"]
        if not any(stat_id.startswith(p) for p in prefixes):
            continue
        state = hass.states.get(stat_id)
        if state is None:
            _LOGGER.debug("[%s] Skipping %s: entity state not available yet", server_id, stat_id)
            continue
        current_unit = state.attributes.get("unit_of_measurement") or ""
        # HA 2024+ returns "statistics_unit_of_measurement"; older versions use "unit_of_measurement"
        stored_unit = stat.get("statistics_unit_of_measurement") or stat.get("unit_of_measurement") or ""
        if current_unit and not stored_unit:
            _LOGGER.debug("[%s] Unit mismatch: %s  stored=%r  entity=%r", server_id, stat_id, stored_unit, current_unit)
            mismatches.append((stat_id, current_unit))

    if not mismatches:
        _LOGGER.debug("[%s] Statistics unit check: no mismatches found", server_id)
        return

    # Schedule label-only metadata updates (no value conversion needed).
    # new_unit_class=None is always valid; the per-unit mapping would require
    # knowing which unit_class strings this HA version accepts, and HA raises
    # HomeAssistantError for unknown values — None is the safe universal fallback.
    for stat_id, new_unit in mismatches:
        async_update_statistics_metadata(
            hass,
            stat_id,
            new_unit_of_measurement=new_unit,
            new_unit_class=None,
        )

    # Give the recorder queue time to commit — async_block_till_done() can deadlock
    # when HA is still starting up (new recorder tasks keep arriving), so a short
    # sleep is the safe alternative.
    await asyncio.sleep(5)

    # Remove stale repair issues — validate_statistics creates them on every Statistics
    # page load whenever it detects a stored-vs-entity unit mismatch. Now that the DB
    # is corrected, future runs will find no mismatch and not recreate them.
    issue_reg = ir.async_get(hass)
    deleted = _delete_stale_statistics_issues(hass, issue_reg, server_slug)

    _LOGGER.info(
        "[%s] Fixed %d statistics unit mismatches; removed %d stale repair issues",
        server_id,
        len(mismatches),
        deleted,
    )


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
