# Version: 0.6.0
from typing import Any
import logging
import asyncio
import datetime
import json
import socket
import voluptuous as vol

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from homeassistant.helpers.network import get_url
from homeassistant.helpers import entity_platform, config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from urllib.parse import urlparse

from .const import (
    DOMAIN, 
    SYNC_DURATION_DELETE, 
    SYNC_DURATION_WRITE,
    SYNC_DURATION_RECREATE
)
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Comexio sync button."""
    coordinator   = hass.data[DOMAIN][entry.entry_id]
    sync_button   = ComexioSyncButton(coordinator, coordinator.server_id)
    cancel_button = ComexioCancelSyncButton(coordinator, coordinator.server_id)
    async_add_entities([sync_button, cancel_button])

    # Register the entity service. 
    # As a custom integration, the service is registered under 
    # the 'comexio' domain (e.g., comexio.press_action).
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "press_action",
        {vol.Required("action"): cv.string},
        "async_handle_press",
    )

class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button for automated Web-IO lifecycle management with Deep Delta Sync."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_btn"
        self._attr_translation_key = "webio_sync"
        self._attr_icon = "mdi:cloud-upload"

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
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
            "last_audit": self.coordinator.last_summary_hash
        }

    async def async_press(self) -> None:
        """Standard UI-Press -> Full Sync."""
        await self.async_handle_press(action="full_sync")

    async def async_handle_press(self, action: str = "full_sync") -> None:
        """Execute the sync logic with mode selection."""
        msg = ""  # Initialized early to avoid UnboundLocalError in finally block or error catch
        self.coordinator.in_sync = True
        self.async_write_ha_state()

        start_time = datetime.datetime.now()
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        notif_id = f"comexio_sync_{self.server_id}"
        is_de = self.hass.config.language == "de"

        try:
            # Provide immediate user feedback via notification
            persistent_notification.async_create(
                self.hass, 
                "Analysiere Comexio-Konfiguration..." if is_de else "Analyzing Comexio configuration...", 
                title="Comexio Sync", notification_id=notif_id
            )

            # Check if the Web-IO device instance is already present
            dev_id = await api.get_webio_device_info(webio_name)
            effective_action = action

            # Get the current network address of this Home Assistant instance
            ha_address = await api.get_ha_address()

            # Retrieve audit results stored in the coordinator
            audit_data = getattr(self.coordinator, "last_audit_results", {})
            missing_items   = audit_data.get("missing", [])
            renamed_items   = audit_data.get("rename", [])
            type_mismatches = audit_data.get("type", [])
            orphans         = audit_data.get("orphan", [])

            # Strategy Decision
            if not dev_id:
                _LOGGER.info("[%s] No device instance found. Forcing recreate.", self.server_id)
                effective_action = "recreate"
            elif action == "update_ip":
                # Skip recreation check if only IP update is requested
                _LOGGER.info("[%s] Targeted IP update requested. Skipping Fast-Track check.", self.server_id)
                effective_action = "update_ip"
            else:
                # Calculate estimated time for Delta-Sync
                total_delta_write = len(missing_items) + len(renamed_items) + len(type_mismatches)
                total_delta_sec = (total_delta_write * SYNC_DURATION_WRITE) + (len(orphans) * SYNC_DURATION_DELETE)
                
                # Threshold for strategy change
                if total_delta_sec > SYNC_DURATION_RECREATE and action == "full_sync":
                    _LOGGER.info("[%s] Delta Sync ETA %ds > %ds. Attempting Fast-Track.", self.server_id, total_delta_sec, SYNC_DURATION_RECREATE)
                
                    # Attempt deletion to check if the device is linked (Fast-Track check)
                    if await api.delete_webio_device(dev_id):
                        effective_action = "recreate"
                        _LOGGER.info("[%s] Fast-Track enabled: Device has been deleted.", self.server_id)
                    else:
                        effective_action = action
                        _LOGGER.info("[%s] Device is in use. Falling back to Delta-Sync.", self.server_id)
                else:
                    effective_action = action
                    _LOGGER.info("[%s] Using requested mode: %s. Skipping Fast-Track check.", self.server_id, effective_action)

            if effective_action == "recreate":
                if action == "full_sync" and not dev_id:
                    status_msg = "🛠️ **Initial Setup**\nErstelle Web-IO Klasse..." if is_de else "🛠️ **Initial Setup**\nCreating Web-IO class..."
                else:
                    status_msg = (
                        f"🚀 **Fast-Track aktiv**\nSchnelle Neuanlage läuft (~{SYNC_DURATION_RECREATE}s)..." 
                        if is_de else 
                        f"🚀 **Fast-Track active**\nHigh-speed recreation in progress (~{SYNC_DURATION_RECREATE}s)..."
                    )
                persistent_notification.async_create(self.hass, status_msg, title="Comexio Sync", notification_id=notif_id)

                # --- case A: rebuild ---
                base_info = await api.get_webio_base_info(webio_name) # Check for existing device class
                
                if base_info:
                    base_id, base_deletable = base_info
                    if base_deletable:
                        del_msg = f"{status_msg}\n\n🗑️ Lösche alte Klasse..." if is_de else f"{status_msg}\n\n🗑️ Deleting old class..."
                        persistent_notification.async_create(self.hass, del_msg, title="Comexio Sync", notification_id=notif_id)
                        await api.delete_webio_base(base_id)
                        await asyncio.sleep(0.5)
                    else:
                        _LOGGER.warning("[%s] Base %s still blocked by other logic. Reusing base structure.", self.server_id, base_id)

                up_msg = f"{status_msg}\n\n📤 Übertrage Konfiguration..." if is_de else f"{status_msg}\n\n📤 Uploading configuration..."
                persistent_notification.async_create(self.hass, up_msg, title="Comexio Sync", notification_id=notif_id)
                web_io_json = api.generate_webio_json(self.server_id, webio_name, self.coordinator.data)
                success, res_id = await api.upload_web_io(self.server_id, webio_name, web_io_json)
                
                if success:
                    await api.create_webio_device(webio_name, res_id, ha_address)
                    duration = datetime.datetime.now() - start_time
                    duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"
                    msg = f"✅ Neuanlage erfolgreich ({duration_str})." if is_de else f"✅ Recreation successful ({duration_str})."
                else:
                    raise Exception(f"Upload failed: {res_id}")
            
            # --- case B: fix step by step ---
            else:
                # Delta-Sync: Targeted updates for individual commands
                _LOGGER.info("[%s] Performing targeted Delta Sync for mode: %s", self.server_id, effective_action)
                persistent_notification.async_create(
                    self.hass, 
                    f"Aktion '{action}' läuft (Delta-Sync)..." if is_de else f"Action '{action}' in progress (Delta-Sync)...", 
                    title="Comexio Sync", notification_id=notif_id
                )
                
                base_id = audit_data.get("com_base_id")
                
                # Fallback: Resolve Base ID dynamically if missing (crucial for create_missing)
                if not base_id or str(base_id) in ("0", "None"):
                    b_info = await api.get_webio_base_info(webio_name)
                    if b_info:
                        base_id = b_info[0]
                        _LOGGER.debug("[%s] Resolved fallback Base ID: %s", self.server_id, base_id)

                tasks_to_do = []
                if effective_action in ("full_sync", "update_renames"):
                    tasks_to_do.extend([{"item": i, "type": "rename"} for i in renamed_items])
                if effective_action in ("full_sync", "delete_orphans"):
                    tasks_to_do.extend([{"item": i, "type": "delete"} for i in orphans])
                if effective_action in ("full_sync", "create_missing"):
                    tasks_to_do.extend([{"item": i, "type": "create"} for i in missing_items])
                if effective_action in ("full_sync", "update_types"):
                    tasks_to_do.extend([{"item": i, "type": "type"} for i in type_mismatches])

                total_tasks = max(1, len(tasks_to_do))
                total_write_tasks = sum(1 for t in tasks_to_do if t["type"] in ("create", "rename", "type"))
                total_eta_seconds = sum(SYNC_DURATION_DELETE if t["type"] == "delete" else SYNC_DURATION_WRITE for t in tasks_to_do)
                
                # Always add IP sync duration if a mismatch exists, as Delta Sync does not auto-update IP
                if audit_data.get("ip_mismatch"):
                    total_eta_seconds += SYNC_DURATION_WRITE
                    
                # Add the 1.5s sleep delay between tasks to the total ETA
                if total_tasks > 1:
                    total_eta_seconds += int(1.5 * (total_tasks - 1))

                # Resolve exact entity_id of the cancel button dynamically for the Markdown Link
                ent_reg = dr.er.async_get(self.hass) if hasattr(dr, 'er') else er.async_get(self.hass) # Ensure er is available, we will import it above or assume er is imported
                cancel_btn_uid = f"comexio_{self.server_id}_cancel_sync_btn"
                cancel_btn_id = ent_reg.async_get_entity_id("button", DOMAIN, cancel_btn_uid) or f"button.comexio_sync_abbrechen"

                def update_progress(current, task_name, task_type):
                    # Update progress UI for the user including time estimation
                    rem_tasks = tasks_to_do[current:]
                    eta_s = sum(SYNC_DURATION_DELETE if t["type"] == "delete" else SYNC_DURATION_WRITE for t in rem_tasks)
                    if len(rem_tasks) > 1:
                        eta_s += int(1.5 * (len(rem_tasks) - 1))
                    elaps = datetime.datetime.now() - start_time
                    elaps_str = f"{elaps.seconds // 60:02}:{elaps.seconds % 60:02d}"
                    rem_str = f"{eta_s // 60:02}:{eta_s % 60:02d}"
                    eta_t = (datetime.datetime.now() + datetime.timedelta(seconds=eta_s)).strftime("%H:%M:%S")
                    
                    labels = {
                        "de": {"rename": "✏️ Umbenennen", "delete": "🗑️ Entfernen", "create": "➕ Hinzufügen", "type": "🔧 Typ-Fix"},
                        "en": {"rename": "✏️ Rename", "delete": "🗑️ Remove", "create": "➕ Add", "type": "🔧 Type-Fix"}
                    }
                    l_key = "de" if is_de else "en"
                    t_label = labels[l_key].get(task_type, task_type)
                    
                    if is_de:
                        prog_msg = (f"**Modus:** `{action}` (Fallback: Delta-Sync)\n**Fortschritt:** Schritt {current + 1} von {total_tasks}\n"
                                    f"**Aktuell:** {t_label}: `{task_name}`\n\n---\n"
                                    f"🕒 **Start:** {start_time.strftime('%H:%M:%S')} (Laufzeit: {elaps_str})\n"
                                    f"🏁 **Fertig:** ~{eta_t} (Rest: {rem_str})\n\n"
                                    f"🛑 **AKTION ABBRECHEN**")
                    else:
                        prog_msg = (f"**Mode:** `{effective_action}`\n**Progress:** Step {current + 1} of {total_tasks}\n"
                                    f"**Current:** {t_label}: `{task_name}`\n\n---\n"
                                    f"🕒 **Start:** {start_time.strftime('%H:%M:%S')} (Runtime: {elaps_str})\n"
                                    f"🏁 **Done:** ~{eta_t} (Remaining: {rem_str})\n\n"
                                    f"🛑 **ABORT ACTION**")
                    persistent_notification.async_create(self.hass, prog_msg, title=f"Comexio Sync ({self.server_id})", notification_id=notif_id)

                added, removed, updated, renamed = 0, 0, 0, 0
                for idx, task in enumerate(tasks_to_do):
                    if getattr(self.coordinator, "cancel_sync", False): break
                    item, t_type = task["item"], task["type"]
                    update_progress(idx, item['name'], t_type)
                    if t_type == "rename":
                        await api.save_single_command(base_id, dev_id, item['payload'], existing_cmd_id=item['id'])
                        renamed += 1
                    elif t_type == "delete":
                        await api.delete_single_command(item['id'], dev_id)
                        removed += 1
                    elif t_type == "type":
                        # Clean up entity from registry if type changed
                        dev_reg = dr.async_get(self.hass)
                        device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{self.server_id}_{item['id']}")})
                        if device: dev_reg.async_remove_device(device.id)
                        await api.save_single_command(base_id, dev_id, item['payload'], existing_cmd_id=item['id'])
                        updated += 1
                    elif t_type == "create":
                        await api.save_single_command(base_id, dev_id, item['payload'])
                        added += 1

                # Final step: Update Server Address (IP)
                updated_ip = False
                if effective_action in ("full_sync", "update_ip") and audit_data.get("ip_mismatch"):
                    ha_addr = audit_data.get("ha_address")
                    com_dev_id = audit_data.get("com_device_id")
                    # Explicitly update IP as save_single_command does not update device-level settings
                    if com_dev_id and ha_addr:
                        status = "🌐 **Reparatur läuft:** Aktualisiere HA IP-Adresse..." if is_de else "🌐 **Repair in progress:** Updating HA IP address..."
                        persistent_notification.async_create(self.hass, status, title="Comexio Sync", notification_id=notif_id)
                        updated_ip = await api.update_webio_device_ip(com_dev_id, ha_addr)

                duration = datetime.datetime.now() - start_time
                duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"
                plan_str = f"{total_eta_seconds // 60}:{total_eta_seconds % 60:02d} min"
                if is_de:
                    if added + updated + renamed + removed == 0 and updated_ip:
                        msg = f"✅ **Comexio Server-Adresse aktualisiert**\n\nDie IP-Adresse wurde erfolgreich im Web-IO Gerät hinterlegt.\n⏱ Duration: {duration_str} (Plan: {plan_str})"
                    else:
                        msg = (f"✅ **Comexio Sync abgeschlossen**\n\nResultat: {added} neu, {updated} Typ-Fix, {renamed} umbenannt, {removed} entfernt" + 
                              (", IP-Adresse aktualisiert" if updated_ip else "") + f".\n⏱ Duration: {duration_str} (Plan: {plan_str})")
                else:
                    if added + updated + renamed + removed == 0 and updated_ip:
                        msg = f"✅ **Comexio Server Address updated**\n\nThe IP address has been successfully updated in the Web-IO device.\n⏱ Duration: {duration_str} (Plan: {plan_str})"
                    else:
                        msg = (f"✅ **Comexio Sync Finished**\n\nResults: +{added}, {updated} updated, {renamed} renamed, -{removed} removed" +
                              (", IP-Address updated" if updated_ip else "") + f".\n⏱ Duration: {duration_str} (Plan: {plan_str})")

            # Reset missing/ignore flag
            new_options = dict(self.coordinator.config_entry.options)
            new_options["audit_ignored"] = False
            self.hass.config_entries.async_update_entry(
                self.coordinator.config_entry, options=new_options
            )
            self.coordinator.last_audit_failed = False

            persistent_notification.async_create(self.hass, msg, title="Success", notification_id=notif_id)
        
        except Exception as e:
            self.coordinator.in_sync = False
            _LOGGER.exception("[%s] Sync failed", self.server_id)
            persistent_notification.async_create(self.hass, f"Error: {e}", title="Sync Failed", notification_id=notif_id)

        finally:
            # 1. Flag reset
            self.coordinator.in_sync = False
            self.coordinator.cancel_sync = False
            
            # 2. Give the Comexio server a moment to finish the write operation
            await asyncio.sleep(0.5)
            
            # 3. Reset UI button
            self.async_write_ha_state()
            
            # 4. Restart integration (necessary!)
            _LOGGER.info("[%s] Forcing integration reload after sync...", self.server_id)
            await self.hass.config_entries.async_reload(self.coordinator.config_entry.entry_id)

class ComexioCancelSyncButton(CoordinatorEntity, ButtonEntity):
    """Button to interrupt an ongoing Comexio sync process."""

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_cancel_sync_btn"
        self._attr_name = "Comexio Sync abbrechen"
        self._attr_icon = "mdi:stop-circle-outline"
        # 'diagnostic' sorgt dafür, dass der Button unter 'Konfiguration' gruppiert wird
        self._attr_entity_category = EntityCategory.DIAGNOSTIC 

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
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
