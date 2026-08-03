# Version: 0.8.0
import asyncio
import contextlib
from datetime import datetime, timedelta
import logging
import re
import socket
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ComexioAPI
from .const import (
    BUS_LOAD_FAIL_STREAK_THRESHOLD,
    BUS_LOAD_POLL_INTERVAL_SEC,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_COVER_KEYWORDS,
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    CONF_FUNCTION_PLAN_FUB_ID,
    CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_FUNCTION_PLAN_PLAN_PREFIX,
    CONF_HOST,
    CONF_IGNORED_MARKERS,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_STATISTICS_CLEANUP_IGNORED,
    CONF_USERNAME,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    DEFAULT_FUNCTION_PLAN_PLAN_PREFIX,
    DOMAIN,
    FIRMWARE_CHECK_HOUR,
    FIRMWARE_CHECK_MINUTE,
    FUNCTION_PLAN_FUB_ID_AUTO,
    FUNCTION_PLAN_LAYOUT_COMMENT_Y,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASSES,
    bus_load_signal,
    fw_update_signal,
    io_audit_key,
    io_column_rows,
    is_io_audit_key,
    snap_to_grid,
    webio_class_label,
)
from .function_plan_backup import FunctionPlanBackupManager, retention_cutoff
from .function_plan_catalog import FunctionPlanCatalogManager

_LOGGER = logging.getLogger(__name__)

# Matches the "(ID <n>)" suffix format.format_plan_label() appends to a plan select-option
# label, letting get_active_function_plan_fub_id() resolve the fub_id directly instead of
# matching by (non-unique) plan name.
_PLAN_LABEL_ID_SUFFIX_RE = re.compile(r"\(ID (\d+)\)\s*$")

# Managed cluster plans are always created as A3 — big enough for a full marker cluster or
# two/three extension columns, while still printable.
_MANAGED_PLAN_PAPER = "A3"
_ORIENT_LANDSCAPE = "landscape"
_ORIENT_PORTRAIT = "portrait"
_PAPER_NAME_BY_ID = {"2": "A3", "3": "A4", "4": "A5"}


async def _device_ip_mismatch(hass: HomeAssistant, ha_address: str, com_ip: str | None, com_dev_id: str | None) -> bool:
    """Whether a Web-IO device's recorded IP/port has drifted from this HA instance's current address."""
    if not (com_dev_id and com_ip and com_ip != ha_address):
        return False
    try:
        ha_host, ha_port = ha_address.rsplit(":", 1)
        com_host, com_port = com_ip.rsplit(":", 1)

        if ha_port != com_port:
            # Textual deviation detected, check DNS resolution
            return True

        # Ports are identical, compare resolved IPs
        def resolve(name: str) -> str:
            try:
                return socket.gethostbyname(name)
            except OSError:
                return name

        ha_ip = await hass.async_add_executor_job(resolve, ha_host)
        com_resolved_ip = await hass.async_add_executor_job(resolve, com_host)
        return ha_ip != com_resolved_ip
    except ValueError:
        # Fallback on unexpected format
        return True


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
        self.function_plan_catalog = FunctionPlanCatalogManager(hass, self.server_id)
        self.function_plan_backup = FunctionPlanBackupManager(hass, self.server_id)
        self._function_plan_backup_lock: asyncio.Lock = asyncio.Lock()
        self.last_changed_plans: list[dict[str, Any]] = []
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
        self.offline_entity_statistic_ids: set[str] = set()
        self.offline_extensions: set[str] | None = None
        self._extension_offline_issue_active: bool = False
        self.cover_keywords: list[str] = []
        # R4: Lock to prevent concurrent sync runs
        self._sync_lock: asyncio.Lock = asyncio.Lock()
        # R2: Suppress update_listener reload when an internal write (sync, ignored-marker
        # cleanup, plan_map persist, ...) triggers a reload of its own. Stores the exact
        # options snapshot just written rather than a bare bool, so a listener run only skips
        # if entry.options still matches what this coordinator wrote — an unrelated write
        # (e.g. a concurrent user options-flow save) landing in between is detected and
        # reloads anyway instead of being silently swallowed. See
        # request_options_update_without_reload().
        self._skip_next_listener_reload_options: dict[str, Any] | None = None
        # R1: Track which markers/IOs received a webhook update during the last API fetch
        self._webhook_updated_markers: set[str] = set()
        self._webhook_updated_io_ids: set[str] = set()
        # Extension firmware check: {name -> raw entry} from the last successful
        # checkextension_fwupdate call (see async_start_firmware_update_check). Empty until
        # the first check actually runs, which only happens once per comexio_version change.
        # Persisted (see async_load_extension_firmware) so a restart doesn't reset update.*
        # entities to Unknown and doesn't re-arm the version gate for no reason — the check
        # itself is too risky to repeat just because the process restarted.
        self.extension_firmware: dict[str, dict[str, Any]] = {}
        self._last_checked_fw_version: str | None = None
        self._firmware_store: Store = Store(hass, 1, f"{DOMAIN}_extension_firmware_{self.server_id}")
        # Bus workload monitoring: latest reading from the independent fast poll
        # (see async_start_bus_load_poll / _async_bus_load_tick). None until the first tick.
        self.bus_workload: int | None = None
        self.bus_sd_card: bool | None = None
        self._bus_load_fail_streak = 0

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

            # async_update_from_raw_config never raises (own contract, enforced internally) —
            # no local guard needed here.
            await self.function_plan_catalog.async_update_from_raw_config(raw_config, self.api.comexio_version)

            import_markers = conf.get("import_markers", True)
            import_ios = conf.get("import_ios", True)

            final_data = {
                "markers": parsed_data["markers"] if import_markers else [],
                "io": parsed_data["io"] if import_ios else [],
                "webio_commands": parsed_data.get("webio_commands", {}),
                "webio_devices": parsed_data.get("webio_devices", {}),
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
            if self.offline_extensions is None:
                # Startup: initialize silently — modules may be intentionally decommissioned.
                if new_offline:
                    _LOGGER.info("[%s] Extensions already offline at startup: %s", self.server_id, new_offline)
                self.offline_extensions = new_offline
            elif new_offline != self.offline_extensions:
                self._handle_offline_extension_transitions(new_offline)

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

            # --- IGNORED MARKERS AUDIT ---
            await self.async_check_ignored_markers(conf, final_data)

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
                key = io_audit_key(io["ext_name"], io["identifier"])

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
                        key = io_audit_key(parts[2], parts[3])

                if key not in com_map:
                    com_map[key] = []
                com_map[key].append(
                    {
                        "name": full_name,
                        "type": mapped_type,
                        "id": cmd_id,
                        "webio_class": info.get("webioClass"),
                    }
                )

            # Create a repair issue if either Web-IO class is entirely missing on the server.
            # Checked directly against the resolved device_id (not "com_map empty") so a
            # half-missing setup (e.g. only the Marker class deleted) is still caught — the
            # normal sync_mismatch flow below would otherwise try to save_single_command
            # against a dev_id of None for that class.
            webio_devices = parsed_data.get("webio_devices", {})
            missing_classes = [cls for cls in WEBIO_CLASSES if not webio_devices.get(cls, {}).get("device_id")]
            if missing_classes:
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
                        translation_placeholders={
                            "server_id": self.server_id,
                            "missing_classes": ", ".join(webio_class_label(c) for c in missing_classes),
                        },
                        data={
                            "entry_id": self.config_entry.entry_id,
                            "missing_classes": missing_classes,
                        },
                    )
                return final_data

            # Reset internal failure flag when the audit is successful
            self.last_audit_failed = False

            # Prepare payload map for future delta updates via button/repairs
            payload_map = {cmd["Name"]: cmd for cmd in self.api.build_webio_commands(self.server_id, final_data)}

            # --- IP/Port Audit --- (checked independently per Web-IO class — marker and IO
            # devices can in theory drift out of sync with each other)
            ha_address = await self.api.get_ha_address()

            webio_device_audit: dict[str, dict[str, Any]] = {}
            for cls in WEBIO_CLASSES:
                dev = webio_devices.get(cls, {})
                webio_device_audit[cls] = {
                    "device_id": dev.get("device_id"),
                    "base_id": dev.get("base_id"),
                    "device_ip": dev.get("device_ip"),
                    "ip_mismatch": await _device_ip_mismatch(
                        self.hass, ha_address, dev.get("device_ip"), dev.get("device_id")
                    ),
                }
            ip_mismatch = any(v["ip_mismatch"] for v in webio_device_audit.values())

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
                key_class = WEBIO_CLASS_IO if is_io_audit_key(key) else WEBIO_CLASS_MARKER
                if key not in com_map:
                    missing_items.append(
                        {"name": ha["name"], "payload": payload_map.get(ha["name"]), "webio_class": key_class}
                    )
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
                    match_class = best_match.get("webio_class") or key_class

                    # Name comparison
                    if ha["name"] != best_match["name"]:
                        renamed_items.append(
                            {
                                "id": best_match["id"],
                                "name": ha["name"],
                                "payload": payload_map.get(ha["name"]),
                                "webio_class": match_class,
                            }
                        )
                        mismatches.add(f"rename_{key}")
                        is_renamed = True

                    if not is_renamed and ha["type"] != best_match.get("type"):
                        type_mismatches.append(
                            {
                                "id": best_match["id"],
                                "name": ha["name"],
                                "payload": payload_map.get(ha["name"]),
                                "webio_class": match_class,
                            }
                        )
                        mismatches.add(f"type_{key}")

                    # All other commands pointing to this key are duplicates -> Orphans
                    for com in com_list:
                        if com != best_match:
                            orphans.append(
                                {"id": com["id"], "name": com["name"], "webio_class": com.get("webio_class")}
                            )
                            mismatches.add(f"orphan_{com['id']}")

            # Find items in Comexio that no longer exist in HA
            for key, com_list in com_map.items():
                if key not in ha_map:
                    for com in com_list:
                        orphans.append({"id": com["id"], "name": com["name"], "webio_class": com.get("webio_class")})
                        mismatches.add(f"orphan_{com['id']}")

            self.last_audit_results = {
                "type": type_mismatches,
                "missing": missing_items,
                "rename": renamed_items,
                "orphan": orphans,
                "ip_mismatch": ip_mismatch,
                "ha_address": ha_address,
                "webio_devices": webio_device_audit,
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
                        mismatched = {cls: v["device_ip"] for cls, v in webio_device_audit.items() if v["ip_mismatch"]}
                        _LOGGER.warning(
                            "[%s] Server address mismatch: HA=%s, Comexio=%s", self.server_id, ha_address, mismatched
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
                        mismatched_ips = {
                            cls: v["device_ip"] for cls, v in webio_device_audit.items() if v["ip_mismatch"]
                        }
                        _LOGGER.info(
                            "🌐 IP/Port Mismatch: Comexio expects %s, but HA is at %s", mismatched_ips, ha_address
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

            # Function Plan backup: load all plan wirings + rotate auto backups in the background.
            # Entry-scoped task (cancelled on unload/reload) so neither startup nor the poll
            # cycle is blocked by the ~0.5s/plan bulk load. Skip spawning a new task entirely
            # while a previous cycle is still running, instead of creating one just to have it
            # return immediately on the lock check.
            if not self._function_plan_backup_lock.locked():
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_function_plan_backup_cycle(),
                    name=f"comexio_{self.server_id}_function_plan_backup",
                )

            return final_data

        except Exception as e:
            _LOGGER.exception("[%s] Data fetch failed: %s", self.server_id, e)
            raise

    async def _async_function_plan_backup_cycle(self) -> None:
        """Load all plan wirings and rotate auto backups (runs as entry-scoped background task)."""
        if self._function_plan_backup_lock.locked():
            _LOGGER.debug("[%s] Function Plan backup cycle skipped: previous run still in progress", self.server_id)
            return
        async with self._function_plan_backup_lock:
            # Reset up front so a failed/empty cycle never leaves a stale result from a
            # previous poll behind — ComexioPlanChangedSensor must reflect *this* cycle only.
            self.last_changed_plans = []
            try:
                plans = await self.api.function_plan_load_all_plans()
            except Exception:
                _LOGGER.exception("[%s] Function Plan bulk load failed — keeping previous snapshot", self.server_id)
                return
            if not plans:
                return
            fub_data = self.api.fub_data
            plan_format = self._current_plan_format(fub_data)
            markers_by_id, webio_by_id, ios_by_id = self.function_plan_label_maps()
            try:
                self.last_changed_plans = await self.function_plan_backup.async_auto_backup(
                    plans,
                    fub_data,
                    plan_format,
                    self.api.comexio_version,
                    markers_by_id,
                    webio_by_id,
                    ios_by_id,
                )
                await self._async_refresh_service_descriptions()
            except Exception:
                _LOGGER.exception("[%s] Function Plan auto backup failed", self.server_id)
            try:
                await self.function_plan_backup.async_backfill_paper_metadata(fub_data, plan_format)
            except Exception:
                _LOGGER.exception("[%s] Function Plan paper/DPI backfill failed", self.server_id)
            try:
                retention_months = self.config_entry.options.get(
                    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS, DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS
                )
                purged = await self.function_plan_backup.async_purge_orphaned(
                    fub_data, cutoff=retention_cutoff(int(retention_months))
                )
                if purged:
                    _LOGGER.info(
                        "[%s] Function Plan backup: purged %d orphaned identity(ies) older than %s month(s)",
                        self.server_id,
                        len(purged),
                        retention_months,
                    )
            except Exception:
                _LOGGER.exception("[%s] Function Plan orphaned-backup purge failed", self.server_id)
            # Refresh diagnostic entities (backup summary sensor) without a full data update
            self.async_update_listeners()

    async def _async_refresh_service_descriptions(self) -> None:
        """Refresh services.yaml's dynamic dropdowns after a new backup was just captured.

        The callback is registered by services.py (async_setup_services); it may not exist
        yet on the very first backup cycle right after HA startup.
        """
        if refresh := self.hass.data.get(DOMAIN, {}).get("_refresh_service_descriptions"):
            await refresh()

    def _current_plan_format(self, fub_data: dict) -> dict[str, tuple[str, int, str]]:
        """Return {fub_id_str: (paper, dpi, orientation)} for every plan currently in fub_data."""
        return {
            key: (
                self.api.get_fub_paper_format(int(key)),
                self.api.get_fub_dpi(int(key)),
                self.api.get_fub_orientation(int(key)),
            )
            for key in fub_data
        }

    async def async_function_plan_change_backup(
        self, fub_id: int, operation: str, plan_data: dict | None = None
    ) -> None:
        """Store a pre-mutation snapshot of one plan. Never raises — a failed backup
        is logged loudly but must not block the operation itself."""
        try:
            if plan_data is None:
                plan_data = await self.api.logikplan_load_elements(fub_id)
            if not plan_data:
                _LOGGER.warning("[%s] Pre-change backup skipped: plan %s could not be loaded", self.server_id, fub_id)
                return
            plan_name = self.api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
            paper = self.api.get_fub_paper_format(fub_id)
            dpi = self.api.get_fub_dpi(fub_id)
            orientation = self.api.get_fub_orientation(fub_id)
            markers_by_id, webio_by_id, ios_by_id = self.function_plan_label_maps()
            await self.function_plan_backup.async_change_backup(
                fub_id,
                plan_data,
                plan_name,
                operation,
                paper,
                dpi,
                orientation,
                self.api.comexio_version,
                markers_by_id,
                webio_by_id,
                ios_by_id,
            )
            await self._async_refresh_service_descriptions()
        except Exception:
            _LOGGER.exception(
                "[%s] Pre-change Function Plan backup failed (fub=%s, op=%s)", self.server_id, fub_id, operation
            )

    def get_active_function_plan_fub_id(self) -> int | None:
        """Return the fub_id for the currently selected 'Function Plans' plan, or None.

        Shared by select.py (backup selector) — kept on the coordinator rather than as a
        free function in an entity-platform module. Parses the fub_id directly out of the
        selector's "<name> (ID <n>)" label (see services.format_plan_label) rather than
        matching on the bare name, since plan names aren't unique in Comexio.

        At startup the select entity is not in the state machine yet (the coordinator's
        first refresh runs before the platforms are set up), so the choice persisted in
        entry.options by the selector is used as the fallback.
        """
        from homeassistant.helpers import entity_registry as er

        uid = f"comexio_{self.server_id}_logikplan_plan_selector"
        select_eid = er.async_get(self.hass).async_get_entity_id("select", DOMAIN, uid) or ""
        lp_state = self.hass.states.get(select_eid)
        if not lp_state or lp_state.state in ("unavailable", "unknown"):
            return self.persisted_function_plan_fub_id()
        if match := _PLAN_LABEL_ID_SUFFIX_RE.search(lp_state.state):
            return int(match.group(1))
        return next((int(fid) for fid, fi in self.api.fub_data.items() if fi.get("Name") == lp_state.state), None)

    def persisted_function_plan_fub_id(self) -> int | None:
        """fub_id the 'Function Plans' selector last persisted, or None if unset/legacy 'auto'."""
        saved = self.config_entry.options.get(CONF_FUNCTION_PLAN_FUB_ID)
        if saved in (None, "", FUNCTION_PLAN_FUB_ID_AUTO):
            return None
        try:
            return int(saved)
        except (TypeError, ValueError):
            _LOGGER.warning("[%s] Ignoring unparseable persisted function plan id %r", self.server_id, saved)
            return None

    def function_plan_label_maps(self) -> tuple[dict, dict, dict]:
        """Lookup dicts (marker/WebIO/IO id -> {"name": ..., ...}) for backup snapshot labeling.

        webio_by_id is built from the live webio_commands map (HA-managed Web-IO classes
        only) — a WebIO block wired into a plan from an unrelated, non-HA-managed Comexio
        device falls back to the generic "WebIO ref=<id>" label in resolve_element_label.
        """
        data = self.data or {}
        markers_by_id = {str(m["id"]): m for m in data.get("markers", [])}
        webio_by_id: dict[str, Any] = {}
        for name, cmd in data.get("webio_commands", {}).items():
            w_id = cmd.get("webIoId")
            if w_id is not None:
                webio_by_id[str(w_id)] = {"name": name, "analog": cmd.get("typeId") == 2}
        ios_by_id = {str(io["id"]): io for io in data.get("io", [])}
        return markers_by_id, webio_by_id, ios_by_id

    async def async_repoint_function_plan_fub_id(self, plan_name: str, old_fub_id: int, new_fub_id: int) -> list[str]:
        """Point any config that references old_fub_id by name at the plan's new fub_id.

        Called after a restore-as-new gives a deleted/reassigned plan a fresh fub_id.
        Updates both places a fub_id can be pinned: the managed cluster plan map and the
        single-plan selector's persisted choice (CONF_FUNCTION_PLAN_FUB_ID) — a restored
        plan is otherwise silently dropped from cluster management, or leaves the selector
        pointing at a fub_id that no longer exists.
        Returns a list of human-readable descriptions of what was updated, for the
        restore service's response message.
        """
        updated: list[str] = []
        new_options = dict(self.config_entry.options)
        plan_map = new_options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        if plan_map.get(plan_name) == old_fub_id:
            new_options[CONF_FUNCTION_PLAN_PLAN_MAP] = {**plan_map, plan_name: new_fub_id}
            updated.append(f"cluster plan map entry '{plan_name}' → fub_id {new_fub_id}")
        if str(new_options.get(CONF_FUNCTION_PLAN_FUB_ID)) == str(old_fub_id):
            new_options[CONF_FUNCTION_PLAN_FUB_ID] = new_fub_id
            updated.append(f"selected function plan → fub_id {new_fub_id}")
        if updated:
            self.request_options_update_without_reload(new_options)
        return updated

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

    async def async_load_extension_firmware(self) -> None:
        """Restore the last checked firmware snapshot from disk (called once at setup).

        Without this, every HA restart would reset extension_firmware to {} and
        _last_checked_fw_version to None — the update.* entities would flip back to
        Unknown, and the version gate would spuriously re-arm (a restart is not a real
        version change), triggering an unnecessary extra run of the risky check at the
        next nightly window.
        """
        stored = await self._firmware_store.async_load()
        if not stored:
            return
        self.extension_firmware = stored.get("extension_firmware", {})
        self._last_checked_fw_version = stored.get("last_checked_fw_version")

    def async_start_firmware_update_check(self):
        """Start the nightly firmware-check gate; returns the cancel callback.

        Fires once a day at FIRMWARE_CHECK_HOUR:FIRMWARE_CHECK_MINUTE, but the actual
        (output-interrupting) API call only runs when api.comexio_version has changed since
        the last successful check — see _async_firmware_check_tick.
        """
        return async_track_time_change(
            self.hass, self._async_firmware_check_tick, hour=FIRMWARE_CHECK_HOUR, minute=FIRMWARE_CHECK_MINUTE, second=0
        )

    async def _async_firmware_check_tick(self, _now: datetime | None = None, force: bool = False) -> bool:
        """Run the extension firmware check, but only if the IO-Server version moved on.

        A base firmware update makes a matching extension firmware update likely, so this
        piggybacks on api.comexio_version (already tracked for the catalog cache) instead of
        polling the risky checkextension_fwupdate endpoint on every nightly window. `force`
        bypasses the version gate (manual button press) — the endpoint's physical risk itself
        is unchanged either way. Returns whether the API call actually ran.
        """
        current_version = self.api.comexio_version
        if not force and (current_version is None or current_version == self._last_checked_fw_version):
            return False
        data = await self.api.check_extension_firmware()
        if not data:
            return False
        self.extension_firmware = {item["name"]: item for item in data if "name" in item}
        self._last_checked_fw_version = current_version
        await self._firmware_store.async_save(
            {"extension_firmware": self.extension_firmware, "last_checked_fw_version": self._last_checked_fw_version}
        )
        async_dispatcher_send(self.hass, fw_update_signal(self.server_id))
        return True

    async def async_force_firmware_check(self) -> bool:
        """Force-run the extension firmware check outside its nightly window (manual button).

        Bypasses the comexio_version gate but not the endpoint's physical risk — Comexio
        warns it can briefly interrupt extension outputs. Returns whether the API call ran.
        """
        return await self._async_firmware_check_tick(force=True)

    def async_start_bus_load_poll(self):
        """Start the independent fast bus-workload poll; returns the cancel callback.

        Also fires one immediate tick so the diagnostics aren't stuck at "Unknown" for up
        to BUS_LOAD_POLL_INTERVAL_SEC after setup/reload (async_track_time_interval only
        fires after the first interval elapses).
        """
        self.hass.async_create_task(self._async_bus_load_tick(), name=f"comexio_{self.server_id}_bus_load_initial_tick")
        return async_track_time_interval(
            self.hass, self._async_bus_load_tick, timedelta(seconds=BUS_LOAD_POLL_INTERVAL_SEC)
        )

    async def _async_bus_load_tick(self, _now: datetime | None = None) -> None:
        """Poll internal bus workload (%) + SD-card presence on a fast, independent cadence.

        Deliberately does NOT call async_set_updated_data — at a 10s cadence that would
        notify every coordinator entity for no benefit. Only
        the dedicated bus-load sensors listen for this dispatcher signal.

        Values are type-checked (not just presence-checked) since they come straight from
        an external HTTP endpoint — an unexpected type falls back to None (HA renders that
        as "unknown") rather than exposing a wrongly-typed state. `workload` accepts int or
        float (Comexio's JSON serializer isn't guaranteed to always emit whole numbers) but
        never bool, since bool is a native int subclass and would otherwise slip through as
        0/1.

        After BUS_LOAD_FAIL_STREAK_THRESHOLD consecutive failed ticks, the readings are
        reset to None instead of silently keeping the last successful value forever — a
        persistent fetch failure should surface as "unknown", not a stale-but-plausible
        percentage.
        """
        result = await self.api.get_bus_workload()
        if not result:
            self._bus_load_fail_streak += 1
            if self._bus_load_fail_streak == BUS_LOAD_FAIL_STREAK_THRESHOLD:
                _LOGGER.warning(
                    "Bus workload poll failed %s times in a row; diagnostics reset to unknown",
                    self._bus_load_fail_streak,
                )
                self.bus_workload = None
                self.bus_sd_card = None
                async_dispatcher_send(self.hass, bus_load_signal(self.server_id))
            return
        self._bus_load_fail_streak = 0
        workload = result.get("workload")
        if isinstance(workload, bool) or not isinstance(workload, (int, float)):
            self.bus_workload = None
        else:
            self.bus_workload = int(workload)
        sd_card = result.get("sd_card")
        self.bus_sd_card = sd_card if isinstance(sd_card, bool) else None
        async_dispatcher_send(self.hass, bus_load_signal(self.server_id))

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

    def _handle_offline_extension_transitions(self, new_offline: set[str]) -> None:
        """Log transitions and manage the extension-offline HA Repair issue."""
        went_offline = new_offline - self.offline_extensions  # type: ignore[operator]
        came_online = self.offline_extensions - new_offline  # type: ignore[operator]
        if went_offline:
            _LOGGER.warning("[%s] Extensions went offline: %s", self.server_id, went_offline)
            self._extension_offline_issue_active = True
        if came_online:
            _LOGGER.info("[%s] Extensions came back online: %s", self.server_id, came_online)
        if self._extension_offline_issue_active:
            if new_offline:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"extension_offline_{self.server_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="extension_offline",
                    translation_placeholders={
                        "server_id": self.server_id,
                        "extensions": ", ".join(sorted(new_offline)),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"extension_offline_{self.server_id}")
                self._extension_offline_issue_active = False
        self.offline_extensions = new_offline

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

    def request_options_update_without_reload(self, new_options: dict[str, Any]) -> None:
        """Persist new_options to the config entry, marking the listener-triggered reload
        it schedules as redundant (see R2) — used when the caller either doesn't need a
        reload at all, or is about to trigger its own explicit one. update_listener only
        honors the skip if entry.options still equals new_options when it runs; if another
        write landed in between (e.g. a concurrent user options-flow save), it reloads
        anyway instead of silently dropping that write.
        """
        self._skip_next_listener_reload_options = new_options.copy()
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    def take_pending_reload_skip_options(self) -> dict[str, Any] | None:
        """Return and clear the options snapshot recorded by
        request_options_update_without_reload(), if any (see R2). One-shot: the snapshot is
        consumed on read so a stale value can't leak into a later, unrelated listener run.
        """
        options = self._skip_next_listener_reload_options
        self._skip_next_listener_reload_options = None
        return options

    async def async_check_ignored_markers(self, conf: dict[str, Any], final_data: dict[str, Any]) -> None:
        """Detect invalid/valid ignored marker IDs and manage repair issues."""
        ignored_raw = conf.get(CONF_IGNORED_MARKERS, "").strip()
        invalid_ids: list[int] = []
        valid_ids: list[int] = []

        if not ignored_raw:
            ir.async_delete_issue(self.hass, DOMAIN, f"ignored_markers_invalid_{self.server_id}")
            ir.async_delete_issue(self.hass, DOMAIN, f"ignored_markers_cleanup_{self.server_id}")
            return

        markers_by_id = {int(m["id"]): m for m in final_data.get("markers", [])}

        for token in ignored_raw.replace(";", ",").split(","):
            stripped = token.strip()
            if not stripped:
                continue
            try:
                marker_id = int(stripped)
                if marker_id in markers_by_id:
                    valid_ids.append(marker_id)
                else:
                    invalid_ids.append(marker_id)
            except ValueError:
                _LOGGER.warning("[%s] ignored_markers contains non-integer token: '%s'", self.server_id, stripped)

        # Issue 1: Invalid marker IDs
        invalid_issue_id = f"ignored_markers_invalid_{self.server_id}"
        if invalid_ids:
            invalid_ids_str = ", ".join(f"M{mid}" for mid in invalid_ids)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                invalid_issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="ignored_markers_invalid",
                translation_placeholders={"server_id": self.server_id, "ids": invalid_ids_str},
                data={"entry_id": self.config_entry.entry_id, "invalid_ids": invalid_ids},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, invalid_issue_id)

        # Issue 2: Valid marker IDs ready for cleanup
        cleanup_issue_id = f"ignored_markers_cleanup_{self.server_id}"
        if valid_ids:
            valid_ids_str = ", ".join(f"M{mid}" for mid in valid_ids)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                cleanup_issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.INFO,
                translation_key="ignored_markers_cleanup",
                translation_placeholders={"server_id": self.server_id, "ids": valid_ids_str},
                data={"entry_id": self.config_entry.entry_id, "ignored_marker_ids": valid_ids},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, cleanup_issue_id)

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
            and stat["statistic_id"] not in self.offline_entity_statistic_ids
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

    # --- MANAGED CLUSTER PLANS ---

    def _function_plan_prefix(self) -> str:
        """Configured name prefix of the HA-managed cluster plans (default 'HA')."""
        return self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX)

    def _function_plan_cluster_size(self) -> int:
        """Configured maximum number of element pairs per managed cluster plan."""
        return int(
            self.config_entry.options.get(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
            )
        )

    @staticmethod
    def _cluster_plan_name(marker_id: int, prefix: str, cluster_size: int) -> str:
        """Name of the managed cluster plan a marker belongs to (deterministic bucket math)."""
        start = ((marker_id - 1) // cluster_size) * cluster_size + 1
        return f"{prefix} - Marker [{start}-{start + cluster_size - 1}]"

    def expected_marker_cluster_name(self, marker_id: int) -> str:
        """Deterministic cluster-plan name a marker ID currently maps to (config-aware)."""
        return self._cluster_plan_name(marker_id, self._function_plan_prefix(), self._function_plan_cluster_size())

    def io_cluster_plan_contains(self, live_plan_name: str, ext_name: str) -> bool:
        """True if a live plan name still parses as a managed IO cluster plan naming ext_name.

        Used right before writing IO pairs to catch a plan that was renamed/repurposed
        between resolve_io_clusters() resolving its fub_id and the actual write (e.g. its
        fub_id got reused by an unrelated create elsewhere in the meantime) — see
        expected_marker_cluster_name() for the marker-side counterpart.
        """
        members = self._io_plan_members(live_plan_name, self._function_plan_prefix())
        return members is not None and ext_name in members

    async def resolve_marker_clusters(self, marker_ids: list[int]) -> tuple[dict[int, list[int]], set[int]]:
        """Group marker IDs by cluster plan and resolve/create each plan.

        Returns ({fub_id: [marker_ids_in_cluster]}, {fub_ids of freshly created plans}).
        Lookup order per cluster: CONF_FUNCTION_PLAN_PLAN_MAP cache → name scan → create.
        """
        prefix = self._function_plan_prefix()
        cluster_size = self._function_plan_cluster_size()
        fub_data = self.api.fub_data

        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map: dict[str, int] = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
        stale = self._stale_plan_map_entries(fub_data)

        clusters: dict[str, list[int]] = {}
        for mid in marker_ids:
            clusters.setdefault(self._cluster_plan_name(mid, prefix, cluster_size), []).append(mid)

        result: dict[int, list[int]] = {}
        created_plans: set[int] = set()
        plan_map_updates: dict[str, int] = {}

        for plan_name, cluster_ids in clusters.items():
            fub_id, created = await self._resolve_single_cluster_plan(plan_name, plan_map, fub_data)
            if fub_id is None:
                _LOGGER.error("[%s] Failed to resolve/create cluster plan '%s'", self.server_id, plan_name)
                continue
            result[fub_id] = cluster_ids
            if created:
                created_plans.add(fub_id)
            plan_map_updates[plan_name] = fub_id

        if plan_map_updates or stale:
            await self._persist_plan_map(plan_map_updates, removals=set(stale))

        return result, created_plans

    async def _resolve_single_cluster_plan(
        self, plan_name: str, plan_map: dict[str, int], fub_data: dict
    ) -> tuple[int | None, bool]:
        """Find or create a single cluster plan by name. Returns (fub_id, freshly_created)."""
        cached = plan_map.get(plan_name)
        if cached is not None and fub_data.get(str(cached), {}).get("Name") == plan_name:
            return cached, False

        for fid_str, fub_info in fub_data.items():
            if fub_info.get("Name") == plan_name:
                _LOGGER.info("[%s] Found cluster plan '%s' (fub_id=%s)", self.server_id, plan_name, fid_str)
                return int(fid_str), False

        fub_id = await self._create_managed_plan(plan_name)
        return fub_id, fub_id is not None

    async def _create_managed_plan(self, plan_name: str, orientation: str = _ORIENT_LANDSCAPE) -> int | None:
        """Create a managed A3 cluster plan carrying the 'administrated by HA' comment."""
        _LOGGER.info("[%s] Creating cluster plan '%s'", self.server_id, plan_name)
        fub_id = await self.api.create_fup(
            plan_name=plan_name, paper_format=_MANAGED_PLAN_PAPER, orientation=orientation
        )
        if fub_id is None:
            _LOGGER.error("[%s] Failed to create cluster plan '%s'", self.server_id, plan_name)
            return None

        # paper_name/orientation override is required: the freshly created plan is not in the
        # cached $Fubs data yet, so bounds lookup by fub_id would fall back to A4 landscape.
        x_max, _ = self.api.get_fub_canvas_bounds(fub_id, paper_name=_MANAGED_PLAN_PAPER, orientation=orientation)
        await self.api.function_plan_add_comment_element(
            fub_id=fub_id,
            text=FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
            x=snap_to_grid(x_max / 2),
            y=FUNCTION_PLAN_LAYOUT_COMMENT_Y,
        )
        return fub_id

    @staticmethod
    def _io_plan_members(plan_name: str, prefix: str) -> list[str] | None:
        """Extension names encoded in a managed IO plan name '{prefix} - IO [A,B]', else None."""
        m = re.fullmatch(re.escape(prefix) + r" - IO \[(.+)\]", plan_name)
        if not m:
            return None
        return [p.strip() for p in m.group(1).split(",") if p.strip()]

    @classmethod
    def _is_managed_cluster_plan_name(cls, plan_name: str, prefix: str) -> bool:
        """True for a name matching either managed cluster scheme (Marker or IO cluster).

        Matched by name rather than plan_map membership so a cluster plan orphaned from
        plan_map (e.g. after a stale-prune) is still recognized as auto-generated content.
        """
        if cls._io_plan_members(plan_name, prefix) is not None:
            return True
        return bool(re.fullmatch(re.escape(prefix) + r" - Marker \[\d+-\d+\]", plan_name))

    @staticmethod
    def _io_plan_name(prefix: str, members: list[str]) -> str:
        """Managed IO cluster plan name encoding its extension membership."""
        return f"{prefix} - IO [{','.join(members)}]"

    def _io_plan_membership(self, prefix: str) -> dict[int, list[str]]:
        """Live membership of every managed IO cluster plan: {fub_id: [ext names]}.

        Read from the live $Fubs plan names (authoritative — plan_map keys can go stale
        after renames); membership order defines each extension's column index.
        """
        result: dict[int, list[str]] = {}
        for fid_str, fub_info in self.api.fub_data.items():
            members = self._io_plan_members(str(fub_info.get("Name", "")), prefix)
            if members:
                with contextlib.suppress(TypeError, ValueError):
                    result[int(fid_str)] = members
        return result

    def managed_io_plan_members(self, fub_id: int) -> list[str] | None:
        """Extension membership when fub_id is an HA-managed IO cluster plan, else None.

        Public counterpart of _io_plan_membership for the services module's grid/sort code.
        """
        return self._io_plan_membership(self._function_plan_prefix()).get(fub_id)

    def _io_cluster_plan_name_for_ext(self, ext_name: str, prefix: str) -> str:
        """Plan name the extension belongs to (existing membership) or would get (new plan)."""
        for members in self._io_plan_membership(prefix).values():
            if ext_name in members:
                return self._io_plan_name(prefix, members)
        return self._io_plan_name(prefix, [ext_name])

    def _io_rows_needed(self, ext_name: str) -> int:
        """Column rows one extension package needs (IOs + header/blank separator rows)."""
        idents = [io["identifier"] for io in (self.data or {}).get("io", []) if io["ext_name"] == ext_name]
        rows = io_column_rows(idents)
        return (max(rows.values()) + 1) if rows else 0

    async def resolve_io_clusters(self, ext_names: list[str]) -> tuple[dict[str, tuple[int, int]], set[int]]:
        """Resolve/create the managed IO cluster plan of every extension (membership-true).

        Returns ({ext_name: (fub_id, column_index)}, {fub_ids of freshly created plans}).
        An extension already encoded in a managed IO plan name keeps that plan and column
        forever; a new extension joins the first plan with free capacity (the plan is
        renamed to extend its membership list) or gets a fresh plan. Capacity derives from
        CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN: 50 → 1, 100 → 2, 150 → 3 extensions per plan.
        """
        prefix = self._function_plan_prefix()
        capacity = max(1, self._function_plan_cluster_size() // 50)

        stale = self._stale_plan_map_entries(self.api.fub_data)
        membership = self._io_plan_membership(prefix)
        result: dict[str, tuple[int, int]] = {}
        created_plans: set[int] = set()
        plan_map_updates: dict[str, int] = {}

        for ext in ext_names:
            placed = next(
                ((fid, members.index(ext)) for fid, members in membership.items() if ext in members),
                None,
            )
            if placed is None:
                placed = await self._join_or_create_io_plan(ext, membership, capacity, prefix)
            if placed is None:
                _LOGGER.error("[%s] Failed to resolve/create IO cluster plan for '%s'", self.server_id, ext)
                continue
            fub_id, _column = placed
            result[ext] = placed
            if fub_id not in membership:
                created_plans.add(fub_id)
                membership[fub_id] = [ext]
            plan_map_updates[self._io_plan_name(prefix, membership[fub_id])] = fub_id

        if plan_map_updates or stale:
            await self._persist_plan_map(plan_map_updates, removals=set(stale))
        return result, created_plans

    async def _join_or_create_io_plan(
        self, ext: str, membership: dict[int, list[str]], capacity: int, prefix: str
    ) -> tuple[int, int] | None:
        """Place a new extension: join an existing plan with free capacity (renaming the
        plan to extend its membership list) or create a fresh single-extension plan.

        A fresh plan turns A3 portrait when the extension package would not fit the
        landscape column height (rows derive from the live IO list incl. blank separators).
        Returns (fub_id, column_index) or None; membership is updated in place on join —
        the caller registers fresh plans itself (fub_id not yet in membership = created).
        """
        for fid, members in membership.items():
            if len(members) < capacity:
                new_members = [*members, ext]
                if not await self._rename_io_plan(fid, self._io_plan_name(prefix, new_members)):
                    _LOGGER.warning(
                        "[%s] Could not rename IO plan fub=%s to add '%s' — creating a fresh plan instead",
                        self.server_id,
                        fid,
                        ext,
                    )
                    break
                membership[fid] = new_members
                return fid, len(new_members) - 1

        landscape_rows = self._io_plan_rows_per_col(_ORIENT_LANDSCAPE)
        orientation = _ORIENT_PORTRAIT if self._io_rows_needed(ext) > landscape_rows else _ORIENT_LANDSCAPE
        fub_id = await self._create_managed_plan(self._io_plan_name(prefix, [ext]), orientation=orientation)
        if fub_id is None:
            return None
        return fub_id, 0

    def _io_plan_rows_per_col(self, orientation: str) -> int:
        """Row slots one column offers on a fresh A3 plan of the given orientation."""
        _, y_max = self.api.get_fub_canvas_bounds(-1, paper_name=_MANAGED_PLAN_PAPER, orientation=orientation)
        return max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))

    async def _rename_io_plan(self, fub_id: int, new_name: str) -> bool:
        """Rename a managed IO plan (membership join), keeping paper/DPI/orientation as-is."""
        fub = self.api.fub_data.get(str(fub_id), {})
        paper = _PAPER_NAME_BY_ID.get(str(fub.get("Paper")), _MANAGED_PLAN_PAPER)
        dpi = int(fub.get("Resolution", 90))
        orientation = _ORIENT_PORTRAIT if str(fub.get("Orientation")) == "1" else _ORIENT_LANDSCAPE
        return await self.api.function_plan_update_paper(fub_id, paper, dpi, orientation, name=new_name)

    async def _persist_plan_map(self, updates: dict[str, int], removals: set[str] | None = None) -> None:
        """Merge updates into CONF_FUNCTION_PLAN_PLAN_MAP, dropping any stale names in removals
        first — a single options write so a stale-prune never races the update it's paired with
        for the reload-skip flag (see R2)."""
        new_options = dict(self.config_entry.options)
        current_map = dict(new_options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {}))
        for name in removals or ():
            current_map.pop(name, None)
        current_map |= updates
        new_options[CONF_FUNCTION_PLAN_PLAN_MAP] = current_map
        self.request_options_update_without_reload(new_options)

    def _stale_plan_map_entries(self, fub_data: dict) -> dict[str, int]:
        """plan_map entries whose fub_id no longer exists (or was renamed) in the live $Fubs
        listing — leftovers from a plan deleted directly in Comexio (extension removed, or a
        forced full resync where all function plans + Web-IO were wiped and recreated under
        different names). The caller is responsible for persisting the options removal via
        _persist_plan_map.
        """
        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        if not isinstance(raw_map, dict) or not fub_data:
            # No live $Fubs snapshot to compare against — an empty/unpopulated fub_data must
            # never be read as "every plan is gone", or a single fetch hiccup would wipe the
            # whole map.
            return {}
        stale = {
            name: int(fid)
            for name, fid in raw_map.items()
            if str(fid) not in fub_data or fub_data[str(fid)].get("Name") != name
        }
        for name, fub_id in stale.items():
            _LOGGER.info(
                "[%s] Pruned stale plan_map entry '%s' (fub %s) — no longer exists in Comexio",
                self.server_id,
                name,
                fub_id,
            )
        return stale
