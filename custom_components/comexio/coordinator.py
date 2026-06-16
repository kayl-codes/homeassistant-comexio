# Version: 0.8.0
import asyncio
import logging
import socket
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ComexioAPI
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_COVER_KEYWORDS,
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_STATISTICS_CLEANUP_IGNORED,
    CONF_USERNAME,
    DEFAULT_COVER_KEYWORDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ComexioCoordinator(DataUpdateCoordinator):
    """Coordinator to manage data fetching and state updates with Type-Audit."""

    def __init__(self, hass: HomeAssistant, api: ComexioAPI, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=None,
            config_entry=entry,
        )
        self.api: ComexioAPI = api
        self.api.config_entry = entry
        self.server_id: str = entry.data[CONF_SERVER_ID]
        self.marker_states: dict[str, Any] = {}
        self.io_states: dict[str, Any] = {}
        # O(1) lookup index for webhook updates: (ext_name_lower, identifier_lower) -> io dict
        self._io_index: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit_ignored: bool = False
        self.last_audit_failed: bool = False
        self.last_summary_hash: str | None = None
        self.in_sync: bool = False
        self.sync_error: bool = False
        self.sync_progress_text: str = "Idle"
        self.sync_progress_pct: int | None = None
        self.sync_current_step: str | None = None
        self.last_audit_results: dict[str, Any] = {}
        self.cancel_sync: bool = False
        self.entity_id_mismatches: list[dict[str, str]] = []
        self.orphaned_statistics: list[str] = []
        self.offline_extensions: set[str] = set()
        self.cover_keywords: list[str] = []
        # R4: Lock to prevent concurrent sync runs
        self._sync_lock: asyncio.Lock = asyncio.Lock()
        # R2: Suppress update_listener reload when sync triggers the options write
        self._skip_next_listener_reload: bool = False
        # R1: Track which markers/IOs received a webhook update during the last API fetch
        self._webhook_updated_markers: set[str] = set()
        self._webhook_updated_io_ids: set[str] = set()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch configuration and perform smart audit including Type-Checks."""
        if self.in_sync:
            _LOGGER.debug("[%s] Periodic audit skipped: Manual sync or repair is currently in progress", self.server_id)
            return self.data

        _LOGGER.debug("[%s] Starting periodic configuration audit to detect mismatches", self.server_id)

        # R1: Clear dirty sets before async fetches so any webhooks arriving during
        # the HTTP round-trips are tracked and win over the (older) API snapshot.
        self._webhook_updated_markers.clear()
        self._webhook_updated_io_ids.clear()

        try:
            conf = {**self.config_entry.data, **self.config_entry.options}

            # Precompute cover keywords once per update
            kw_str = str(conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS))
            self.cover_keywords = [kw.strip().lower() for kw in kw_str.split(",") if kw.strip()]

            # Fetch current raw configuration from the Comexio API
            raw_config = await self.api.get_raw_config()
            marker_data = raw_config.get("FubModules", {}).get("2", {})
            max_id = max(int(m.get("Id", 0)) for m in marker_data.values()) if marker_data else 0

            live_states = await self.api.get_live_states(max_id)
            parsed_data = self.api.parse_config(raw_config, live_states)

            import_markers = conf.get("import_markers", True)
            import_ios = conf.get("import_ios", True)

            final_data = {
                "markers": parsed_data["markers"] if import_markers else [],
                "io": parsed_data["io"] if import_ios else [],
                "webio_commands": parsed_data.get("webio_commands", {}),
            }

            # R1: Merge API snapshot with any webhook values that arrived during the fetch.
            # Webhooks that fired while awaiting get_raw_config / get_live_states already
            # updated marker_states / io_states — prefer those over the (older) API value.
            for m in final_data["markers"]:
                if m["id"] in self._webhook_updated_markers:
                    m["value"] = self.marker_states.get(m["id"], m["value"])
                else:
                    self.marker_states[m["id"]] = m["value"]

            for io in final_data["io"]:
                if io["id"] in self._webhook_updated_io_ids:
                    io["value"] = self.io_states.get(io["id"], io["value"])
                else:
                    self.io_states[io["id"]] = io["value"]

            # Rebuild O(1) lookup index for webhook IO updates
            self._io_index = {(io["ext_name"].lower(), io["identifier"].lower()): io for io in final_data["io"]}

            # Track offline extensions and log transitions
            new_offline = {io["ext_name"] for io in final_data["io"] if io.get("offline")}
            if new_offline != self.offline_extensions:
                went_offline = new_offline - self.offline_extensions
                came_online = self.offline_extensions - new_offline
                if went_offline:
                    _LOGGER.warning("[%s] Extensions went offline: %s", self.server_id, went_offline)
                if came_online:
                    _LOGGER.info("[%s] Extensions came back online: %s", self.server_id, came_online)
                self.offline_extensions = new_offline

            # --- ENTITY-ID MISMATCH DETECTION ---
            # Runs every poll so the migration button reflects the real state.
            # The ignore flag only suppresses the repair issue, never the button.
            mismatches = self.detect_entity_id_mismatches()
            if mismatches and not conf.get(CONF_ENTITY_ID_MIGRATION_IGNORED, False):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"entity_id_mismatch_{self.server_id}",
                    is_fixable=True,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="entity_id_mismatch",
                    translation_placeholders={"server_id": self.server_id, "count": str(len(mismatches))},
                    data={"entry_id": self.config_entry.entry_id, "count": len(mismatches)},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"entity_id_mismatch_{self.server_id}")

            # --- ORPHANED STATISTICS DETECTION ---
            await self.async_check_orphaned_statistics(conf)

            # --- SMART AUDIT LOGIC ---
            com_commands = final_data["webio_commands"]

            # 1. HA Map: Markers
            ha_map = {}
            for m in final_data["markers"]:
                ha_map[f"M{m['id']}"] = {
                    "name": f"HA {m['name']}",
                    "type": m["type"],  # Trusting the preprocessing of api.py
                }
            # 2. HA Map: IOs
            for io in final_data["io"]:
                key = f"IO_{io['ext_name']}_{io['identifier']}"

                # Since api.py now provides 'is_binary', derive
                # the audit type ('digital'/'analog') here:
                mapped_type = "digital" if io.get("is_binary") else "analog"

                ha_map[key] = {"name": f"HA IO {io['ext_name']} {io['identifier']}", "type": mapped_type}

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
                        self.hass,
                        DOMAIN,
                        f"sync_mismatch_{self.server_id}",
                        is_fixable=True,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="missing_webio_class",
                        translation_placeholders={"server_id": self.server_id},
                        data={
                            "entry_id": self.config_entry.entry_id,
                            "counts": {"type": 0, "missing": len(ha_map), "rename": 0, "orphan": 0, "all": len(ha_map)},
                        },
                    )
                return final_data

            # Reset internal failure flag when the audit is successful
            self.last_audit_failed = False

            # Prepare payload map for future delta updates via button/repairs
            payload_map = {cmd["Name"]: cmd for cmd in self.api.build_webio_commands(self.server_id, final_data)}

            # --- IP/Port Audit ---
            ha_address = await self.api.get_ha_address()
            com_device_ip = parsed_data.get("device_ip")
            com_device_id = parsed_data.get("device_id")

            ip_mismatch = False
            if com_device_id and com_device_ip and com_device_ip != ha_address:
                try:
                    ha_host, ha_port = ha_address.rsplit(":", 1)
                    com_host, com_port = com_device_ip.rsplit(":", 1)

                    if ha_port != com_port:
                        # Textual deviation detected, check DNS resolution
                        ip_mismatch = True
                    else:
                        # Ports are identical, compare resolved IPs
                        def resolve(name: str) -> str:
                            try:
                                return socket.gethostbyname(name)
                            except OSError:
                                return name

                        ha_ip = await self.hass.async_add_executor_job(resolve, ha_host)
                        com_ip = await self.hass.async_add_executor_job(resolve, com_host)
                        if ha_ip != com_ip:
                            ip_mismatch = True
                except ValueError:
                    # Fallback on unexpected format
                    ip_mismatch = True

            # Compare HA entities with Comexio commands to find inconsistencies
            type_mismatches: list[dict[str, Any]] = []
            missing_items: list[dict[str, Any]] = []
            renamed_items: list[dict[str, Any]] = []
            orphans: list[dict[str, Any]] = []
            mismatches: set[str] = set()

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
                        renamed_items.append(
                            {"id": best_match["id"], "name": ha["name"], "payload": payload_map.get(ha["name"])}
                        )
                        mismatches.add(f"rename_{key}")
                        is_renamed = True

                    if not is_renamed and ha["type"] != best_match.get("type"):
                        type_mismatches.append(
                            {"id": best_match["id"], "name": ha["name"], "payload": payload_map.get(ha["name"])}
                        )
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
                "type": type_mismatches,
                "missing": missing_items,
                "rename": renamed_items,
                "orphan": orphans,
                "ip_mismatch": ip_mismatch,
                "ha_address": ha_address,
                "com_device_id": com_device_id,
                "com_base_id": parsed_data.get("base_id"),
            }

            # 📈 --- AUDIT SUMMARY LOGGING ---
            if mismatches:
                # Create a simple string representation to detect changes
                current_summary_content = (
                    f"{len(type_mismatches)}-{len(missing_items)}-{len(renamed_items)}-{len(orphans)}-{ip_mismatch}"
                )

                # Only log details if the audit result differs from the previous run
                if self.last_summary_hash != current_summary_content:
                    self.last_summary_hash = current_summary_content

                    # Consolidated warning for the Home Assistant log overview
                    _LOGGER.warning(
                        "[%s] Comexio Audit Mismatch: %d issues detected (Type:%d, Missing:%d, "
                        "Renames:%d, Orphans:%d, IP:%d)",
                        self.server_id,
                        len(mismatches),
                        len(type_mismatches),
                        len(missing_items),
                        len(renamed_items),
                        len(orphans),
                        1 if ip_mismatch else 0,
                    )
                    if ip_mismatch:
                        _LOGGER.warning(
                            "[%s] Server address mismatch: HA=%s, Comexio=%s", self.server_id, ha_address, com_device_ip
                        )

                    # Consolidated audit summary with details for each category
                    _LOGGER.info("=== ⚠️ COMEXIO AUDIT SUMMARY [%s] ===", self.server_id)
                    _LOGGER.info("🔧 Type-Mismatches (%d):", len(type_mismatches))
                    for item in type_mismatches:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("➕ Missing Webhooks (%d):", len(missing_items))
                    for item in missing_items:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("✏️ Renames (%d):", len(renamed_items))
                    for item in renamed_items:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("🗑️ Orphans (%d):", len(orphans))
                    for item in orphans:
                        _LOGGER.info("   -> %s", item["name"])

                    if ip_mismatch:
                        _LOGGER.info(
                            "🌐 IP/Port Mismatch: Comexio expects %s, but HA is at %s", com_device_ip, ha_address
                        )

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
                    "all": len(mismatches),
                }

                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"sync_mismatch_{self.server_id}",
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
                        "i_count": str(1 if ip_mismatch else 0),
                    },
                    data={"entry_id": self.config_entry.entry_id, "counts": issue_data_counts},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"sync_mismatch_{self.server_id}")

            return final_data

        except Exception as e:
            _LOGGER.exception("[%s] Data fetch failed: %s", self.server_id, e)
            raise

    def update_marker(self, marker_id: str | int, value: float | int | str) -> None:
        marker_id_str = str(marker_id)
        self.marker_states[marker_id_str] = value
        self._webhook_updated_markers.add(marker_id_str)  # R1: mark as received during possible fetch
        if self.data and "markers" in self.data:
            for m in self.data["markers"]:
                if str(m["id"]) == marker_id_str:
                    m["value"] = value
                    break
        self.async_set_updated_data(self.data)

    def update_io_by_name(self, ext_name: str, identifier: str, value: float | int | str) -> None:
        key = (ext_name.lower(), identifier.lower())
        if io := self._io_index.get(key):
            self.io_states[io["id"]] = value
            io["value"] = value
            self._webhook_updated_io_ids.add(io["id"])  # R1: mark as received during possible fetch
        self.async_set_updated_data(self.data)

    async def async_config_entry_updated(self) -> None:
        """Handle config entry update (e.g. from Options Flow)."""
        _LOGGER.info("[%s] Configuration updated, reloading API settings", self.server_id)

        data = self.config_entry.data
        self.api.host = data.get(CONF_HOST)
        self.api.username = data.get(CONF_USERNAME)
        self.api.password = data.get(CONF_PASSWORD)
        self.api.api_user = data.get(CONF_API_USERNAME)
        self.api.api_pass = data.get(CONF_API_PASSWORD)

        # Re-authenticate with new credentials
        await self.api.login()

        # Trigger an immediate refresh to verify new settings
        await self.async_request_refresh()

    def detect_entity_id_mismatches(self) -> list[dict[str, str]]:
        """Scan the entity registry for entries whose entity_id contains a duplicate server_id.

        Returns a list of dicts with 'current_id' and 'corrected_id'.
        """
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self.hass)
        server_slug = self.server_id.lower()
        double_prefix = f"comexio_{server_slug}_{server_slug}_"
        single_prefix = f"comexio_{server_slug}_"

        mismatches: list[dict[str, str]] = []
        for entity_entry in er.async_entries_for_config_entry(ent_reg, self.config_entry.entry_id):
            platform, slug = entity_entry.entity_id.split(".", 1)
            if slug.startswith(double_prefix):
                corrected_slug = single_prefix + slug[len(double_prefix) :]
                corrected_id = f"{platform}.{corrected_slug}"
                if not ent_reg.async_get(corrected_id):
                    mismatches.append(
                        {
                            "current_id": entity_entry.entity_id,
                            "corrected_id": corrected_id,
                        }
                    )

        self.entity_id_mismatches = mismatches
        return mismatches

    async def async_check_orphaned_statistics(self, conf: dict[str, Any]) -> None:
        """Detect orphaned long-term statistics and manage the corresponding repair issue."""
        orphans = await self.async_detect_orphaned_statistics()
        issue_id = f"statistics_orphaned_{self.server_id}"
        if orphans and not conf.get(CONF_STATISTICS_CLEANUP_IGNORED, False):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="statistics_orphaned",
                translation_placeholders={"server_id": self.server_id, "count": str(len(orphans))},
                data={"entry_id": self.config_entry.entry_id, "count": len(orphans)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_detect_orphaned_statistics(self) -> list[str]:
        """Return statistic_ids for this integration that no longer have a matching entity.

        Statistics of live entities (still in the registry) are never flagged.
        """
        from homeassistant.helpers import entity_registry as er

        if "recorder" not in self.hass.config.components:
            self.orphaned_statistics = []
            return []

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import list_statistic_ids
        except ImportError:
            _LOGGER.warning("[%s] Recorder statistics API not available, skipping orphan detection", self.server_id)
            self.orphaned_statistics = []
            return []

        try:
            instance = get_instance(self.hass)
            all_stats = await instance.async_add_executor_job(list_statistic_ids, self.hass)
        except Exception:
            _LOGGER.exception("[%s] Failed to list statistic IDs", self.server_id)
            self.orphaned_statistics = []
            return []

        ent_reg = er.async_get(self.hass)
        server_slug = self.server_id.lower()
        # Match all historical naming patterns for this server_id:
        # - current:  sensor.comexio_{server_id}_...
        # - legacy:   sensor.comexio_server_{server_id}_...  (pre-sub-device-grouping naming)
        prefixes = (
            f"sensor.comexio_{server_slug}_",
            f"sensor.comexio_server_{server_slug}_",
        )

        # Accept any source — the entity-registry check is the authoritative safety gate.
        orphans = [
            stat["statistic_id"]
            for stat in all_stats
            if any(stat["statistic_id"].startswith(p) for p in prefixes)
            and ent_reg.async_get(stat["statistic_id"]) is None
        ]

        _LOGGER.debug(
            "[%s] Orphaned statistics detected: %d (total recorder stats scanned: %d)",
            self.server_id,
            len(orphans),
            len(all_stats),
        )

        self.orphaned_statistics = orphans
        return orphans

    def async_migrate_entity_ids(self) -> int:
        """Migrate entity_ids by removing the duplicate server_id prefix. Returns count of migrated IDs."""
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self.hass)
        migrated = 0
        for mismatch in self.entity_id_mismatches:
            try:
                ent_reg.async_update_entity(mismatch["current_id"], new_entity_id=mismatch["corrected_id"])
                migrated += 1
            except Exception:
                _LOGGER.exception("[%s] Failed to migrate entity_id %s", self.server_id, mismatch["current_id"])
        self.entity_id_mismatches = []
        _LOGGER.info("[%s] Entity ID migration complete: %d IDs updated", self.server_id, migrated)
        return migrated
