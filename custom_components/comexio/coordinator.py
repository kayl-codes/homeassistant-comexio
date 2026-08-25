# Version: 0.8.0
import asyncio
import contextlib
from datetime import datetime, timedelta
import logging
import pathlib
import re
import socket
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

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
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
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
    EVENT_PLAN_VALUE,
    FIRMWARE_CHECK_HOUR,
    FIRMWARE_CHECK_MINUTE,
    FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC,
    FUNCTION_PLAN_FUB_ID_AUTO,
    FUNCTION_PLAN_LAYOUT_COMMENT_Y,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    ICON_ADD,
    ICON_DELETE,
    ICON_FIX,
    ICON_LINK,
    ICON_NETWORK,
    ICON_RENAME,
    ICON_WARNING,
    SYNC_DURATION_FUNCTION_PLAN_ELEMENT,
    SYNC_DURATION_FUNCTION_PLAN_FINALIZE,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASSES,
    bus_load_signal,
    expand_ignored_marker_ids,
    fw_update_signal,
    io_audit_key,
    io_column_rows,
    is_io_audit_key,
    snap_to_grid,
    webio_class_label,
)
from .function_plan_backup import (
    FunctionPlanBackupManager,
    build_source_id_translation,
    retention_cutoff,
    snapshot_label_maps,
)
from .function_plan_catalog import FunctionPlanCatalogManager
from .function_plan_render import render_plan_svg

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

# Debounce for live plan-preview refreshes: webhook bursts (e.g. a dimmer ramp) collapse
# into one re-render at most every ~0.5 s; single value pushes still show up promptly.
_PREVIEW_LIVE_REFRESH_DELAY = 0.5

# Stufe-2 connection-value poll (see async_generate_plan_preview / _async_poll_connection_values):
# background cadence while a live plan preview is armed but no debug session is open, and the
# faster cadence matching the plan card's debug box (comexio_plan_event fires promptly, so the
# wire colors should keep up while someone is actually watching the log).
_CONNECTION_POLL_INTERVAL_NORMAL = timedelta(seconds=2)
_CONNECTION_POLL_INTERVAL_FAST = timedelta(seconds=0.5)
_CONNECTION_POLL_MAX_FAILURES = 5

# Stufe-2 poll auto-stop: a live plan preview left open (e.g. a forgotten browser tab)
# would otherwise poll forever — disarm it after this many minutes by default. The plan
# card's debug box can extend this once per arm, in memory only (function_plan_preview_extend),
# for someone deliberately watching a phenomenon over a longer window.
_PREVIEW_AUTO_STOP_DEFAULT_MINUTES = 15

# Calendar-function (type 3) Freq -> sun.sun attribute providing HA's own next-occurrence
# time for that astro event (computed by HA's sun integration from the configured location,
# no need to reimplement astral math here). WEEKLY has no astro time and is intentionally
# absent.
_SUN_ATTR_BY_FREQ = {
    "SUN_RISE": "next_rising",
    "SUN_SET": "next_setting",
    "DAWN": "next_dawn",
    "DUSK": "next_dusk",
}


def _build_sun_times(hass: HomeAssistant) -> dict[str, str]:
    """Pre-format sun.sun's next-occurrence attributes for the plan-preview tooltip.

    Formatting (not just parsing) happens here rather than in function_plan_render.py, which is
    deliberately HA-free (see its module docstring) — the renderer only ever sees a plain
    {Freq: "DD.MM. HH:MM"} string dict.
    """
    sun_state = hass.states.get("sun.sun")
    if sun_state is None:
        return {}
    sun_times: dict[str, str] = {}
    for freq, attr in _SUN_ATTR_BY_FREQ.items():
        raw = sun_state.attributes.get(attr)
        parsed = dt_util.parse_datetime(raw) if raw else None
        if parsed is not None:
            sun_times[freq] = dt_util.as_local(parsed).strftime("%d.%m. %H:%M")
    return sun_times


def _write_preview_svg(path: pathlib.Path, svg_content: str) -> None:
    """Blocking file write — always run via hass.async_add_executor_job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg_content, encoding="utf-8")


def _parse_snapshot_source(source: str) -> tuple[str, int]:
    """Split async_generate_plan_preview's 'snapshot:<kind>:<slot>' source back into its parts."""
    _prefix, kind, slot = source.split(":", 2)
    return kind, int(slot)


def _plan_ref_ids(elements: dict[str, Any]) -> tuple[set[str], set[str]]:
    """marker_ids/io_ids (type-2/type-1 element refs) referenced by a plan's elements.

    Drives _fire_plan_event's gating of the comexio_plan_event bus events (the plan card's
    debug box) — only a value push belonging to the currently-displayed plan is published.
    """
    marker_ids: set[str] = set()
    io_ids: set[str] = set()
    for elem in elements.values():
        ref = elem.get("reference") or {}
        if ref.get("type") == 2:
            marker_ids.add(str(ref.get("ref_id")))
        elif ref.get("type") == 1:
            io_ids.add(str(ref.get("ref_id")))
    return marker_ids, io_ids


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
        # Bulk snapshot of every live plan's elements/connections, refreshed each backup
        # cycle — lets function_plan_search answer instantly instead of a fresh ~11s
        # live fetch per plan (Comexio serializes requests server-side regardless of
        # client-side concurrency, so per-call fetching doesn't parallelize away).
        self.function_plan_plans: dict[int, dict] = {}
        # Set when the audit's wiring check was skipped because the bulk snapshot above
        # wasn't loaded yet — re-run once the backup cycle has loaded it (see _audit_wired_pairs
        # / _async_function_plan_backup_cycle).
        self._lp_missing_recheck_pending: bool = False
        # Marker IDs (type-2 element ref_ids) referenced in a plan as of the last parse_config
        # call — an unnamed marker still needs a real entity/value if it's wired somewhere (see
        # api._process_markers). Tracked here so a change triggers an immediate extra refresh
        # instead of waiting for the next scheduled poll (which could be hours away with a long
        # scan_interval).
        self._last_referenced_marker_ids: set[str] = set()
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
        self._cleanup_entity_ids: list[int] = []
        self._cleanup_function_plan_count: int = 0
        self.cancel_sync: bool = False
        self.entity_id_mismatches: list[dict[str, str]] = []
        self.orphaned_statistics: list[str] = []
        self.offline_entity_statistic_ids: set[str] = set()
        self.offline_extensions: set[str] | None = None
        self._extension_offline_issue_active: bool = False
        self.cover_keywords: list[str] = []
        # R4: Lock to prevent concurrent sync runs
        self._sync_lock: asyncio.Lock = asyncio.Lock()
        # Lock to prevent two function_plan_restore calls racing on the same (or a different)
        # plan — a second restore reading live state mid-first-restore would act on a
        # half-applied snapshot. No per-plan granularity: only one restore anywhere, anytime.
        self._restore_lock: asyncio.Lock = asyncio.Lock()
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
        self.last_plan_preview: dict[str, Any] | None = None
        # In-memory copy of the last rendered preview SVG, served directly by the
        # image entity (image.py) without touching the config/www file.
        self.last_plan_preview_svg: str | None = None
        # Live-value preview refresh (Stufe 1): plan structure of the last LIVE preview is
        # cached so webhook pushes can re-render it with fresh values without re-fetching
        # the plan from Comexio; a shown snapshot clears the cache (it must not be
        # overwritten by live refreshes). Structural plan edits need a new button press.
        self._preview_plan_cache: dict[str, Any] | None = None
        self._preview_refresh_cancel: Any = None
        # Stufe 2: last fetched {connection_id: value} for the armed live plan (see
        # _async_poll_connection_values) and the timer driving that poll. Cleared/stopped
        # whenever the live cache above is cleared or points at a different plan.
        self._connection_values: dict[str, Any] = {}
        self._connection_poll_cancel: Any = None
        self._connection_poll_fast_requested: bool = False
        # Consecutive fetch failures for the poll above (e.g. transient ServerDisconnectedError).
        # Disarms the preview after _CONNECTION_POLL_MAX_FAILURES instead of retrying forever at
        # full cadence with no backoff — a past incident hammered Comexio's serialized endpoint
        # hard enough to starve the main coordinator poll and the Function-Plan auto-backup cycle.
        self._connection_poll_fail_count: int = 0
        # Auto-stop timer for the armed preview (see _PREVIEW_AUTO_STOP_DEFAULT_MINUTES) and
        # the in-memory-only extension requested via the debug box, reset to the default on
        # every new arm (a fresh plan view never inherits a previous session's extension).
        self._preview_auto_stop_cancel: Any = None
        self._preview_auto_stop_minutes: int = _PREVIEW_AUTO_STOP_DEFAULT_MINUTES
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
            # Cold start: the bulk plan snapshot isn't loaded yet (it lands after this first
            # cycle, via the backup cycle) — fall back to the last stored auto-backup so an
            # unnamed-but-wired marker still gets an entity on every restart, not just after
            # the first backup cycle has run.
            referenced_markers = self._referenced_marker_ids()
            if referenced_markers is None:
                referenced_markers = await self.function_plan_backup.async_referenced_marker_ids()
            self._last_referenced_marker_ids = referenced_markers
            parsed_data = self.api.parse_config(raw_config, live_states, referenced_markers)

            # async_update_from_raw_config never raises (own contract, enforced internally) —
            # no local guard needed here.
            await self.function_plan_catalog.async_update_from_raw_config(raw_config, self.api.comexio_version)

            import_markers = conf.get("import_markers", True)
            import_ios = conf.get("import_ios", True)

            final_data = {
                "markers": parsed_data["markers"] if import_markers else [],
                "io": parsed_data["io"] if import_ios else [],
                "io_all": parsed_data.get("io_all", []) if import_ios else [],
                "webio_commands": parsed_data.get("webio_commands", {}),
                "webio_names": parsed_data.get("webio_names", {}),
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
            # Ignored markers are intentionally excluded from the Web-IO/Function Plan sync
            # (see CONF_IGNORED_MARKERS) — leaving them in would make the audit report them as
            # permanently "missing" and let Full Sync / create_missing actually create and wire
            # Web-IO commands for markers the user explicitly opted out of.
            ignored_marker_ids = self.ignored_marker_ids
            ha_map = {}
            for m in final_data["markers"]:
                if int(m["id"]) in ignored_marker_ids:
                    continue
                ha_map[f"M{m['id']}"] = {
                    "name": f"HA {m['name']}",
                    "type": m["type"],  # Trusting the preprocessing of api.py
                }
            # 2. HA Map: IOs
            io_meta_by_key: dict[str, dict[str, Any]] = {}
            for io in final_data["io"]:
                key = io_audit_key(io["ext_name"], io["identifier"])

                # Since api.py now provides 'is_binary', derive
                # the audit type ('digital'/'analog') here:
                mapped_type = "digital" if io.get("is_binary") else "analog"

                ha_map[key] = {"name": f"HA IO {io['ext_name']} {io['identifier']}", "type": mapped_type}
                io_meta_by_key[key] = io

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
                        "webIoId": info.get("webIoId"),
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
            payload_map = {
                cmd["Name"]: cmd
                for cmd in self.api.build_webio_commands(self.server_id, final_data, None, ignored_marker_ids)
            }

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

            # Check whether a function plan is actively selected (guards against false positives).
            # At startup the select entity is not yet in the state machine; fall back to the
            # fub_id persisted in options by async_select_option.
            _lp_sel = self._active_plan_selector_state()
            if _lp_sel and _lp_sel.state not in ("unavailable", "unknown"):
                has_active_plan = True
            else:
                has_active_plan = self.config_entry.options.get(CONF_FUNCTION_PLAN_FUB_ID) is not None or bool(
                    self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP)
                )

            # Wiring truth comes from the plan bulk snapshot (loadelements): the server-side
            # WebCommandIoId survives plan deletion and is not maintained by add_element
            # wiring, so it cannot be trusted. While the snapshot is still empty (first poll
            # after startup/reload) the check is skipped and re-run once the backup cycle
            # has loaded the plans.
            # Offline extensions are exempt from the wiring check: their hardware is not
            # present, so wiring their IOs is pointless. Once the extension comes back
            # online the next poll flags any remaining gaps again.
            managed_io_exts: set[str] = set(self.config_entry.options.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, []))
            managed_io_exts -= self.offline_extensions or set()
            wired_marker_webio_pairs, wired_io_webio_pairs, connected_marker_ids, connected_io_ids = (
                self._audit_wired_pairs(has_active_plan, managed_io_exts)
            )

            # Compare HA entities with Comexio commands to find inconsistencies
            type_mismatches: list[dict[str, Any]] = []
            missing_items: list[dict[str, Any]] = []
            renamed_items: list[dict[str, Any]] = []
            orphans: list[dict[str, Any]] = []
            function_plan_missing_items: list[dict[str, Any]] = []
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

                    # Function Plan gap: command exists but is not wired directly to its
                    # marker (M-keys) / IO (IO_-keys of managed extensions)
                    if not is_renamed:
                        gap_item = self._function_plan_gap_item(
                            key,
                            ha["name"],
                            best_match,
                            (wired_marker_webio_pairs, wired_io_webio_pairs),
                            io_meta_by_key.get(key),
                            managed_io_exts,
                        )
                        if gap_item:
                            function_plan_missing_items.append(gap_item)
                            mismatches.add(f"function_plan_missing_{key}")

                    # All other commands pointing to this key are duplicates -> Orphans
                    for com in com_list:
                        if com != best_match:
                            orphans.append(
                                {
                                    "id": com["id"],
                                    "name": com["name"],
                                    "webio_class": com.get("webio_class"),
                                    "webIoId": com.get("webIoId"),
                                }
                            )
                            mismatches.add(f"orphan_{com['id']}")

            # Find items in Comexio that no longer exist in HA
            for key, com_list in com_map.items():
                if key not in ha_map:
                    for com in com_list:
                        orphans.append(
                            {
                                "id": com["id"],
                                "name": com["name"],
                                "webio_class": com.get("webio_class"),
                                "webIoId": com.get("webIoId"),
                            }
                        )
                        mismatches.add(f"orphan_{com['id']}")

            # Function Plan debris: marker/IO elements left in a managed plan after their
            # WebIO counterpart was removed (e.g. directly in Comexio Studio) without also
            # removing the wired source element — invisible to every check above since those
            # all pivot on webio_commands/ha_map, never the raw plan elements themselves.
            markers_by_id = {str(m["id"]): m["name"] for m in final_data["markers"]}
            # io_all (not "io"): inactive IOs are excluded from "io" but can still sit as
            # debris in a plan (e.g. the extension was deactivated after the wiring was cut),
            # so resolving against "io" alone silently fell back to a bare "IO#<ref_id>" label.
            io_by_id = {str(io["id"]): io for io in final_data["io_all"]}
            function_plan_dangling_items: list[dict[str, Any]] = []
            if has_active_plan:
                for rid in self._dangling_source_ids("2", wired_marker_webio_pairs, connected_marker_ids):
                    function_plan_dangling_items.append(
                        {"name": markers_by_id.get(rid, f"M{rid}"), "ref_id": rid, "webio_class": WEBIO_CLASS_MARKER}
                    )
                    mismatches.add(f"function_plan_dangling_M{rid}")
            if managed_io_exts:
                for rid in self._dangling_source_ids("1", wired_io_webio_pairs, connected_io_ids):
                    io = io_by_id.get(rid)
                    name = f"{io['ext_name']} {io['identifier']}" if io else f"IO#{rid}"
                    function_plan_dangling_items.append({"name": name, "ref_id": rid, "webio_class": WEBIO_CLASS_IO})
                    mismatches.add(f"function_plan_dangling_IO{rid}")

            self.last_audit_results = {
                "type": type_mismatches,
                "missing": missing_items,
                "rename": renamed_items,
                "orphan": orphans,
                "ip_mismatch": ip_mismatch,
                "ha_address": ha_address,
                "webio_devices": webio_device_audit,
                "cleanup_entities": self._cleanup_entity_ids,
                "cleanup_function_plan_count": self._cleanup_function_plan_count,
                "function_plan_missing": function_plan_missing_items,
                "function_plan_dangling": function_plan_dangling_items,
            }

            # Include pending entity cleanups (ignored markers with remaining HA entities) in mismatches
            for mid in self._cleanup_entity_ids:
                mismatches.add(f"cleanup_entity_{mid}")

            # 📈 --- AUDIT SUMMARY LOGGING ---
            if mismatches:
                # Create a simple string representation to detect changes
                current_summary_content = (
                    f"{len(type_mismatches)}-{len(missing_items)}-{len(renamed_items)}"
                    f"-{len(orphans)}-{ip_mismatch}-{len(function_plan_missing_items)}"
                    f"-{len(function_plan_dangling_items)}"
                )

                # Only log details if the audit result differs from the previous run
                if self.last_summary_hash != current_summary_content:
                    self.last_summary_hash = current_summary_content

                    # Consolidated warning for the Home Assistant log overview
                    _LOGGER.warning(
                        "[%s] Comexio Audit Mismatch: %d issues detected (Type:%d, Missing:%d, "
                        "Renames:%d, Orphans:%d, IP:%d, Plan debris:%d)",
                        self.server_id,
                        len(mismatches),
                        len(type_mismatches),
                        len(missing_items),
                        len(renamed_items),
                        len(orphans),
                        1 if ip_mismatch else 0,
                        len(function_plan_dangling_items),
                    )
                    if ip_mismatch:
                        mismatched = {cls: v["device_ip"] for cls, v in webio_device_audit.items() if v["ip_mismatch"]}
                        _LOGGER.warning(
                            "[%s] Server address mismatch: HA=%s, Comexio=%s", self.server_id, ha_address, mismatched
                        )

                    # Consolidated audit summary with details for each category
                    _LOGGER.info("=== %s COMEXIO AUDIT SUMMARY [%s] ===", ICON_WARNING, self.server_id)
                    _LOGGER.info("%s Type-Mismatches (%d):", ICON_FIX, len(type_mismatches))
                    for item in type_mismatches:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("%s Missing Webhooks (%d):", ICON_ADD, len(missing_items))
                    for item in missing_items:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("%s Renames (%d):", ICON_RENAME, len(renamed_items))
                    for item in renamed_items:
                        _LOGGER.info("   -> %s", item["name"])

                    _LOGGER.info("%s Orphans (%d):", ICON_DELETE, len(orphans))
                    for item in orphans:
                        _LOGGER.info("   -> %s", item["name"])

                    if function_plan_missing_items:
                        _LOGGER.info("%s Not wired in Function Plan (%d):", ICON_LINK, len(function_plan_missing_items))
                        for item in function_plan_missing_items:
                            _LOGGER.info("   -> %s", item["name"])

                    if function_plan_dangling_items:
                        _LOGGER.info("%s Function Plan debris (%d):", ICON_DELETE, len(function_plan_dangling_items))
                        for item in function_plan_dangling_items:
                            _LOGGER.info("   -> %s", item["name"])

                    if ip_mismatch:
                        mismatched_ips = {
                            cls: v["device_ip"] for cls, v in webio_device_audit.items() if v["ip_mismatch"]
                        }
                        _LOGGER.info(
                            "%s IP/Port Mismatch: Comexio expects %s, but HA is at %s",
                            ICON_NETWORK,
                            mismatched_ips,
                            ha_address,
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
                    "cleanup_entities": len(self._cleanup_entity_ids),
                    "cleanup_function_plan_count": self._cleanup_function_plan_count,
                    "function_plan_missing": len(function_plan_missing_items),
                    "function_plan_missing_eta_sec": self._function_plan_missing_eta_sec(function_plan_missing_items),
                    "function_plan_dangling": len(function_plan_dangling_items),
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
                        "ce_count": str(len(self._cleanup_entity_ids)),
                    },
                    data={
                        "entry_id": self.config_entry.entry_id,
                        "counts": issue_data_counts,
                        "function_plan_missing_detail": self._function_plan_missing_detail(
                            function_plan_missing_items, ha_map, io_meta_by_key
                        ),
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"sync_mismatch_{self.server_id}")

            self._check_duplicate_plan_names()

            # Function Plan backup: load all plan wirings + rotate auto backups in the background.
            # Entry-scoped task (cancelled on unload/reload) so neither startup nor the poll
            # cycle is blocked by the ~0.5s/plan bulk load. Skip spawning a new task entirely
            # while a previous cycle is still running, instead of creating one just to have it
            # return immediately on the lock check.
            if not self._function_plan_backup_lock.locked():
                _LOGGER.debug("[%s] Function Plan backup cycle: spawning background task", self.server_id)
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_function_plan_backup_cycle(),
                    name=f"comexio_{self.server_id}_function_plan_backup",
                )
            else:
                _LOGGER.warning("[%s] Function Plan backup cycle: NOT spawned — lock already held", self.server_id)

            return final_data

        except Exception as e:
            _LOGGER.exception("[%s] Data fetch failed: %s", self.server_id, e)
            raise

    async def _async_function_plan_backup_cycle(self) -> None:
        """Load all plan wirings and rotate auto backups (runs as entry-scoped background task)."""
        if self._function_plan_backup_lock.locked():
            _LOGGER.debug("[%s] Function Plan backup cycle skipped: previous run still in progress", self.server_id)
            return
        try:
            async with asyncio.timeout(FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC):
                await self._async_function_plan_backup_cycle_locked()
        except TimeoutError:
            _LOGGER.warning(
                "[%s] Function Plan backup cycle aborted after %ss — lock released, will retry next poll",
                self.server_id,
                FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC,
            )

    async def _async_function_plan_backup_cycle_locked(self) -> None:
        """Body of the backup cycle, bounded by the outer timeout in the caller."""
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
                _LOGGER.warning(
                    "[%s] Function Plan backup cycle: bulk load returned no plans — skipping this cycle",
                    self.server_id,
                )
                return
            self.function_plan_plans = plans
            # Re-evaluate which unlabeled markers are now referenced in a plan (see
            # api._process_markers). Compared against the set last USED by parse_config —
            # a no-op on every normal cycle; only an actual change (marker newly wired in,
            # or dropped from every plan) triggers an extra refresh, so the fix isn't tied
            # to (potentially very long) scan_interval waits. _last_referenced_marker_ids
            # is deliberately NOT updated here: the triggered refresh does that itself,
            # keeping the comparison anchored to what entities were actually built from.
            new_referenced_markers = self._referenced_marker_ids() or set()
            markers_changed = new_referenced_markers != self._last_referenced_marker_ids
            if self._lp_missing_recheck_pending or markers_changed:
                # The audit skipped the function_plan_missing check because the snapshot was
                # not loaded yet — re-run it now that wiring data is available.
                self._lp_missing_recheck_pending = False
                await self.async_request_refresh()
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
                plan_data = await self.api.function_plan_load_elements(fub_id)
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

    def _referenced_marker_ids(self) -> set[str] | None:
        """Return marker IDs (type-2 element ref_ids) present in any existing plan.

        Built from the bulk snapshot of the backup cycle, same as _wired_source_webio_pairs;
        returns None while that snapshot is not loaded yet. Unlike the WebIO check this
        does NOT require the element to participate in a connection — a marker merely
        placed in a plan already needs its type/live value resolved in the preview.
        """
        if not self.function_plan_plans:
            return None
        referenced: set[str] = set()
        for plan_data in self.function_plan_plans.values():
            for elem in (plan_data.get("elements") or {}).values():
                ref = elem.get("reference") or {}
                ref_id = ref.get("ref_id")
                if str(ref.get("type")) == "2" and ref_id is not None:
                    referenced.add(str(ref_id))
        return referenced

    def function_plan_label_maps(self) -> tuple[dict, dict, dict]:
        """Lookup dicts (marker/WebIO/IO id -> {"name": ..., ...}) for backup snapshot labeling.

        webio_by_id is primarily webio_names (ALL Web-IO classes, labelled Studio-style as
        '{deviceId}. {name}', so foreign commands wired into plans get real labels);
        HA's own webio_commands only fill ids missing from it (e.g. stale poll).
        """
        data = self.data or {}
        markers_by_id = {str(m["id"]): m for m in data.get("markers", [])}
        webio_by_id: dict[str, Any] = dict(data.get("webio_names", {}))
        for name, cmd in data.get("webio_commands", {}).items():
            w_id = cmd.get("webIoId")
            if w_id is not None and str(w_id) not in webio_by_id:
                # Same {2, "2"} check as _build_webio_name_lexicon, for consistency across
                # both Web-IO label sources even though this one is already int-normalized.
                webio_by_id[str(w_id)] = {"name": name, "analog": cmd.get("typeId") in {2, "2"}}
        # io_all (unfiltered, includes inactive IOs) so a plan wired to an inactive IO still
        # resolves a real name/greyed state instead of falling back to "IO ref=<id>".
        ios_by_id = {str(io["id"]): io for io in data.get("io_all", [])}
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

    async def _delete_managed_plans(self, plan_map: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Delete all HA-managed Function Plans. Returns (deleted, failed) plan names."""
        deleted_plans: list[str] = []
        failed_plans: list[str] = []
        for plan_name, fub_id in plan_map.items():
            try:
                fub_id_int = int(fub_id)
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "[%s] Uninstall cleanup: skipping plan %r with invalid fub_id %r",
                    self.server_id,
                    plan_name,
                    fub_id,
                )
                failed_plans.append(plan_name)
                continue
            if await self.api.delete_fup(fub_id_int):
                deleted_plans.append(plan_name)
            else:
                failed_plans.append(plan_name)
        return deleted_plans, failed_plans

    async def _delete_webio_devices_and_classes(
        self,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
        """Delete all Web-IO device instances, then their classes. Returns
        (devices, classes, failed_classes, skipped)."""
        devices: dict[str, str] = {}
        classes: dict[str, str] = {}
        # Base-deletion failures are tracked separately from `skipped` so callers can tell
        # "class deletion failed" apart from "no base was configured for this class".
        failed_classes: dict[str, str] = {}
        skipped: dict[str, str] = {}
        # Force a fresh audit rather than trusting a poll-interval-old snapshot — devices
        # created/removed since the last poll would otherwise be missed or acted on stale.
        await self.async_request_refresh()
        webio_devices = self.last_audit_results.get("webio_devices", {})
        for cls in WEBIO_CLASSES:
            dev = webio_devices.get(cls, {})
            device_id = dev.get("device_id")
            base_id = dev.get("base_id")
            if not device_id:
                continue
            if not await self.api.delete_webio_device(device_id):
                skipped[cls] = "delete_webio_device failed"
                continue
            devices[cls] = str(device_id)
            if not base_id:
                continue
            if await self.api.delete_webio_base(base_id):
                classes[cls] = str(base_id)
            else:
                failed_classes[cls] = "delete_webio_base failed"
        return devices, classes, failed_classes, skipped

    async def async_uninstall_cleanup(self) -> dict[str, Any]:
        """Tear down everything the integration created in Comexio: HA-managed Function
        Plans (CONF_FUNCTION_PLAN_PLAN_MAP), then the Web-IO device instances, then the
        Web-IO device classes — in that order, since Comexio refuses to delete a device
        or class still in use. Best-effort per phase; a failure in one plan/class does
        not block the others, and failed plan deletions stay in plan_map for a retry.
        """
        plan_map = dict(self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {}))
        deleted_plans, failed_plans = await self._delete_managed_plans(plan_map)
        if deleted_plans:
            await self._persist_plan_map({}, removals=set(deleted_plans))

        devices, classes, failed_classes, skipped = await self._delete_webio_devices_and_classes()

        _LOGGER.info(
            "[%s] Uninstall cleanup: plans deleted=%s failed=%s, devices=%s, classes=%s, failed_classes=%s, skipped=%s",
            self.server_id,
            deleted_plans,
            failed_plans,
            devices,
            classes,
            failed_classes,
            skipped,
        )
        return {
            "deleted_plans": deleted_plans,
            "failed_plans": failed_plans,
            "deleted_devices": devices,
            "deleted_classes": classes,
            "failed_classes": failed_classes,
            "skipped": skipped,
        }

    async def async_generate_plan_preview(
        self,
        fub_id: int,
        plan_name: str,
        elements: dict[str, Any],
        connections: dict[str, Any],
        source: str,
        label_metadata: dict[str, dict[str, str]] | None = None,
    ) -> str:
        """Render elements/connections (live plan or backup snapshot) to a preview SVG file.

        Writes to config/www so the frontend can serve it under /local/. One rotating file
        per server_id — each call overwrites the previous preview. Updates last_plan_preview
        so the Plan Preview sensor (entity_picture) reflects whatever was last generated,
        whether triggered by the Preview button (source='live') or a function_plan_visualize
        service call with format=svg (source='snapshot:<kind>:<slot>'). Returns the /local/ URL.

        A snapshot render's wiring/elements stay frozen at the stored snapshot — see
        select.py's ComexioPlanBackupSelectEntity — but its per-connection VALUES still track
        the live plan, translated through build_source_id_translation since Comexio may have
        renumbered ids since the snapshot was captured (see _resolve_preview_connection_values).

        label_metadata: a snapshot's stored labels (snapshot.get("labels"), see
        function_plan_backup._referenced_label_metadata) — when given, overlaid onto the live
        label maps so a historical snapshot shows the names it had at capture time rather
        than today's (possibly since-renamed) live names. None for a live render.
        """
        markers_by_id, webio_by_id, ios_by_id = self._resolve_preview_label_maps(label_metadata)
        catalog = await self.function_plan_catalog.async_get_catalog()
        title_suffix, canvas = self._build_preview_title_and_canvas(fub_id)
        sun_times = _build_sun_times(self.hass)
        # A plan switch invalidates the previous plan's connection-id values BEFORE this
        # render — otherwise one frame could color wires from an unrelated plan whose
        # connection ids happen to collide (the poll below only catches up afterwards).
        # Applies to a snapshot render too: build_source_id_translation's live_id_map keys
        # are the NEW plan's live element ids, which could numerically collide with the old
        # plan's still-cached values.
        plan_switched = self._preview_plan_cache is None or self._preview_plan_cache["fub_id"] != fub_id
        if plan_switched:
            self._connection_values = {}
        connection_values, live_id_map = await self._resolve_preview_connection_values(fub_id, elements, source)
        svg_content = render_plan_svg(
            elements,
            connections,
            catalog,
            markers_by_id,
            webio_by_id,
            ios_by_id,
            plan_name,
            title_suffix,
            sun_times,
            canvas=canvas,
            connection_values=connection_values,
        )

        filename = f"comexio_{self.server_id}_plan_preview.svg"
        file_path = pathlib.Path(self.hass.config.path("www", filename))
        await self.hass.async_add_executor_job(_write_preview_svg, file_path, svg_content)

        self.last_plan_preview_svg = svg_content
        self.last_plan_preview = {
            "fub_id": fub_id,
            "plan_name": plan_name,
            "source": source,
            "generated_at": dt_util.utcnow().isoformat(),
        }
        if source == "live":
            self._update_live_preview_cache(fub_id, plan_name, elements, connections, plan_switched)
        else:
            kind, slot = _parse_snapshot_source(source)
            self._update_snapshot_preview_cache(
                fub_id, plan_name, elements, connections, kind, slot, label_metadata, live_id_map
            )
        self.async_set_updated_data(self.data)
        return f"/local/{filename}"

    def _armed_snapshot_live_id_map(self, fub_id: int, source: str) -> dict[str, str] | None:
        """The already-armed snapshot's live-id translation table, if this render refreshes it.

        Returns None when this is a first-time view of a snapshot (or of a different one than
        currently armed) — the caller then fetches the live plan once and builds a fresh table,
        instead of doing that live-elements fetch on every debounced/polled re-render.
        """
        cache = self._preview_plan_cache
        if cache is not None and cache.get("snapshot_source") == source and cache["fub_id"] == fub_id:
            return cache.get("live_id_map")
        return None

    async def _resolve_preview_connection_values(
        self, fub_id: int, elements: dict[str, Any], source: str
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """Return (connection_values for render_plan_svg, live_id_map to cache alongside it).

        Live source: the plain live per-connection ground truth, no translation needed.
        Snapshot source: the same ground truth, translated from the live plan's element ids to
        this snapshot's own (possibly renumbered-since-capture) ids via build_source_id_translation.
        """
        if source == "live":
            return self._connection_values, None
        live_id_map = self._armed_snapshot_live_id_map(fub_id, source)
        if live_id_map is None:
            live_plan_data = await self.api.function_plan_load_elements(fub_id)
            live_id_map = (
                build_source_id_translation(elements, live_plan_data.get("elements", {})) if live_plan_data else {}
            )
        connection_values = {
            snap_id: self._connection_values[live_id]
            for snap_id, live_id in live_id_map.items()
            if live_id in self._connection_values
        }
        return connection_values, live_id_map

    def _resolve_preview_label_maps(self, label_metadata: dict[str, dict[str, str]] | None) -> tuple[dict, dict, dict]:
        """Return (markers_by_id, webio_by_id, ios_by_id), overlaid with snapshot labels if given."""
        markers_by_id, webio_by_id, ios_by_id = self.function_plan_label_maps()
        if label_metadata:
            markers_by_id, webio_by_id, ios_by_id = snapshot_label_maps(
                label_metadata, markers_by_id, webio_by_id, ios_by_id
            )
        return markers_by_id, webio_by_id, ios_by_id

    def _build_preview_title_and_canvas(self, fub_id: int) -> tuple[str, Any]:
        """Build the Studio-style title suffix (state + paper/DPI) and canvas bounds for a live plan.

        Title suffix carries the plan's live state + paper/DPI (Studio-style header info),
        rendered at half the plan-name size. Read from the live fub data — for snapshots
        of deleted plans it stays empty.
        """
        if (active := self.api.get_fub_active(fub_id)) is None:
            return "", None
        paper = self.api.get_fub_paper_format(fub_id)
        dpi = self.api.get_fub_dpi(fub_id)
        title_suffix = f"[{'aktiv' if active else 'inaktiv'}] [{paper} | {dpi}dpi]"
        # Paper-sized viewBox: same relative element scale for every plan of this paper
        # format instead of blowing a nearly empty plan up to full card width.
        canvas = self.api.get_fub_canvas_bounds(fub_id)
        return title_suffix, canvas

    def _update_live_preview_cache(
        self,
        fub_id: int,
        plan_name: str,
        elements: dict[str, Any],
        connections: dict[str, Any],
        plan_switched: bool,
    ) -> None:
        """Arm the preview cache for the live plan and restart the connection poll on plan switch."""
        marker_ids, io_ids = _plan_ref_ids(elements)
        self._preview_plan_cache = {
            "fub_id": fub_id,
            "plan_name": plan_name,
            "elements": elements,
            "connections": connections,
            "marker_ids": marker_ids,
            "io_ids": io_ids,
            "snapshot_source": None,
            "label_metadata": None,
            "live_id_map": None,
        }
        if plan_switched:
            self._restart_connection_poll(fast=self._connection_poll_fast_requested)
            self._preview_auto_stop_minutes = _PREVIEW_AUTO_STOP_DEFAULT_MINUTES
            self._restart_preview_auto_stop()

    def _update_snapshot_preview_cache(
        self,
        fub_id: int,
        plan_name: str,
        elements: dict[str, Any],
        connections: dict[str, Any],
        kind: str,
        slot: int,
        label_metadata: dict[str, dict[str, str]] | None,
        live_id_map: dict[str, str] | None,
    ) -> None:
        """Arm the preview cache for a frozen backup snapshot (wiring stays put, values stay live).

        live_id_map: this snapshot's element-id -> live-plan element-id translation (see
        build_source_id_translation), reused as-is by subsequent refreshes of the SAME
        snapshot instead of being rebuilt on every debounced/polled re-render.
        """
        source = f"snapshot:{kind}:{slot}"
        cache = self._preview_plan_cache
        already_armed = cache is not None and cache.get("snapshot_source") == source and cache["fub_id"] == fub_id
        marker_ids, io_ids = _plan_ref_ids(elements)
        self._preview_plan_cache = {
            "fub_id": fub_id,
            "plan_name": plan_name,
            "elements": elements,
            "connections": connections,
            "marker_ids": marker_ids,
            "io_ids": io_ids,
            "snapshot_source": source,
            "label_metadata": label_metadata,
            "live_id_map": live_id_map,
        }
        if not already_armed:
            self._restart_connection_poll(fast=self._connection_poll_fast_requested)
            self._preview_auto_stop_minutes = _PREVIEW_AUTO_STOP_DEFAULT_MINUTES
            self._restart_preview_auto_stop()

    def schedule_plan_preview_refresh(self) -> None:
        """Debounced re-render of the currently armed plan preview after a value update.

        Called from the webhook update paths (update_marker/update_io_by_name) so the
        preview's pill values and red HIGH wires follow the plant in near-real-time — for a
        displayed backup snapshot this refreshes its live-value overlay only, the frozen
        wiring is untouched (see _update_snapshot_preview_cache). No-op while no plan cache is
        armed or a refresh is already scheduled; the render after the debounce window reads
        the then-current coordinator values, so every push that arrives within the window is
        included in that one refresh.
        """
        if self._preview_plan_cache is None or self._preview_refresh_cancel is not None:
            return
        self._preview_refresh_cancel = async_call_later(
            self.hass, _PREVIEW_LIVE_REFRESH_DELAY, self._async_refresh_plan_preview
        )

    async def _render_armed_preview(self) -> None:
        """Re-render whatever is currently armed — the live plan or a frozen backup snapshot.

        Shared by the webhook-driven debounce and the Stufe-2 connection-value poll.
        """
        cache = self._preview_plan_cache
        if cache is None:
            return
        source = cache["snapshot_source"] or "live"
        await self.async_generate_plan_preview(
            cache["fub_id"],
            cache["plan_name"],
            cache["elements"],
            cache["connections"],
            source,
            cache["label_metadata"],
        )

    async def _async_refresh_plan_preview(self, _now: Any) -> None:
        self._preview_refresh_cancel = None
        if self._preview_plan_cache is None:
            return
        try:
            await self._render_armed_preview()
        except Exception:
            # Disable further refreshes; the next preview button press re-arms.
            self._preview_plan_cache = None
            _LOGGER.exception("[%s] Plan preview refresh failed", self.server_id)

    def _restart_connection_poll(self, fast: bool) -> None:
        """(Re)start the Stufe-2 connection-value poll at the given cadence.

        Replaces any already-running timer (a plan switch or a debug-session toggle both
        call this) rather than layering a second one — see _CONNECTION_POLL_INTERVAL_*.
        """
        if self._connection_poll_cancel is not None:
            self._connection_poll_cancel()
        interval = _CONNECTION_POLL_INTERVAL_FAST if fast else _CONNECTION_POLL_INTERVAL_NORMAL
        self._connection_poll_cancel = async_track_time_interval(
            self.hass, self._async_poll_connection_values, interval
        )

    def _stop_connection_poll(self) -> None:
        if self._connection_poll_cancel is not None:
            self._connection_poll_cancel()
            self._connection_poll_cancel = None
        if self._preview_auto_stop_cancel is not None:
            self._preview_auto_stop_cancel()
            self._preview_auto_stop_cancel = None
        self._connection_values = {}

    def _restart_preview_auto_stop(self) -> None:
        """(Re)arm the auto-stop timer at the currently requested duration.

        Called on every genuine (re-)arm of the preview (see _update_live_preview_cache /
        _update_snapshot_preview_cache) — always resets to _PREVIEW_AUTO_STOP_DEFAULT_MINUTES
        first; function_plan_preview_extend then bumps _preview_auto_stop_minutes and calls
        this again for the SAME arm, without touching the default for the next one.
        """
        if self._preview_auto_stop_cancel is not None:
            self._preview_auto_stop_cancel()
        self._preview_auto_stop_cancel = async_call_later(
            self.hass, self._preview_auto_stop_minutes * 60, self._async_auto_stop_preview
        )

    async def _async_auto_stop_preview(self, _now: Any) -> None:
        """Disarm a preview left open past its auto-stop window — see _PREVIEW_AUTO_STOP_DEFAULT_MINUTES."""
        self._preview_auto_stop_cancel = None
        _LOGGER.info(
            "[%s] Function Plan preview auto-stopped after %s min of inactivity-unaware runtime",
            self.server_id,
            self._preview_auto_stop_minutes,
        )
        self._fire_plan_system_event(
            f"Live-Poll nach {self._preview_auto_stop_minutes} Minuten automatisch gestoppt — "
            "Plan erneut öffnen, um ihn fortzusetzen"
        )
        self._preview_plan_cache = None
        self._stop_connection_poll()

    def set_preview_auto_stop_extension(self, minutes: int) -> bool:
        """Extend the CURRENT arm's auto-stop window (function_plan_preview_extend service).

        In-memory only, for this arm alone — the next plan view resets to the default (see
        _restart_preview_auto_stop). No-op (returns False) while nothing is armed.
        """
        if self._preview_plan_cache is None:
            return False
        self._preview_auto_stop_minutes = minutes
        self._restart_preview_auto_stop()
        return True

    def set_debug_session_active(self, active: bool) -> None:
        """Switch the Stufe-2 poll cadence for the plan card's debug box (function_plan_debug_session).

        `active` bumps the currently-armed live plan's poll to the 0.5 s cadence matching
        the card's webhook-debounce responsiveness (user request: wire colors should keep
        up with the debug log while someone is actually watching it); otherwise the 2 s
        background cadence continues. A no-op while no live plan preview is armed — the
        next arm (async_generate_plan_preview) picks up the requested cadence itself.
        """
        self._connection_poll_fast_requested = active
        if self._preview_plan_cache is not None:
            self._restart_connection_poll(fast=active)

    async def _async_poll_connection_values(self, _now: Any) -> None:
        """Stufe 2: refresh live per-connection wire values and re-render the armed plan
        (live plan or a displayed backup snapshot — see _render_armed_preview)."""
        cache = self._preview_plan_cache
        if cache is None:
            return
        try:
            preview_session = await self.api.ensure_preview_session()
            self._connection_values = await self.api.get_function_plan_connection_values(
                cache["fub_id"], session=preview_session
            )
        except Exception:
            self._connection_poll_fail_count += 1
            if self._connection_poll_fail_count >= _CONNECTION_POLL_MAX_FAILURES:
                _LOGGER.exception(
                    "[%s] Connection-value poll failed %s times in a row — disarming plan preview "
                    "(re-open the preview to retry)",
                    self.server_id,
                    self._connection_poll_fail_count,
                )
                self._preview_plan_cache = None
                self._stop_connection_poll()
                self._connection_poll_fail_count = 0
                self._fire_plan_system_event(
                    f"Live-Poll nach {_CONNECTION_POLL_MAX_FAILURES} fehlgeschlagenen Versuchen "
                    "gestoppt — Plan erneut öffnen, um ihn fortzusetzen"
                )
            else:
                _LOGGER.warning(
                    "[%s] Connection-value poll failed (%s/%s), will retry next cycle",
                    self.server_id,
                    self._connection_poll_fail_count,
                    _CONNECTION_POLL_MAX_FAILURES,
                )
            return
        self._connection_poll_fail_count = 0
        _LOGGER.debug(
            "[%s] Connection-value poll fub=%s plan=%s -> %s",
            self.server_id,
            cache["fub_id"],
            cache["plan_name"],
            self._connection_values,
        )
        try:
            await self._render_armed_preview()
        except Exception:
            _LOGGER.exception("[%s] Connection-value plan preview refresh failed", self.server_id)

    async def async_shutdown(self) -> None:
        """Cancel a pending preview refresh before the coordinator shuts down."""
        self._stop_connection_poll()
        if self._preview_refresh_cancel is not None:
            self._preview_refresh_cancel()
            self._preview_refresh_cancel = None
        await super().async_shutdown()

    def _fire_plan_system_event(self, message: str) -> None:
        """Publish a card-only status line (not a signal value) — see _fire_plan_event.

        Unlike _fire_plan_event, not gated on the preview cache: callers (e.g. the auto-stop
        timer) may need to announce a state change made just as the cache is torn down.
        """
        self.hass.bus.async_fire(EVENT_PLAN_VALUE, {"server_id": self.server_id, "type": "system", "message": message})

    def _fire_plan_event(self, kind: str, ref_id: str, label: str, value: float | int | str) -> None:
        """Publish a value push on the HA bus IF it belongs to the live plan on display.

        The comexio-plan-card debug box subscribes to these events to show a timestamped
        log of everything happening in the currently rendered plan. Gated on the preview
        cache so an idle dashboard (no live preview armed) causes zero bus traffic.
        """
        cache = self._preview_plan_cache
        if cache is None or ref_id not in cache.get(f"{kind}_ids", ()):
            return
        self.hass.bus.async_fire(
            EVENT_PLAN_VALUE,
            {
                "server_id": self.server_id,
                "fub_id": cache["fub_id"],
                "plan_name": cache["plan_name"],
                "type": kind,
                "id": ref_id,
                "label": label,
                "value": value,
            },
        )

    def update_marker(self, marker_id: str | int, value: float | int | str) -> None:
        marker_id_str = str(marker_id)
        self.marker_states[marker_id_str] = value
        self._webhook_updated_markers.add(marker_id_str)  # R1: mark as received during possible fetch
        label = f"M{marker_id_str}"
        if self.data and "markers" in self.data:
            for m in self.data["markers"]:
                if str(m["id"]) == marker_id_str:
                    m["value"] = value
                    label = m.get("name") or label
                    break
        self.async_set_updated_data(self.data)
        self._fire_plan_event("marker", marker_id_str, label, value)
        self.schedule_plan_preview_refresh()

    def update_io_by_name(self, ext_name: str, identifier: str, value: float | int | str) -> None:
        key = (ext_name.lower(), identifier.lower())
        if io := self._io_index.get(key):
            self.io_states[io["id"]] = value
            io["value"] = value
            self._webhook_updated_io_ids.add(io["id"])  # R1: mark as received during possible fetch
            self._fire_plan_event("io", str(io["id"]), io.get("name") or f"{ext_name} {identifier}", value)
        self.async_set_updated_data(self.data)
        self.schedule_plan_preview_refresh()

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

    @property
    def ignored_marker_ids(self) -> set[int]:
        """Configured marker IDs excluded from entity creation and Web-IO command generation.

        Parses CONF_IGNORED_MARKERS from the config entry options via
        expand_ignored_marker_ids() (comma/semicolon/space separators, optional M/m prefix,
        ranges like '8-12'). Returns an empty set if unset.
        """
        ignored_raw = self.config_entry.options.get(CONF_IGNORED_MARKERS, "").strip()
        if not ignored_raw:
            return set()
        return expand_ignored_marker_ids(ignored_raw)

    async def async_check_ignored_markers(self, conf: dict[str, Any], final_data: dict[str, Any]) -> None:
        """Check ignored marker IDs and manage repair issues.

        - Stale IDs (marker no longer in Comexio) are auto-removed from options + notified.
        - Legacy IDs that still have HA entities or Function Plan links trigger a cleanup repair.
        - IDs whose marker exists but has no entities/links are the intended normal state — no action.
        """
        # Clean up the legacy "invalid" issue if it still exists from a previous version
        ir.async_delete_issue(self.hass, DOMAIN, f"ignored_markers_invalid_{self.server_id}")

        ignored_raw = conf.get(CONF_IGNORED_MARKERS, "").strip()
        if not ignored_raw:
            self._cleanup_entity_ids = []
            self._cleanup_function_plan_count = 0
            ir.async_delete_issue(self.hass, DOMAIN, f"ignored_markers_cleanup_{self.server_id}")
            return

        markers_by_id = {int(m["id"]): m for m in final_data.get("markers", [])}
        stale_ids: list[int] = []
        cleanup_ids: list[int] = []
        affected_fub_ids: set[int] = set()

        all_ignored_ids = expand_ignored_marker_ids(ignored_raw)
        lp_plans = await self._load_function_plan_check_data()
        marker_ids_with_entities = set(self.marker_entities_by_id(list(all_ignored_ids)).keys())
        _LOGGER.debug(
            "[%s] async_check_ignored_markers: ignored=%s, plans_loaded=%s",
            self.server_id,
            sorted(all_ignored_ids),
            sorted(lp_plans.keys()),
        )
        for marker_id in sorted(all_ignored_ids):
            marker = markers_by_id.get(marker_id)
            if not marker or not marker.get("name", "").strip():
                # Marker no longer exists in Comexio → stale, auto-remove
                stale_ids.append(marker_id)
                continue

            # Marker exists and is intentionally ignored — only flag if legacy entities/links remain
            has_entities = marker_id in marker_ids_with_entities
            function_plan_fub_id = self._check_marker_function_plan_link(marker_id, lp_plans)
            _LOGGER.debug(
                "[%s] ignored marker M%s: has_entities=%s, function_plan_fub_id=%s",
                self.server_id,
                marker_id,
                has_entities,
                function_plan_fub_id,
            )
            if function_plan_fub_id is not None:
                affected_fub_ids.add(function_plan_fub_id)
            if has_entities or function_plan_fub_id is not None:
                cleanup_ids.append(marker_id)

        # Auto-remove stale IDs from options (marker deactivated/removed in Comexio)
        if stale_ids:
            new_options = {**self.config_entry.options}
            if remaining_ids := sorted(all_ignored_ids - set(stale_ids)):
                new_options[CONF_IGNORED_MARKERS] = ",".join(str(i) for i in remaining_ids)
            else:
                new_options.pop(CONF_IGNORED_MARKERS, None)
            self.request_options_update_without_reload(new_options)
            stale_str = ", ".join(f"M{mid}" for mid in stale_ids)
            _LOGGER.info(
                "[%s] Auto-removed stale ignored_markers IDs (no longer in Comexio): %s",
                self.server_id,
                stale_str,
            )

            # persistent_notification has no per-user language context; use English
            notif_body = (
                f"Marker IDs **{stale_str}** were automatically removed from `ignored_markers` "
                "because they no longer exist in Comexio. "
                "They will be created as entities again on the next integration restart."
            )
            persistent_notification.async_create(
                self.hass,
                notif_body,
                title=f"Comexio ({self.server_id})",
                notification_id=f"comexio_stale_ignored_{self.server_id}",
            )

        # Store cleanup_ids for the combined sync_mismatch repair (no separate issue anymore)
        self._cleanup_entity_ids = cleanup_ids
        self._cleanup_function_plan_count = len(affected_fub_ids)
        # Remove legacy ignored_markers_cleanup issue if it still exists from an older version
        ir.async_delete_issue(self.hass, DOMAIN, f"ignored_markers_cleanup_{self.server_id}")

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

    def marker_entities_by_id(self, marker_ids: list[int]) -> dict[int, er.RegistryEntry]:
        """Return registry entries for the given marker ids that still have an HA entity.

        Single pass over the registry regardless of how many marker_ids are checked, instead
        of one full registry scan per marker. Shared by the audit check (read-only) and the
        cleanup button (which also deletes the returned entries) to avoid divergent matching
        logic between the two.
        """
        ent_reg = er.async_get(self.hass)
        marker_id_by_unique_id = {f"{DOMAIN}_{self.server_id}_m{mid}".lower(): mid for mid in marker_ids}
        return {
            marker_id: entity
            for entity in ent_reg.entities.values()
            if (marker_id := marker_id_by_unique_id.get((entity.unique_id or "").lower())) is not None
        }

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

    def _check_duplicate_plan_names(self) -> None:
        """Warn (informational, no fix flow) if two or more LIVE plans share the same name.

        Comexio does not enforce unique plan names — only fub_id is a real identity — but
        this integration resolves plans by name in several places (function_plan_connect/
        sort/stop/activate/restore). A name collision means those lookups could silently
        target the wrong plan; there's nothing HA can safely auto-fix here (which of the
        duplicates should be renamed is a Comexio-side, human decision), so this is a
        plain heads-up rather than an actionable repair.
        """
        names_to_ids: dict[str, list[int]] = {}
        for fid, fub in self.api.fub_data.items():
            if name := fub.get("Name"):
                names_to_ids.setdefault(name, []).append(int(fid))
        duplicates = {name: ids for name, ids in names_to_ids.items() if len(ids) > 1}

        issue_id = f"duplicate_plan_names_{self.server_id}"
        if not duplicates:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        plans_str = "; ".join(
            f"'{name}' (fub {', '.join(str(fid) for fid in sorted(ids))})" for name, ids in sorted(duplicates.items())
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="duplicate_plan_names",
            translation_placeholders={"plans": plans_str},
        )

    def _active_plan_selector_state(self) -> State | None:
        """State of this server's Function Plan selector entity, or None if not registered yet."""
        uid = f"comexio_{self.server_id}_logikplan_plan_selector"
        entity_id = er.async_get(self.hass).async_get_entity_id("select", DOMAIN, uid)
        return self.hass.states.get(entity_id) if entity_id else None

    def _function_plan_check_fub_ids(self) -> set[int]:
        """Collect the fub_ids of every managed plan relevant for the wiring audit.

        Union of the plan selected in the selector entity (with the persisted option as
        startup fallback) and every cluster plan from CONF_FUNCTION_PLAN_PLAN_MAP.
        """
        fub_ids: set[int] = set()
        # Resolved via get_active_function_plan_fub_id() (parses the "(ID n)" suffix) rather
        # than matching the selector state against the bare Name here: the selector label can
        # carry the "⏸ " inactive-plan prefix (see select.py's _plan_option_label), which a
        # direct Name comparison would never match — silently dropping the selected plan from
        # every audit below.
        if (active_fub_id := self.get_active_function_plan_fub_id()) is not None:
            fub_ids.add(active_fub_id)

        plan_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        if isinstance(plan_map, dict):
            fub_ids.update(int(v) for v in plan_map.values())
        return fub_ids

    async def _load_function_plan_check_data(self) -> dict[int, dict]:
        """Load wiring data of all managed function plans for batch marker checks.

        Prefers the bulk snapshot from the backup cycle; plans missing there are fetched
        directly. Returns {fub_id: {"elements": ..., "connections": ...}}; failed plans
        are skipped.
        """
        plans: dict[int, dict] = {}
        for fub_id in self._function_plan_check_fub_ids():
            plan_data = self.function_plan_plans.get(fub_id)
            if plan_data is None:
                try:
                    plan_data = await self.api.function_plan_load_elements(fub_id)
                except Exception:
                    _LOGGER.exception("[%s] Error loading function plan %s for link check", self.server_id, fub_id)
                    continue
            if plan_data:
                plans[fub_id] = plan_data
        if not plans:
            _LOGGER.debug("[%s] No function plans available for link check", self.server_id)
        return plans

    async def resolve_marker_cleanup_plans(
        self, marker_ids: list[int], preferred_fub_id: int | None = None
    ) -> dict[int, list[int]]:
        """Group markers by the managed plan they are wired in, for per-plan cleanup.

        A user-selected plan (preferred_fub_id) keeps the single-plan behaviour: all supplied
        markers are grouped under it as-is, without an upfront wiring check — the per-marker
        cleanup call itself is a no-op for any marker not actually wired there. Otherwise each
        marker is looked up in all managed plans and unwired markers are omitted here instead.
        """
        if preferred_fub_id is not None:
            return {preferred_fub_id: list(marker_ids)}
        plans = await self._load_function_plan_check_data()
        plan_to_ids: dict[int, list[int]] = {}
        for marker_id in marker_ids:
            if (fub_id := self._check_marker_function_plan_link(marker_id, plans)) is not None:
                plan_to_ids.setdefault(fub_id, []).append(marker_id)
        return plan_to_ids

    async def _resolve_unwire_plan_targets(
        self, webio_ids: list[int], preferred_fub_id: int | None
    ) -> tuple[dict[int, dict], dict[int, list[int]]]:
        """Resolve which plan(s) each webio_id needs to be unwired from.

        Scoped to preferred_fub_id when given; otherwise scans every managed plan for a
        wiring match. See unwire_webio_commands for the overall contract.
        """
        if preferred_fub_id is not None:
            plan_data = await self.api.function_plan_load_elements(preferred_fub_id)
            if not plan_data:
                return {}, {}
            return {preferred_fub_id: plan_data}, {preferred_fub_id: list(webio_ids)}

        plans = await self._load_function_plan_check_data()
        plan_to_ids: dict[int, list[int]] = {}
        for webio_id in webio_ids:
            for fub_id, plan_data in plans.items():
                if self.api._find_webio_wiring(webio_id, plan_data) is not None:
                    # No break: a stray duplicate element (e.g. left behind by a delete+
                    # recreate cycle, see _wired_source_webio_pairs) can reuse the same
                    # webIoId in more than one managed plan. Collecting into every matching
                    # plan instead of just the first keeps the promise made by
                    # unwire_webio_commands' docstring ("across managed plans") — cmd_ids/
                    # touched_fub_ids are deduplicated by the caller either way.
                    plan_to_ids.setdefault(fub_id, []).append(webio_id)
        return plans, plan_to_ids

    async def _unwire_plan(
        self, fub_id: int, ids: list[int], plan_data: dict, webio_id_to_cmd_id: dict[str, Any]
    ) -> dict[str, Any]:
        """Unwire the given webio_ids from ONE plan and resolve their real cmdIds.

        Returns {"deleted_elem_count", "cmd_ids", "stopped_plan" (name, fub_id)|None,
        "stop_failure" (name, fub_id)|None, "touched" bool} for this single plan — aggregated
        by the caller across every plan (see unwire_webio_commands).
        """
        elem_ids: list[int] = []
        found_webio_ids: list[int] = []
        for webio_id in ids:
            pair = self.api._find_webio_wiring(webio_id, plan_data)
            if pair is not None:
                elem_ids.extend(pair)
                found_webio_ids.append(webio_id)
        if not elem_ids:
            return {
                "deleted_elem_count": 0,
                "cmd_ids": [],
                "stopped_plan": None,
                "stop_failure": None,
                "touched": False,
            }

        elem_ids = list(dict.fromkeys(elem_ids))
        result = await self.api._delete_plan_elements_and_restart(
            fub_id, elem_ids, found_webio_ids, self.api.function_plan_name(fub_id)
        )
        stopped_plan = None
        stop_failure = None
        if result.get("stop_failed"):
            stop_failure = (result.get("plan_name", "?"), fub_id)
        elif result.get("plan_stopped") and result.get("fub_id") is not None:
            stopped_plan = (result.get("plan_name", "?"), result["fub_id"])

        # webio_cmd_ids echoes back only the ids actually unwired (empty on failure) —
        # resolving cmdIds only for those avoids deleting a command whose plan element
        # deletion never actually succeeded.
        cmd_ids: list[int] = []
        for webio_id in result.get("webio_cmd_ids", []):
            cmd_id = webio_id_to_cmd_id.get(str(webio_id))
            if cmd_id is not None:
                cmd_ids.append(cmd_id)
            else:
                _LOGGER.warning(
                    "[%s] unwire_webio_commands: no cmdId found for webIoId=%s, skipping command deletion",
                    self.server_id,
                    webio_id,
                )
        return {
            "deleted_elem_count": result.get("deleted_elem_count", 0),
            "cmd_ids": cmd_ids,
            "stopped_plan": stopped_plan,
            "stop_failure": stop_failure,
            "touched": result.get("deleted_elem_count", 0) > 0,
        }

    async def unwire_webio_commands(self, webio_ids: list[int], preferred_fub_id: int | None = None) -> dict[str, Any]:
        """Remove Function-Plan wiring for the given webIoIds and resolve their real cmdIds.

        For each webio_id: locates its WebIO element + connected source element (marker or
        IO) across managed plans (or just preferred_fub_id if given), deletes both, restarts
        the plan. The real Web-IO command id is resolved from self.data["webio_commands"]
        (webIoId -> cmdId) — never guessed from a plan element's ref_id, which IS the
        webIoId, not the WebCommandId (see project-logikplan-api memory).

        Returns {"deleted_elem_count": int, "cmd_ids": list[int], "stopped_plans": [...],
        "stop_failures": [...], "touched_fub_ids": list[int]}. cmd_ids only includes commands
        actually found wired and successfully unwired; the caller is responsible for deleting
        each via api.delete_single_command afterwards. touched_fub_ids lists plans that had at
        least one element successfully removed — useful for a caller that wants to re-sort the
        plan afterwards (deletion opens a gap in the grid that a sort would close).
        """
        webio_id_to_cmd_id = {
            str(cmd["webIoId"]): cmd.get("cmdId")
            for cmd in (self.data or {}).get("webio_commands", {}).values()
            if cmd.get("webIoId") is not None
        }
        plans, plan_to_ids = await self._resolve_unwire_plan_targets(webio_ids, preferred_fub_id)

        deleted_elem_count = 0
        cmd_ids: list[int] = []
        stopped_plans: list[tuple[str, int]] = []
        stop_failures: list[tuple[str, int]] = []
        touched_fub_ids: list[int] = []

        for fub_id, ids in plan_to_ids.items():
            plan_data = plans.get(fub_id)
            if not plan_data:
                continue
            plan_result = await self._unwire_plan(fub_id, ids, plan_data, webio_id_to_cmd_id)
            deleted_elem_count += plan_result["deleted_elem_count"]
            cmd_ids.extend(plan_result["cmd_ids"])
            if plan_result["touched"]:
                touched_fub_ids.append(fub_id)
            if plan_result["stop_failure"]:
                stop_failures.append(plan_result["stop_failure"])
            if plan_result["stopped_plan"]:
                stopped_plans.append(plan_result["stopped_plan"])

        return {
            "deleted_elem_count": deleted_elem_count,
            "cmd_ids": list(dict.fromkeys(cmd_ids)),
            "stopped_plans": stopped_plans,
            "stop_failures": stop_failures,
            "touched_fub_ids": list(dict.fromkeys(touched_fub_ids)),
        }

    def _check_marker_function_plan_link(self, marker_id: int, plans: dict[int, dict]) -> int | None:
        """Check if the marker is wired in any of the pre-loaded managed plans.

        Returns the fub_id of the first plan in which the marker element has a
        WebIO connection, else None.
        """
        for fub_id, plan_data in plans.items():
            if self._marker_wired_in_plan(marker_id, plan_data):
                return fub_id
        return None

    @staticmethod
    def _marker_wired_in_plan(marker_id: int, plan_data: dict) -> bool:
        """Check if the marker element in this plan has an outgoing connection."""
        all_matches = [
            elem_id
            for elem_id, elem_data in plan_data.get("elements", {}).items()
            if (ref := elem_data.get("reference", {})).get("type") == 2 and int(ref.get("ref_id", -1)) == marker_id
        ]
        if len(all_matches) > 1:
            _LOGGER.debug("_marker_wired_in_plan: M%s has MULTIPLE elements in this plan: %s", marker_id, all_matches)
        marker_elem_id = ComexioAPI._find_marker_element_id(plan_data.get("elements", {}), marker_id)
        if not marker_elem_id:
            _LOGGER.debug("_marker_wired_in_plan: M%s not found as an element in this plan", marker_id)
            return False

        # DEBUG: dump every connection touching ANY of the matched marker elements, in either role
        for conn_id, conn_data in plan_data.get("connections", {}).items():
            in_id = str(conn_data.get("input", {}).get("FubElementId", -1))
            out_ids = ComexioAPI._connection_output_ids(conn_data)
            if in_id in all_matches or any(oid in all_matches for oid in out_ids):
                _LOGGER.debug(
                    "_marker_wired_in_plan: M%s conn=%s touches a marker element (input=%s, outputs=%s, raw=%s)",
                    marker_id,
                    conn_id,
                    in_id,
                    out_ids,
                    conn_data,
                )

        input_ids = [
            str(conn_data.get("input", {}).get("FubElementId", -1))
            for conn_data in plan_data.get("connections", {}).values()
        ]
        wired = marker_elem_id in input_ids
        _LOGGER.debug(
            "_marker_wired_in_plan: M%s -> elem_id=%s (all_matches=%s), wired=%s",
            marker_id,
            marker_elem_id,
            all_matches,
            wired,
        )
        return wired

    @staticmethod
    def _plan_wired_pairs(plan_data: dict, source_type: str = "2") -> set[tuple[str, str]]:
        """(source ref_id, webIoId) pairs joined by a direct connection within one plan."""
        elem_refs: dict[str, tuple[str, str]] = {}
        for elem_id, elem in (plan_data.get("elements") or {}).items():
            ref = elem.get("reference") or {}
            ref_type = str(ref.get("type"))
            if ref_type in (source_type, "10"):
                elem_refs[str(elem_id)] = (ref_type, str(ref.get("ref_id")))
        pairs: set[tuple[str, str]] = set()
        for conn in (plan_data.get("connections") or {}).values():
            endpoint_ids = {str((conn.get("input") or {}).get("FubElementId"))}
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            endpoint_ids.update(str(o.get("FubElementId")) for o in outputs)

            endpoint_refs = [elem_refs[eid] for eid in endpoint_ids if eid in elem_refs]
            source_ids = {rid for typ, rid in endpoint_refs if typ == source_type}
            webio_ids = {rid for typ, rid in endpoint_refs if typ == "10"}
            pairs.update((s, w) for s in source_ids for w in webio_ids)
        return pairs

    @staticmethod
    def _connection_endpoint_ids(conn: dict) -> set[str]:
        """FubElementIds referenced by one connection — its single input plus every output."""
        endpoint_ids = {str((conn.get("input") or {}).get("FubElementId"))}
        outputs = conn.get("output") or []
        if isinstance(outputs, dict):
            outputs = list(outputs.values())
        endpoint_ids.update(str(o.get("FubElementId")) for o in outputs)
        return endpoint_ids

    @classmethod
    def _plan_connected_elem_ids(cls, plan_data: dict) -> set[str]:
        """elem_ids that are an endpoint of ANY connection in the plan, regardless of type.

        Used by delete_dangling_plan_elements to re-verify — against a freshly reloaded
        snapshot, at delete time — that a candidate element is still actually unconnected in
        THIS specific plan: the same ref_id can appear as a separate element instance in
        several managed plans, dangling in one but genuinely wired in another, so a match on
        ref_id+type alone is not enough to justify deleting it everywhere it turns up.
        """
        connected: set[str] = set()
        for conn in (plan_data.get("connections") or {}).values():
            connected.update(cls._connection_endpoint_ids(conn))
        return connected

    @classmethod
    def _plan_connected_source_ids(cls, plan_data: dict, source_type: str) -> set[str]:
        """ref_ids of `source_type` (marker "2" / IO "1") elements that are an endpoint of ANY
        connection in the plan, regardless of what's on the other end.

        Used by _dangling_source_ids as a conservative floor: a source element wired only to
        non-WebIO logic (a timer, comparator, AND/OR block, ...) is still very much in active
        use within the plan and must never be treated as WebIO debris just because it lacks a
        direct WebIO pairing — only an element with NO connection at all is unambiguously
        abandoned.
        """
        ref_by_elem_id: dict[str, str] = {}
        for elem_id, elem in (plan_data.get("elements") or {}).items():
            ref = elem.get("reference") or {}
            if str(ref.get("type")) == source_type:
                ref_by_elem_id[str(elem_id)] = str(ref.get("ref_id"))
        if not ref_by_elem_id:
            return set()

        connected: set[str] = set()
        for conn in (plan_data.get("connections") or {}).values():
            endpoint_ids = cls._connection_endpoint_ids(conn)
            connected.update(ref_by_elem_id[eid] for eid in endpoint_ids if eid in ref_by_elem_id)
        return connected

    def _connected_source_ids(self, source_type: str) -> set[str] | None:
        """ref_ids of `source_type` elements with ANY connection at all, across every plan
        relevant to the wiring audit — see _plan_connected_source_ids. Mirrors
        _wired_source_webio_pairs' scoping (same relevant_fub_ids, same None-while-not-loaded
        contract) since both feed the same audit cycle.
        """
        if not self.function_plan_plans:
            return None
        relevant_fub_ids = self._function_plan_check_fub_ids()
        connected: set[str] = set()
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id in relevant_fub_ids:
                connected.update(self._plan_connected_source_ids(plan_data, source_type))
        return connected

    def _wired_source_webio_pairs(self, source_type: str) -> set[tuple[str, str]] | None:
        """Return (ref_id, webIoId) pairs of source elements (marker "2" / IO "1") directly
        wired to a WebIO element in a managed plan.

        Built from the bulk snapshot of the backup cycle; returns None while that snapshot is
        not loaded yet. A pair only counts as wired when the source element and the WebIO
        (type-10) element are endpoints of the SAME connection — merely having the WebIO
        ref_id appear in some unrelated connection is not enough. ref_ids are device-local and
        the server assigns them globally increasing only "in practice", so a stray element of
        another Web-IO device (e.g. recreated during a delete+recreate cycle) could reuse a
        ref_id that used to belong to this device; requiring the direct edge makes such
        collisions harmless instead of masking a real gap.

        Scoped to _function_plan_check_fub_ids() (the selected plan + plan_map clusters) rather
        than every cached plan: an unrelated plan on the server (e.g. a scratch/test copy) can
        reference the same source and WebIO command without being wired the way the managed
        plan is, and scanning it too would mask a real gap in the managed plan as "wired
        elsewhere".
        """
        if not self.function_plan_plans:
            return None
        relevant_fub_ids = self._function_plan_check_fub_ids()
        pairs: set[tuple[str, str]] = set()
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id in relevant_fub_ids:
                pairs.update(self._plan_wired_pairs(plan_data, source_type))
        return pairs

    def _audit_wired_pairs(
        self, has_active_plan: bool, managed_io_exts: set[str]
    ) -> tuple[set[tuple[str, str]] | None, set[tuple[str, str]] | None, set[str] | None, set[str] | None]:
        """Wired (ref_id, webIoId) pair sets for the audit: (markers, IOs), plus the
        any-connection-at-all ref_id sets _dangling_source_ids needs alongside them (see there).

        Each set is None when its check is disabled (no active plan / no managed IO
        extensions) or the bulk plan snapshot is not loaded yet (recheck scheduled).
        """
        wired_marker_pairs: set[tuple[str, str]] | None = None
        connected_marker_ids: set[str] | None = None
        if has_active_plan:
            wired_marker_pairs = self._wired_source_webio_pairs("2")
            connected_marker_ids = self._connected_source_ids("2")
            if wired_marker_pairs is None:
                self._lp_missing_recheck_pending = True
        wired_io_pairs: set[tuple[str, str]] | None = None
        connected_io_ids: set[str] | None = None
        if managed_io_exts:
            wired_io_pairs = self._wired_source_webio_pairs("1")
            connected_io_ids = self._connected_source_ids("1")
            if wired_io_pairs is None:
                self._lp_missing_recheck_pending = True
        return wired_marker_pairs, wired_io_pairs, connected_marker_ids, connected_io_ids

    def _dangling_source_ids(
        self,
        source_type: str,
        wired_pairs: set[tuple[str, str]] | None,
        connected_ids: set[str] | None,
    ) -> set[str]:
        """ref_ids of marker ("2") / IO ("1") elements present in a managed plan but never
        paired with a WebIO element there AND without any other connection either —
        Function-Plan debris left behind when a Web-IO command/element was removed (e.g.
        directly in Comexio Studio) without also removing its wired source element.

        `wired_pairs`/`connected_ids` (from _audit_wired_pairs) are used only as the "bulk
        snapshot loaded yet" signal here — the actual wired/connected floor is recomputed
        PER PLAN below (via _plan_wired_pairs / _plan_connected_source_ids) and a ref_id is
        unioned in as soon as ANY relevant plan has an unconnected, unwired instance of it.
        The same ref_id can be a separate element instance in more than one managed plan —
        wired or otherwise connected in one, genuinely orphaned in another — so subtracting
        a floor combined across all plans (as an earlier version of this method did) would
        hide real debris in the second plan just because the first one still uses it.
        """
        if wired_pairs is None or connected_ids is None or not self.function_plan_plans:
            return set()
        relevant_fub_ids = self._function_plan_check_fub_ids()
        dangling: set[str] = set()
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id not in relevant_fub_ids:
                continue
            present_ids: set[str] = set()
            for elem in (plan_data.get("elements") or {}).values():
                ref = elem.get("reference") or {}
                if str(ref.get("type")) == source_type:
                    present_ids.add(str(ref.get("ref_id")))
            if not present_ids:
                continue
            plan_wired_ids = {rid for rid, _ in self._plan_wired_pairs(plan_data, source_type)}
            plan_connected_ids = self._plan_connected_source_ids(plan_data, source_type)
            dangling.update(present_ids - plan_wired_ids - plan_connected_ids)
        return dangling

    async def delete_dangling_plan_elements(self, source_type: str, ref_ids: list[str]) -> dict[str, Any]:
        """Delete Function-Plan marker/IO elements left behind after their WebIO counterpart
        was already removed — the debris _dangling_source_ids detects every audit cycle.

        Unlike unwire_webio_commands, there is no Web-IO command to resolve/delete here (it's
        already gone); this is a plain element delete + plan restart. Returns
        {"deleted_elem_count": int, "touched_fub_ids": list[int]}.

        `ref_ids` reflects a snapshot potentially taken cycles ago and is a global judgement
        (dangling across all managed plans combined) — the same ref_id can also exist as a
        separate, still-genuinely-wired element instance in another plan. Each candidate is
        therefore re-verified against the freshly reloaded snapshot here, per plan, via
        _plan_connected_elem_ids, instead of trusting the ref_id match alone.
        """
        plans = await self._load_function_plan_check_data()
        plan_to_elem_ids: dict[int, list[int]] = {}
        for fub_id, plan_data in plans.items():
            connected_elem_ids = self._plan_connected_elem_ids(plan_data)
            for elem_id, elem in (plan_data.get("elements") or {}).items():
                ref = elem.get("reference") or {}
                if (
                    str(ref.get("type")) == source_type
                    and str(ref.get("ref_id")) in ref_ids
                    and str(elem_id) not in connected_elem_ids
                ):
                    plan_to_elem_ids.setdefault(fub_id, []).append(int(elem_id))

        deleted_elem_count = 0
        touched_fub_ids: list[int] = []
        for fub_id, elem_ids in plan_to_elem_ids.items():
            result = await self.api._delete_plan_elements_and_restart(
                fub_id, elem_ids, [], self.api.function_plan_name(fub_id)
            )
            if result.get("deleted_elem_count", 0) > 0:
                deleted_elem_count += result["deleted_elem_count"]
                touched_fub_ids.append(fub_id)
        return {"deleted_elem_count": deleted_elem_count, "touched_fub_ids": touched_fub_ids}

    @staticmethod
    def _function_plan_gap_item(
        key: str,
        ha_name: str,
        best_match: dict,
        wired_pairs: tuple[set[tuple[str, str]] | None, set[tuple[str, str]] | None],
        io_meta: dict | None,
        managed_io_exts: set[str],
    ) -> dict[str, Any] | None:
        """Missing-wiring item for one audit key, or None when the pair is wired or unchecked.

        wired_pairs = (marker pairs, IO pairs); a set of None means that check is disabled
        or not yet loadable this run. Marker keys ("M<id>") are checked against the marker
        set; IO keys only when their extension is opted into IO cluster management.
        """
        wired_marker_pairs, wired_io_pairs = wired_pairs
        web_id = str(best_match.get("webIoId"))
        if key.startswith("M"):
            if wired_marker_pairs is not None and (key[1:], web_id) not in wired_marker_pairs:
                return {"name": ha_name, "marker_id": int(key[1:])}
            return None
        if io_meta is None or io_meta["ext_name"] not in managed_io_exts or wired_io_pairs is None:
            return None
        if (str(io_meta["id"]), web_id) not in wired_io_pairs:
            return {"name": ha_name, "ext_name": io_meta["ext_name"], "identifier": io_meta["identifier"]}
        return None

    def _function_plan_missing_detail(
        self, missing_items: list[dict], ha_map: dict, io_meta_by_key: dict
    ) -> dict[str, Any]:
        """Split missing-wiring items into per-cluster marker gaps and per-extension IO gaps.

        The repair dialog uses this to word whole-cluster gaps (cluster plan never created,
        or deleted directly in Comexio — nothing placed/wired yet) differently from
        individual missing connections. Mirrors ios_by_ext (ext_name -> [gap, total]) with
        markers_by_plan (marker cluster plan name -> [gap, total]), bucketed the same way
        the cluster plans themselves are named (_cluster_plan_name).
        """
        prefix = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX)
        cluster_size = int(
            self.config_entry.options.get(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
            )
        )
        gaps_by_plan: dict[str, int] = {}
        for item in missing_items:
            if "marker_id" in item:
                plan_name = self._cluster_plan_name(item["marker_id"], prefix, cluster_size)
                gaps_by_plan[plan_name] = gaps_by_plan.get(plan_name, 0) + 1
        totals_by_plan = dict.fromkeys(gaps_by_plan, 0)
        for key in ha_map:
            if key.startswith("M"):
                plan_name = self._cluster_plan_name(int(key[1:]), prefix, cluster_size)
                if plan_name in totals_by_plan:
                    totals_by_plan[plan_name] += 1

        gaps_by_ext: dict[str, int] = {}
        for item in missing_items:
            if "ext_name" in item:
                gaps_by_ext[item["ext_name"]] = gaps_by_ext.get(item["ext_name"], 0) + 1
        totals_by_ext = dict.fromkeys(gaps_by_ext, 0)
        for meta in io_meta_by_key.values():
            if meta["ext_name"] in totals_by_ext:
                totals_by_ext[meta["ext_name"]] += 1
        return {
            "markers_by_plan": {name: [gaps_by_plan[name], totals_by_plan[name]] for name in sorted(gaps_by_plan)},
            "ios_by_ext": {ext: [gaps_by_ext[ext], totals_by_ext[ext]] for ext in sorted(gaps_by_ext)},
        }

    def _function_plan_missing_eta_sec(self, missing_items: list[dict]) -> int:
        """Estimate the duration of the add-pairs repair action in seconds.

        Per affected cluster plan the finalize step dominates, and its expensive part —
        saving positions and reactivating the plan — scales with the number of elements
        already in that plan (activating a full plan takes ~10x longer than a small one,
        measured live). Element counts come from the bulk snapshot cache; a plan that
        does not exist yet only holds its new pairs (2 elements each).
        """
        if not missing_items:
            return 0
        prefix = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX)
        cluster_size = int(
            self.config_entry.options.get(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
            )
        )
        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}

        new_pairs_per_plan: dict[str, int] = {}
        for item in missing_items:
            if "marker_id" in item:
                plan_name = self._cluster_plan_name(item["marker_id"], prefix, cluster_size)
            else:
                plan_name = self._io_cluster_plan_name_for_ext(item["ext_name"], prefix)
            new_pairs_per_plan[plan_name] = new_pairs_per_plan.get(plan_name, 0) + 1

        total = 0.0
        for plan_name, new_pairs in new_pairs_per_plan.items():
            plan_data = self.function_plan_plans.get(plan_map.get(plan_name, -1))
            elements = len(plan_data.get("elements") or {}) if plan_data else 0
            total += (
                SYNC_DURATION_FUNCTION_PLAN_FINALIZE + (elements + 2 * new_pairs) * SYNC_DURATION_FUNCTION_PLAN_ELEMENT
            )
        return round(total)

    def _io_cluster_plan_name_for_ext(self, ext_name: str, prefix: str) -> str:
        """Plan name the extension belongs to (existing membership) or would get (new plan)."""
        for members in self._io_plan_membership(prefix).values():
            if ext_name in members:
                return self._io_plan_name(prefix, members)
        return self._io_plan_name(prefix, [ext_name])
