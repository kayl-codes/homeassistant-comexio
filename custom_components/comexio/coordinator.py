# Version: 0.6.0
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers import issue_registry as ir
import logging
import json
import socket

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
        self.sync_error = False
        self.sync_progress_text = "Idle"
        self.sync_progress_pct = None
        self.sync_current_step = None
        self.last_audit_results = {}
        self.cancel_sync = False

    async def _async_update_data(self):
        """Fetch configuration and perform smart audit including Type-Checks."""
        if self.in_sync:
            _LOGGER.debug("[%s] Periodic audit skipped: Manual sync or repair is currently in progress", self.server_id)
            return self.data

        _LOGGER.debug("[%s] Starting periodic configuration audit to detect mismatches", self.server_id)

        try:
            conf = {**self.config_entry.data, **self.config_entry.options}
            # Fetch current raw configuration from the Comexio API
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

            # Update local state cache to ensure the UI remains responsive
            for m in final_data["markers"]: self.marker_states[m["id"]] = m["value"]
            for io in final_data["io"]: self.io_states[io["id"]] = io["value"]

            # --- SMART AUDIT LOGIC ---
            com_commands = final_data["webio_commands"]
            
            # 1. HA Map: Markers
            ha_map = {}
            for m in final_data["markers"]:
                ha_map[f"M{m['id']}"] = {
                    "name": f"HA {m['name']}", 
                    "type": m["type"]  # Trusting the preprocessing of api.py
                }
            # 2. HA Map: IOs
            for io in final_data["io"]:
                key = f"IO_{io['ext_name']}_{io['identifier']}"
                
                # Since api.py now provides 'is_binary', derive 
                # the audit type ('digital'/'analog') here:
                mapped_type = "digital" if io.get("is_binary") else "analog"
                
                ha_map[key] = {
                    "name": f"HA IO {io['ext_name']} {io['identifier']}", 
                    "type": mapped_type 
                }

            # 3. Comexio Map (Audit the counterpart on the server)
            com_map = {}
            for full_name, info in com_commands.items():
                cmd_id = info.get("cmdId")
                comexio_type_id = int(info.get("typeId", 1))
                # Mapping Web-IO Command TypeId: 1 = Digital, 2 = Analog
                mapped_type = "analog" if comexio_type_id == 2 else "digital"

                key = full_name
                parts = full_name.split(" ")
                if len(parts) >= 3:
                    if parts[1].startswith("M"):
                        # Marker identification via "HA M<ID> <Name>"
                        key = parts[1]
                    elif parts[1] == "IO" and len(parts) >= 4:
                        # IO identification via "HA IO <Ext> <Ident>"
                        key = f"IO_{parts[2]}_{parts[3]}"
                
                if key not in com_map:
                    com_map[key] = []
                com_map[key].append({"name": full_name, "type": mapped_type, "id": cmd_id})

            # Create a repair issue if the Web-IO class is entirely missing on the server
            if not com_map:
                is_ignored = conf.get("audit_ignored", False)
                self.last_audit_results = {}
                if not is_ignored and not self.in_sync:
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

            # Reset internal failure flag when the audit is successful
            self.last_audit_failed = False

            # Prepare payload map for future delta updates via button/repairs
            webio_name = conf.get("webio_name", "HomeAssistant")
            full_json = json.loads(self.api.generate_webio_json(self.server_id, webio_name, final_data))
            payload_map = {cmd["Name"]: cmd for cmd in full_json.get("commands", [])}

            # --- IP/Port Audit ---
            ha_address = await self.api.get_ha_address()
            com_device_ip = parsed_data.get("device_ip")
            com_device_id = parsed_data.get("device_id")
            
            ip_mismatch = False
            if com_device_id and com_device_ip:
                if com_device_ip != ha_address:
                    try:
                        ha_host, ha_port = ha_address.rsplit(":", 1)
                        com_host, com_port = com_device_ip.rsplit(":", 1)
                        
                        if ha_port != com_port:
                            # Textual deviation detected, check DNS resolution
                            ip_mismatch = True
                        else:
                            # Ports are identical, compare resolved IPs
                            def resolve(name):
                                try: return socket.gethostbyname(name)
                                except (socket.error, socket.gaierror): return name
                            
                            ha_ip = await self.hass.async_add_executor_job(resolve, ha_host)
                            com_ip = await self.hass.async_add_executor_job(resolve, com_host)
                            if ha_ip != com_ip:
                                ip_mismatch = True
                    except ValueError:
                        # Fallback on unexpected format
                        ip_mismatch = True

            # Compare HA entities with Comexio commands to find inconsistencies
            type_mismatches = []
            missing_items = []
            renamed_items = []
            orphans = []
            mismatches = set()
            
            if ip_mismatch:
                mismatches.add("ip_address")

            # Check for missing, renamed or type-mismatched items
            for key, ha in ha_map.items():
                if key not in com_map:
                    missing_items.append({"name": ha["name"], "payload": payload_map.get(ha["name"])})
                    mismatches.add(f"missing_{key}")
                else:
                    com_list = com_map[key]
                    
                    # Try to find a perfect name match first
                    best_match = None
                    for com in com_list:
                        if com["name"] == ha["name"]:
                            best_match = com
                            break
                    
                    # Fallback: if no perfect match, use the first one
                    if not best_match:
                        best_match = com_list[0]
                        
                    is_renamed = False
                    
                    # Name comparison
                    if ha["name"] != best_match["name"]:
                        renamed_items.append({"id": best_match["id"], "name": ha["name"], "payload": payload_map.get(ha["name"])})
                        mismatches.add(f"rename_{key}")
                        is_renamed = True
                        
                    if not is_renamed and ha["type"] != best_match.get("type"):
                        type_mismatches.append({"id": best_match["id"], "name": ha["name"], "payload": payload_map.get(ha["name"])})
                        mismatches.add(f"type_{key}")

                    # All other commands pointing to this key are duplicates -> Orphans
                    for com in com_list:
                        if com != best_match:
                            orphans.append({"id": com["id"], "name": com["name"]})
                            mismatches.add(f"orphan_{com['id']}")

            # Find items in Comexio that no longer exist in HA
            for key, com_list in com_map.items():
                if key not in ha_map:
                    for com in com_list:
                        orphans.append({"id": com["id"], "name": com["name"]})
                        mismatches.add(f"orphan_{com['id']}")

            self.last_audit_results = {
                "type": type_mismatches, "missing": missing_items, 
                "rename": renamed_items, "orphan": orphans,
                "ip_mismatch": ip_mismatch,
                "ha_address": ha_address,
                "com_device_id": com_device_id,
                "com_base_id": parsed_data.get("base_id")
            }

            # 📈 --- AUDIT SUMMARY LOGGING ---
            if mismatches:
                # Create a simple string representation to detect changes
                current_summary_content = f"{len(type_mismatches)}-{len(missing_items)}-{len(renamed_items)}-{len(orphans)}-{ip_mismatch}"
                
                # Only log details if the audit result differs from the previous run
                if self.last_summary_hash != current_summary_content:
                    self.last_summary_hash = current_summary_content

                    # Consolidated warning for the Home Assistant log overview
                    _LOGGER.warning("[%s] Comexio Audit Mismatch: %d issues detected (Type:%d, Missing:%d, Renames:%d, Orphans:%d, IP:%d)", 
                                    self.server_id, len(mismatches), len(type_mismatches), len(missing_items), len(renamed_items), len(orphans), 1 if ip_mismatch else 0)
                    if ip_mismatch:
                        _LOGGER.warning("[%s] Server address mismatch: HA=%s, Comexio=%s", self.server_id, ha_address, com_device_ip)

                    # Consolidated audit summary with details for each category
                    _LOGGER.info("=== ⚠️ COMEXIO AUDIT SUMMARY [%s] ===", self.server_id)
                    _LOGGER.info("🔧 Type-Mismatches (%d):", len(type_mismatches))
                    for item in type_mismatches: _LOGGER.info("   -> %s", item['name'])
                        
                    _LOGGER.info("➕ Missing Webhooks (%d):", len(missing_items))
                    for item in missing_items: _LOGGER.info("   -> %s", item['name'])
                        
                    _LOGGER.info("✏️ Renames (%d):", len(renamed_items))
                    for item in renamed_items: _LOGGER.info("   -> %s", item['name'])
                        
                    _LOGGER.info("🗑️ Orphans (%d):", len(orphans))
                    for item in orphans: _LOGGER.info("   -> %s", item['name'])

                    if ip_mismatch: 
                        _LOGGER.info("🌐 IP/Port Mismatch: Comexio expects %s, but HA is at %s", com_device_ip, ha_address)

                    _LOGGER.info("========================================")
            else:
                if self.last_summary_hash is not None:
                    _LOGGER.info("[%s] Audit successful: All systems are 100%% in sync!", self.server_id)
                self.last_summary_hash = None

            # Manage repair issues in the Home Assistant UI
            if mismatches:
                issue_data_counts = {
                    "type": len(type_mismatches),
                    "missing": len(missing_items),
                    "rename": len(renamed_items),
                    "orphan": len(orphans),
                    "ip_mismatch": 1 if ip_mismatch else 0,
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
                        "t_count": str(len(type_mismatches)),
                        "m_count": str(len(missing_items)),
                        "r_count": str(len(renamed_items)),
                        "o_count": str(len(orphans)),
                        "i_count": str(1 if ip_mismatch else 0)
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
        marker_id_str = str(marker_id)
        self.marker_states[marker_id_str] = value
        if self.data and "markers" in self.data:
            for m in self.data["markers"]:
                if str(m["id"]) == marker_id_str:
                    m["value"] = value
                    break
        self.async_set_updated_data(self.data)

    def update_io_by_name(self, ext_name, identifier, value):
        if self.data and "io" in self.data:
            for io in self.data["io"]:
                if io["ext_name"].lower() == ext_name.lower() and io["identifier"].lower() == identifier.lower():
                    self.io_states[io["id"]] = value
                    io["value"] = value
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
