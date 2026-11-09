# Version: 0.8.1
from collections.abc import Callable, Iterable
from datetime import timedelta
import logging
from typing import Any

from aiocomexio import __version__ as AIOCOMEXIO_VERSION
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .api import ComexioAPI
from .certificate import (
    async_clear_certificate_changed,
    async_create_api,
    async_handle_certificate_action,
    async_remove_certificate_pushes,
    async_replaced_fingerprint,
    async_report_certificate_changed,
)
from .const import (
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_HOST,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_SERVER_ID,
    CONFIG_ENTRY_MINOR_VERSION,
    DOMAIN,
    ISSUE_KNX_PRERELEASE_CLEANUP,
    LOGIN_ERROR_CONNECTION,
    MOBILE_APP_NOTIFICATION_ACTION_EVENT,
    ONEWIRE_DATA_KEY,
    QUICKBUS_IO_DATA_KEY,
    SETUP_UNREACHABLE_ENTRIES,
    SOURCE_CATEGORIES,
    STATISTICS_UNIT_FIX_ATTEMPTS,
    STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC,
    STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC,
    STATISTICS_UNIT_FIX_RETRY_DELAY_SEC,
    STATISTICS_UNIT_FIX_START_DELAY_SEC,
    STATISTICS_UNITS_CHANGED_ISSUE_DOMAIN,
    STATISTICS_UNITS_CHANGED_ISSUE_PREFIX,
    MarkerKind,
    WebioClass,
    extension_firmware_update_unique_id,
    function_plan_ids,
    function_plan_run_state_unique_id,
    io_unique_id,
    known_extension_names,
    migrate_entry_options,
    offline_io_skipped,
    onewire_platform,
    onewire_problem_unique_id,
    onewire_unique_id,
    onewire_unique_id_prefix,
    quickbus_io_platform,
    quickbus_io_unique_id,
    stable_object_id,
    webio_range_check_entity_id,
)
from .coordinator import (
    ComexioCoordinator,
    async_remove_legacy_plan_preview,
    plan_preview_store,
    saved_config_store,
)
from .entity import track_quickbus_devices
from .orphaned_statistics import find_unit_mismatches, legacy_statistic_prefixes
from .reference_monitor import async_integration_version
from .services import async_setup_services
from .unsupported_ios import io_has_entity, io_platform, unsupported_io_issue_id
from .webhook_security import WebhookSecurity, legacy_webhook_id, webhook_security_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    "sensor",
    "switch",
    "number",
    "button",
    "binary_sensor",
    "select",
    "image",
    "update",
    "cover",
    "light",
]


# A pinned entry connects over HTTPS only; the reconfigure flow is the way back to plain HTTP.
_HTTPS_RETRY_HINT = " (over HTTPS — if Comexio no longer serves HTTPS, reconfigure the entry to go back to HTTP)"

# unique_id suffixes (after "comexio_{server_id}_") of the always-present server-level entities, kept by
# the orphan cleanup in async_setup_entry — an entity missing here is deleted from the registry on every setup.
SERVER_LEVEL_UNIQUE_ID_SUFFIXES = (
    "webio_sync_start_btn",
    "webio_sync_cancel_btn",
    "webio_sync_status_sensor",
    "entity_id_fix_btn",
    "fw_check_btn",
    "webio_range_check_btn",
    "statistics_cleanup_btn",
    "uninstall_cleanup_btn",
    "offline_extensions_sensor",
    "logikplan_plan_selector",
    "function_plan_backups_sensor",
    "orphaned_backups",
    "version_sensor",
    "plan_changed_sensor",
    "plan_backup_selector",
    "bus_load_sensor",
    "sd_card_sensor",
    "free_storage_sensor",
    "last_boot_sensor",
    "connection",
    "time_server_problem",
    "plan_preview_sensor",
    "plan_preview_image",
    "function_plan_toggle_btn",
    "managed_plans_problem",
    "function_plan_auto_start",
    "function_plan_auto_start_user",
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # NOSONAR
    """Listen for the certificate push's "Accept certificate" button once, for every entry.

    Not per entry: an entry that met another certificate at setup waits for the repair without being loaded,
    and its push button must work all the same.
    """

    async def _certificate_action(event: Event) -> None:
        await async_handle_certificate_action(hass, event)

    hass.bus.async_listen(MOBILE_APP_NOTIFICATION_ACTION_EVENT, _certificate_action)
    return True


_WEBHOOK_KIND_LABELS = {WebioClass.QUICKBUS: "Quickbus", WebioClass.KNX: "KNX", WebioClass.ONEWIRE: "1-Wire"}


def _dispatch_push(coordinator: ComexioCoordinator, security: WebhookSecurity, data: dict[str, Any]) -> None:
    """Hand the value of one webhook payload to the coordinator, by the kind of item it names.

    A malformed push is logged throttled and without its payload: the old id is guessable while it is on, and
    the sender is anybody who can reach the endpoint.
    """
    val = data.get("value")
    webhook_type = data.get("type")
    server_id = coordinator.server_id
    if webhook_type == WebioClass.IO:
        ext = data.get("ext")
        io_id = data.get("io")
        if not ext or not io_id:
            security.warn_refused("missing-io", "[%s] Webhook IO event missing ext/io", server_id)
            return
        coordinator.update_io_by_name(ext, io_id, val)
        return
    item_id = data.get("id")
    if item_id is None:
        label = _WEBHOOK_KIND_LABELS.get(str(webhook_type), "marker")
        security.warn_refused(f"missing-id-{label}", "[%s] Webhook %s event missing id", server_id, label)
    elif webhook_type == WebioClass.QUICKBUS:
        coordinator.update_quickbus_io(item_id, val)
    elif webhook_type == WebioClass.ONEWIRE:
        # The id of a 1-Wire push is the sensor's address (OT1), not its config id (aiocomexio.webio).
        coordinator.update_onewire(str(item_id), val)
    elif webhook_type == WebioClass.KNX:
        # Blind guess: KNX webhook payload shape assumed identical to markers
        # (no real KNX hardware/sample payload available — see project_knx_objects memory).
        coordinator.update_knx(item_id, val)
    else:
        coordinator.update_marker(item_id, val)


def _unregister_webhooks(hass: HomeAssistant, registered: set[str]) -> None:
    """Unregister every id in registered and empty it, so a second call (unload, then its callback) is a no-op."""
    for webhook_id in registered:
        webhook.async_unregister(hass, webhook_id)
    registered.clear()


def _restore_webhooks(coordinator: ComexioCoordinator) -> None:
    """Register the ids again after a failed unload; a failure here is logged, the unload's own error stays."""
    try:
        coordinator.sync_webhooks()
    except Exception:
        _LOGGER.exception(
            "[%s] The webhook ids could not be registered again after the failed unload", coordinator.server_id
        )


def _webhook_handler(coordinator: ComexioCoordinator, security: WebhookSecurity):
    """The handler every webhook id of the entry is registered with."""
    server_id = coordinator.server_id

    async def handle_webhook(hass, webhook_id, request):
        try:
            try:
                data = await request.json()
            except Exception:
                security.warn_refused("non-json", "[%s] Received non-JSON payload on a Comexio webhook", server_id)
                return
            if not isinstance(data, dict):
                security.warn_refused(
                    "not-object", "[%s] Webhook request refused: its payload is not a JSON object", server_id
                )
                return
            if not security.accepts(webhook_id, data):
                security.warn_refused(
                    "id-mismatch", "[%s] Webhook request refused: its id does not belong to its payload", server_id
                )
                return
            _dispatch_push(coordinator, security, data)
        except Exception as e:
            _LOGGER.exception("Webhook Error: %s", e)

    return handle_webhook


def _register_webhooks(
    hass: HomeAssistant, handler, server_id: str, registered: set[str], refused: set[str], wanted: set[str]
) -> None:
    """Bring the registered ids to wanted; an id another registration holds is skipped, never taken over.

    refused remembers the skipped ids, so the warning comes once per id and not with every coordinator update.
    """
    for webhook_id in registered - wanted:
        webhook.async_unregister(hass, webhook_id)
    registered.difference_update(registered - wanted)
    refused.intersection_update(wanted)
    for webhook_id in wanted - registered:
        try:
            webhook.async_register(hass, DOMAIN, f"Comexio {server_id}", webhook_id, handler)
        except ValueError:
            if webhook_id not in refused:
                # No part of the id in the log: it is the secret of its command.
                _LOGGER.warning("[%s] A webhook id is already registered by another registration, skipped", server_id)
                refused.add(webhook_id)
            continue
        refused.discard(webhook_id)
        registered.add(webhook_id)


def _setup_webhooks(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: ComexioCoordinator, security: WebhookSecurity
) -> None:
    """Register the webhook of every Web-IO command (dg), and keep the registrations in step with the items."""
    server_id = coordinator.server_id
    handler = _webhook_handler(coordinator, security)
    registered: set[str] = set()
    refused: set[str] = set()
    hass.data[DOMAIN][f"{entry.entry_id}_webhooks"] = registered

    @callback
    def sync_webhooks() -> None:
        """Register the webhook ids the commands use now, drop the ones no command can use any more."""
        if not coordinator.data:
            # No data yet: only the ids of the saved 1-Wire sensors' commands (webhook_command_ids), whose readings
            # the coordinator holds for the first refresh's merge. Not the old id: nothing could take its pushes
            # before the first data (no IO index, an IO push would warn of an unknown IO). So an install whose
            # commands still push to the old id (legacy_enabled, not synced yet) is heard from the first data on,
            # as before. The registered ids stay as they are (`refused` ids keep their one-time warning).
            keep = registered | refused
            _register_webhooks(hass, handler, server_id, registered, refused, keep | coordinator.webhook_command_ids())
            return
        wanted = coordinator.webhook_command_ids()
        if security.legacy_enabled:
            wanted.add(legacy_webhook_id(server_id))
        _register_webhooks(hass, handler, server_id, registered, refused, wanted)

    coordinator.sync_webhooks = sync_webhooks
    sync_webhooks()
    entry.async_on_unload(coordinator.async_add_listener(sync_webhooks))
    # HA runs these callbacks also when the setup fails after this point (a retry would otherwise meet the ids of
    # the failed attempt, registered with its stale handler).
    entry.async_on_unload(lambda: _unregister_webhooks(hass, registered))
    # A sync ends with a reload, which builds a new WebhookSecurity from the store: write what a delayed save still
    # holds (a lost last_write could switch the old id off before Comexio has restarted).
    entry.async_on_unload(security.async_save)


async def _async_load_webhook_security(
    hass: HomeAssistant, entry: ConfigEntry, api: ComexioAPI, server_id: str
) -> WebhookSecurity:
    """The webhook ledger of the entry; a store that refuses closes the session first (a retry would leak it)."""
    security = WebhookSecurity(hass, entry.entry_id, server_id, str(entry.data.get(CONF_HOST, "")))
    try:
        await security.async_load()
    except Exception:
        api.close()
        raise
    return security


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Comexio from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    server_id = entry.data[CONF_SERVER_ID]
    # Versions first, so every shared debug log tells what it was recorded with (#196).
    _LOGGER.info(
        "Comexio__init__: Entered async_setup_entry for %s (integration %s, aiocomexio %s, Home Assistant %s)",
        server_id,
        await async_integration_version(hass) or "?",
        AIOCOMEXIO_VERSION,
        HA_VERSION,
    )
    # Before anything that can fail: the copy older releases left under /local/ is public (dh).
    await async_remove_legacy_plan_preview(hass, server_id)

    # Initialize API (an entry still on plain HTTP is switched to HTTPS here when Comexio serves it)
    try:
        api = await async_create_api(hass, entry)
    except (ConfigEntryNotReady, ConfigEntryAuthFailed):
        # Raised only once Comexio answered the HTTPS login (see async_create_api).
        _track_setup_outage(hass, entry.entry_id, False)
        raise

    # Every Web-IO command gets a webhook id of its own (dg); the builders need the secret before the first poll.
    security = await _async_load_webhook_security(hass, entry, api, server_id)
    api.webhook_security = security

    # Initialize Coordinator (passes config_entry to base class, sets api.config_entry and server_id)
    coordinator = ComexioCoordinator(hass, api, entry)

    # Set custom update interval from config
    conf = {**entry.data, **entry.options}
    interval = conf.get("scan_interval", 15)
    coordinator.update_interval = timedelta(minutes=interval)

    # Seed the KNX DPT catalog from disk before the first refresh below — that first poll is
    # exactly the case api.seed_knx_dpt_catalog exists to protect: a freshly created api
    # instance has nothing cached yet, so if its very first live catalog fetch fails, this
    # restores last known-good data instead of leaving DPT3.x composite pairs untagged.
    await coordinator.async_load_knx_dpt_catalog()

    if entry.entry_id in _setup_unreachable_entries(hass):
        # The last attempt could not reach Comexio, which may still be booting: the first refresh starts the grace.
        coordinator.note_comexio_unreachable()

    # ---------------------------
    # Webhook Setup
    # ---------------------------
    # Before the login and the first refresh: a 1-Wire reading only comes by push, so one sent meanwhile would be
    # lost until the sensor changes again. Nothing is registered here yet (no data); _async_connect registers the
    # ids of the saved 1-Wire sensors, the first data those of every command (sync_webhooks).
    _setup_webhooks(hass, entry, coordinator, security)

    try:
        from_saved_config = await _async_connect(hass, entry, api, coordinator)
    except Exception:
        # Close the session on any setup failure to prevent leaks on retry (ConfigEntryNotReady)
        api.close()
        # HA unregisters the ids (the unload callbacks of _setup_webhooks); their set must not outlive the attempt.
        hass.data[DOMAIN].pop(f"{entry.entry_id}_webhooks", None)
        # A rejected login proves Comexio answers, even with the flag carried over from the last attempt.
        _track_setup_outage(
            hass,
            entry.entry_id,
            api.last_login_error == LOGIN_ERROR_CONNECTION
            or (coordinator.comexio_unreachable and api.last_login_error is None),
        )
        raise
    _track_setup_outage(hass, entry.entry_id, False)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # The ids of every command, right after the first refresh told them (a saved config standing in for it tells
    # no listener) and before the awaited rest of the setup (stores, extension renames, platforms), so a push sent
    # meanwhile finds its webhook. The handlers write into coordinator.data, which the entities read once added; a
    # restored last state yields to a reading pushed since (restore_onewire_value).
    coordinator.sync_webhooks()

    # Restore the last checked extension-firmware snapshot before entities are created,
    # so update.* entities (update.py) don't start at Unknown after every restart.
    await coordinator.async_load_extension_firmware()

    # Restore the last known extension serial->name registry, then migrate any entity/device
    # whose extension was renamed in Comexio since the last setup. Must run before the entity
    # cleanup below, which would otherwise treat the old (pre-rename) unique_ids as orphaned
    # and delete them instead of letting them be renamed in place.
    # Not from the saved config: it tells nothing new about the extensions, the next full setup checks.
    await coordinator.async_load_extension_registry()
    renamed_extensions = [] if from_saved_config else await coordinator.async_detect_and_migrate_extension_renames()
    for renamed in renamed_extensions:
        _LOGGER.info(
            "[%s] Comexio extension renamed: '%s' -> '%s' — migrated HA entities/device in place",
            server_id,
            renamed["old_name"],
            renamed["new_name"],
        )

    # Restore the Bus-Load-Watchdog's persisted event history before its poll starts (below),
    # so an event written early after restart can't race the load and be lost.
    await coordinator.async_load_watchdog_history()
    # Before the platforms, so the preview image and its info sensor show the last preview right away.
    await coordinator.async_load_plan_preview()

    # One-time migration: the Web-IO range-check button's entity_id used to be derived from
    # its translated name at first registration, which could freeze in a nonsense object_id
    # (e.g. "button.system_iosrv1_2") if the translation file was momentarily out of sync at
    # setup time. The button now pins its own entity_id in code (button.py), but that only
    # applies to entities registered from now on — this renames an already-registered one in
    # place. Must run before the platform setup below, so the rename (and its state migration)
    # happens before button.py's entity attaches to its registry entry, not after.
    _migrate_webio_range_check_entity_id(hass, server_id)

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
    entry.async_on_unload(track_quickbus_devices(coordinator))
    if not from_saved_config:
        _start_comexio_checks(hass, entry, coordinator)

    # Set up services
    await async_setup_services(hass)

    # Auto-fix statistics unit mismatches (one-time migration: empty → correct unit)
    # A background task: its waits for the recorder (STATISTICS_UNIT_FIX_*) must not hold up
    # hass.async_block_till_done() (startup, tests); unload cancels it.
    entry.async_create_background_task(
        hass, _async_fix_statistics_units(hass, server_id, entry.entry_id), f"comexio_{server_id}_fix_statistics_units"
    )

    # One-shot check after an update from a KNX pre-release (flag set by async_migrate_entry).
    # Deliberately BEFORE the update listener is registered: clearing the flag writes the
    # options, which must not trigger a reload of the entry that is still being set up.
    coordinator.check_knx_prerelease_cleanup()

    # R2: an internal options write of the setup so far (first refresh, the check above) ran no
    # listener, so its skip snapshot was never consumed — left pending, it would swallow the
    # reload of the first unrelated listener run whose options happen to match.
    coordinator.take_pending_reload_skip_options()
    entry.async_on_unload(entry.add_update_listener(update_listener))

    if from_saved_config:
        # Nothing new is known about Comexio: no entity counts as gone. The full setup after the pinned
        # certificate answers again (accepted, or back) catches the cleanup up.
        return True

    # ---------------------------
    # Entity Cleanup
    # ---------------------------
    ent_reg = er.async_get(hass)

    # Get all IDs of objects currently recognized by the coordinator. Markers and KNX objects
    # (registry-driven via SOURCE_CATEGORIES.id_keyed_entities) share the "M{id}"/"K{id}" unique_id
    # shape; IO, Quickbus IOs and 1-Wire sensors use their own shapes and are handled separately below.
    active_unique_ids = set()
    # Bridge Markers (kind == KNX_BRIDGE, see MARKER_KNX_BRIDGE_SUFFIX_RE) get no HA entity on
    # any platform — their uid is excluded from active_unique_ids below so the cleanup loop
    # removes a stale entity if an existing, previously-normal marker gets manually renamed to
    # append the bridge suffix. Tracked separately so that removal gets a specific, non-DEBUG
    # log line instead of the generic "orphaned entity" one — an admin renaming a marker into a
    # bridge is a deliberate, non-obvious cause for an entity to disappear.
    bridge_reclassified_uids: set[str] = set()
    # DPT3.x composite members (cover.py/light.py, see aiocomexio parse_config's DPT3 pairing) no
    # longer get their own switch/number entity on this platform — switch.py/number.py skip
    # any item with knx_composite set. Their old per-K-element uid must NOT stay in
    # active_unique_ids, or the stale pre-upgrade switch/number registry entry never gets
    # cleaned up (it neither fails the "not in active_unique_ids" orphan check nor the
    # platform-mismatch check below, since nothing recomputes its expected platform either)
    # and lingers forever as a permanently-unavailable zombie duplicate of the new composite
    # entity. Tracked separately for the same reason as bridge_reclassified_uids: a specific
    # log line beats the generic "orphaned entity" one for a deliberate reclassification.
    composite_reclassified_uids: set[str] = set()
    for category in (cat for cat in SOURCE_CATEGORIES.values() if cat.id_keyed_entities):
        for src in coordinator.data.get(category.data_key, []):
            uid = f"comexio_{server_id}_{category.unique_id_infix}{src['id']}".lower()
            if src.get("kind") == MarkerKind.KNX_BRIDGE:
                bridge_reclassified_uids.add(uid)
                continue
            if src.get("knx_composite") is not None:
                composite_reclassified_uids.add(uid)
                continue
            active_unique_ids.add(uid)

    # DPT3.x composite cover/light entities (cover.py/light.py, see
    # aiocomexio parse_config's DPT3 pairing) use their own two-K-element unique_id shape
    # (ComexioKnxDpt3Entity.__init__) — distinct from the per-K-element uid excluded above,
    # which both halves of a composite keep unused: they still exist as $FubModules["11"]
    # entries, just without their own switch/number entity on this platform.
    knx_infix = SOURCE_CATEGORIES[WebioClass.KNX].unique_id_infix
    for knx_item in coordinator.data.get("knx", []):
        composite = knx_item.get("knx_composite")
        if composite and composite["role"] == "direction":
            active_unique_ids.add(
                f"comexio_{server_id}_{knx_infix}{knx_item['id']}_{knx_infix}{composite['partner_id']}".lower()
            )

    include_offline = conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False)
    for io in coordinator.data.get("io", []):
        # An IO of an unsupported type has no entity: an earlier one (a writable number) goes (#196).
        if io_has_entity(io, include_offline):
            active_unique_ids.add(io_unique_id(server_id, io))
    quickbus_platforms, quickbus_offline_uids = _quickbus_entity_uids(server_id, coordinator.data, include_offline)
    active_unique_ids |= quickbus_platforms.keys()
    onewire_platforms = _onewire_entity_uids(
        server_id,
        coordinator.data,
        coordinator.api.onewire_known,
        er.async_entries_for_config_entry(ent_reg, entry.entry_id),
    )
    active_unique_ids |= onewire_platforms.keys()

    # Server-level buttons and sensors (these are always active)
    active_unique_ids.update(f"comexio_{server_id}_{suffix}" for suffix in SERVER_LEVEL_UNIQUE_ID_SUFFIXES)

    # Function plan run-state sensors (sensor.py), one per plan in $Fubs. Without a
    # scraped $Fubs the existing ones are kept; the platform removes those of plans that disappear
    # while running, the ones of plans deleted while HA was down go with the next scraped setup.
    # Like the platform, keep the plans in the cache too: a plan HA created after the scrape
    # (create_fup via a service while the setup awaits) is in fub_data, not in scraped_plan_ids.
    if coordinator.scraped_plan_ids is not None:
        plan_uids = {
            function_plan_run_state_unique_id(server_id, fid)
            for fid in coordinator.scraped_plan_ids | function_plan_ids(coordinator.api.fub_data)
        }
    else:
        plan_uids = {
            e.unique_id
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
            if e.unique_id.startswith(function_plan_run_state_unique_id(server_id, ""))
        }
    active_unique_ids |= plan_uids

    # Bus-Load-Watchdog diagnostic sensors (always active, independent of extension/marker state)
    active_unique_ids.update(
        {
            f"comexio_{server_id}_extension_count_sensor",
            f"comexio_{server_id}_active_marker_count_sensor",
            f"comexio_{server_id}_function_plan_count_sensor",
            f"comexio_{server_id}_webio_command_count_sensor",
            f"comexio_{server_id}_watchdog_event_sensor",
        }
    )

    # Extension firmware updates (update.py): one entity per known extension + BASE. Built the
    # same way as the IO entities above, since extension existence is independent of the
    # firmware check ever having run — entities just stay "unknown" until the first check.
    active_unique_ids.add(f"comexio_{server_id}_base_firmware_update")
    for ext_name in known_extension_names(coordinator.data, include_offline):
        active_unique_ids.add(extension_firmware_update_unique_id(server_id, ext_name))

    # Build expected-platform map so that entities which migrated to a different
    # HA domain (e.g. sensor → binary_sensor after firmware upgrade) get removed.
    # Markers and KNX objects share the same kind/type → platform rules.
    expected_platform: dict[str, str] = {}
    for category in (cat for cat in SOURCE_CATEGORIES.values() if cat.id_keyed_entities):
        for src in coordinator.data.get(category.data_key, []):
            kind = src.get("kind")
            if kind == MarkerKind.KNX_BRIDGE:
                # No entity on any platform — already excluded from active_unique_ids above,
                # so it is cleaned up as orphaned rather than via a platform-mismatch here.
                continue
            uid = f"comexio_{server_id}_{category.unique_id_infix}{src['id']}".lower()
            if kind == MarkerKind.READ_ONLY:
                expected_platform[uid] = "binary_sensor" if src["type"] == "digital" else "sensor"
            elif kind == MarkerKind.TRIGGER:
                expected_platform[uid] = "button"
            else:
                expected_platform[uid] = "switch" if src["type"] == "digital" else "number"
    for io in coordinator.data.get("io", []):
        if io_has_entity(io, include_offline) and (platform := io_platform(io)):
            expected_platform[io_unique_id(server_id, io)] = platform
    expected_platform |= quickbus_platforms | onewire_platforms
    # The run-state sensors were binary sensors before they became enum sensors.
    expected_platform |= dict.fromkeys(plan_uids, "sensor")

    # Before removing offline IO entities from the registry, snapshot their entity_ids.
    # async_detect_orphaned_statistics uses this set to exclude "temporarily orphaned"
    # statistics (extension offline but still known) from the repair issue. Deleted registry
    # entries count too: after the first restart with the extension offline, its entities are
    # already gone from the live registry, but their statistics must stay protected.
    offline_unique_ids = {
        io_unique_id(server_id, io) for io in coordinator.data.get("io", []) if offline_io_skipped(io, include_offline)
    } | quickbus_offline_uids
    coordinator.offline_entity_statistic_ids = (
        {
            e.entity_id
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
            if e.unique_id in offline_unique_ids
        }
        | {
            deleted.entity_id
            for deleted in ent_reg.deleted_entities.values()
            if deleted.platform == DOMAIN and deleted.unique_id in offline_unique_ids
        }
        | {
            # Registry-independent: the stable entity_id follows from the unique_id alone, so the
            # protection survives HA purging the deleted registry entries.
            f"sensor.{stable_object_id(uid)}"
            for uid in offline_unique_ids
        }
    )

    # Delete entities that are no longer active, or that migrated to a different HA domain.
    _LOGGER.debug("Comexio Cleanup: protecting %d active unique IDs", len(active_unique_ids))
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = entity_entry.unique_id
        if uid not in active_unique_ids:
            if uid in bridge_reclassified_uids:
                _LOGGER.info(
                    "Removing entity %s: its Marker was renamed into a KNX write-path bridge "
                    "('[K<id>]' title suffix) and no longer gets an HA entity (Unique ID: %s)",
                    entity_entry.entity_id,
                    uid,
                )
            else:
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


async def _async_connect(
    hass: HomeAssistant, entry: ConfigEntry, api: ComexioAPI, coordinator: ComexioCoordinator
) -> bool:
    """Log in and run the first refresh; True when the entry starts read only from its saved config instead (#250).

    Another certificate than the pinned one gets no login, or fails the first poll after one: the setup goes on
    from the config the last scraped poll saved, so the entities stay and webhook pushes keep arriving. Without
    one it waits for the repair.
    """
    # The saved 1-Wire sensors first: a first poll that reads none keeps the last known ones (those, not none), and
    # the webhook ids of their commands are registered with them. A reading pushed during the login or the first
    # refresh then waits in the coordinator for that refresh's merge (update_onewire) instead of being refused.
    await coordinator.async_seed_saved_onewire_sensors()
    coordinator.sync_webhooks()
    if not await api.login():
        if (actual := await async_replaced_fingerprint(api)) is not None:
            # Never accepted on its own: the repair shows both fingerprints and asks.
            async_report_certificate_changed(hass, entry, actual)
            if await coordinator.async_start_from_saved_config():
                return True
            raise ConfigEntryNotReady(
                f"Comexio at {entry.data[CONF_HOST]} presents another certificate than the pinned one — see Repairs"
            )
        if api.last_login_error == LOGIN_ERROR_CONNECTION:
            hint = _HTTPS_RETRY_HINT if CONF_CERTIFICATE_FINGERPRINT in entry.data else ""
            _LOGGER.warning(
                "Comexio login failed due to a connection problem for %s — will retry%s",
                entry.data[CONF_HOST],
                hint,
            )
            raise ConfigEntryNotReady(f"Cannot connect to Comexio at {entry.data[CONF_HOST]}{hint}")
        _LOGGER.error("Comexio login rejected for %s — check credentials", entry.data[CONF_HOST])
        raise ConfigEntryAuthFailed
    async_clear_certificate_changed(hass, entry.entry_id)
    return await _async_first_refresh(entry, coordinator)


async def _async_first_refresh(entry: ConfigEntry, coordinator: ComexioCoordinator) -> bool:
    """Run the first refresh after a login; True when it met another certificate and the saved config stands in."""
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # The login got through with the pinned certificate, a request of the first poll met another one.
        if not coordinator.last_poll_met_another_certificate:
            raise
        _LOGGER.debug("Comexio first poll for %s failed: %s", entry.data[CONF_HOST], coordinator.last_exception)
        if await coordinator.async_start_from_saved_config():
            return True
        raise
    return False


def _start_comexio_checks(hass: HomeAssistant, entry: ConfigEntry, coordinator: ComexioCoordinator) -> None:
    """Start the timers and tasks that read from or write to Comexio beside the full poll."""
    server_id = coordinator.server_id
    # Extension firmware check: nightly window, version-gated (see
    # coordinator.async_start_firmware_update_check). Cancelled automatically on unload/reload.
    entry.async_on_unload(coordinator.async_start_firmware_update_check())

    # Web-IO analog Min/Max range check: nightly window (see
    # coordinator.async_start_webio_range_check). Cancelled automatically on unload/reload.
    entry.async_on_unload(coordinator.async_start_webio_range_check())
    entry.async_on_unload(coordinator.async_start_webhook_migration())

    # Bus workload monitoring: independent fast poll (see const.BUS_LOAD_POLL_INTERVAL_SEC),
    # cancelled automatically on unload/reload.
    entry.async_on_unload(coordinator.async_start_bus_load_poll())
    # Uptime: own 5-minute poll (see const.UPTIME_POLL_INTERVAL_SEC). Free storage and time server ride the config poll.
    entry.async_on_unload(coordinator.async_start_system_info_poll())
    # Function plan run states between the full polls (see const.FUNCTION_PLAN_RUN_STATE_*).
    entry.async_on_unload(coordinator.async_start_plan_run_state_poll())
    # After the platforms: the auto-start switches have restored the user's choice by now.
    entry.async_create_background_task(
        hass, coordinator.async_initial_plan_watch(), f"comexio_{server_id}_initial_plan_watch"
    )
    # "Start plan" button of the managed-plan watchdog's phone push (see plan_watchdog).
    entry.async_on_unload(
        hass.bus.async_listen(MOBILE_APP_NOTIFICATION_ACTION_EVENT, coordinator.async_handle_notification_action)
    )


def _quickbus_entity_uids(
    server_id: str, data: dict[str, Any], include_offline: bool
) -> tuple[dict[str, str], set[str]]:
    """(unique_id → platform of each active Quickbus IO entity, unique_ids of the offline ones).

    Quickbus IOs are read-only (binary_sensor/sensor, see quickbus_io_platform); an IO of a
    Quickbus device on an offline extension counts as offline like the extension's own IOs.
    """
    active: dict[str, str] = {}
    offline: set[str] = set()
    for io in data.get(QUICKBUS_IO_DATA_KEY, []):
        uid = quickbus_io_unique_id(server_id, io["id"])
        if io.get("offline") and not include_offline:
            offline.add(uid)
        else:
            active[uid] = quickbus_io_platform(io)
    return active, offline


def _onewire_entity_uids(
    server_id: str, data: dict[str, Any], onewire_known: bool, registry_entries: Iterable[er.RegistryEntry]
) -> dict[str, str]:
    """unique_id → platform of each 1-Wire entity to keep: the sensors of data and their problem sensor.

    While no config has told the 1-Wire sensors yet (api.onewire_known), the registered 1-Wire entities stay as they
    are: an unparsed group is no installation without sensors, and their history would go with the entities.
    """
    active = {
        onewire_unique_id(server_id, sensor["address"]): onewire_platform(sensor)
        for sensor in data.get(ONEWIRE_DATA_KEY, [])
    }
    problem_uid = onewire_problem_unique_id(server_id)
    if active:
        active[problem_uid] = "binary_sensor"
    if not onewire_known:
        prefix = onewire_unique_id_prefix(server_id)
        active |= {
            e.unique_id: e.domain
            for e in registry_entries
            if e.unique_id == problem_uid or e.unique_id.startswith(prefix)
        }
    return active


def _setup_unreachable_entries(hass: HomeAssistant) -> set[str]:
    """Entry ids whose last setup attempt could not reach Comexio (see SETUP_UNREACHABLE_ENTRIES)."""
    return hass.data.setdefault(SETUP_UNREACHABLE_ENTRIES, set())


def _track_setup_outage(hass: HomeAssistant, entry_id: str, unreachable: bool) -> None:
    """Remember for the next setup attempt whether this one could not reach Comexio."""
    if unreachable:
        _setup_unreachable_entries(hass).add(entry_id)
    else:
        _setup_unreachable_entries(hass).discard(entry_id)


def _migrate_webio_range_check_entity_id(hass: HomeAssistant, server_id: str) -> None:
    """One-time rename of the Web-IO range-check button to its pinned entity_id (see caller)."""
    ent_reg = er.async_get(hass)
    unique_id = f"comexio_{server_id}_webio_range_check_btn"
    old_entity_id = ent_reg.async_get_entity_id("button", DOMAIN, unique_id)
    target_entity_id = webio_range_check_entity_id(server_id)
    if not old_entity_id or old_entity_id == target_entity_id:
        return
    if ent_reg.async_get(target_entity_id) is not None:
        _LOGGER.warning(
            "[%s] Cannot migrate '%s' to '%s' — target entity_id already taken",
            server_id,
            old_entity_id,
            target_entity_id,
        )
        return
    try:
        ent_reg.async_update_entity(old_entity_id, new_entity_id=target_entity_id)
    except (ValueError, HomeAssistantError):
        _LOGGER.exception("[%s] Failed to migrate entity_id '%s' -> '%s'", server_id, old_entity_id, target_entity_id)
        return
    _LOGGER.info("[%s] Migrated entity_id '%s' -> '%s'", server_id, old_entity_id, target_entity_id)


def _delete_stale_statistics_issues(hass: HomeAssistant, statistic_ids: Iterable[str]) -> int:
    """Remove the unit-change repair issues of the fixed statistics (exact ids: iosrv1 never hits iosrv10)."""
    issue_reg = ir.async_get(hass)
    stale = [
        issue_id
        for issue_id in (f"{STATISTICS_UNITS_CHANGED_ISSUE_PREFIX}{stat_id}" for stat_id in statistic_ids)
        if issue_reg.async_get_issue(STATISTICS_UNITS_CHANGED_ISSUE_DOMAIN, issue_id)
    ]
    for issue_id in stale:
        ir.async_delete_issue(hass, STATISTICS_UNITS_CHANGED_ISSUE_DOMAIN, issue_id)
    return len(stale)


async def _async_fix_statistics_units(hass: HomeAssistant, server_id: str, entry_id: str) -> None:
    """Fix statistics unit labels: empty from a firmware upgrade, or Comexio's spelling (lux → lx).

    Values stored in the recorder are already in the correct unit — only the
    statistics_meta.unit_of_measurement label was absent before Comexio OS 11.0.2
    introduced $IOTypesBinary. async_change_statistics_unit is wrong here because it
    attempts a numeric conversion (fails for "" → "V"). We use
    async_update_statistics_metadata (label-only update, no value conversion), wait
    for the recorder to commit, then explicitly delete stale repair issues that
    validate_statistics may have already created.

    Runs up to STATISTICS_UNIT_FIX_ATTEMPTS times: a recorder that is not ready yet, or an
    enabled entity without its first state, would otherwise leave the label wrong until the
    next restart. A lookup that times out ends the retries: it still holds a recorder worker,
    and another lookup would only queue behind it. A later setup or reload waits for that
    same lookup instead of submitting a new one.
    """
    import asyncio

    await asyncio.sleep(STATISTICS_UNIT_FIX_START_DELAY_SEC)
    pending: str | None = None
    attempt = 0
    for attempt in range(1, STATISTICS_UNIT_FIX_ATTEMPTS + 1):
        try:
            pending = await _async_fix_statistics_units_once(hass, server_id, entry_id)
        except _StatisticsLookupStuck as err:
            pending = str(err)
            break
        if pending is None:
            return
        if attempt < STATISTICS_UNIT_FIX_ATTEMPTS:
            _LOGGER.debug(
                "[%s] Statistics unit fix incomplete (%s); next check in %d s",
                server_id,
                pending,
                STATISTICS_UNIT_FIX_RETRY_DELAY_SEC,
            )
            await asyncio.sleep(STATISTICS_UNIT_FIX_RETRY_DELAY_SEC)
    _LOGGER.warning(
        "[%s] Statistics unit fix incomplete after %d attempt(s) (%s); the next restart or reload tries again",
        server_id,
        attempt,
        pending,
    )


class _StatisticsLookupStuck(Exception):
    """The recorder did not answer the statistics lookup in time; its reason is the message."""


# The statistics lookup in flight, shared by every entry and reload: a lookup stuck in the
# recorder is awaited again instead of queueing another job behind it. A caller that joins it
# gets that lookup's snapshot; statistics registered later are seen by the next attempt.
_STATISTICS_LOOKUP_KEY = f"{DOMAIN}_statistics_lookup"


def _shared_statistics_lookup(hass: HomeAssistant, instance: Any, list_statistic_ids: Callable[..., Any]) -> Any:
    """Return the lookup still in flight, or submit a new one once the last has finished."""
    import asyncio

    lookup = hass.data.get(_STATISTICS_LOOKUP_KEY)
    if lookup is not None and not lookup.done():
        return lookup

    def _finished(done: Any) -> None:
        if hass.data.get(_STATISTICS_LOOKUP_KEY) is done:
            del hass.data[_STATISTICS_LOOKUP_KEY]
        # Retrieved here, so a lookup nobody awaits any more is logged, not "exception never retrieved".
        if not done.cancelled() and (err := done.exception()) is not None:
            _LOGGER.debug("Statistics unit check: shared statistics lookup failed: %r", err)

    lookup = asyncio.ensure_future(instance.async_add_executor_job(list_statistic_ids, hass))
    lookup.add_done_callback(_finished)
    hass.data[_STATISTICS_LOOKUP_KEY] = lookup
    return lookup


async def _async_list_statistics(hass: HomeAssistant, instance: Any, list_statistic_ids: Callable[..., Any]) -> Any:
    """Look up the recorder's statistics, raising _StatisticsLookupStuck when our time limit expires.

    Our time limit only stops waiting: the shared lookup keeps running, and the next caller
    waits for that same lookup. A TimeoutError from the database itself is not ours and
    propagates as an ordinary failure.
    """
    import asyncio

    lookup = _shared_statistics_lookup(hass, instance, list_statistic_ids)
    try:
        async with asyncio.timeout(STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC) as lookup_timeout:
            return await asyncio.shield(lookup)
    except TimeoutError as err:
        if not lookup_timeout.expired():
            raise
        reason = (
            f"statistics lookup timed out after {STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC} s, recorder busy?"
            " No further attempt while it still runs"
        )
        raise _StatisticsLookupStuck(reason) from err


async def _async_fix_statistics_units_once(hass: HomeAssistant, server_id: str, entry_id: str) -> str | None:
    """One pass of the statistics unit fix. Returns why it is incomplete, or None when nothing is left."""

    if "recorder" not in hass.config.components:
        return None
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_update_statistics_metadata,
            list_statistic_ids,
        )
    except ImportError:
        _LOGGER.debug("[%s] Recorder API unavailable; skipping statistics unit fix", server_id)
        return None

    try:
        all_stats = await _async_list_statistics(hass, get_instance(hass), list_statistic_ids)
    except _StatisticsLookupStuck:
        raise
    except Exception as err:  # any other recorder failure means "try again later"
        _LOGGER.debug("[%s] Statistics unit check: statistics lookup failed: %r", server_id, err)
        return f"statistics lookup failed: {err!r}"

    server_slug = slugify(server_id)
    # Ownership via the entity registry: the stable entity_ids (sensor.iosrv1_base_ai2) carry
    # no comexio_ prefix, so a prefix test alone would never select them.
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    # A disabled entity never gets a state, so waiting for it would never end.
    enabled_ids = {e.entity_id for e in entries if e.disabled_by is None}
    waiting: list[str] = []

    def _current_unit(stat_id: str) -> str | None:
        state = hass.states.get(stat_id)
        if state is None:
            _LOGGER.debug("[%s] Skipping %s: entity state not available yet", server_id, stat_id)
            if stat_id in enabled_ids:
                waiting.append(stat_id)
            return None
        return state.attributes.get("unit_of_measurement") or ""

    mismatches = find_unit_mismatches(
        all_stats,
        owned_entity_ids={e.entity_id for e in entries},
        legacy_prefixes=legacy_statistic_prefixes(server_slug),
        current_unit=_current_unit,
    )
    pending = f"no state yet: {', '.join(sorted(set(waiting)))}" if waiting else None
    for stat_id, unit in mismatches:
        _LOGGER.debug("[%s] Unit mismatch: %s  entity=%r", server_id, stat_id, unit)

    if not mismatches:
        _LOGGER.debug("[%s] Statistics unit check: no mismatches found", server_id)
        return pending

    await _async_apply_statistics_unit_fixes(hass, server_id, mismatches, async_update_statistics_metadata)
    return pending


async def _async_apply_statistics_unit_fixes(
    hass: HomeAssistant,
    server_id: str,
    mismatches: list[tuple[str, str]],
    update_metadata: Callable[..., None],
) -> None:
    """Write the label-only metadata updates, then remove the unit-change repairs they settle."""
    import asyncio

    # Schedule label-only metadata updates (no value conversion needed).
    # new_unit_class=None is always valid; the per-unit mapping would require
    # knowing which unit_class strings this HA version accepts, and HA raises
    # HomeAssistantError for unknown values — None is the safe universal fallback.
    fixed_ids = [stat_id for stat_id, _ in mismatches]
    for stat_id, new_unit in mismatches:
        update_metadata(
            hass,
            stat_id,
            new_unit_of_measurement=new_unit,
            new_unit_class=None,
        )

    # Remove stale repair issues — validate_statistics creates them on every Statistics
    # page load whenever it detects a stored-vs-entity unit mismatch. Now that the DB
    # is corrected, future runs will find no mismatch and not recreate them.
    # Give the recorder queue time to commit — async_block_till_done() can deadlock
    # when HA is still starting up (new recorder tasks keep arriving), so a short
    # sleep is the safe alternative.
    try:
        await asyncio.sleep(STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC)
    except asyncio.CancelledError:
        # Unload/reload during the wait: the metadata updates are queued already, so the next run
        # finds no mismatch and would never get here — remove the issues now or they stay for good.
        _delete_stale_statistics_issues(hass, fixed_ids)
        raise
    deleted = _delete_stale_statistics_issues(hass, fixed_ids)

    _LOGGER.info(
        "[%s] Fixed %d statistics unit mismatches; removed %d stale repair issues",
        server_id,
        len(mismatches),
        deleted,
    )


# HA awaits async_migrate_entry — the async signature is its contract, not an unused async.
async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:  # NOSONAR
    """Migrate an older config entry to the current (1, CONFIG_ENTRY_MINOR_VERSION) layout.

    1.1 -> 1.2 (v0.10.0): no data change; flags the entry for the one-shot KNX pre-release
    cleanup check (see coordinator.check_knx_prerelease_cleanup). Every pre-1.2 entry gets
    the flag — the check itself only raises the repair issue if KNX artifacts exist, so
    plain v0.9.x installs just clear it again.
    1.2 -> 1.3: pins the old IO naming schema into entries that never saved one, so the new
    DEFAULT_SCHEMA_IO only applies to new installs (see const.migrate_entry_options).
    1.3 -> 1.4: an entry that saved the old default cover keywords gets the new default
    (markise/awning added, #179); a customised list is kept.
    """
    if entry.version > 1:
        # Downgrade from a future major version: refuse rather than guess the layout.
        return False
    if entry.minor_version < CONFIG_ENTRY_MINOR_VERSION:
        hass.config_entries.async_update_entry(
            entry,
            options=migrate_entry_options(entry.minor_version, entry.data, entry.options),
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
        _LOGGER.info("Comexio: migrated config entry %s to version 1.%d", entry.entry_id, CONFIG_ENTRY_MINOR_VERSION)
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    # R2: internal option writes (sync, ignored-marker cleanup, plan_map persist, ...) mark
    # the reload they trigger as redundant via request_options_update_without_reload(), which
    # records the exact options snapshot it wrote. Only skip if entry.options still matches
    # that snapshot — if a different write (e.g. a concurrent user options-flow save) landed
    # in between, entry.options has moved on and this reload must go through, or that write
    # would be silently lost.
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    expected_options = coordinator.take_pending_reload_skip_options() if coordinator else None
    if expected_options is not None:
        if entry.options == expected_options:
            _LOGGER.debug(
                "[%s] update_listener: skipping reload (explicit reload pending, or none needed)",
                coordinator.server_id,
            )
            return
        _LOGGER.debug(
            "[%s] update_listener: options changed since the internal write this skip was meant for — reloading anyway",
            coordinator.server_id,
        )
    if coordinator is None:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    # e.g. a repair's "Ignore" or an options-flow save during a sync — see async_reload_entry
    await coordinator.async_reload_entry("options changed")


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the entry's repairs with nothing left to fix, the certificate push on the phones, its saved config and
    its last plan preview."""
    async_remove_certificate_pushes(hass, entry)
    if (server_id := entry.data.get(CONF_SERVER_ID)) is not None:
        ir.async_delete_issue(hass, DOMAIN, unsupported_io_issue_id(server_id))
        # The one persistent repair: raised once and kept across restarts, so one raised before the config entry
        # link existed (issue_owner) never gets it, and its dialog has no coordinator left to run on.
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_KNX_PRERELEASE_CLEANUP}_{server_id}")
        # An entry removed while disabled never ran the new setup, which deletes the public copy (dh).
        await async_remove_legacy_plan_preview(hass, server_id)
    # Nothing else reads them once the entry is gone: a new entry gets a new entry id (#250).
    # finally: a failed delete of one must not keep the others, the preview names the user's wiring.
    try:
        try:
            await saved_config_store(hass, entry.entry_id).async_remove()
        finally:
            await webhook_security_store(hass, entry.entry_id).async_remove()
    finally:
        await plan_preview_store(hass, entry.entry_id).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload and cleanup."""
    running = hass.data[DOMAIN].get(entry.entry_id)
    unload_ok = False
    try:
        if running is not None and hasattr(running, "api"):
            await running.async_wait_before_unload()
        _unregister_webhooks(hass, hass.data[DOMAIN].get(f"{entry.entry_id}_webhooks", set()))
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    finally:
        if not unload_ok and running is not None and hasattr(running, "api"):
            running.unloading = False  # the entry stays loaded: restores and plan edits may start again
            _restore_webhooks(running)  # its pushes must arrive again: the ids were unregistered above
    if unload_ok:
        # A default like the get() above: the config flow's fallback reload also reaches an entry whose coordinator
        # is missing, and a KeyError here would leave that entry failed to unload.
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator and hasattr(coordinator, "api"):
            # Only now, not before the wait: the work it waited for still sends its pushes, and a failed unload
            # keeps them. The reloaded entry's watchdog takes the plans' pushes over (its first check alarms every
            # stopped and deleted plan); a check of this one still running sends nothing more (a push with buttons
            # on its way still arrives: the new watchdog's checks take it off for a plan they see running).
            coordinator.plan_watchdog.close()
            # Stop the connection-value poll timer (and any pending preview refresh) before
            # closing the sessions it polls through — otherwise the timer keeps firing on
            # this now-unloaded entry, each tick racing close() for the preview session.
            # finally: a raise out of async_shutdown() must not skip the session cleanup below.
            try:
                await coordinator.async_shutdown()
            finally:
                coordinator.api.close()
        hass.data[DOMAIN].pop(f"{entry.entry_id}_webhooks", None)

        # Remove global services when the last entry is unloaded
        if not hass.config_entries.async_entries(DOMAIN):
            hass.services.async_remove(DOMAIN, "generate_web_io")

    return unload_ok
