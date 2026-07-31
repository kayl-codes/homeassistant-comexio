# Version: 0.7.5
import asyncio
import datetime
import logging
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import voluptuous as vol

from .const import (
    CONF_ENABLE_NOTIFICATIONS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DOMAIN,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASSES,
    webio_class_name,
)
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio sync button."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sync_button = ComexioSyncButton(coordinator, coordinator.server_id)
    cancel_button = ComexioCancelSyncButton(coordinator, coordinator.server_id)
    migration_button = ComexioEntityIdMigrationButton(coordinator, coordinator.server_id)
    stats_cleanup_button = ComexioStatisticsCleanupButton(coordinator, coordinator.server_id)
    fw_check_button = ComexioFirmwareCheckButton(coordinator, coordinator.server_id)
    async_add_entities([sync_button, cancel_button, migration_button, stats_cleanup_button, fw_check_button])

    # Register the entity service.
    # As a custom integration, the service is registered under
    # the 'comexio' domain (e.g., comexio.press_action).
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "press_action",
        {
            vol.Required("action"): vol.In(
                [
                    "full_sync",
                    "update_types",
                    "create_missing",
                    "update_renames",
                    "delete_orphans",
                    "update_ip",
                ]
            )
        },
        "async_handle_press",
    )


class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button for automated Web-IO lifecycle management with Deep Delta Sync."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_start_btn"
        self._attr_translation_key = "webio_sync"
        self._attr_icon = "mdi:cloud-upload"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def icon(self) -> str:
        """Show a different icon while syncing."""
        if getattr(self.coordinator, "in_sync", False):
            return "mdi:sync-circle"  # Loading icon
        return "mdi:cloud-upload"

    @property
    def available(self) -> bool:
        """Gray out the button in the UI while syncing."""
        return not getattr(self.coordinator, "in_sync", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show status text in attributes."""
        return {
            "syncing": getattr(self.coordinator, "in_sync", False),
            "last_audit": self.coordinator.last_summary_hash,
        }

    async def async_press(self) -> None:
        """Standard UI-Press -> Full Sync."""
        await self.async_handle_press(action="full_sync")

    async def async_handle_press(self, action: str = "full_sync") -> None:
        """Execute the sync logic with mode selection."""
        if self.coordinator._sync_lock.locked():
            _LOGGER.warning("[%s] Sync already in progress, ignoring concurrent request", self.server_id)
            return
        await self.coordinator._sync_lock.acquire()
        self.coordinator.in_sync = True
        self.coordinator.sync_error = False

        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        notify_enabled = conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS)
        notif_id = f"comexio_sync_{self.server_id}"

        def _update_status(
            msg: str, is_error: bool = False, pct: int | None = None, step_info: str | None = None
        ) -> None:
            """Helper to update sensor state and optionally show a UI notification."""
            self.coordinator.sync_progress_text = msg
            if pct is not None:
                self.coordinator.sync_progress_pct = pct
            if step_info is not None:
                self.coordinator.sync_current_step = step_info
            self.coordinator.async_set_updated_data(self.coordinator.data)
            if notify_enabled:
                title = "Comexio Sync Failed" if is_error else f"Comexio Sync ({self.server_id})"
                persistent_notification.async_create(self.hass, msg, title=title, notification_id=notif_id)

        msg = ""
        _update_status("Initializing sync process...", pct=0, step_info="Initializing")
        start_time = datetime.datetime.now()
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        class_names = {cls: webio_class_name(webio_name, cls) for cls in WEBIO_CLASSES}

        try:
            _update_status("Analyzing Comexio configuration...", pct=5, step_info="Analyzing configuration")

            # Check if the Web-IO device instances are already present (one per class)
            dev_ids = {cls: await api.get_webio_device_info(class_names[cls]) for cls in WEBIO_CLASSES}

            # Get the current network address of this Home Assistant instance
            ha_address = await api.get_ha_address()

            # Retrieve audit results stored in the coordinator
            audit_data = getattr(self.coordinator, "last_audit_results", {})
            missing_items = audit_data.get("missing", [])
            renamed_items = audit_data.get("rename", [])
            type_mismatches = audit_data.get("type", [])
            orphans = audit_data.get("orphan", [])
            webio_devices_audit = audit_data.get("webio_devices", {})

            def _items_of(seq: list[dict], cls: str) -> list[dict]:
                return [i for i in seq if i.get("webio_class") == cls]

            async def _sync_class(
                cls: str,
                class_dev_id: str | None,
                cls_missing: list[dict],
                cls_renamed: list[dict],
                cls_types: list[dict],
                cls_orphans: list[dict],
                dev_ip_mismatch: bool,
                pct_start: int,
                pct_end: int,
            ) -> dict[str, Any]:
                """Recreate-or-Delta-Sync one Web-IO class (marker/io).

                Run independently per class so the two devices' Fast-Track eligibility (and
                any resulting device deletion) never interfere with each other.
                """
                class_name = class_names[cls]
                label = "Marker" if cls == WEBIO_CLASS_MARKER else "IO"
                result: dict[str, Any] = {
                    "added": 0,
                    "removed": 0,
                    "updated": 0,
                    "renamed": 0,
                    "updated_ip": False,
                    "recreated": False,
                }

                cls_action_eta = 0
                cls_task_count = 0
                if action in {"full_sync", "update_renames"}:
                    cls_action_eta += len(cls_renamed) * SYNC_DURATION_WRITE
                    cls_task_count += len(cls_renamed)
                if action in {"full_sync", "delete_orphans"}:
                    cls_action_eta += len(cls_orphans) * SYNC_DURATION_DELETE
                    cls_task_count += len(cls_orphans)
                if action in {"full_sync", "create_missing"}:
                    cls_action_eta += len(cls_missing) * SYNC_DURATION_WRITE
                    cls_task_count += len(cls_missing)
                if action in {"full_sync", "update_types"}:
                    cls_action_eta += len(cls_types) * SYNC_DURATION_WRITE
                    cls_task_count += len(cls_types)
                if action in {"full_sync", "update_ip"} and dev_ip_mismatch:
                    cls_action_eta += SYNC_DURATION_WRITE
                if cls_task_count > 1:
                    cls_action_eta += int(1.5 * (cls_task_count - 1))

                if not class_dev_id:
                    _LOGGER.info("[%s] %s: no device instance found. Forcing recreate.", self.server_id, label)
                    cls_effective_action = "recreate"
                elif action == "update_ip":
                    _LOGGER.info(
                        "[%s] %s: targeted IP update requested. Skipping Fast-Track check.", self.server_id, label
                    )
                    cls_effective_action = "update_ip"
                elif cls_action_eta > SYNC_DURATION_RECREATE:
                    _LOGGER.info(
                        "[%s] %s ETA (%ds) > Fast-Track (%ds). Attempting Fast-Track.",
                        self.server_id,
                        label,
                        cls_action_eta,
                        SYNC_DURATION_RECREATE,
                    )
                    if await api.delete_webio_device(class_dev_id):
                        cls_effective_action = "recreate"
                        _LOGGER.info("[%s] %s Fast-Track enabled: device has been deleted.", self.server_id, label)
                    else:
                        cls_effective_action = action
                        _LOGGER.info("[%s] %s device is in use. Falling back to Delta-Sync.", self.server_id, label)
                else:
                    cls_effective_action = action
                    _LOGGER.info(
                        "[%s] %s ETA (%ds) is faster than Fast-Track. Proceeding exactly as requested.",
                        self.server_id,
                        label,
                        cls_action_eta,
                    )

                if cls_effective_action == "recreate":
                    status_msg = (
                        f"🛠️ **Initial Setup ({label})**\nCreating Web-IO class..."
                        if action == "full_sync" and not class_dev_id
                        else f"🚀 **Fast-Track active ({label})**\nHigh-speed recreation in progress..."
                    )
                    _update_status(status_msg, pct=pct_start, step_info=f"Creating Web-IO class ({label})")

                    base_info = await api.get_webio_base_info(class_name)
                    if base_info:
                        base_id, base_deletable = base_info
                        if base_deletable:
                            _update_status(
                                f"{status_msg}\n\n🗑️ Deleting old class...",
                                pct=pct_start + 2,
                                step_info="Deleting old class",
                            )
                            await api.delete_webio_base(base_id)
                            await asyncio.sleep(0.5)
                        else:
                            _LOGGER.warning(
                                "[%s] %s base %s still blocked by other logic. Reusing base structure.",
                                self.server_id,
                                label,
                                base_id,
                            )

                    _update_status(
                        f"{status_msg}\n\n📤 Uploading configuration...",
                        pct=(pct_start + pct_end) // 2,
                        step_info="Uploading configuration",
                    )
                    web_io_json = api.generate_webio_json(
                        self.server_id, class_name, self.coordinator.data, webio_class=cls
                    )
                    success, res_id = await api.upload_web_io(self.server_id, class_name, web_io_json)
                    if not success:
                        raise RuntimeError(f"Upload failed ({label}): {res_id}")
                    if not await api.create_webio_device(class_name, res_id, ha_address):
                        raise RuntimeError(f"Device creation failed ({label}, class created, device instance not)")
                    result["recreated"] = True
                    return result

                # --- Delta-Sync: targeted updates for individual commands ---
                _LOGGER.info(
                    "[%s] %s: performing targeted Delta Sync for mode: %s", self.server_id, label, cls_effective_action
                )

                base_id = webio_devices_audit.get(cls, {}).get("base_id")
                if not base_id or str(base_id) in {"0", "None"}:
                    b_info = await api.get_webio_base_info(class_name)
                    if b_info:
                        base_id = b_info[0]
                        _LOGGER.debug("[%s] %s: resolved fallback Base ID: %s", self.server_id, label, base_id)

                tasks_to_do = []
                if cls_effective_action in {"full_sync", "update_renames"}:
                    tasks_to_do.extend([{"item": i, "type": "rename"} for i in cls_renamed])
                if cls_effective_action in {"full_sync", "delete_orphans"}:
                    tasks_to_do.extend([{"item": i, "type": "delete"} for i in cls_orphans])
                if cls_effective_action in {"full_sync", "create_missing"}:
                    tasks_to_do.extend([{"item": i, "type": "create"} for i in cls_missing])
                if cls_effective_action in {"full_sync", "update_types"}:
                    tasks_to_do.extend([{"item": i, "type": "type"} for i in cls_types])

                total_tasks = max(1, len(tasks_to_do))

                def _cls_progress(current: int, task_name: str, task_type: str) -> None:
                    rem_tasks = tasks_to_do[current:]
                    eta_s = sum(
                        SYNC_DURATION_DELETE if t["type"] == "delete" else SYNC_DURATION_WRITE for t in rem_tasks
                    )
                    if len(rem_tasks) > 1:
                        eta_s += int(1.5 * (len(rem_tasks) - 1))
                    elaps = datetime.datetime.now() - start_time
                    elaps_str = f"{elaps.seconds // 60:02}:{elaps.seconds % 60:02d}"
                    rem_str = f"{eta_s // 60:02}:{eta_s % 60:02d}"
                    eta_t = (datetime.datetime.now() + datetime.timedelta(seconds=eta_s)).strftime("%H:%M:%S")
                    labels = {"rename": "✏️ Rename", "delete": "🗑️ Remove", "create": "➕ Add", "type": "🔧 Type-Fix"}
                    t_label = labels.get(task_type, task_type)
                    prog_msg = (
                        f"**Class:** {label}\n**Mode:** `{cls_effective_action}`\n"
                        f"**Progress:** Step {current + 1} of {total_tasks}\n"
                        f"**Current:** {t_label}: `{task_name}`\n\n---\n"
                        f"🕒 **Start:** {start_time.strftime('%H:%M:%S')} (Runtime: {elaps_str})\n"
                        f"🏁 **Done:** ~{eta_t} (Remaining: {rem_str})"
                    )
                    pct_val = pct_start + int((pct_end - pct_start) * (current / total_tasks))
                    _update_status(
                        prog_msg,
                        pct=pct_val,
                        step_info=f"{label} {current + 1}/{total_tasks} | {t_label}: '{task_name}' | Rem: {rem_str}",
                    )

                for idx, task in enumerate(tasks_to_do):
                    if getattr(self.coordinator, "cancel_sync", False):
                        break
                    item, t_type = task["item"], task["type"]
                    _cls_progress(idx, item["name"], t_type)
                    if t_type == "rename":
                        await api.save_single_command(
                            base_id, class_dev_id, item["payload"], existing_cmd_id=item["id"]
                        )
                        result["renamed"] += 1
                    elif t_type == "delete":
                        await api.delete_single_command(item["id"], class_dev_id)
                        result["removed"] += 1
                    elif t_type == "type":
                        # Clean up entity from registry if type changed
                        dev_reg = dr.async_get(self.hass)
                        device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{self.server_id}_{item['id']}")})
                        if device:
                            dev_reg.async_remove_device(device.id)
                        await api.save_single_command(
                            base_id, class_dev_id, item["payload"], existing_cmd_id=item["id"]
                        )
                        result["updated"] += 1
                    elif t_type == "create":
                        await api.save_single_command(base_id, class_dev_id, item["payload"])
                        result["added"] += 1

                # Final step: Update Server Address (IP) — save_single_command does not
                # update device-level settings, so this is always a separate explicit call.
                if cls_effective_action in {"full_sync", "update_ip"} and dev_ip_mismatch and class_dev_id:
                    _update_status(
                        f"🌐 **Repair in progress ({label}):** Updating HA IP address...",
                        pct=pct_end - 2,
                        step_info="Updating HA IP address",
                    )
                    result["updated_ip"] = await api.update_webio_device_ip(class_dev_id, ha_address, class_name)

                return result

            added, removed, updated, renamed = 0, 0, 0, 0
            updated_ip = False
            recreated_classes: list[str] = []
            class_pct_ranges = {WEBIO_CLASS_MARKER: (5, 50), WEBIO_CLASS_IO: (50, 95)}

            for cls in WEBIO_CLASSES:
                pct_start, pct_end = class_pct_ranges[cls]
                cls_result = await _sync_class(
                    cls,
                    dev_ids[cls],
                    _items_of(missing_items, cls),
                    _items_of(renamed_items, cls),
                    _items_of(type_mismatches, cls),
                    _items_of(orphans, cls),
                    webio_devices_audit.get(cls, {}).get("ip_mismatch", False),
                    pct_start,
                    pct_end,
                )
                added += cls_result["added"]
                removed += cls_result["removed"]
                updated += cls_result["updated"]
                renamed += cls_result["renamed"]
                updated_ip = updated_ip or cls_result["updated_ip"]
                if cls_result["recreated"]:
                    recreated_classes.append(cls)

            recreate_note = ""
            if recreated_classes:
                recreated_str = ", ".join("Marker" if c == WEBIO_CLASS_MARKER else "IO" for c in recreated_classes)
                recreate_note = f"🚀 Recreated: {recreated_str}\n\n"

            duration = datetime.datetime.now() - start_time
            duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"

            if added + updated + renamed + removed == 0 and not recreated_classes and updated_ip:
                msg = (
                    "✅ **Comexio Server Address updated**\n\n"
                    f"The IP address has been successfully updated in the Web-IO device(s).\n"
                    f"⏱ Duration: {duration_str}"
                )
            elif added + updated + renamed + removed == 0 and recreated_classes and not updated_ip:
                msg = f"✅ **Comexio Recreation Finished**\n\n{recreate_note}⏱ Duration: {duration_str}"
            else:
                msg = (
                    f"✅ **Comexio Sync Finished**\n\n{recreate_note}"
                    f"Results: +{added}, {updated} updated, {renamed} renamed, -{removed} removed"
                    + (", IP-Address updated" if updated_ip else "")
                    + f".\n⏱ Duration: {duration_str}"
                )

            self.coordinator.last_audit_failed = False
            _update_status(msg, pct=100, step_info="Done")

        except Exception as e:
            self.coordinator.in_sync = False
            self.coordinator.sync_error = True
            _LOGGER.exception("[%s] Sync failed", self.server_id)
            _update_status(f"Error: {e}", is_error=True)

        finally:
            # 1. Flag reset
            self.coordinator.in_sync = False
            self.coordinator.cancel_sync = False
            if not getattr(self.coordinator, "sync_error", False):
                self.coordinator.sync_progress_text = "Idle"
            self.coordinator.sync_progress_pct = None
            self.coordinator.sync_current_step = None
            self.coordinator.async_set_updated_data(self.coordinator.data)

            # 2. Reset audit_ignored. Set the skip flag first so the update_listener
            #    suppresses its reload — the explicit reload below is the single reload (R2).
            new_options = dict(self.coordinator.config_entry.options)
            new_options["audit_ignored"] = False
            self.coordinator._skip_next_listener_reload = True
            self.hass.config_entries.async_update_entry(self.coordinator.config_entry, options=new_options)

            # 3. Release sync lock before reload (old coordinator is replaced by reload anyway)
            self.coordinator._sync_lock.release()

            # 4. Give the Comexio server a moment to finish the write operation;
            #    the update_listener task runs here, sees the skip flag, and returns. (R2)
            await asyncio.sleep(0.5)

            # 5. Reset UI button
            self.async_write_ha_state()

            # 6. Restart integration (necessary!)
            _LOGGER.info("[%s] Forcing integration reload after sync...", self.server_id)
            await self.hass.config_entries.async_reload(self.coordinator.config_entry.entry_id)


class ComexioCancelSyncButton(CoordinatorEntity, ButtonEntity):
    """Button to interrupt an ongoing Comexio sync process."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_cancel_btn"
        self._attr_translation_key = "cancel_sync"
        self._attr_icon = "mdi:stop-circle-outline"
        # 'diagnostic' ensures the button is grouped under 'Configuration'
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Only available when a sync is actually running."""
        return getattr(self.coordinator, "in_sync", False)

    async def async_press(self) -> None:
        """Trigger the cancel flag in the coordinator."""
        _LOGGER.warning("[%s] Manual cancel requested by user", self.server_id)
        self.coordinator.cancel_sync = True
        self.async_write_ha_state()


class ComexioFirmwareCheckButton(CoordinatorEntity, ButtonEntity):
    """Button to force-run the extension firmware check outside its nightly window.

    Comexio warns this call can briefly interrupt extension outputs — pressing it
    deliberately accepts that risk (e.g. to test the update.* entities without waiting
    for both the 04:00 window and a comexio_version change; see async_force_firmware_check).
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_fw_check_btn"
        self._attr_translation_key = "fw_check"
        self._attr_icon = "mdi:chip"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_press(self) -> None:
        """Force-run the firmware check, bypassing the comexio_version gate."""
        _LOGGER.warning("[%s] Manual extension firmware check requested by user", self.server_id)
        start = time.monotonic()
        ran = await self.coordinator.async_force_firmware_check()
        duration = time.monotonic() - start

        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        if conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS):
            if ran:
                names = ", ".join(sorted(self.coordinator.extension_firmware))
                msg = f"✅ Firmware check finished in {duration:.1f}s.\n\nChecked modules: {names}"
            else:
                msg = f"⚠️ Firmware check returned no data ({duration:.1f}s) — see log for details."
            persistent_notification.async_create(
                self.hass,
                msg,
                title=f"Comexio Firmware Check ({self.server_id})",
                notification_id=f"comexio_fw_check_{self.server_id}",
            )


class ComexioEntityIdMigrationButton(CoordinatorEntity, ButtonEntity):
    """Button to fix duplicate server_id in entity IDs."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_entity_id_fix_btn"
        self._attr_translation_key = "entity_id_fix"
        self._attr_icon = "mdi:identifier"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Active only when entity_id mismatches exist."""
        return len(self.coordinator.entity_id_mismatches) > 0

    async def async_press(self) -> None:
        """Migrate entity_ids by removing the duplicate server_id prefix."""
        from homeassistant.helpers import issue_registry as ir

        self.coordinator.async_migrate_entity_ids()
        ir.async_delete_issue(self.hass, DOMAIN, f"entity_id_mismatch_{self.server_id}")
        self.coordinator.async_set_updated_data(self.coordinator.data)


class ComexioStatisticsCleanupButton(CoordinatorEntity, ButtonEntity):
    """Button to delete orphaned long-term statistics left behind by entity renames."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_statistics_cleanup_btn"
        self._attr_translation_key = "statistics_cleanup"
        self._attr_icon = "mdi:database-remove"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Active only when orphaned statistics exist."""
        return len(self.coordinator.orphaned_statistics) > 0

    async def async_press(self) -> None:
        """Delete the orphaned statistic_ids via the recorder."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.helpers import issue_registry as ir

        ids = list(self.coordinator.orphaned_statistics)
        if ids and "recorder" in self.hass.config.components:
            instance = get_instance(self.hass)
            instance.async_clear_statistics(ids)

        self.coordinator.orphaned_statistics = []
        ir.async_delete_issue(self.hass, DOMAIN, f"statistics_orphaned_{self.server_id}")

        _LOGGER.info("[%s] Cleared %d orphaned statistics", self.server_id, len(ids))
        self.coordinator.async_set_updated_data(self.coordinator.data)
