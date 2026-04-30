# Version: 0.4.1
import logging
import asyncio
import datetime
import json
import voluptuous as vol

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components import persistent_notification
from homeassistant.helpers.network import get_url
from homeassistant.helpers import entity_platform, config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.const import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Comexio sync button."""
    coordinator   = hass.data[DOMAIN][entry.entry_id]
    sync_button   = ComexioSyncButton(coordinator, coordinator.server_id)
    cancel_button = ComexioCancelSyncButton(coordinator, coordinator.server_id)
    async_add_entities([sync_button, cancel_button])

    # Registriert den Entity Service. 
    # WICHTIG: Da dies eine benutzerdefinierte Integration ist, wird der Dienst 
    # unter der Domäne 'comexio' registriert (also comexio.press_action).
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "press_action",
        {vol.Required("action"): cv.string},
        "async_handle_press",
    )

class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button for automated Web-IO lifecycle management with Deep Delta Sync."""

    def __init__(self, coordinator, server_id):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_btn"
        self._attr_translation_key = "webio_sync"
        self._attr_icon = "mdi:cloud-upload"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": f"Comexio Server {self.coordinator.server_id}",
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def icon(self) -> str:
        """Show a different icon during sync."""
        if getattr(self.coordinator, "in_sync", False):
            return "mdi:sync-circle"  # Ein "ladendes" Icon
        return "mdi:cloud-upload"

    @property
    def available(self) -> bool:
        """Gray out the button in the UI while syncing."""
        return not getattr(self.coordinator, "in_sync", False)

    @property
    def extra_state_attributes(self):
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
        self.coordinator.in_sync = True
        self.async_write_ha_state()        

        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        notif_id = f"comexio_sync_{self.server_id}"
        is_de = self.hass.config.language == "de"
        
        start_msg = f"Aktion '{action}' läuft..." if is_de else f"Action '{action}' in progress..."
        persistent_notification.async_create(self.hass, start_msg, title="Comexio Sync", notification_id=notif_id)

        try:
            # 1. Versuche die URL aus den HA-Einstellungen zu bekommen
            ha_url = get_url(self.hass, require_ssl=False, prefer_external=False, allow_internal=True)
            ha_address = ha_url.replace("http://", "").replace("https://", "").split(":")[0]
            
            # 2. Sicherheits-Check: Wenn HA 'localhost' oder '127.0.0.1' liefert
            if ha_address in ["localhost", "127.0.0.1", "::1"]:
                # Nutze die IP, über die HA mit dem Internet kommuniziert
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ha_address = s.getsockname()[0]
                s.close()
            
            dev_id = await api.get_web_io_device_info(webio_name)
            base_info = await api.get_web_io_base_info(webio_name)
            
            if base_info:
                base_id, deletable = base_info
                
                # --- AUTO-MODE LOGIK ---
                # Wir ermitteln dynamisch den besten Modus für den Full Sync
                effective_action = action
                if action == "full_sync":
                    if deletable:
                        effective_action = "recreate"
                        _LOGGER.info("[%s] Auto-Mode: Class is not in use. Choosing high-speed recreation.", self.server_id)
                    else:
                        effective_action = "full_sync"
                        _LOGGER.info("[%s] Auto-Mode: Class is in use! Falling back to safe Delta-Sync.", self.server_id)

                # --- FALL A: Radikaler schneller Neuaufbau ---
                if effective_action == "recreate":
                    _LOGGER.info("[%s] Performing full sync by dropping and recreating the class.", self.server_id)
                    if dev_id: 
                        _LOGGER.info("[%s] Dropping device instance %s", self.server_id, dev_id)
                        await api.delete_web_io_device(dev_id)
                        await asyncio.sleep(2)
                    
                    _LOGGER.info("[%s] Dropping device base %s", self.server_id, base_id)
                    await api.delete_web_io_base(base_id)
                    await asyncio.sleep(2)
                    
                    web_io_json = api.generate_web_io_json(self.server_id, webio_name, self.coordinator.data)
                    success, res_id = await api.upload_web_io(self.server_id, webio_name, web_io_json)
                    if success: 
                        await api.create_web_io_device(webio_name, res_id, ha_address)
                        msg = "Vollständige Neuanlage erfolgreich." if is_de else "Full recreation complete."
                    else: 
                        raise Exception(f"Upload failed: {res_id}")
                
                # --- FALL B: Sicherer Delta Sync oder gezielte Einzelreparatur ---
                else:
                    _LOGGER.info("[%s] Performing targeted Delta Sync for mode: %s", self.server_id, effective_action)
                    
                    # Generiere die gewünschte Ziel-Konfiguration
                    current_json = json.loads(api.generate_web_io_json(self.server_id, webio_name, self.coordinator.data))
                    desired_cmds = {c["Name"]: c for c in current_json["commands"]}
                    existing_cmds = self.coordinator.data.get("webio_commands", {})
                    
                    added, removed, updated, renamed = 0, 0, 0, 0
                    
                    # --- Delta-Analyse für die Zählung und Verarbeitung ---
                    audit_data = getattr(self.coordinator, "last_audit_results", {})
                    missing_items = audit_data.get("missing", [])
                    renamed_items = audit_data.get("rename", [])
                    type_mismatches = audit_data.get("type", [])
                    orphans = audit_data.get("orphan", [])

                    # --- Jetzt die korrekte total_tasks Zuweisung ---
                    if effective_action == "full_sync":
                        total_tasks = len(missing_items) + len(type_mismatches) + len(renamed_items) + len(orphans)
                    elif effective_action == "create_missing":
                        total_tasks = len(missing_items)
                    elif effective_action == "update_renames":
                        total_tasks = len(renamed_items)
                    elif effective_action == "delete_orphans":
                        total_tasks = len(orphans)
                    elif effective_action == "update_types":
                        total_tasks = len(type_mismatches)
                    else:
                        total_tasks = 1 # Fallback für recreate

                    # Sicherheitscheck gegen Division durch Null
                    total_tasks = max(1, total_tasks)
                    start_time = datetime.datetime.now()
                    if effective_action == "full_sync":
                        total_eta_seconds = (len(missing_items) + len(type_mismatches) + len(renamed_items)) * 35 + (len(orphans) * 4)
                    elif effective_action == "delete_orphans":
                        total_eta_seconds = len(orphans) * 4
                    else:
                        total_eta_seconds = total_tasks * 35
                    
                    def update_progress(current, task_name, task_type):
                        # Berechnung der Restzeit: (Gesamt - Aktuell) * Durchschnittsdauer
                        # Wir nehmen 35s pro Write und 4s pro Delete an
                        remaining = total_tasks - current
                        eta_seconds = remaining * (4 if task_type == "delete" else 35)
                        
                        # Berechnungen für die Anzeige
                        elapsed = datetime.datetime.now() - start_time
                        elapsed_str = f"{elapsed.seconds // 60:02}:{elapsed.seconds % 60:02d}"
                        rem_str = f"{eta_seconds // 60:02}:{eta_seconds % 60:02d}"
                        
                        eta_time = (datetime.datetime.now() + datetime.timedelta(seconds=eta_seconds)).strftime("%H:%M:%S")
                        
                        type_label = {
                            "rename": "✏️ Umbenennen",
                            "delete": "🗑️ Entfernen",
                            "create": "➕ Hinzufügen",
                            "type":   "🔧 Typ-Fix"
                        }.get(task_type, task_type)
                        
                        # Abort Link
                        # 1. Device ID aus der Registry holen
                        dev_reg = dr.async_get(self.hass)
                        device = dev_reg.async_get_device(identifiers={(DOMAIN, self.server_id)})
                        # 2. Link generieren (Fallback auf Entitäten-Liste, falls Gerät nicht gefunden)
                        if device:
                            device_link = f"/config/devices/device/{device.id}"
                        else:
                            device_link = f"/config/entities?config_entry={self.coordinator.config_entry.entry_id}"

                        prog_msg = (
                            f"**Modus:** `{effective_action}`\n"
                            f"**Fortschritt:** Schritt {current} von {total_tasks}\n"
                            f"**Aktuell:** {type_label}: `{task_name}`\n\n"
                            f"--- \n"
                            f"🕒 **Start:** {start_time.strftime('%H:%M:%S')} (Laufzeit: {elapsed_str})\n"
                            f"🏁 **Fertig:** ~{eta_time} (Rest: {rem_str})\n\n"
                            f"🛑 **[AKTION ABBRECHEN]({device_link})**"
                        )
                        
                        persistent_notification.async_create(
                            self.hass,
                            prog_msg, 
                            title=f"Comexio Sync Progress ({self.server_id})", 
                            notification_id=notif_id
                        )

                    step_counter = 0

                    # 2. LOGIK FÜR UMBENENNUNGEN
                    if effective_action in ("full_sync", "update_renames"):
                        for new_name, payload in desired_cmds.items():
                            if getattr(self.coordinator, "cancel_sync", False): break
                            if new_name not in existing_cmds:
                                parts = new_name.split(" ")
                                if len(parts) < 2: continue
                                ident = parts[1]

                                for old_name, info in list(existing_cmds.items()):
                                    if f" {ident} " in f" {old_name} " and old_name not in desired_cmds:
                                        step_counter += 1
                                        update_progress(step_counter, new_name, "rename")
                                        
                                        await api.save_single_command(base_id, dev_id, payload, existing_cmd_id=info["cmdId"])
                                        existing_cmds[new_name] = info
                                        del existing_cmds[old_name]
                                        renamed += 1
                                        break

                    # 3. Orphans löschen
                    if effective_action in ("full_sync", "delete_orphans"):
                        for name, info in list(existing_cmds.items()):
                            if getattr(self.coordinator, "cancel_sync", False): break
                            if name not in desired_cmds:
                                step_counter += 1
                                update_progress(step_counter, f"Delete {name}", "delete")
                                
                                await api.delete_single_command(info["cmdId"], dev_id)
                                removed += 1
                                del existing_cmds[name]
                    
                    # 4. Fehlende oder Typ-Updates
                    for name, payload in desired_cmds.items():
                        if getattr(self.coordinator, "cancel_sync", False): break
                        if name not in existing_cmds:
                            if effective_action in ("full_sync", "create_missing"):
                                step_counter += 1
                                update_progress(step_counter, name, "create")
                                
                                await api.save_single_command(base_id, dev_id, payload)
                                added += 1
                        else:
                            ext_info = existing_cmds[name]
                            if int(payload["TypeId"]) != int(ext_info.get("typeId", 1)):
                                if effective_action in ("full_sync", "update_types"):
                                    step_counter += 1
                                    update_progress(step_counter, f"Type-Fix {name}", "type")
                                    
                                    await api.save_single_command(base_id, dev_id, payload, existing_cmd_id=ext_info["cmdId"])
                                    updated += 1

                    # Am Ende der erfolgreichen Verarbeitung:
                    # 1. Jetzt erst die Endzeit nehmen
                    end_time = datetime.datetime.now() 
                    duration = end_time - start_time
                    # 2. Strings für die Meldung bauen
                    duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"
                    plan_str = f"{total_eta_seconds // 60}:{total_eta_seconds % 60:02d} min"

                    if is_de:
                        msg = (f"✅ **Comexio Sync abgeschlossen**\n\n"
                            f"Ergebnis: {added} neu, {updated} Typ-Fix, {renamed} umbenannt, {removed} entfernt.\n"
                            f"⏱ Dauer: {duration_str} (Geplant: {plan_str})")
                    else:
                        msg = (f"✅ **Comexio Sync Finished**\n\n"
                            f"Results: +{added}, {updated} updated, {renamed} renamed, -{removed} removed.\n"
                            f"⏱ Duration: {duration_str} (Plan: {plan_str})")

                    # 3. Den finalen Bericht senden
                    persistent_notification.async_create(
                        self.hass, msg, 
                        title=f"Comexio Bericht ({self.server_id})", 
                        notification_id=notif_id
                    )
            
            else:
                # --- CASE 3: INITIAL INSTALLATION ---
                web_io_json = api.generate_web_io_json(self.server_id, webio_name, self.coordinator.data)
                success, res_id = await api.upload_web_io(self.server_id, webio_name, web_io_json)
                if success: 
                    await api.create_web_io_device(webio_name, res_id, ha_address)
                    msg = "Erst-Einrichtung abgeschlossen." if is_de else "Initial setup finished."

            # reset missing/ignore flag
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
            # 1. Flag zurücksetzen
            self.coordinator.in_sync = False
            self.coordinator.cancel_sync = False
            
            # 2. Dem Comexio-Server kurz Zeit geben, den Schreibvorgang abzuschließen
            await asyncio.sleep(1.5)
            
            # 3. UI-Button zurücksetzen
            self.async_write_ha_state()
            
            # 4. Integration restarten (notwendig!)
            _LOGGER.info("[%s] Erzwinge Reload der Integration nach Sync...", self.server_id)
            self.hass.config_entries.async_reload(self.coordinator.config_entry.entry_id)

class ComexioCancelSyncButton(CoordinatorEntity, ButtonEntity):
    """Button to interrupt an ongoing Comexio sync process."""

    def __init__(self, coordinator, server_id):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_cancel_sync_btn"
        self._attr_name = "Comexio Sync abbrechen"
        self._attr_icon = "mdi:stop-circle-outline"
        # 'diagnostic' sorgt dafür, dass der Button unter 'Konfiguration' gruppiert wird
        self._attr_entity_category = EntityCategory.DIAGNOSTIC 

    @property
    def device_info(self):
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
