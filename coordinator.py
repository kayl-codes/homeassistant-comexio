# Version: 0.4.3
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers import issue_registry as ir
import logging

from .api import ComexioAPI

from .const import (
    DOMAIN, 
    CONF_HOST, 
    CONF_USERNAME, 
    CONF_PASSWORD, 
    CONF_API_USERNAME,
    CONF_API_PASSWORD
)

_LOGGER = logging.getLogger(__name__)

class ComexioCoordinator(DataUpdateCoordinator):
    """Coordinator to manage data fetching and state updates with Type-Audit."""

    def __init__(self, hass, api: ComexioAPI):
        super().__init__(hass, logger=_LOGGER, name=DOMAIN, update_interval=None)
        self.api = api
        self.api.config_entry = None
        self.server_id = None
        self.config_entry = None
        self.marker_states = {}
        self.io_states = {}
        self.audit_ignored = False
        self.last_audit_failed = False
        self.last_summary_hash = None
        self.in_sync = False
        self.cancel_sync = False

    async def _async_update_data(self):
        """Fetch configuration and perform smart audit including Type-Checks."""
        if self.in_sync:
            _LOGGER.debug("[%s] Periodic sync skipped because a manual sync or repair is in progress", self.server_id)
            return self.data

        _LOGGER.debug("[%s] Periodic sync / Audit started", self.server_id)

        try:
            conf = {**self.config_entry.data, **self.config_entry.options}

            raw_config = await self.api.get_raw_config()
            marker_data = raw_config.get("FubModules", {}).get("2", {})
            max_id = max([int(m.get("Id", 0)) for m in marker_data.values()]) if marker_data else 0
            
            live_states = await self.api.get_live_states(max_id)
            parsed_data = self.api.parse_config(raw_config, live_states)

            import_markers = conf.get("import_markers", True)
            import_ios = conf.get("import_ios", True)

            final_data = {
                "markers": parsed_data["markers"] if import_markers else [], 
                "io": parsed_data["io"] if import_ios else [], 
                "webio_commands": parsed_data.get("webio_commands", {})
            }

            # State-Cache aktualisieren
            for m in final_data["markers"]: self.marker_states[m["id"]] = m["value"]
            for io in final_data["io"]: self.io_states[io["id"]] = io["value"]

            # --- SMART AUDIT LOGIK ---
            com_commands = final_data["webio_commands"]
            
            type_mismatches = []
            missing_items = []
            renamed_items = []
            orphans = []
            mismatches = set()
            
            # 1. HA Map: Merker
            ha_map = {}
            for m in final_data["markers"]:
                ha_map[f"M{m['id']}"] = {
                    "name": f"HA M{m['id']} {m['name']}", 
                    "type": m["type"]  # Wir vertrauen der Vorarbeit der api.py
                }
            # 2. HA Map: IOs
            for io in final_data["io"]:
                key = f"IO_{io['ext_name']}_{io['identifier']}"
                
                # Da die api.py jetzt 'is_binary' liefert, leiten wir 
                # den Audit-Typ ('digital'/'analog') hier davon ab:
                mapped_type = "digital" if io.get("is_binary") else "analog"
                
                ha_map[key] = {
                    "name": f"HA IO {io['ext_name']} {io['identifier']}", 
                    "type": mapped_type 
                }

            # 3. Comexio Map (Audit der Gegenseite)
            com_map = {}
            for full_name, info in com_commands.items():
                parts = full_name.split(" ")
                if len(parts) >= 3:
                    if parts[1].startswith("M"):
                        key = parts[1]
                        comexio_type_id = int(info.get("typeId", 1))
                        # Markers follow the Web-Command TypeId (1=digital, 2=analog)
                        mapped_type = "analog" if comexio_type_id == 2 else "digital"
                    elif parts[1] == "IO" and len(parts) >= 4:
                        key = f"IO_{parts[2]}_{parts[3]}"
                        
                        # HIER IST DIE LÖSUNG:
                        # Wir holen die InOutputTypeId vom Befehl
                        raw_io_type = str(info.get("typeId", 1)) 
                        # Und schauen in unserer dynamischen Liste nach der 'binary' Eigenschaft
                        type_info = self.api.io_types.get(raw_io_type, {})
                        is_binary = type_info.get("binary", False)
                        
                        mapped_type = "digital" if is_binary else "analog"
                    else:
                        key = full_name
                        mapped_type = "digital"
                    
                    com_map[key] = {"name": full_name, "type": mapped_type}

            # ABBRUCH-WEICHE: Wenn überhaupt keine Webhooks in Comexio vorhanden sind!
            if not com_map:
                is_ignored = conf.get("audit_ignored", False)
                if not is_ignored:
                    ir.async_create_issue(
                        self.hass, DOMAIN, f"sync_mismatch_{self.server_id}",
                        is_fixable=True, 
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="missing_webio_class",
                        translation_placeholders={"server_id": self.server_id},
                        data={
                            "entry_id": self.config_entry.entry_id,
                            "counts": {
                                "type": 0,
                                "missing": len(ha_map),
                                "rename": 0,
                                "orphan": 0,
                                "all": len(ha_map)
                            }
                        }
                    )
                return final_data

            # FALL 2: Gerät ist da, aber wir haben inhaltliche Differenzen (Delta)
            # (Hier folgt dein bestehender Code für type_mismatches, missing_items, etc.)
            mismatches = set()
            if mismatches:
                ir.async_create_issue(
                        self.hass, DOMAIN, f"sync_mismatch_{self.server_id}",
                        is_fixable=True, severity=ir.IssueSeverity.ERROR,
                        translation_key="sync_mismatch",
                        translation_placeholders={
                            "ha_count": str(len(ha_map)), 
                            "com_count": str(len(com_map)), 
                            "t_count": str(len(type_mismatches)),
                            "m_count": str(len(missing_items)),
                            "r_count": str(len(renamed_items)),
                            "o_count": str(len(orphans))
                        },
                        data={
                            "entry_id": self.config_entry.entry_id,
                            "counts": {
                                "type": len(type_mismatches),
                                "missing": len(missing_items),
                                "rename": len(renamed_items),
                                "orphan": len(orphans),
                                "all": (len(type_mismatches) + len(missing_items) + len(renamed_items) + len(orphans))
                            }
                        }
                    )
                # Log warning only once per failure cycle
                if not self.last_audit_failed:
                    _LOGGER.warning("[%s] ⚠️ Audit abgebrochen: Web-IO Gerät/Klasse fehlt! Bitte Erst-Einrichtung starten.", self.server_id)
                    self.last_audit_failed = True

            else:
                # Alles okay -> Issue löschen
                ir.async_delete_issue(self.hass, DOMAIN, f"sync_mismatch_{self.server_id}")

            # Reset failure flag if audit succeeds (Web-IO exists again)
            self.last_audit_failed = False

            # Debugging: Log HA Map and Comexio Map for detailed analysis
            #_LOGGER.debug("HA Map: %s", ha_map)
            #_LOGGER.debug("Comexio Map: %s", com_map)

            # Debugging: Validate HA Map types against raw marker data
            for key, ha in ha_map.items():
                if key.startswith("M") and key[1:].isdigit():
                    marker_id = int(key[1:])
                    raw_marker = raw_config.get("FubModules", {}).get("2", {}).get(str(marker_id), {})
                    if raw_marker:
                        raw_type = raw_marker.get("Type")
                        
                        # KORREKTUR: raw_type 2 UND 3 sind für HA "analog"
                        expected_type = "analog" if raw_type in [2, 3] else "digital"
                        
                        if ha["type"] != expected_type:
                            _LOGGER.error("Type mismatch in HA Map creation: Key: %s, HA Type: %s, Raw Type: %s", key, ha["type"], raw_type)

            mismatches = set()
            
            # Validate types in ha_map and com_map
            # Listen für das Summary
            type_mismatches = []
            missing_items = []
            renamed_items = []
            orphans = []

            # 1. Check: Typ-Konflikte und fehlende Einträge
            for key, ha in ha_map.items():
                if key not in com_map:
                    missing_items.append(ha["name"])
                    mismatches.add(key)
                else:
                    com = com_map[key]
                    is_renamed = False
                    
                    # Namen prüfen
                    if ha["name"] != com["name"]:
                        renamed_items.append(f"{com['name']} -> {ha['name']}")
                        mismatches.add(key)
                        is_renamed = True
                        
                    # Typen prüfen (NUR wenn nicht schon als Umbenennung markiert, 
                    # da das Update beides gleichzeitig fixiert)
                    if not is_renamed and ha["type"] != com["type"]:
                        type_mismatches.append(f"{ha['name']} (HA: {ha['type']} | Com: {com['type']})")
                        mismatches.add(key)

            # 2. Check: Orphans (Waisen)
            for key, com in com_map.items():
                if key not in ha_map:
                    orphans.append(com["name"])
                    mismatches.add(key)

            # 📈 --- THE SCANABLE SUMMARY IN LOG ---
            if mismatches:
                # Create a simple string representation to detect changes
                current_summary_content = f"{len(type_mismatches)}-{len(missing_items)}-{len(renamed_items)}-{len(orphans)}"
                
                # Only log if something changed compared to last audit
                if self.last_summary_hash != current_summary_content:
                    self.last_summary_hash = current_summary_content

                    # One single WARNING for the UI overview
                    _LOGGER.warning("[%s] Comexio Audit Mismatch: %d issues detected (Type:%d, Missing:%d, Renames:%d, Orphans:%d)", 
                                    self.server_id, len(mismatches), len(type_mismatches), len(missing_items), len(renamed_items), len(orphans))

                    _LOGGER.info("=== ⚠️ COMEXIO AUDIT SUMMARY [%s] ===", self.server_id)
                    _LOGGER.info("🔧 Type-Mismatches (%d):", len(type_mismatches))
                    for item in type_mismatches: _LOGGER.info("   -> %s", item)
                        
                    _LOGGER.info("➕ Missing Webhooks (%d):", len(missing_items))
                    for item in missing_items: _LOGGER.info("   -> %s", item)
                        
                    _LOGGER.info("✏️ Renames (%d):", len(renamed_items))
                    for item in renamed_items: _LOGGER.info("   -> %s", item)
                        
                    _LOGGER.info("🗑️ Orphans (%d):", len(orphans))
                    for item in orphans: _LOGGER.info("   -> %s", item)
                    _LOGGER.info("========================================")
            else:
                if self.last_summary_hash is not None:
                    _LOGGER.info("[%s] Audit successful: All systems are 100%% in sync!", self.server_id)
                self.last_summary_hash = None

            # Issue Management
            if mismatches:
                issue_data_counts = {
                    "type": len(type_mismatches),
                    "missing": len(missing_items),
                    "rename": len(renamed_items),
                    "orphan": len(orphans),
                    "all": len(mismatches)
                }

                ir.async_create_issue(
                    self.hass, DOMAIN, f"sync_mismatch_{self.server_id}",
                    is_fixable=True, 
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="sync_mismatch",
                    translation_placeholders={
                        "ha_count": str(len(ha_map)), 
                        "com_count": str(len(com_map)), 
                        # Wir übergeben die Zahlen einzeln
                        "t_count": str(len(type_mismatches)),
                        "m_count": str(len(missing_items)),
                        "r_count": str(len(renamed_items)),
                        "o_count": str(len(orphans))
                    },
                    data={
                        "entry_id": self.config_entry.entry_id,
                        "counts": issue_data_counts
                    }
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"sync_mismatch_{self.server_id}")

            return final_data

        except Exception as e:
            _LOGGER.error("[%s] Data fetch failed: %s", self.server_id, e)
            raise

    def update_marker(self, marker_id, value):
        self.marker_states[str(marker_id)] = value
        self.async_set_updated_data(self.data)

    def update_io_by_name(self, ext_name, identifier, value):
        for io in self.data.get("io", []):
            if io["ext_name"].lower() == ext_name.lower() and io["identifier"].lower() == identifier.lower():
                self.io_states[io["id"]] = value
                break
        self.async_set_updated_data(self.data)

    async def async_config_entry_updated(self):
        """Handle config entry update (e.g. from Options Flow)."""
        _LOGGER.info("[%s] Configuration updated, reloading API settings", self.server_id)
        
        # Merge data and options
        conf = {**self.config_entry.data, **self.config_entry.options}
        
        # Update API instance with potentially new credentials
        self.api.host = conf.get(CONF_HOST)
        self.api.username = conf.get(CONF_USERNAME)
        self.api.password = conf.get(CONF_PASSWORD)
        self.api.api_user = conf.get(CONF_API_USERNAME)
        self.api.api_pass = conf.get(CONF_API_PASSWORD)
        
        # Re-authenticate with new credentials
        await self.api.login()
        
        # Trigger an immediate refresh to verify new settings
        await self.async_request_refresh()
