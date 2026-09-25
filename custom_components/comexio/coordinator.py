# Version: 0.8.1
import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import pathlib
import re
import socket
import time
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util, slugify

from .api import ComexioAPI
from .cleanup_scope import (
    CLEANUP_SCOPE_FULL,
    CLEANUP_SCOPE_KNX,
    SKIPPED_KNX_BRIDGE_MARKERS,
    TRIGGER_PLAN_DELETE,
    TRIGGER_PLAN_KEEP,
    TRIGGER_PLAN_REMOVE_PAIRS,
    has_knx_artifacts,
    plans_in_scope,
    scope_counts,
    scope_includes_knx,
    scope_trigger_ref_type,
    trigger_plan_action,
    trigger_sources_by_category,
    webio_classes_in_scope,
)
from .const import (
    BUS_LOAD_FAIL_STREAK_THRESHOLD,
    BUS_LOAD_POLL_INTERVAL_SEC,
    BUS_LOAD_RISE_THRESHOLD_PCT,
    BUS_LOAD_RISE_WINDOW_SEC,
    BUS_LOAD_SMOOTHING_SAMPLES,
    BUS_LOAD_STARTUP_GRACE_SEC,
    CASCADE_COOLDOWN_SEC,
    CASCADE_POST_RESTART_SETTLE_SEC,
    CASCADE_POST_STOP_WAIT_SEC,
    CASCADE_RECOVERY_DROP_PCT,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_BUS_WATCHDOG_AUTO_REBOOT,
    CONF_BUS_WATCHDOG_ENABLED,
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    CONF_FUNCTION_PLAN_FUB_ID,
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
    CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_FUNCTION_PLAN_PLAN_PREFIX,
    CONF_HOST,
    CONF_KNX_DPT_SUFFIX_IGNORED,
    CONF_KNX_PRERELEASE_CLEANUP_PENDING,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_STATISTICS_CLEANUP_IGNORED,
    CONF_USERNAME,
    DEFAULT_BUS_WATCHDOG_AUTO_REBOOT,
    DEFAULT_BUS_WATCHDOG_ENABLED,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    DEFAULT_FUNCTION_PLAN_PLAN_PREFIX,
    DOMAIN,
    EMERGENCY_REBOOT_COOLDOWN_SEC,
    EMERGENCY_REBOOT_THRESHOLD_PCT,
    EMERGENCY_REBOOT_WINDOW_SEC,
    EVENT_PLAN_VALUE,
    FIRMWARE_CHECK_HOUR,
    FIRMWARE_CHECK_MINUTE,
    FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC,
    FUNCTION_PLAN_FUB_ID_AUTO,
    FUNCTION_PLAN_KNX_CLUSTER_SIZE,
    FUNCTION_PLAN_LAYOUT_COMMENT_Y,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    ICON_ADD,
    ICON_DELETE,
    ICON_FIX,
    ICON_LINK,
    ICON_NETWORK,
    ICON_RENAME,
    ICON_WARNING,
    ISSUE_KNX_PRERELEASE_CLEANUP,
    KNX_DPT_AUTOTAG_MAX_RETRIES,
    MARKER_READ_ONLY_SUFFIX,
    RANGE_CHECK_CHECKED,
    RANGE_CHECK_CORRECTION_FAILED,
    RANGE_CHECK_EXCLUDED,
    RANGE_CHECK_FAILED,
    RANGE_CHECK_FIXED,
    RANGE_CHECK_SKIPPED,
    SOURCE_CATEGORIES,
    SYNC_DURATION_FUNCTION_PLAN_ELEMENT,
    SYNC_DURATION_FUNCTION_PLAN_FINALIZE,
    UNINSTALL_CLEANUP_PROGRESS_EVERY,
    WATCHDOG_HISTORY_MAX_ENTRIES,
    WEBHOOK_UNKNOWN_IO_LOG_MSG,
    WEBHOOK_VALUE_LOG_MSG,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_CLASSES,
    WEBIO_DEVICE_NAME_KNX_LOOPBACK,
    WEBIO_RANGE_CHECK_HOUR,
    WEBIO_RANGE_CHECK_MINUTE,
    MarkerKind,
    WebioClass,
    active_webio_classes,
    bus_load_signal,
    category_by_fub_module_type,
    classify_audit_key,
    expand_ignored_marker_ids,
    fw_update_signal,
    io_audit_key,
    io_column_rows,
    snap_to_grid,
    source_audit_key,
    source_category,
    trigger_pair_categories,
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

# Marks a function_plan_plans[fub_id] entry seeded by _create_managed_plan (verified-empty at
# creation time) rather than loaded from a real function_plan_load_all_plans()/loadelements()
# response. _relevant_plans_loaded()-style "is this fub_id known at all" checks treat a seeded
# entry the same as real data (accurate, since it genuinely was empty at seed time) — but
# _load_function_plan_check_data() must NOT: its contract is "cache miss -> live fetch", and a
# seeded entry can already be stale by the time it's consulted (real wiring written afterward,
# e.g. by function_plan_add_source_pairs, never gets mirrored back into this cache).
_SEEDED_EMPTY_PLAN_MARKER = "_seeded_empty"

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


def _plan_ref_ids(elements: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """marker_ids/io_ids/knx_ids (type-2/type-1/type-11 element refs) referenced by a plan's elements.

    Drives _fire_plan_event's gating of the comexio_plan_event bus events (the plan card's
    debug box) — only a value push belonging to the currently-displayed plan is published.
    """
    marker_ids: set[str] = set()
    io_ids: set[str] = set()
    knx_ids: set[str] = set()
    for elem in elements.values():
        ref = elem.get("reference") or {}
        # Comexio encodes reference.type as int or string depending on the response shape —
        # normalize like every other ref.type check in this codebase (_function_plan_existing_refs
        # et al.); a bare int compare would silently drop string-typed refs.
        if str(ref.get("type")) == "2":
            marker_ids.add(str(ref.get("ref_id")))
        elif str(ref.get("type")) == "1":
            io_ids.add(str(ref.get("ref_id")))
        elif str(ref.get("type")) == "11":  # blind guess: KNX objects use $FubModules key "11"
            knx_ids.add(str(ref.get("ref_id")))
    return marker_ids, io_ids, knx_ids


def _count_by_ref(by_ref: dict[int, list[int]]) -> int:
    """Total id count across all source categories of a per-ref_type trigger-audit map."""
    return sum(len(ids) for ids in by_ref.values())


def _format_by_ref(by_ref: dict[int, list[int]]) -> str:
    """Join a per-ref_type trigger-audit map to 'M<id>, K<id>, …' using each category's audit prefix."""
    return ", ".join(
        f"{category_by_fub_module_type(ref_type).audit_key_prefix}{mid}"
        for ref_type, ids in by_ref.items()
        for mid in ids
    )


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


def _marker_reset_progress(report: Callable[[str], None], step: str) -> Callable[[int, int], None]:
    """Progress callback for the bridge-marker reset: a status line every
    UNINSTALL_CLEANUP_PROGRESS_EVERY markers (and on the last), with an ETA from the rate so far."""
    t0 = time.monotonic()

    def _cb(done: int, total: int) -> None:
        if done != total and done % UNINSTALL_CLEANUP_PROGRESS_EVERY:
            return
        remaining = (time.monotonic() - t0) / done * (total - done)
        report(f"{step}: resetting KNX bridge markers {done}/{total} (about {int(remaining)} s left)")

    return _cb


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
        # fub_ids present in function_plan_plans as of the last backup cycle — lets the cycle
        # tell "a relevant plan just landed" apart from "nothing changed, but a plan the bulk
        # endpoint never delivers is still missing" (e.g. a persistently malformed entry skipped
        # by function_plan_load_all_plans). Without this, a recheck that can never succeed would
        # retrigger async_request_refresh() on every single backup cycle forever.
        self._last_bulk_snapshot_fub_ids: frozenset[int] = frozenset()
        # Marker IDs (type-2 element ref_ids) referenced in a plan as of the last parse_config
        # call — an unnamed marker still needs a real entity/value if it's wired somewhere (see
        # api._process_markers). Tracked here so a change triggers an immediate extra refresh
        # instead of waiting for the next scheduled poll (which could be hours away with a long
        # scan_interval).
        self._last_referenced_marker_ids: set[str] = set()
        self.marker_states: dict[str, Any] = {}
        self.io_states: dict[str, Any] = {}
        self.knx_states: dict[str, Any] = {}
        # O(1) lookup index for webhook updates: (ext_name_lower, identifier_lower) -> io dict
        self._io_index: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit_ignored: bool = False
        self.last_audit_failed: bool = False
        self.last_summary_hash: str | None = None
        self.in_sync: bool = False
        # Whether the most recent _async_update_data run actually scraped the server: a poll
        # skipped for in_sync, or one whose get_raw_config came back empty ({} on a non-200),
        # still "succeeds" with old or empty data. Destructive callers check this instead of
        # last_update_success alone.
        self._last_poll_scraped: bool = False
        # Whether the last scraped config holds titled KNX bridge markers — taken from the
        # unfiltered parse, since data["markers"] is empty when marker import is off.
        self._knx_bridge_markers_present: bool = False
        self.sync_error: bool = False
        self.sync_progress_text: str = "Idle"
        self.sync_progress_pct: int | None = None
        self.sync_current_step: str | None = None
        self.last_audit_results: dict[str, Any] = {}
        # (webio_class value, source id) tuples — markers and KNX objects share a numeric id
        # namespace, so the category tag keeps the cleanup button routing them apart.
        self._cleanup_entity_ids: list[tuple[str, int]] = []
        self._cleanup_function_plan_count: int = 0
        # fub_ids a _verify_new_plan_is_empty() check rejected as contaminated/unverifiable
        # AND couldn't fully un-register (delete_fup failed). A plain cache pop isn't durable
        # here — the very next parse_config() (every poll, and the reload every sync ends
        # with) repopulates fub_data wholesale from Comexio's still-live $Fubs listing, which
        # would let _resolve_single_cluster_plan's by-name scan silently re-adopt the same
        # poisoned plan. Session-local only (cleared on HA restart, same as every other
        # in-memory coordinator field) — a human still needs to clean it up in Comexio Studio.
        self._distrusted_fub_ids: set[int] = set()
        self.cancel_sync: bool = False
        self.entity_id_mismatches: list[dict[str, str]] = []
        self.orphaned_statistics: list[str] = []
        self.offline_entity_statistic_ids: set[str] = set()
        self.offline_extensions: set[str] | None = None
        self._extension_offline_issue_active: bool = False
        # Per-category item counts from the last successful poll, unfiltered by import_*
        # opt-in (unlike final_data) but still subject to parse_config()'s own filtering
        # (e.g. unnamed markers, offline IOs) — {} before the first poll. Lets options_flow
        # show/hide an opt-in toggle (e.g. import_knx) based on whether the Comexio server
        # currently has that category's objects at all, without options_flow itself needing
        # to know about parse_config().
        self.available_source_counts: dict[WebioClass, int] = {}
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
        # Per-id consecutive-failure count for _auto_suffix_unambiguous_knx's rename attempts.
        # A transient error (momentary HTTP hiccup) gets a few retries across the next few
        # polls; only once an id reaches KNX_DPT_AUTOTAG_MAX_RETRIES is it treated as
        # permanently stuck (stale admin session, name collision, ...) and skipped for the
        # rest of this coordinator's lifetime — reset by an integration reload/restart, which
        # is also how the user is expected to recover after fixing the underlying cause.
        self._knx_dpt_autotag_fail_counts: dict[int, int] = {}
        # Debounce handle for _schedule_knx_dpt_reload (Any: same async_call_later() cancel
        # callable type as _preview_refresh_cancel below) — at most one pending reload timer
        # no matter how many KNX objects get auto-tagged within the same poll or across
        # consecutive polls before the first timer fires.
        self._knx_dpt_reload_cancel: Any = None
        # R1: Track which markers/IOs/KNX objects received a webhook update during the last
        # API fetch — see the R1 merge in _async_update_data. KNX gained a real per-object
        # live-value query (get_live_states) 2026-09-20; before that it had no authoritative
        # freshly-parsed value to race against, so this set is a recent addition for KNX.
        self._webhook_updated_markers: set[str] = set()
        self._webhook_updated_io_ids: set[str] = set()
        self._webhook_updated_knx_ids: set[str] = set()
        self.last_plan_preview: dict[str, Any] | None = None
        # In-memory copy of the last rendered preview SVG, served directly by the
        # image entity (image.py) without touching the config/www file.
        self.last_plan_preview_svg: str | None = None
        # Live-value preview refresh (Stufe 1): plan structure of the last LIVE preview is
        # cached so webhook pushes can re-render it with fresh values without re-fetching
        # the plan from Comexio; a shown snapshot clears the cache (it must not be
        # overwritten by live refreshes). Structural plan edits need a new button press.
        # INVARIANT: only ever reassigned wholesale (to a fresh dict literal or None), never
        # mutated in place. _async_poll_connection_values' stale-request guard compares object
        # identity (self._preview_plan_cache is not cache) to notice a mid-await arm/disarm/
        # switch; an in-place self._preview_plan_cache[...] = ... would keep the identity and
        # silently defeat that guard. Re-arming the same plan still allocates a new dict.
        self._preview_plan_cache: dict[str, Any] | None = None
        # Bumped every time the cache above is cleared (explicit stop, auto-stop, poll-failure
        # disarm, or coordinator shutdown) OR re-armed for a different render
        # (_update_live_preview_cache / _update_snapshot_preview_cache). async_generate_plan_preview
        # captures this at entry and skips its cache-committing tail if it changed while the render
        # was awaiting I/O — otherwise a stale render finishing after stop_preview() would resurrect
        # a preview that was already told to stop, or a slow render could clobber a newer,
        # concurrently-armed one for a different plan (#77).
        self._preview_cache_generation: int = 0
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
        # Extension identity registry: {serial -> {"name": ext_name}}. Lets a Comexio-side
        # extension rename be detected (same stable serial, different name) and migrated in
        # place instead of being treated as a deleted+recreated extension. See
        # async_detect_and_migrate_extension_renames.
        self.extension_registry: dict[str, dict[str, str]] = {}
        self._extension_registry_store: Store = Store(hass, 1, f"{DOMAIN}_extension_registry_{self.server_id}")
        # Bus workload monitoring: latest reading from the independent fast poll
        # (see async_start_bus_load_poll / _async_bus_load_tick). None until the first tick.
        self.bus_workload: int | None = None
        self.bus_sd_card: bool | None = None
        self._bus_load_fail_streak = 0
        # Bus-Load-Watchdog: rolling sample buffer feeding rise/emergency detection (see
        # _evaluate_bus_load_watchdog), trimmed to the longer of the two detection windows.
        # The lock also blocks a concurrent cascade and emergency reboot from overlapping.
        self._bus_load_samples: deque[tuple[datetime, int]] = deque()
        self._watchdog_lock: asyncio.Lock = asyncio.Lock()
        self._watchdog_cooldown_until: datetime | None = None
        self._watchdog_started_at: datetime = dt_util.utcnow()
        self.watchdog_history: list[dict[str, Any]] = []
        self._watchdog_history_store: Store = Store(hass, 1, f"{DOMAIN}_watchdog_history_{self.server_id}")
        # KNX DPT catalog: persisted so a fresh ComexioAPI instance (created on every restart
        # AND every config-entry reload, not just a full HA restart) still has a fallback
        # catalog if its very first live fetch fails — without this, api.get_knx_dpt_catalog's
        # own in-process stale-cache fallback can't help yet (nothing has been fetched in this
        # process), so a transient failure right after a reload would return {} and silently
        # drop knx_composite tagging for that poll (Sourcery finding, review 2026-09-21; see
        # async_load_knx_dpt_catalog).
        self._knx_dpt_catalog_store: Store = Store(hass, 1, f"{DOMAIN}_knx_dpt_catalog_{self.server_id}")
        self._last_persisted_knx_dpt_catalog_version: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch configuration and perform smart audit including Type-Checks."""
        self._last_poll_scraped = False
        if self.in_sync:
            _LOGGER.debug("[%s] Periodic audit skipped: Manual sync or repair is currently in progress", self.server_id)
            return self.data

        _LOGGER.debug("[%s] Starting periodic configuration audit to detect mismatches", self.server_id)

        # R1: Clear dirty sets before async fetches so any webhooks arriving during
        # the HTTP round-trips are tracked and win over the (older) API snapshot.
        self._webhook_updated_markers.clear()
        self._webhook_updated_io_ids.clear()
        self._webhook_updated_knx_ids.clear()

        try:
            conf = {**self.config_entry.data, **self.config_entry.options}

            # Precompute cover keywords once per update
            kw_str = str(conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS))
            self.cover_keywords = [kw.strip().lower() for kw in kw_str.split(",") if kw.strip()]

            import_markers = conf.get("import_markers", True)
            import_ios = conf.get("import_ios", True)
            import_knx = conf.get("import_knx", False)

            # Fetch current raw configuration from the Comexio API
            raw_config = await self.api.get_raw_config()
            self._last_poll_scraped = bool(raw_config.get("FubModules"))
            marker_data = raw_config.get("FubModules", {}).get("2", {})
            max_id = max(int(m.get("Id", 0)) for m in marker_data.values()) if marker_data else 0

            # KNX groups are frequently small and gap-free (e.g. K1-K10), which is exactly the
            # shape Comexio serializes as a JSON array instead of an object (see
            # api._process_source_items's docstring) — handle both shapes, unlike marker_data
            # above, which has never been observed as an array in practice.
            knx_group = raw_config.get("FubModules", {}).get("11", {})
            knx_items = knx_group.values() if isinstance(knx_group, dict) else (knx_group or [])
            # Same per-item guard as api._process_source_items, which parses this exact group:
            # a malformed entry (non-dict, or Id missing/None) must be skipped here too, or a
            # single bad KNX record raises out of this comprehension and fails the entire poll.
            knx_max_id = max(
                (int(k["Id"]) for k in knx_items if isinstance(k, dict) and k.get("Id") is not None),
                default=0,
            )

            live_states, knx_live_states = await self.api.get_live_states(max_id, knx_max_id)
            if live_states is None:
                # Fetch/parse failure this cycle (see get_live_states' docstring) — keep last
                # known values instead of letting parse_config default every item to 0/off.
                _LOGGER.warning("[%s] Live states fetch failed; keeping last known values", self.server_id)
                live_states = self.marker_states
            if knx_live_states is None:
                knx_live_states = self.knx_states
            # Cold start: the bulk plan snapshot isn't loaded yet (it lands after this first
            # cycle, via the backup cycle) — fall back to the last stored auto-backup so an
            # unnamed-but-wired marker still gets an entity on every restart, not just after
            # the first backup cycle has run.
            referenced_markers = self._referenced_marker_ids()
            if referenced_markers is None:
                referenced_markers = await self.function_plan_backup.async_referenced_marker_ids()
            self._last_referenced_marker_ids = referenced_markers
            # Only fetched when KNX import is enabled — an extra HTTP round-trip nobody without
            # KNX objects needs. get_knx_dpt_catalog() never raises (own contract, {} on
            # failure), so an unreachable/failed fetch just leaves every KNX analog item on its
            # generic fallback range rather than failing this whole poll.
            knx_dpt_catalog = await self.api.get_knx_dpt_catalog() if import_knx else None
            if import_knx and knx_dpt_catalog:
                await self._maybe_persist_knx_dpt_catalog()
            parsed_data = self.api.parse_config(
                raw_config, live_states, referenced_markers, knx_live_states, knx_dpt_catalog
            )
            # Unfiltered per-category counts — parsed_data carries every category regardless of
            # import_* opt-in, unlike final_data below. See available_source_counts docstring.
            # Held locally and only published to self.available_source_counts right before the
            # final `return final_data` below — this dict is built early in the poll, well
            # before the rest of this method (audits, IP checks, Function Plan sync) has had a
            # chance to fail, and the attribute's contract is "last *successful* poll". Writing
            # it here directly would leak counts from a poll that ends up raising further down.
            source_counts = {cat.key: len(parsed_data.get(cat.data_key, [])) for cat in SOURCE_CATEGORIES.values()}
            self._knx_bridge_markers_present = any(
                m.get("kind") == MarkerKind.KNX_BRIDGE for m in parsed_data.get("markers", [])
            )

            # async_update_from_raw_config never raises (own contract, enforced internally) —
            # no local guard needed here.
            await self.function_plan_catalog.async_update_from_raw_config(raw_config, self.api.comexio_version)

            final_data = {
                "markers": parsed_data["markers"] if import_markers else [],
                "io": parsed_data["io"] if import_ios else [],
                "io_all": parsed_data.get("io_all", []) if import_ios else [],
                "knx": parsed_data.get("knx", []) if import_knx else [],
                "webio_commands": parsed_data.get("webio_commands", {}),
                "webio_names": parsed_data.get("webio_names", {}),
                "webio_devices": parsed_data.get("webio_devices", {}),
                "extensions": parsed_data.get("extensions", {}),
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

            # KNX now follows the same R1 pattern as markers/IO above: get_live_states() gained
            # a real per-object KNX query 2026-09-20 (live-tested against a real KNX-equipped
            # Comexio instance, see project_knx_write_path_design memory) — a webhook that fired
            # during the get_raw_config/get_live_states round-trip wins over this poll's (older)
            # snapshot; otherwise the fresh, authoritative poll value wins and is cached.
            #
            # knx_live_states membership is checked explicitly (not just "value differs from
            # cache") because api._build_source_item defaults a KNX id absent from the dashboard
            # response to 0 — an HTTP 200 that simply omits one requested key (partial refresh,
            # unsupported/stale K-element) would otherwise overwrite a real cached value with
            # that 0 and make the entity report off/0 until the object reappears in a response
            # (Sourcery finding, review 2026-09-21).
            for k in final_data["knx"]:
                if k["id"] in self._webhook_updated_knx_ids:
                    k["value"] = self.knx_states.get(k["id"], k["value"])
                elif k["id"] in knx_live_states:
                    self.knx_states[k["id"]] = k["value"]
                else:
                    k["value"] = self.knx_states.get(k["id"], k["value"])

            # Prune knx_states down to the object ids the server still reports. The merge loop
            # above only revisits ids currently present in final_data["knx"] — a value cached
            # for a since-deleted KNX object would otherwise linger forever and be inherited by
            # a different object that later reuses the same numeric id. Keyed off parsed_data
            # (not final_data) so the cache stays correct even while import_knx is off, and
            # gated on a non-empty scrape (get_raw_config returns {} on a transient
            # HTTP failure — pruning then would wipe every cached value over a blip).
            if raw_config.get("FubModules"):
                known_knx_ids = {k["id"] for k in parsed_data.get("knx", [])}
                self.knx_states = {kid: v for kid, v in self.knx_states.items() if kid in known_knx_ids}

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

            # --- IGNORED SOURCES AUDIT (markers=2/KNX=11, registry-driven) ---
            # Reset the shared cleanup accumulator once per cycle; each wrapper below extends it
            # rather than overwriting, so neither category's contribution clobbers the other's.
            self._cleanup_entity_ids = []
            self._cleanup_function_plan_count = 0
            await self.async_check_ignored_markers(conf, final_data)
            await self.async_check_ignored_knx(conf, final_data)

            # --- SMART AUDIT LOGIC ---
            com_commands = final_data["webio_commands"]

            # 1. HA Map: Markers + KNX objects (registry-driven over range_clustered categories)
            # Ignored ids are intentionally excluded from the Web-IO/Function Plan sync (see
            # CONF_IGNORED_MARKERS/CONF_IGNORED_KNX) — leaving them in would make the audit
            # report them as permanently "missing" and let Full Sync / create_missing actually
            # create and wire Web-IO commands for sources the user explicitly opted out of.
            # Auto-created write-path bridge Markers (kind == KNX_BRIDGE, title suffix
            # "[K<id>]" — see MARKER_KNX_BRIDGE_SUFFIX_RE) are excluded the same way: they are
            # pure internal wiring glue with no HA entity and no Web-IO command of their own
            # (see project_knx_write_path_design memory) — without this they permanently show
            # up as "Fehlend" since no webIO is ever expected to exist for them.
            ha_map = {}
            for cat in SOURCE_CATEGORIES.values():
                if not cat.range_clustered:
                    continue
                ignored_ids = self.ignored_ids_for(cat.key)
                for item in final_data[cat.data_key]:
                    if int(item["id"]) in ignored_ids or item.get("kind") == MarkerKind.KNX_BRIDGE:
                        continue
                    ha_map[source_audit_key(cat, item["id"])] = {
                        "name": f"HA {item['name']}",
                        "type": item["type"],  # Trusting the preprocessing of api.py
                    }

            # 2. HA Map: IOs (own composite ext_name+identifier key shape, kept as its own block)
            io_meta_by_key: dict[str, dict[str, Any]] = {}
            for io in final_data["io"]:
                key = io_audit_key(io["ext_name"], io["identifier"])

                # Since api.py now provides 'is_binary', derive
                # the audit type ('digital'/'analog') here:
                mapped_type = "digital" if io.get("is_binary") else "analog"

                ha_map[key] = {"name": f"HA IO {io['ext_name']} {io['identifier']}", "type": mapped_type}
                io_meta_by_key[key] = io

            # 4. Comexio Map (Audit the counterpart on the server)
            # Exact reverse lookup first: ha_map's "name" values are built from the same
            # extension names that may contain spaces, so a positional full_name.split(" ")
            # would misparse "HA IO <Ext With Space> <Ident>" (parts[2] wouldn't be the whole
            # extension name). Only fall back to the positional heuristic for commands with no
            # current HA counterpart (renamed/deleted markers or extensions) purely for grouping.
            name_to_key = {info["name"]: key for key, info in ha_map.items()}
            com_map = {}
            for full_name, info in com_commands.items():
                cmd_id = info.get("cmdId")
                comexio_type_id = int(info.get("typeId", 1))
                # Mapping Web-IO Command TypeId: 1 = Digital, 2 = Analog
                mapped_type = "analog" if comexio_type_id == 2 else "digital"

                key = name_to_key.get(full_name, full_name)
                if key == full_name:
                    parts = full_name.split(" ")
                    if len(parts) >= 3:
                        # Registry-driven: any range_clustered category ("HA <prefix><ID> <Name>",
                        # e.g. Marker/KNX) is identified by its audit_key_prefix, so a further
                        # range_clustered category needs no new branch here.
                        range_clustered_cat = next(
                            (
                                cat
                                for cat in SOURCE_CATEGORIES.values()
                                if cat.range_clustered and parts[1].startswith(cat.audit_key_prefix)
                            ),
                            None,
                        )
                        if range_clustered_cat is not None:
                            key = parts[1]
                        elif parts[1] == "IO" and len(parts) >= 4:
                            # IO identification via "HA IO <Ext> <Ident>" (best-effort only —
                            # may misparse if <Ext> itself contains spaces)
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
            # Only classes the user has opted into (import_conf_key) count as "missing" — an
            # opted-out category (e.g. KNX by default) has no Web-IO device on the server by
            # design, and flagging that as missing would wipe last_audit_results on every poll
            # (see active_webio_classes docstring).
            missing_classes = [
                cls for cls in active_webio_classes(conf) if not webio_devices.get(cls, {}).get("device_id")
            ]
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
                for cmd in self.api.build_webio_commands(
                    self.server_id, final_data, None, self.ignored_marker_ids, self.ignored_knx_ids
                )
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
            has_active_plan = self._has_active_function_plan()

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
            (
                wired_marker_webio_pairs,
                wired_io_webio_pairs,
                wired_knx_webio_pairs,
                connected_marker_ids,
                connected_io_ids,
                connected_knx_ids,
            ) = self._audit_wired_pairs(has_active_plan, managed_io_exts)

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
                key_class = classify_audit_key(key)
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

                    if not is_renamed:
                        if ha["type"] != best_match.get("type"):
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
                        gap_item = self._function_plan_gap_item(
                            key,
                            ha["name"],
                            best_match,
                            (wired_marker_webio_pairs, wired_io_webio_pairs, wired_knx_webio_pairs),
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
            knx_by_id = {str(k["id"]): k["name"] for k in final_data["knx"]}
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
                for rid in self._dangling_source_ids("11", wired_knx_webio_pairs, connected_knx_ids):
                    function_plan_dangling_items.append(
                        {"name": knx_by_id.get(rid, f"K{rid}"), "ref_id": rid, "webio_class": WEBIO_CLASS_KNX}
                    )
                    mismatches.add(f"function_plan_dangling_K{rid}")
            if managed_io_exts:
                for rid in self._dangling_source_ids("1", wired_io_webio_pairs, connected_io_ids):
                    io = io_by_id.get(rid)
                    name = f"{io['ext_name']} {io['identifier']}" if io else f"IO#{rid}"
                    function_plan_dangling_items.append({"name": name, "ref_id": rid, "webio_class": WEBIO_CLASS_IO})
                    mismatches.add(f"function_plan_dangling_IO{rid}")

            # Trigger markers/KNX objects ([TRIG]/[TP]): independent of the Web-IO comparison
            # above — the source's own Web-IO wiring is audited exactly like any other
            # marker/KNX object (missing_items etc.); this only checks the separate
            # Marker+Flanke self-reset construct, shared verbatim by KNX triggers.
            trigger_ids_by_ref = self._trigger_ids_by_ref(final_data)
            trigger_audit_result = self._audit_all_trigger_pairs(trigger_ids_by_ref)
            if trigger_audit_result is None:
                # Trigger plan exists but its data has not landed in the bulk snapshot yet —
                # defer the whole trigger check to the next cycle rather than misread an
                # unloaded plan as "every trigger source unwired" (same partial-snapshot
                # guard #78 added for the generic Web-IO wiring check).
                self._lp_missing_recheck_pending = True
                trigger_audit_result = ({}, {})
            function_plan_trigger_missing_by_ref, function_plan_trigger_orphan_by_ref = trigger_audit_result
            for ref_type, ids in function_plan_trigger_missing_by_ref.items():
                prefix = category_by_fub_module_type(ref_type).audit_key_prefix
                for mid in ids:
                    mismatches.add(f"function_plan_trigger_missing_{prefix}{mid}")
            for ref_type, ids in function_plan_trigger_orphan_by_ref.items():
                prefix = category_by_fub_module_type(ref_type).audit_key_prefix
                for mid in ids:
                    mismatches.add(f"function_plan_trigger_orphan_{prefix}{mid}")

            # KNX write path (Entwurf A "Merker-Brücke") audit, incl. Phase 7's API-Loopback
            # fan-out check — see _audit_knx_bridge_items's own docstring for the full rationale.
            knx_bridge_missing_items, knx_bridge_loopback_missing_items = self._audit_knx_bridge_items(
                has_active_plan, final_data["knx"], ha_map, wired_knx_webio_pairs, mismatches
            )

            # DPT1.x classification for digital KNX objects with no [RO]/[TRIG]/[K<id>] suffix
            # yet (real ETS imports never carry Comexio's own naming convention) — see
            # _auto_suffix_unambiguous_knx / _audit_knx_dpt_ambiguous docstrings.
            if import_knx:
                await self._auto_suffix_unambiguous_knx(final_data["knx"])
                self._audit_knx_dpt_ambiguous(final_data["knx"])

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
                "function_plan_trigger_missing": function_plan_trigger_missing_by_ref,
                "function_plan_trigger_orphan": function_plan_trigger_orphan_by_ref,
                "knx_bridge_missing": knx_bridge_missing_items,
                "knx_bridge_loopback_missing": knx_bridge_loopback_missing_items,
            }

            # Include pending entity cleanups (ignored markers/KNX with remaining HA entities) in mismatches
            for cls_val, mid in self._cleanup_entity_ids:
                mismatches.add(f"cleanup_entity_{cls_val}_{mid}")

            # 📈 --- AUDIT SUMMARY LOGGING ---
            if mismatches:
                # Create a simple string representation to detect changes
                current_summary_content = (
                    f"{len(type_mismatches)}-{len(missing_items)}-{len(renamed_items)}"
                    f"-{len(orphans)}-{ip_mismatch}-{len(function_plan_missing_items)}"
                    f"-{len(function_plan_dangling_items)}-{_count_by_ref(function_plan_trigger_missing_by_ref)}"
                    f"-{_count_by_ref(function_plan_trigger_orphan_by_ref)}-{len(knx_bridge_missing_items)}"
                    f"-{len(knx_bridge_loopback_missing_items)}"
                )

                # Only log details if the audit result differs from the previous run
                if self.last_summary_hash != current_summary_content:
                    self.last_summary_hash = current_summary_content

                    # Consolidated warning for the Home Assistant log overview
                    _LOGGER.warning(
                        "[%s] Comexio Audit Mismatch: %d issues detected (Type:%d, Missing:%d, "
                        "Renames:%d, Orphans:%d, IP:%d, Plan debris:%d, Trigger gaps:%d, Trigger orphans:%d, "
                        "KNX bridges:%d, KNX loopback:%d)",
                        self.server_id,
                        len(mismatches),
                        len(type_mismatches),
                        len(missing_items),
                        len(renamed_items),
                        len(orphans),
                        1 if ip_mismatch else 0,
                        len(function_plan_dangling_items),
                        _count_by_ref(function_plan_trigger_missing_by_ref),
                        _count_by_ref(function_plan_trigger_orphan_by_ref),
                        len(knx_bridge_missing_items),
                        len(knx_bridge_loopback_missing_items),
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

                    if knx_bridge_missing_items:
                        _LOGGER.info(
                            "%s KNX objects without bridge Marker (%d):", ICON_LINK, len(knx_bridge_missing_items)
                        )
                        for item in knx_bridge_missing_items:
                            _LOGGER.info("   -> %s", item["name"])

                    if knx_bridge_loopback_missing_items:
                        _LOGGER.info(
                            "%s KNX bridges without API-Loopback fan-out (%d):",
                            ICON_LINK,
                            len(knx_bridge_loopback_missing_items),
                        )
                        for item in knx_bridge_loopback_missing_items:
                            _LOGGER.info("   -> %s", item["name"])

                    if function_plan_trigger_missing_by_ref:
                        _LOGGER.info(
                            "%s Trigger markers not wired (%d): %s",
                            ICON_LINK,
                            _count_by_ref(function_plan_trigger_missing_by_ref),
                            _format_by_ref(function_plan_trigger_missing_by_ref),
                        )

                    if function_plan_trigger_orphan_by_ref:
                        _LOGGER.info(
                            "%s Orphaned trigger constructs (%d): %s",
                            ICON_DELETE,
                            _count_by_ref(function_plan_trigger_orphan_by_ref),
                            _format_by_ref(function_plan_trigger_orphan_by_ref),
                        )

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
                    "function_plan_trigger_missing": _count_by_ref(function_plan_trigger_missing_by_ref),
                    "function_plan_trigger_orphan": _count_by_ref(function_plan_trigger_orphan_by_ref),
                    "knx_bridge_missing": len(knx_bridge_missing_items),
                    "knx_bridge_loopback_missing": len(knx_bridge_loopback_missing_items),
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
                _LOGGER.debug("[%s] Function Plan backup cycle: NOT spawned — lock already held", self.server_id)

            # Publish only now that the whole poll succeeded — see source_counts comment above.
            self.available_source_counts = source_counts
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
            snapshot_fub_ids = frozenset(plans.keys())
            snapshot_changed = snapshot_fub_ids != self._last_bulk_snapshot_fub_ids
            self._last_bulk_snapshot_fub_ids = snapshot_fub_ids
            if (self._lp_missing_recheck_pending and snapshot_changed) or markers_changed:
                # The audit skipped the function_plan_missing check because a relevant plan
                # wasn't loaded yet — re-run it now that the snapshot actually changed. Gating
                # on snapshot_changed (not just the pending flag) keeps a relevant plan that the
                # bulk endpoint can never deliver (e.g. a persistently malformed entry) from
                # retriggering this refresh every single cycle forever.
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

    def function_plan_knx_label_map(self) -> dict[str, Any]:
        """KNX object id -> item ({"name", "type", "value", ...}) for plan-preview "K{id}" pills.

        Kept separate from function_plan_label_maps' marker map: both share the same plain
        numeric id space, so K5 and M5 would collide in one dict. Empty while import_knx is
        off (coordinator data carries no KNX items then) — pills/labels fall back to "K{id} (unknown)".
        """
        return {str(k["id"]): k for k in (self.data or {}).get("knx", [])}

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

    def _parsed_webio_devices(self) -> dict[str, Any]:
        """Web-IO device/class ids per class from the last successful poll ({cls: {device_id, base_id, ...}}).

        Read from coordinator.data rather than last_audit_results: the audit is wiped to {}
        whenever an active class has no device (missing_webio_class path), which after a
        partial cleanup (e.g. scope knx) would hide the remaining classes from the next one.
        """
        return (self.data or {}).get("webio_devices", {})

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
        self, webio_classes: tuple[WebioClass, ...] = WEBIO_CLASSES
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
        """Delete the Web-IO device instances of webio_classes, then their classes. Returns
        (devices, classes, failed_classes, skipped)."""
        devices: dict[str, str] = {}
        classes: dict[str, str] = {}
        # Base-deletion failures are tracked separately from `skipped` so callers can tell
        # "class deletion failed" apart from "no base was configured for this class".
        failed_classes: dict[str, str] = {}
        skipped: dict[str, str] = {}
        # Force a fresh poll rather than trusting a poll-interval-old snapshot — devices
        # created/removed since the last poll would otherwise be missed or acted on stale.
        # async_refresh, not async_request_refresh: the latter is debounced and silently
        # does nothing when the cleanup button refreshed moments ago. Neither raises — a
        # failed, skipped (in_sync) or empty poll keeps old or empty data, so the outcome
        # is checked explicitly instead of deleting by stale ids or reporting "0 removed".
        await self.async_refresh()
        if not (self.last_update_success and self._last_poll_scraped):
            reason = f"refresh from Comexio failed ({self.last_exception or 'no data'}) — Web-IO not deleted"
            _LOGGER.warning("[%s] Uninstall cleanup: %s", self.server_id, reason)
            for cls in webio_classes:
                skipped[cls] = reason
            return devices, classes, failed_classes, skipped
        webio_devices = self._parsed_webio_devices()
        for cls in webio_classes:
            dev = webio_devices.get(cls) or {}
            await self._delete_webio_class_entry(
                cls, dev.get("device_id"), dev.get("base_id"), (devices, classes, failed_classes, skipped)
            )
        return devices, classes, failed_classes, skipped

    async def _delete_webio_class_entry(
        self,
        cls: str,
        device_id: Any,
        base_id: Any,
        results: tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]],
    ) -> None:
        """Delete one class's Web-IO device, then its class; record the outcome in results
        (devices, classes, failed_classes, skipped). A class whose device is already gone is
        still deleted — nothing can use it anymore, and skipping it silently left an orphan."""
        devices, classes, failed_classes, skipped = results
        if device_id:
            if not await self.api.delete_webio_device(device_id):
                skipped[cls] = "delete_webio_device failed"
                return
            devices[cls] = str(device_id)
        if not base_id:
            return
        if await self.api.delete_webio_base(base_id):
            classes[cls] = str(base_id)
        else:
            failed_classes[cls] = "delete_webio_base failed"

    async def _delete_knx_loopback_webio(
        self,
        devices: dict[str, str],
        classes: dict[str, str],
        failed_classes: dict[str, str],
        skipped: dict[str, str],
    ) -> None:
        """Delete the Phase 7 API-Loopback Web-IO device+class, if present.

        This class/device lives OUTSIDE WEBIO_CLASSES (see ensure_knx_loopback_webio's
        docstring — its commands are never keys of parse_config()'s webio_commands), so the
        per-class loop above never sees it via the parsed "webio_devices" and would otherwise
        leave it behind forever on an uninstall. Needs its own live lookup instead of the
        cached poll data. Mutates the four result dicts in place under a "knx_loopback" key —
        callers only ever report len(...) over these dicts, so no further reporting change is
        needed for this extra entry to show up in the cleanup summary.
        """
        try:
            device_id = await self.api.get_webio_device_info(WEBIO_DEVICE_NAME_KNX_LOOPBACK)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # get_webio_device_info raises RuntimeError on a non-200 response, but its own
            # session.get() call is unwrapped — a connection failure/timeout propagates as
            # aiohttp.ClientError/TimeoutError instead. Both are routine "couldn't delete it
            # this time" outcomes here, not a crash.
            skipped["knx_loopback"] = f"get_webio_device_info failed: {err}"
            return
        try:
            base_info = await self.api.get_webio_base_info(WEBIO_CLASS_NAME_KNX_LOOPBACK)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # Same raise contract as get_webio_device_info above. Checked BEFORE deleting the
            # device: deleting it first and then failing the class lookup would leave the
            # class behind with no retry path. Skip everything instead.
            skipped["knx_loopback"] = f"get_webio_base_info failed: {err}"
            return
        if device_id:
            if not await self.api.delete_webio_device(device_id):
                skipped["knx_loopback"] = "delete_webio_device failed"
                return
            devices["knx_loopback"] = str(device_id)
        # No device but a class: a retry after a failed class delete — delete the orphan too.
        if base_info is None:
            # A successful lookup that found no class: genuinely already gone, nothing to delete.
            return
        base_id = base_info[0]
        if await self.api.delete_webio_base(base_id):
            classes["knx_loopback"] = str(base_id)
        else:
            failed_classes["knx_loopback"] = "delete_webio_base failed"

    def uninstall_cleanup_counts(self) -> dict[str, dict[str, int]]:
        """Per cleanup scope: managed plans / Web-IO devices / classes it would delete (from
        the plan map and the last audit) — the counters of the uninstall-cleanup repair dialog."""
        return scope_counts(
            dict(self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})),
            self._parsed_webio_devices(),
        )

    def create_uninstall_cleanup_issue(
        self, translation_key: str, default_scope: str | None = None, persistent: bool = False
    ) -> None:
        """Raise the (fixable) uninstall-cleanup repair issue with per-scope counters.

        translation_key: ISSUE_UNINSTALL_CLEANUP (test button) or ISSUE_KNX_PRERELEASE_CLEANUP
        (after an update from a KNX pre-release); both run the same repair flow.
        default_scope preselects a scope in the dialog — None keeps "Cancel" preselected.
        persistent keeps the issue (and its data) across an HA restart — needed for the
        one-shot pre-release issue, whose trigger flag is cleared right after raising it.
        The counters come from the last poll; callers refresh first if they need them live.
        """
        counts = self.uninstall_cleanup_counts()
        full = counts[CLEANUP_SCOPE_FULL]
        data: dict[str, Any] = {
            "entry_id": self.config_entry.entry_id,
            "plan_count": full["plans"],
            "device_count": full["devices"],
            "class_count": full["classes"],
            "counts": counts,
        }
        if default_scope is not None:
            data["default_scope"] = default_scope
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{translation_key}_{self.server_id}",
            is_fixable=True,
            is_persistent=persistent,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "server_id": self.server_id,
                "plan_count": str(full["plans"]),
                "device_count": str(full["devices"]),
                "class_count": str(full["classes"]),
            },
            data=data,
        )

    def check_knx_prerelease_cleanup(self) -> None:
        """One-shot after an update from a KNX pre-release (CONF_KNX_PRERELEASE_CLEANUP_PENDING,
        set by async_migrate_entry): raise the KNX cleanup repair issue if KNX artifacts exist,
        then clear the flag.

        v0.10.0-rc1..rc3 built KNX plans, Web-IO and bridge markers with a layout the release
        no longer matches; the issue preselects the "knx" scope, which removes them and resets
        the bridge markers so the next sync rebuilds everything cleanly. Must run after the
        first refresh (needs the audit's Web-IO devices) and BEFORE the update listener is
        registered — the flag is cleared with a plain options write, which would otherwise
        reload the entry mid-setup.
        """
        options = self.config_entry.options
        if not options.get(CONF_KNX_PRERELEASE_CLEANUP_PENDING):
            return
        if not self._last_poll_scraped:
            # An empty/failed config scrape looks exactly like "no leftovers" — keep the flag
            # and decide on a later setup instead of dropping the one-shot check for good.
            _LOGGER.warning(
                "[%s] KNX pre-release check postponed: the Comexio config could not be read", self.server_id
            )
            return
        if self.has_knx_artifacts():
            _LOGGER.warning(
                "[%s] KNX pre-release leftovers found — raising the KNX cleanup repair issue", self.server_id
            )
            self.create_uninstall_cleanup_issue(
                ISSUE_KNX_PRERELEASE_CLEANUP, default_scope=CLEANUP_SCOPE_KNX, persistent=True
            )
        else:
            _LOGGER.info("[%s] Update from a KNX pre-release: no KNX leftovers found, nothing to clean", self.server_id)
        new_options = {k: v for k, v in options.items() if k != CONF_KNX_PRERELEASE_CLEANUP_PENDING}
        self.hass.config_entries.async_update_entry(self.config_entry, options=new_options)

    def has_knx_artifacts(self) -> bool:
        """Whether a KNX cluster plan, the KNX Web-IO device or a bridge marker exists (see cleanup_scope)."""
        return has_knx_artifacts(
            dict(self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})),
            self._parsed_webio_devices(),
            has_bridge_markers=self._knx_bridge_markers_present,
        )

    async def async_uninstall_cleanup(
        self, scope: str = CLEANUP_SCOPE_FULL, progress_cb: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        """Tear down what the integration created in Comexio for a cleanup scope (see
        cleanup_scope): the scope's HA-managed Function Plans (CONF_FUNCTION_PLAN_PLAN_MAP),
        then its Web-IO device instances, then their Web-IO device classes — in that order,
        since Comexio refuses to delete a device or class still in use. Scopes full/knx also
        remove the KNX API-Loopback Web-IO and blank the titles of the KNX bridge markers,
        so the next sync rebuilds the bridge block from scratch. Scopes marker/knx touch the
        shared trigger plan only via _cleanup_trigger_plan (own pairs removed, the plan itself
        only when no other category's pairs remain in it). Best-effort per phase; a
        failure in one plan/class does not block the others, and failed plan deletions stay
        in plan_map for a retry. progress_cb(text) receives a status line per phase (and per
        batch of reset markers) for the running notification.
        """
        report = progress_cb or (lambda _text: None)
        steps = 4 if scope_includes_knx(scope) else 2
        full_map = dict(self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {}))
        plan_map = plans_in_scope(full_map, scope)
        report(
            f"Step 1/{steps}: removing Function Plans ({len(plan_map)} in scope, plus the shared trigger plan check)"
        )
        trigger_pairs_removed = 0
        if (own_ref_type := scope_trigger_ref_type(scope)) is not None and (
            trigger_fub_id := full_map.get(FUNCTION_PLAN_TRIGGER_PLAN_NAME)
        ) is not None:
            action, trigger_pairs_removed = await self._cleanup_trigger_plan(trigger_fub_id, own_ref_type)
            if action == TRIGGER_PLAN_DELETE:
                plan_map[FUNCTION_PLAN_TRIGGER_PLAN_NAME] = trigger_fub_id
        deleted_plans, failed_plans = await self._delete_managed_plans(plan_map)
        if trigger_pairs_removed < 0:
            failed_plans.append(FUNCTION_PLAN_TRIGGER_PLAN_NAME)
            trigger_pairs_removed = 0
        if deleted_plans:
            await self._persist_plan_map({}, removals=set(deleted_plans))

        report(f"Step 2/{steps}: deleting Web-IO devices and classes")
        devices, classes, failed_classes, skipped = await self._delete_webio_devices_and_classes(
            webio_classes_in_scope(scope)
        )
        reset_markers: list[int] = []
        failed_markers: list[int] = []
        marker_reset_error: str | None = None
        if scope_includes_knx(scope):
            report(f"Step 3/{steps}: removing the KNX loopback Web-IO")
            await self._delete_knx_loopback_webio(devices, classes, failed_classes, skipped)
            report(f"Step 4/{steps}: resetting KNX bridge markers (checking which are free)")
            reset_markers, failed_markers, marker_reset_error = await self._reset_knx_bridge_markers(
                skipped, _marker_reset_progress(report, f"Step 4/{steps}")
            )

        _LOGGER.info(
            "[%s] Uninstall cleanup (scope=%s): plans deleted=%s failed=%s, devices=%s, classes=%s, "
            "failed_classes=%s, markers reset=%d failed=%s, skipped=%s",
            self.server_id,
            scope,
            deleted_plans,
            failed_plans,
            devices,
            classes,
            failed_classes,
            len(reset_markers),
            failed_markers,
            skipped,
        )
        return {
            "scope": scope,
            "deleted_plans": deleted_plans,
            "failed_plans": failed_plans,
            "deleted_devices": devices,
            "deleted_classes": classes,
            "failed_classes": failed_classes,
            "reset_markers": reset_markers,
            "failed_markers": failed_markers,
            "marker_reset_error": marker_reset_error,
            "trigger_pairs_removed": trigger_pairs_removed,
            "skipped": skipped,
        }

    async def _cleanup_trigger_plan(self, fub_id: Any, own_ref_type: int) -> tuple[str, int]:
        """Partial cleanup of the shared trigger plan for one category (ref_type 2/11).

        Returns (action, removed element count); count -1 = failed. Deletes the plan only
        when it holds no other category's trigger pairs — otherwise just this category's
        source+Flanke pairs are removed, so the other category's self-reset keeps working.
        """
        try:
            fub_id_int = int(fub_id)
        except (TypeError, ValueError):
            return TRIGGER_PLAN_KEEP, -1
        plan_data = await self.api.function_plan_load_elements(fub_id_int)
        if plan_data is None:
            _LOGGER.error("[%s] Uninstall cleanup: could not load the trigger plan — left as is", self.server_id)
            return TRIGGER_PLAN_KEEP, -1
        marker_titles = await self.api.fetch_marker_titles()
        if marker_titles is None:
            _LOGGER.error(
                "[%s] Uninstall cleanup: could not fetch the marker titles — trigger plan left as is", self.server_id
            )
            return TRIGGER_PLAN_KEEP, -1
        sources = trigger_sources_by_category(
            self.api.function_plan_element_refs(plan_data),
            marker_titles,
            {int(cat.fub_module_type) for cat in trigger_pair_categories()},
        )
        action = trigger_plan_action(own_ref_type, set(sources))
        _LOGGER.info(
            "[%s] Uninstall cleanup: trigger plan holds sources %s — action for ref_type %s: %s",
            self.server_id,
            {t: len(ids) for t, ids in sources.items()},
            own_ref_type,
            action,
        )
        if action != TRIGGER_PLAN_REMOVE_PAIRS:
            return action, 0
        # Group by the elements' own ref_type: a KNX pair may sit in the plan as a K element
        # (11) or via its bridge marker (2) — each is removed with its own ref_type.
        by_elem_type: dict[int, list[int]] = {}
        for ref_type, ref_id in sources[own_ref_type]:
            by_elem_type.setdefault(ref_type, []).append(ref_id)
        removed = 0
        for ref_type, ref_ids in by_elem_type.items():
            count, plan_stopped = await self.api.function_plan_remove_trigger_pairs(fub_id_int, ref_ids, ref_type)
            if not count or plan_stopped:
                # 0 although pairs were found = stop/delete failed; plan_stopped = restart
                # failed, which silently breaks the other category's trigger self-reset.
                _LOGGER.error(
                    "[%s] Uninstall cleanup: removing trigger pairs (ref_type %s) from fub %s failed"
                    " (removed=%s, plan left stopped=%s)",
                    self.server_id,
                    ref_type,
                    fub_id_int,
                    count,
                    plan_stopped,
                )
                return action, -1
            removed += count
        return action, removed

    async def _reset_knx_bridge_markers(
        self, skipped: dict[str, str], progress_cb: Callable[[int, int], None] | None = None
    ) -> tuple[list[int], list[int], str | None]:
        """Blank the KNX bridge marker titles as the last cleanup phase. Returns
        (reset, failed, error). Plans/Web-IO are already gone at this point, so a connection
        error must not escape and discard the result of those phases — it is returned as
        error instead, and the user re-runs the KNX cleanup. Still-placed markers are
        reported via skipped."""
        try:
            reset, failed, placed, error = await self.api.reset_knx_bridge_markers(progress_cb)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.exception("[%s] Resetting the KNX bridge markers failed", self.server_id)
            return [], [], f"connection error: {err}"
        except Exception:  # last phase: must not discard the earlier phases' result
            _LOGGER.exception("[%s] Resetting the KNX bridge markers failed unexpectedly", self.server_id)
            return [], [], "unexpected error, see log"
        if error:
            _LOGGER.error("[%s] Resetting the KNX bridge markers skipped: %s", self.server_id, error)
        if placed:
            skipped[SKIPPED_KNX_BRIDGE_MARKERS] = f"{placed} bridge marker(s) still placed in a plan"
        return reset, failed, error

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
        cache_generation_before = self._preview_cache_generation
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
            knx_by_id=self.function_plan_knx_label_map(),
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
        # The preview may have been stopped (or auto-stopped, disarmed after repeated poll
        # failures, shut down, or re-armed by a concurrent render for a different plan) while
        # the awaits above were in flight — committing the cache now would resurrect a preview
        # that was explicitly told to stop, or clobber a newer render with a stale one (#77).
        # The already-written SVG/last_plan_preview above stay as the last rendered frame
        # either way; only the re-arming is skipped.
        if cache_generation_before == self._preview_cache_generation:
            if source == "live":
                self._update_live_preview_cache(fub_id, plan_name, elements, connections, plan_switched)
            else:
                kind, slot = _parse_snapshot_source(source)
                self._update_snapshot_preview_cache(
                    fub_id, plan_name, elements, connections, kind, slot, label_metadata, live_id_map
                )
        else:
            _LOGGER.debug(
                "[%s] Plan preview cache commit skipped: cache generation changed while rendering",
                self.server_id,
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
        marker_ids, io_ids, knx_ids = _plan_ref_ids(elements)
        self._preview_plan_cache = {
            "fub_id": fub_id,
            "plan_name": plan_name,
            "elements": elements,
            "connections": connections,
            "marker_ids": marker_ids,
            "io_ids": io_ids,
            "knx_ids": knx_ids,
            "snapshot_source": None,
            "label_metadata": None,
            "live_id_map": None,
        }
        self._preview_cache_generation += 1
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
        marker_ids, io_ids, knx_ids = _plan_ref_ids(elements)
        self._preview_plan_cache = {
            "fub_id": fub_id,
            "plan_name": plan_name,
            "elements": elements,
            "connections": connections,
            "marker_ids": marker_ids,
            "io_ids": io_ids,
            "knx_ids": knx_ids,
            "snapshot_source": source,
            "label_metadata": label_metadata,
            "live_id_map": live_id_map,
        }
        self._preview_cache_generation += 1
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
            # Log before cleanup: a failure inside _disarm_preview_cache()/_stop_connection_poll()
            # (both synchronous, no I/O, but not provably infallible) must never mask the actual
            # render failure's traceback.
            _LOGGER.exception("[%s] Plan preview refresh failed", self.server_id)
            # Disable further refreshes; the next preview button press re-arms. Also stop the
            # Stufe-2 connection-value poll — otherwise its timer keeps firing indefinitely as a
            # no-op against an already-cleared cache (#77).
            self._disarm_preview_cache()
            self._stop_connection_poll()

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
        # stop_preview() now runs this on every routine view switch (#75), not just rare
        # true removals — reset here so a transient failure streak doesn't carry into the
        # next arm and trip the breaker after fewer than _CONNECTION_POLL_MAX_FAILURES.
        self._connection_poll_fail_count = 0

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
        self._disarm_preview_cache()
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

    def _disarm_preview_cache(self) -> None:
        """Clear the armed-preview cache and bump _preview_cache_generation (see its docstring).

        Shared by every disarm path (explicit stop, auto-stop, refresh/poll failure, coordinator
        shutdown) so none of them can forget the generation bump and reintroduce the
        stale-render race (#77).
        """
        self._preview_plan_cache = None
        self._preview_cache_generation += 1

    def stop_preview(self) -> bool:
        """Disarm the currently armed preview immediately (function_plan_preview_stop service).

        Called by the plan card's disconnectedCallback when it leaves the DOM (page
        navigation/close), so the Stufe-2 poll stops right away instead of continuing until
        _PREVIEW_AUTO_STOP_DEFAULT_MINUTES elapses (#75). No-op (returns False) while nothing
        is armed, same convention as set_preview_auto_stop_extension.
        """
        if self._preview_plan_cache is None:
            return False
        self._disarm_preview_cache()
        self._stop_connection_poll()
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
            connection_values = await self.api.get_function_plan_connection_values(
                cache["fub_id"], session=preview_session
            )
        except Exception:
            if self._preview_plan_cache is not cache:
                # The preview was stopped/replaced (stop_preview() or a new plan armed)
                # while this request was in flight — its failure no longer belongs to the
                # now-current preview's failure streak (#75). The identity compare is reliable
                # only because the cache is always reassigned wholesale, never mutated in
                # place (see _preview_plan_cache's definition).
                return
            self._connection_poll_fail_count += 1
            if self._connection_poll_fail_count >= _CONNECTION_POLL_MAX_FAILURES:
                _LOGGER.exception(
                    "[%s] Connection-value poll failed %s times in a row — disarming plan preview "
                    "(re-open the preview to retry)",
                    self.server_id,
                    self._connection_poll_fail_count,
                )
                self._disarm_preview_cache()
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
        if self._preview_plan_cache is not cache:
            # Stale response for a preview that's no longer armed (stopped/replaced while
            # this request was in flight, #75) — discard it instead of overwriting the
            # currently armed preview's fresh connection values with old data. Relies on the
            # cache being reassigned wholesale on every (re-)arm, never mutated in place
            # (see _preview_plan_cache's definition).
            return
        self._connection_values = connection_values
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
        # Disarm the preview cache too — otherwise a render still in flight at shutdown time
        # (e.g. an in-progress async_generate_plan_preview) could commit its cache after
        # HA has already moved on, one of the disarm paths missed by the original fix (#77).
        self._disarm_preview_cache()
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
        previous = self.marker_states.get(marker_id_str)
        self.marker_states[marker_id_str] = value
        self._webhook_updated_markers.add(marker_id_str)  # R1: mark as received during possible fetch
        label = f"M{marker_id_str}"
        if self.data and "markers" in self.data:
            for m in self.data["markers"]:
                if str(m["id"]) == marker_id_str:
                    m["value"] = value
                    label = m.get("name") or label
                    break
        _LOGGER.debug(WEBHOOK_VALUE_LOG_MSG, "marker", label, value, previous)
        self.async_set_updated_data(self.data)
        self._fire_plan_event("marker", marker_id_str, label, value)
        self.schedule_plan_preview_refresh()

    def update_knx(self, knx_id: str | int, value: float | int | str) -> None:
        knx_id_str = str(knx_id)
        previous = self.knx_states.get(knx_id_str)
        self.knx_states[knx_id_str] = value
        self._webhook_updated_knx_ids.add(knx_id_str)  # R1: mark as received during possible fetch
        label = f"K{knx_id_str}"
        if self.data and "knx" in self.data:
            for k in self.data["knx"]:
                if str(k["id"]) == knx_id_str:
                    k["value"] = value
                    label = k.get("name") or label
                    break
        _LOGGER.debug(WEBHOOK_VALUE_LOG_MSG, "knx", label, value, previous)
        self.async_set_updated_data(self.data)
        self._fire_plan_event("knx", knx_id_str, label, value)
        self.schedule_plan_preview_refresh()

    def source_states(self, source: WebioClass) -> dict[str, Any]:
        """Live value cache for a marker-like source category (registry-driven: markers/KNX)."""
        return self.knx_states if source == WebioClass.KNX else self.marker_states

    def update_source(self, source: WebioClass, source_id: str | int, value: float | int | str) -> None:
        """Dispatch an optimistic value update to the right per-category cache updater."""
        (self.update_knx if source == WebioClass.KNX else self.update_marker)(source_id, value)

    def update_io_by_name(self, ext_name: str, identifier: str, value: float | int | str) -> None:
        key = (ext_name.lower(), identifier.lower())
        if io := self._io_index.get(key):
            previous = self.io_states.get(io["id"])
            self.io_states[io["id"]] = value
            io["value"] = value
            self._webhook_updated_io_ids.add(io["id"])  # R1: mark as received during possible fetch
            label = io.get("name") or f"{ext_name} {identifier}"
            _LOGGER.debug(WEBHOOK_VALUE_LOG_MSG, "io", label, value, previous)
            self._fire_plan_event("io", str(io["id"]), label, value)
        else:
            _LOGGER.warning(WEBHOOK_UNKNOWN_IO_LOG_MSG, ext_name, identifier, value)
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

    async def async_load_extension_registry(self) -> None:
        """Restore the last known extension serial->name registry (called once at setup).

        Without this, every HA restart would forget which name was last seen for a given
        extension serial, and a Comexio-side rename that happened while HA was offline could
        never be detected as a rename (see async_detect_and_migrate_extension_renames).
        """
        stored = await self._extension_registry_store.async_load()
        if not stored:
            return
        self.extension_registry = stored.get("extensions", {})

    async def async_load_knx_dpt_catalog(self) -> None:
        """Restore the last known-good KNX DPT catalog from disk (called once at setup).

        Seeds api.ComexioAPI's in-memory cache before the first poll so its own fetch-failure
        fallback (get_knx_dpt_catalog: "return self._knx_dpt_catalog or {}") has something
        real to fall back to even on a fresh instance — see _knx_dpt_catalog_store's docstring
        for why that matters on every reload, not just a full HA restart.
        """
        stored = await self._knx_dpt_catalog_store.async_load()
        if not stored:
            return
        self.api.seed_knx_dpt_catalog(stored.get("catalog", {}), stored.get("version"))
        self._last_persisted_knx_dpt_catalog_version = stored.get("version")

    async def _maybe_persist_knx_dpt_catalog(self) -> None:
        """Persist the KNX DPT catalog when a freshly fetched version differs from disk.

        Only called after a non-empty knx_dpt_catalog was returned this poll (see
        _async_update_data) — version-gated like the other *_store saves in this class so an
        unchanged catalog (the common case, see get_knx_dpt_catalog's own version-cache check)
        doesn't hit disk every poll.
        """
        snapshot = self.api.get_knx_dpt_catalog_snapshot()
        if snapshot is None:
            return
        catalog, version = snapshot
        if version == self._last_persisted_knx_dpt_catalog_version:
            return
        await self._knx_dpt_catalog_store.async_save({"catalog": catalog, "version": version})
        self._last_persisted_knx_dpt_catalog_version = version

    async def async_load_watchdog_history(self) -> None:
        """Restore the persisted Bus-Load-Watchdog event history (called once at setup)."""
        stored = await self._watchdog_history_store.async_load()
        if not stored:
            return
        self.watchdog_history = stored.get("events", [])

    async def async_detect_and_migrate_extension_renames(self) -> list[dict[str, str]]:
        """Detect Comexio-side extension renames via their stable serial and migrate in place.

        Must run after the first refresh (self.data["extensions"] populated) and before the
        orphan-cleanup in __init__.py builds its active-unique-ids set, so migrated entities
        already carry their new unique_id/device identifiers when cleanup compares against it.
        Extensions currently offline are skipped: their serial's format changes (see
        api._is_extension_offline) when a module drops offline, which would make it
        indistinguishable from an unrelated serial.
        """
        renames: list[dict[str, str]] = []
        dirty = False

        for meta in self.data.get("extensions", {}).values():
            serial = meta.get("serial", "")
            new_name = meta.get("name", "")
            if not serial or "-" not in serial:
                continue

            known = self.extension_registry.get(serial)
            if known is None:
                self.extension_registry[serial] = {"name": new_name}
                dirty = True
            elif known["name"] != new_name:
                old_name = known["name"]
                await self._migrate_extension_entities(old_name, new_name)
                self.extension_registry[serial]["name"] = new_name
                dirty = True
                renames.append({"old_name": old_name, "new_name": new_name})

        if dirty:
            await self._extension_registry_store.async_save({"extensions": self.extension_registry})

        return renames

    async def _migrate_extension_entities(self, old_name: str, new_name: str) -> None:
        """Rewrite unique_id/device identifiers for one renamed extension, in place."""
        ent_reg = er.async_get(self.hass)
        server_slug = self.server_id.lower()
        old_prefix = f"comexio_{server_slug}_{old_name.lower()}_"
        for entity_entry in er.async_entries_for_config_entry(ent_reg, self.config_entry.entry_id):
            if not entity_entry.unique_id.startswith(old_prefix):
                continue
            suffix = entity_entry.unique_id[len(old_prefix) :]
            new_uid = f"comexio_{server_slug}_{new_name.lower()}_{suffix}"
            if not ent_reg.async_get_entity_id(entity_entry.domain, DOMAIN, new_uid):
                ent_reg.async_update_entity(entity_entry.entity_id, new_unique_id=new_uid)

        dev_reg = dr.async_get(self.hass)
        old_device = dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{server_slug}_{old_name}".lower()), self.config_entry.entry_id
        )
        if old_device:
            dev_reg.async_update_device(
                old_device.id,
                new_identifiers={(DOMAIN, f"{server_slug}_{new_name}".lower())},
                name=f"{self.server_id} {new_name}",
            )

        # Keep the firmware cache under the new name so update.* doesn't briefly show
        # "Unknown" until the next nightly firmware check re-populates it. Persisted
        # immediately so a restart before that nightly check doesn't reload the stale
        # old-name key from disk.
        if old_name in self.extension_firmware:
            self.extension_firmware[new_name] = self.extension_firmware.pop(old_name)
            await self._firmware_store.async_save(
                {
                    "extension_firmware": self.extension_firmware,
                    "last_checked_fw_version": self._last_checked_fw_version,
                }
            )

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

    def async_start_webio_range_check(self):
        """Start the nightly Web-IO analog range-check gate; returns the cancel callback.

        Fires once a day at WEBIO_RANGE_CHECK_HOUR:WEBIO_RANGE_CHECK_MINUTE. The bulk config
        scrape ($FubModules["10"]) never returns Min/Max for HA's own Web-IO commands (see
        api.get_webio_command_range), so drift can only be detected by reading each analog
        command's edit form individually — one HTTP GET per command. That cost is why this
        runs on its own nightly schedule instead of piggybacking on the regular poll audit
        or the manual sync button.
        """
        return async_track_time_change(
            self.hass,
            self._async_webio_range_check_tick,
            hour=WEBIO_RANGE_CHECK_HOUR,
            minute=WEBIO_RANGE_CHECK_MINUTE,
            second=0,
        )

    async def _async_check_one_webio_range(
        self, name: str, cmd_id: Any, device: dict[str, Any], target: dict[str, Any], result: dict[str, int]
    ) -> None:
        """Check one analog Web-IO command's Min/Max against target; correct on drift.

        Mutates result's "checked"/"fixed"/"failed"/"correction_failed" counters in place —
        factored out of _async_webio_range_check_tick purely to keep that method's cognitive
        complexity down. "failed" counts unreadable commands (read errors); "correction_failed"
        counts commands that were read fine, found drifted, but whose corrective write failed —
        kept separate so a run where every fix fails can't misreport as a clean success.
        """
        device_id = device.get("device_id")
        live_min, live_max = await self.api.get_webio_command_range(cmd_id, device_id)
        if live_min is None or live_max is None:
            result[RANGE_CHECK_FAILED] += 1
            return
        result[RANGE_CHECK_CHECKED] += 1
        if live_min == target["Min"] and live_max == target["Max"]:
            return

        if await self.api.save_single_command(device.get("base_id"), device_id, target, existing_cmd_id=cmd_id):
            result[RANGE_CHECK_FIXED] += 1
            _LOGGER.warning(
                "Web-IO command '%s' Min/Max drifted (%s/%s -> %s/%s); corrected.",
                name,
                live_min,
                live_max,
                target["Min"],
                target["Max"],
            )
        else:
            result[RANGE_CHECK_CORRECTION_FAILED] += 1
            _LOGGER.error("Failed to correct Web-IO command '%s' Min/Max drift.", name)

    async def _async_webio_range_check_tick(self, _now: datetime | None = None) -> dict[str, int]:
        """Check and correct analog Web-IO command Min/Max drift against the target range.

        For every analog Web-IO command, compares its live Min/Max (read via
        api.get_webio_command_range) against the target build_webio_commands would create
        today — WEBIO_MARKER_ANALOG_MIN/MAX for markers, the authentic IO type range for
        physical IOs — and rewrites it via api.save_single_command on a mismatch. Returns a
        {"checked": n, "fixed": n, "failed": n, "correction_failed": n, "excluded": n,
        "skipped": 0|1} summary; shared by the nightly schedule and the manual force trigger.
        "skipped" is 1 (with the other counters left at 0) when no data is loaded yet, or while
        a sync is in progress — save_single_command would otherwise race the sync button's
        delta/full-recreate writes against the same Web-IO devices (see _sync_lock / R4) — so
        callers can tell "skipped" apart from a genuine clean run that found nothing to fix.
        "excluded" counts commands that were never read at all because they couldn't be matched
        to a target (naming mismatch, stale command) or are missing an id/device — kept separate
        so a mass name-matching regression can't hide behind a "Checked: N" count that quietly
        excludes most of the fleet.
        """
        result = {
            RANGE_CHECK_CHECKED: 0,
            RANGE_CHECK_FIXED: 0,
            RANGE_CHECK_FAILED: 0,
            RANGE_CHECK_CORRECTION_FAILED: 0,
            RANGE_CHECK_EXCLUDED: 0,
            RANGE_CHECK_SKIPPED: 0,
        }
        if not self.data:
            result[RANGE_CHECK_SKIPPED] = 1
            return result
        if self._sync_lock.locked():
            _LOGGER.warning("[%s] Sync in progress, skipping Web-IO range check", self.server_id)
            result[RANGE_CHECK_SKIPPED] = 1
            return result

        async with self._sync_lock:
            webio_commands = self.data.get("webio_commands", {})
            webio_devices = self.data.get("webio_devices", {})
            target_by_name = {
                cmd["Name"]: cmd
                for cmd in self.api.build_webio_commands(
                    self.server_id, self.data, None, self.ignored_marker_ids, self.ignored_knx_ids
                )
            }

            for name, cmd in webio_commands.items():
                if cmd.get("typeId") not in {2, "2"}:
                    continue
                target = target_by_name.get(name)
                cmd_id = cmd.get("cmdId")
                device = webio_devices.get(cmd.get("webioClass"), {})
                if target is None or not cmd_id or not device.get("device_id"):
                    result[RANGE_CHECK_EXCLUDED] += 1
                    _LOGGER.warning(
                        "Web-IO command '%s' excluded from range check (no matching target/id/device)", name
                    )
                    continue
                try:
                    await self._async_check_one_webio_range(name, cmd_id, device, target, result)
                except Exception:
                    result[RANGE_CHECK_FAILED] += 1
                    _LOGGER.exception("Unexpected error checking Web-IO command '%s' range", name)

        _LOGGER.info(
            "[%s] Web-IO range check: %d checked, %d fixed, %d failed to read, %d correction failed, %d excluded",
            self.server_id,
            result[RANGE_CHECK_CHECKED],
            result[RANGE_CHECK_FIXED],
            result[RANGE_CHECK_FAILED],
            result[RANGE_CHECK_CORRECTION_FAILED],
            result[RANGE_CHECK_EXCLUDED],
        )
        return result

    async def async_force_webio_range_check(self) -> dict[str, int]:
        """Force-run the Web-IO analog range check outside its nightly window (manual trigger)."""
        return await self._async_webio_range_check_tick()

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
        if isinstance(workload, bool) or not isinstance(workload, int | float):
            self.bus_workload = None
        else:
            self.bus_workload = int(workload)
        sd_card = result.get("sd_card")
        self.bus_sd_card = sd_card if isinstance(sd_card, bool) else None
        async_dispatcher_send(self.hass, bus_load_signal(self.server_id))

        now = dt_util.utcnow()
        if self.bus_workload is not None:
            self._bus_load_samples.append((now, self.bus_workload))
        cutoff = now - timedelta(seconds=max(BUS_LOAD_RISE_WINDOW_SEC, EMERGENCY_REBOOT_WINDOW_SEC))
        while self._bus_load_samples and self._bus_load_samples[0][0] < cutoff:
            self._bus_load_samples.popleft()
        self._evaluate_bus_load_watchdog(now)

    def _evaluate_bus_load_watchdog(self, now: datetime) -> None:
        """Check the latest bus-load samples for a sustained rise or emergency-level average.

        Pure detection, no I/O — spawns background tasks (see _async_bus_load_cascade_restart /
        _async_bus_load_emergency_reboot) for the actual reaction. Emergency takes priority over
        a plain rise so only one background task is spawned per tick.
        """
        if self.bus_workload is None:
            return
        if (now - self._watchdog_started_at).total_seconds() < BUS_LOAD_STARTUP_GRACE_SEC:
            return
        if self._watchdog_lock.locked():
            return
        if self._watchdog_cooldown_until and now < self._watchdog_cooldown_until:
            return

        conf = {**self.config_entry.data, **self.config_entry.options}
        recent = [v for _, v in list(self._bus_load_samples)[-BUS_LOAD_SMOOTHING_SAMPLES:]]
        if not recent:
            return
        current = sum(recent) / len(recent)

        if conf.get(
            CONF_BUS_WATCHDOG_AUTO_REBOOT, DEFAULT_BUS_WATCHDOG_AUTO_REBOOT
        ) and self._bus_load_emergency_average_exceeded(now):
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_bus_load_emergency_reboot(),
                name=f"comexio_{self.server_id}_bus_load_emergency_reboot",
            )
            return

        if conf.get(CONF_BUS_WATCHDOG_ENABLED, DEFAULT_BUS_WATCHDOG_ENABLED) and self._bus_load_rise_detected(
            now, current
        ):
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_bus_load_cascade_restart(current),
                name=f"comexio_{self.server_id}_bus_load_cascade",
            )

    def _bus_load_emergency_average_exceeded(self, now: datetime) -> bool:
        """Whether the average bus load over EMERGENCY_REBOOT_WINDOW_SEC exceeds the threshold.

        Requires the sample buffer to already span the full window — otherwise a fresh restart
        with only a few samples could look like a sustained high average.
        """
        window_start = now - timedelta(seconds=EMERGENCY_REBOOT_WINDOW_SEC)
        if not self._bus_load_samples or self._bus_load_samples[0][0] > window_start:
            return False
        windowed = [v for ts, v in self._bus_load_samples if ts >= window_start]
        return sum(windowed) / len(windowed) > EMERGENCY_REBOOT_THRESHOLD_PCT

    def _bus_load_rise_detected(self, now: datetime, current: float) -> bool:
        """Whether bus load has risen by BUS_LOAD_RISE_THRESHOLD_PCT over BUS_LOAD_RISE_WINDOW_SEC.

        Compares against the oldest samples still inside the window, not an absolute high value —
        a stable-but-high or falling reading must not trigger (see project backlog rationale). The
        baseline is smoothed the same way as `current` (average of BUS_LOAD_SMOOTHING_SAMPLES) so a
        single low outlier at the start of the window can't fake a sustained rise.
        """
        window_start = now - timedelta(seconds=BUS_LOAD_RISE_WINDOW_SEC)
        if not self._bus_load_samples or self._bus_load_samples[0][0] > window_start:
            return False
        windowed = [v for ts, v in self._bus_load_samples if ts >= window_start]
        baseline_samples = windowed[:BUS_LOAD_SMOOTHING_SAMPLES]
        if not baseline_samples:
            return False
        baseline = sum(baseline_samples) / len(baseline_samples)
        return (current - baseline) >= BUS_LOAD_RISE_THRESHOLD_PCT

    async def _async_bus_load_cascade_restart(self, baseline: float) -> None:
        """Cascade-restart HA-managed cluster function plans (back to front) to relieve a
        sustained bus-load rise — automates what the user previously did by hand (stop/start
        each managed plan, checking after each one whether the bus load recovers).
        """
        async with self._watchdog_lock:
            raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
            plan_map: dict[str, int] = {}
            if isinstance(raw_map, dict):
                for plan_name, fub_id in raw_map.items():
                    try:
                        plan_map[plan_name] = int(fub_id)
                    except (TypeError, ValueError):
                        _LOGGER.warning(
                            "[%s] Bus-load watchdog: skipping non-numeric plan_map entry '%s' -> %r",
                            self.server_id,
                            plan_name,
                            fub_id,
                        )
            if not plan_map:
                _LOGGER.warning(
                    "[%s] Bus-load watchdog: rise detected but no managed cluster plans to restart",
                    self.server_id,
                )
                self._watchdog_cooldown_until = dt_util.utcnow() + timedelta(seconds=CASCADE_COOLDOWN_SEC)
                return

            _LOGGER.warning(
                "[%s] Bus-load watchdog: sustained rise detected (baseline %.0f%%) — "
                "cascading restart of %d managed plan(s)",
                self.server_id,
                baseline,
                len(plan_map),
            )
            culprit: str | None = None
            for plan_name, fub_id in reversed(list(plan_map.items())):
                if not await self.api.function_plan_stop_fup(fub_id):
                    _LOGGER.warning(
                        "[%s] Bus-load watchdog: stop_fup failed for plan '%s' (fub=%s) — "
                        "restarting it anyway, run_fup re-applies its state authoritatively",
                        self.server_id,
                        plan_name,
                        fub_id,
                    )
                await asyncio.sleep(CASCADE_POST_STOP_WAIT_SEC)
                if not await self.api.function_plan_run_fup(fub_id):
                    _LOGGER.warning(
                        "[%s] Bus-load watchdog: run_fup failed for plan '%s' (fub=%s) — it may now be left stopped",
                        self.server_id,
                        plan_name,
                        fub_id,
                    )
                await asyncio.sleep(CASCADE_POST_RESTART_SETTLE_SEC)
                if self.bus_workload is not None and baseline - self.bus_workload >= CASCADE_RECOVERY_DROP_PCT:
                    culprit = plan_name
                    break

            await self._async_finish_watchdog_cascade(baseline, culprit, len(plan_map))

    async def _async_finish_watchdog_cascade(self, baseline: float, culprit: str | None, plan_count: int) -> None:
        """Notify + persist history for a finished cascade run, and start its cooldown."""
        now = dt_util.utcnow()
        self._watchdog_cooldown_until = now + timedelta(seconds=CASCADE_COOLDOWN_SEC)
        if culprit:
            outcome = "recovered"
            msg = (
                f"Sustained bus-load rise detected (baseline ~{baseline:.0f}%). Cascade-restarted "
                f"managed function plans — **{culprit}** was the trigger, bus load has recovered."
            )
        else:
            outcome = "cascade_failed"
            msg = (
                f"Sustained bus-load rise detected (baseline ~{baseline:.0f}%). Cascade-restarted "
                f"all {plan_count} managed function plan(s), but bus load did NOT recover. The "
                "problem may be deeper than a single plan — manual investigation (or an emergency "
                "reboot) may be needed."
            )
        _LOGGER.warning("[%s] Bus-load watchdog cascade finished: %s", self.server_id, outcome)
        await self._async_persist_watchdog_event(
            {
                "timestamp": now.isoformat(),
                "trigger_type": "rise",
                "culprit_plan": culprit,
                "outcome": outcome,
            }
        )
        self._notify_watchdog(f"comexio_watchdog_cascade_{self.server_id}", "Comexio Bus-Load Watchdog", msg)

    async def _async_bus_load_emergency_reboot(self) -> None:
        """Trigger Comexio's immediate, unconfirmed full system reboot (emergency threshold only).

        Notification + history are written BEFORE the API call so there's a paper trail even if
        the reboot makes the instance briefly unreachable right after.
        """
        async with self._watchdog_lock:
            now = dt_util.utcnow()
            _LOGGER.warning(
                "[%s] Bus-load watchdog: EMERGENCY threshold (%s%% over %ss) exceeded — "
                "triggering immediate Comexio reboot",
                self.server_id,
                EMERGENCY_REBOOT_THRESHOLD_PCT,
                EMERGENCY_REBOOT_WINDOW_SEC,
            )
            self._notify_watchdog(
                f"comexio_watchdog_emergency_{self.server_id}",
                "Comexio Emergency Reboot Triggered",
                f"Bus load stayed above {EMERGENCY_REBOOT_THRESHOLD_PCT}% for over "
                f"{EMERGENCY_REBOOT_WINDOW_SEC // 60} minutes. Triggering an immediate Comexio "
                "system reboot (no confirmation — this happens instantly on the Comexio side).",
            )
            await self._async_persist_watchdog_event(
                {
                    "timestamp": now.isoformat(),
                    "trigger_type": "emergency",
                    "culprit_plan": None,
                    "outcome": "reboot_attempted",
                }
            )
            if await self.api.system_emergency_reboot():
                self._watchdog_cooldown_until = now + timedelta(seconds=EMERGENCY_REBOOT_COOLDOWN_SEC)
                return

            _LOGGER.error(
                "[%s] Bus-load watchdog: emergency reboot request failed (HTTP error) — "
                "will re-evaluate on the next tick instead of waiting out the reboot cooldown",
                self.server_id,
            )
            await self._async_persist_watchdog_event(
                {
                    "timestamp": dt_util.utcnow().isoformat(),
                    "trigger_type": "emergency",
                    "culprit_plan": None,
                    "outcome": "reboot_request_failed",
                }
            )
            self._notify_watchdog(
                f"comexio_watchdog_emergency_failed_{self.server_id}",
                "Comexio Emergency Reboot Request Failed",
                "The emergency reboot request could not be delivered to Comexio (HTTP error). "
                "The watchdog will retry on the next bus-load evaluation instead of waiting out "
                "the normal reboot cooldown.",
            )

    def _notify_watchdog(self, notification_id: str, title: str, message: str) -> None:
        """Persistent-notification helper for watchdog events, gated on CONF_ENABLE_NOTIFICATIONS."""
        conf = {**self.config_entry.data, **self.config_entry.options}
        if not conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS):
            return
        persistent_notification.async_create(self.hass, message, title=title, notification_id=notification_id)

    async def _async_persist_watchdog_event(self, event: dict[str, Any]) -> None:
        """Append one event to the watchdog history, trim it, persist to disk, and refresh listeners.

        Watchdog events are rare (unlike the 10s bus-load tick, see _async_bus_load_tick), so
        unlike that tick this safely calls async_set_updated_data to push the new history to
        ComexioWatchdogEventSensor — a CoordinatorEntity that would otherwise stay stale until
        the next unrelated main-coordinator refresh.

        A disk-write failure here must not propagate: callers persist this event either right
        before the actual emergency reboot call or right before the cascade-finished notification,
        so an unhandled exception would silently skip that action too — turning a storage hiccup
        into a lost recovery action.
        """
        self.watchdog_history.append(event)
        self.watchdog_history = self.watchdog_history[-WATCHDOG_HISTORY_MAX_ENTRIES:]
        try:
            await self._watchdog_history_store.async_save({"events": self.watchdog_history})
        except Exception:
            _LOGGER.exception(
                "[%s] Failed to persist watchdog history to disk — continuing with in-memory history only",
                self.server_id,
            )
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
        server_slug = slugify(self.server_id)
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

    def _ignored_ids_for(self, category: WebioClass) -> set[int]:
        """Configured IDs excluded from entity creation and Web-IO command generation for a category.

        Registry-driven: resolves the option key via SourceCategory.ignored_conf_key, then
        parses it with expand_ignored_marker_ids() (comma/semicolon/space separators, optional
        letter prefix, ranges like '8-12'). Returns an empty set if unset or unsupported.
        """
        cat = source_category(category)
        conf_key = cat.ignored_conf_key
        if conf_key is None:
            return set()
        ignored_raw = self.config_entry.options.get(conf_key, "").strip()
        if not ignored_raw:
            return set()
        return expand_ignored_marker_ids(ignored_raw, cat.audit_key_prefix + cat.audit_key_prefix.lower())

    @property
    def ignored_marker_ids(self) -> set[int]:
        """Configured marker IDs excluded from entity creation and Web-IO command generation."""
        return self._ignored_ids_for(WebioClass.MARKER)

    @property
    def ignored_knx_ids(self) -> set[int]:
        """Configured KNX object IDs excluded from entity creation and Web-IO command generation."""
        return self._ignored_ids_for(WebioClass.KNX)

    def ignored_ids_for(self, webio_class: WebioClass) -> set[int]:
        """Registry-driven public accessor for a category's ignore-list (markers/KNX)."""
        return self._ignored_ids_for(webio_class)

    @property
    def active_webio_classes(self) -> tuple[WebioClass, ...]:
        """Web-IO classes currently opted into (see active_webio_classes() in const.py)."""
        conf = {**self.config_entry.data, **self.config_entry.options}
        return active_webio_classes(conf)

    async def async_check_ignored_sources(
        self, conf: dict[str, Any], final_data: dict[str, Any], webio_class: WebioClass
    ) -> None:
        """Check ignored source IDs and manage repair issues (registry-driven: markers=2/KNX=11).

        - Stale IDs (source no longer in Comexio) are auto-removed from options + notified.
        - Legacy IDs that still have HA entities or Function Plan links extend the shared
          self._cleanup_entity_ids / self._cleanup_function_plan_count accumulator (reset once
          per audit cycle by the caller, see the "IGNORED SOURCES AUDIT" block above).
        - IDs whose source exists but has no entities/links are the intended normal state — no action.
        """
        category = source_category(webio_class)
        conf_key = category.ignored_conf_key
        if conf_key is None:
            return

        # Clean up the legacy "invalid" issue if it still exists from a previous version
        ir.async_delete_issue(self.hass, DOMAIN, f"{conf_key}_invalid_{self.server_id}")

        # A category the user has opted out of contributes an empty final_data[data_key] by
        # design (see _async_update_data). Running the stale-ID sweep below against that empty
        # list would flag EVERY configured ignore-id as "no longer in Comexio" and silently wipe
        # the user's ignore list on a mere toggle-off. The list is dormant config while the
        # category is off — leave it untouched; the sweep resumes when it is re-enabled.
        if webio_class not in active_webio_classes(conf):
            ir.async_delete_issue(self.hass, DOMAIN, f"{conf_key}_cleanup_{self.server_id}")
            return

        ignored_raw = conf.get(conf_key, "").strip()
        if not ignored_raw:
            ir.async_delete_issue(self.hass, DOMAIN, f"{conf_key}_cleanup_{self.server_id}")
            return

        sources_by_id = {int(item["id"]): item for item in final_data.get(category.data_key, [])}
        stale_ids: list[int] = []
        cleanup_ids: list[int] = []
        affected_fub_ids: set[int] = set()

        all_ignored_ids = expand_ignored_marker_ids(
            ignored_raw, category.audit_key_prefix + category.audit_key_prefix.lower()
        )
        lp_plans = await self._load_function_plan_check_data()
        ref_type = int(category.fub_module_type)
        ids_with_entities = set(self.marker_entities_by_id(list(all_ignored_ids), category.unique_id_infix).keys())
        _LOGGER.debug(
            "[%s] async_check_ignored_sources[%s]: ignored=%s, plans_loaded=%s",
            self.server_id,
            category.label,
            sorted(all_ignored_ids),
            sorted(lp_plans.keys()),
        )
        for source_id in sorted(all_ignored_ids):
            source = sources_by_id.get(source_id)
            if not source or not source.get("name", "").strip():
                # Source no longer exists in Comexio → stale, auto-remove
                stale_ids.append(source_id)
                continue

            # Source exists and is intentionally ignored — only flag if legacy entities/links remain
            has_entities = source_id in ids_with_entities
            function_plan_fub_id = self._check_source_function_plan_link(source_id, lp_plans, ref_type)
            _LOGGER.debug(
                "[%s] ignored %s%s: has_entities=%s, function_plan_fub_id=%s",
                self.server_id,
                category.audit_key_prefix,
                source_id,
                has_entities,
                function_plan_fub_id,
            )
            if function_plan_fub_id is not None:
                affected_fub_ids.add(function_plan_fub_id)
            if has_entities or function_plan_fub_id is not None:
                cleanup_ids.append(source_id)

        # Auto-remove stale IDs from options (source deactivated/removed in Comexio)
        if stale_ids:
            new_options = {**self.config_entry.options}
            if remaining_ids := sorted(all_ignored_ids - set(stale_ids)):
                new_options[conf_key] = ",".join(str(i) for i in remaining_ids)
            else:
                new_options.pop(conf_key, None)
            self.request_options_update_without_reload(new_options)
            stale_str = ", ".join(f"{category.audit_key_prefix}{sid}" for sid in stale_ids)
            _LOGGER.info(
                "[%s] Auto-removed stale %s IDs (no longer in Comexio): %s",
                self.server_id,
                conf_key,
                stale_str,
            )

            # persistent_notification has no per-user language context; use English
            notif_body = (
                f"{category.label} IDs **{stale_str}** were automatically removed from `{conf_key}` "
                "because they no longer exist in Comexio. "
                "They will be created as entities again on the next integration restart."
            )
            persistent_notification.async_create(
                self.hass,
                notif_body,
                title=f"Comexio ({self.server_id})",
                notification_id=f"comexio_stale_ignored_{webio_class.value}_{self.server_id}",
            )

        # Extend the shared cleanup accumulator for the combined sync_mismatch repair
        self._cleanup_entity_ids.extend((webio_class.value, sid) for sid in cleanup_ids)
        self._cleanup_function_plan_count += len(affected_fub_ids)
        # Remove legacy ignored_<category>_cleanup issue if it still exists from an older version
        ir.async_delete_issue(self.hass, DOMAIN, f"{conf_key}_cleanup_{self.server_id}")

    async def async_check_ignored_markers(self, conf: dict[str, Any], final_data: dict[str, Any]) -> None:
        """Check ignored marker IDs and manage repair issues (thin wrapper, see async_check_ignored_sources)."""
        await self.async_check_ignored_sources(conf, final_data, WebioClass.MARKER)

    async def async_check_ignored_knx(self, conf: dict[str, Any], final_data: dict[str, Any]) -> None:
        """Check ignored KNX object IDs and manage repair issues (thin wrapper, see async_check_ignored_sources)."""
        await self.async_check_ignored_sources(conf, final_data, WebioClass.KNX)

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
        server_slug = slugify(self.server_id)
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

    def marker_entities_by_id(self, marker_ids: list[int], unique_id_infix: str = "m") -> dict[int, er.RegistryEntry]:
        """Return registry entries for the given source ids that still have an HA entity.

        Single pass over the registry regardless of how many marker_ids are checked, instead
        of one full registry scan per marker. Shared by the audit check (read-only) and the
        cleanup button (which also deletes the returned entries) to avoid divergent matching
        logic between the two. unique_id_infix distinguishes source categories sharing this
        helper (e.g. "m" for markers, "k" for KNX objects, see SourceCategory.unique_id_infix).
        """
        ent_reg = er.async_get(self.hass)
        marker_id_by_unique_id = {
            f"{DOMAIN}_{self.server_id}_{unique_id_infix}{mid}".lower(): mid for mid in marker_ids
        }
        return {
            marker_id: entity
            for entity in ent_reg.entities.values()
            if (marker_id := marker_id_by_unique_id.get((entity.unique_id or "").lower())) is not None
        }

    # --- MANAGED CLUSTER PLANS ---

    def _function_plan_prefix(self) -> str:
        """Configured name prefix of the HA-managed cluster plans (default 'HA')."""
        return self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX)

    def _is_managed_function_plan(self, fub_id: int) -> bool:
        """True if fub_id's live plan name follows the '{prefix} - ...' managed naming.

        _function_plan_check_fub_ids() also includes the plan currently picked in the plan
        selector — which the user is free to point at any plan on the server, including one
        they author and lay out by hand (e.g. "Rolladen") — for the read-only missing-wiring
        check. Any *destructive* action (deleting debris elements, unwiring, re-sorting) must
        never touch a plan outside HA's own naming convention, or it silently rewrites the
        user's own logic. Callers that mutate plan elements must filter through this first.
        """
        return self.api.function_plan_name(fub_id).startswith(f"{self._function_plan_prefix()} - ")

    def _function_plan_cluster_size(self) -> int:
        """Configured maximum number of element pairs per managed cluster plan."""
        return int(
            self.config_entry.options.get(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
            )
        )

    @staticmethod
    def _cluster_plan_name(source_id: int, prefix: str, cluster_size: int, category_label: str) -> str:
        """Name of the managed cluster plan a marker/KNX object belongs to (deterministic bucket math).

        KNX ignores the passed-in cluster_size and always buckets at FUNCTION_PLAN_KNX_CLUSTER_SIZE
        (50), regardless of the shared CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN option (user decision,
        2026-09-20) — a KNX bridge row needs its own K-object column plus a wider WebIO column and
        reserves an extra row-slot per pair for the Phase 7 API-Loopback fan-out (see services/_grid.py
        _KNX_COLUMN_WIDTH / _assign_grid_positions). With the KNX-only pair-to-pair row pitch
        (FUNCTION_PLAN_KNX_LAYOUT_Y_STEP = 18.75, see is_knx_cluster_plan's row_step override in
        services/plan_actions.py) an A3-formatted canvas fits 2 columns × 26 two-slot pairs = 52
        pairs — just above 50, so bucketing at 50 leaves a little headroom rather than exactly
        maxing out the canvas. (This docstring went through two corrections on 2026-09-20: first
        it claimed "25 rows = 50 pairs" using the generic, untightened row pitch, which was never
        actually reachable; then, once the pair-to-pair pitch was tightened to match the
        within-pair hop pitch exactly [15.0], user testing found that made every row equidistant
        with no visual gap between pairs at all ["press an press"] — FUNCTION_PLAN_KNX_LAYOUT_Y_STEP
        [18.75] is the resulting compromise value, see its own docstring in const.py.) Bucketing
        more than the canvas can hold under the generic (marker-sized) default would silently
        overflow the canvas and drop pairs.
        """
        if category_label == SOURCE_CATEGORIES[WebioClass.KNX].label:
            cluster_size = FUNCTION_PLAN_KNX_CLUSTER_SIZE
        start = ((source_id - 1) // cluster_size) * cluster_size + 1
        return f"{prefix} - {category_label} [{start}-{start + cluster_size - 1}]"

    def expected_source_cluster_name(self, source_id: int, category_label: str) -> str:
        """Deterministic cluster-plan name a marker/KNX ID currently maps to (config-aware)."""
        return self._cluster_plan_name(
            source_id, self._function_plan_prefix(), self._function_plan_cluster_size(), category_label
        )

    def expected_marker_cluster_name(self, marker_id: int) -> str:
        """Deterministic cluster-plan name a marker ID currently maps to (config-aware)."""
        return self.expected_source_cluster_name(marker_id, SOURCE_CATEGORIES[WebioClass.MARKER].label)

    def expected_knx_cluster_name(self, knx_id: int) -> str:
        """Deterministic cluster-plan name a KNX object ID currently maps to (config-aware)."""
        return self.expected_source_cluster_name(knx_id, SOURCE_CATEGORIES[WebioClass.KNX].label)

    def io_cluster_plan_contains(self, live_plan_name: str, ext_name: str) -> bool:
        """True if a live plan name still parses as a managed IO cluster plan naming ext_name.

        Used right before writing IO pairs to catch a plan that was renamed/repurposed
        between resolve_io_clusters() resolving its fub_id and the actual write (e.g. its
        fub_id got reused by an unrelated create elsewhere in the meantime) — see
        expected_marker_cluster_name() for the marker-side counterpart.
        """
        members = self._io_plan_members(live_plan_name, self._function_plan_prefix())
        return members is not None and ext_name in members

    async def resolve_marker_clusters(
        self, marker_ids: list[int], category_label: str
    ) -> tuple[dict[int, list[int]], set[int], list[str]]:
        """Group marker/KNX IDs by cluster plan and resolve/create each plan.

        Returns ({fub_id: [source_ids_in_cluster]}, {fub_ids of freshly created plans},
        [names of cluster plans that could not be resolved/created]). The third element lets
        callers surface a partial failure to the user instead of it only appearing in the log —
        a plan can fail (e.g. the _verify_new_plan_is_empty guard rejecting a contaminated
        fub_id) while other clusters in the same batch succeed, so the emptiness of the first
        dict alone is not a reliable "something went wrong" signal.
        Lookup order per cluster: CONF_FUNCTION_PLAN_PLAN_MAP cache → name scan → create.
        category_label picks the plan-name category ("Marker"/"KNX") — see resolve_knx_clusters.
        """
        prefix = self._function_plan_prefix()
        cluster_size = self._function_plan_cluster_size()
        fub_data = self.api.fub_data

        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map: dict[str, int] = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
        stale = self._stale_plan_map_entries(fub_data)

        if category_label == SOURCE_CATEGORIES[WebioClass.KNX].label and cluster_size != FUNCTION_PLAN_KNX_CLUSTER_SIZE:
            _LOGGER.debug(
                "[%s] KNX cluster plans ignore the configured max-pairs-per-plan (%d) and always "
                "bucket at %d — see _cluster_plan_name",
                self.server_id,
                cluster_size,
                FUNCTION_PLAN_KNX_CLUSTER_SIZE,
            )

        clusters: dict[str, list[int]] = {}
        for mid in marker_ids:
            clusters.setdefault(self._cluster_plan_name(mid, prefix, cluster_size, category_label), []).append(mid)

        result: dict[int, list[int]] = {}
        created_plans: set[int] = set()
        plan_map_updates: dict[str, int] = {}
        failed_plans: list[str] = []

        for plan_name, cluster_ids in clusters.items():
            fub_id, created = await self._resolve_single_cluster_plan(plan_name, plan_map, fub_data)
            if fub_id is None:
                _LOGGER.error("[%s] Failed to resolve/create cluster plan '%s'", self.server_id, plan_name)
                failed_plans.append(plan_name)
                continue
            result[fub_id] = cluster_ids
            if created:
                created_plans.add(fub_id)
            plan_map_updates[plan_name] = fub_id

        if plan_map_updates or stale:
            await self._persist_plan_map(plan_map_updates, removals=set(stale))

        return result, created_plans, failed_plans

    async def resolve_knx_clusters(self, knx_ids: list[int]) -> tuple[dict[int, list[int]], set[int], list[str]]:
        """Group KNX object IDs by cluster plan and resolve/create each plan (KNX counterpart of
        resolve_marker_clusters — same bucket math, plans named "HA - KNX [x-y]")."""
        return await self.resolve_marker_clusters(knx_ids, category_label=SOURCE_CATEGORIES[WebioClass.KNX].label)

    async def resolve_trigger_plan(self) -> tuple[int | None, bool]:
        """Find or create the single dedicated "HA - TRIGGER" plan. Returns (fub_id, freshly_created)."""
        fub_data = self.api.fub_data
        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map: dict[str, int] = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
        fub_id, created = await self._resolve_single_cluster_plan(FUNCTION_PLAN_TRIGGER_PLAN_NAME, plan_map, fub_data)
        if fub_id is not None:
            await self._persist_plan_map({FUNCTION_PLAN_TRIGGER_PLAN_NAME: fub_id})
        return fub_id, created

    async def _resolve_single_cluster_plan(
        self, plan_name: str, plan_map: dict[str, int], fub_data: dict
    ) -> tuple[int | None, bool]:
        """Find or create a single cluster plan by name. Returns (fub_id, freshly_created)."""
        cached = plan_map.get(plan_name)
        if (
            cached is not None
            and cached not in self._distrusted_fub_ids
            and fub_data.get(str(cached), {}).get("Name") == plan_name
        ):
            return cached, False

        for fid_str, fub_info in fub_data.items():
            fid = int(fid_str)
            # Skip a plan _verify_new_plan_is_empty rejected but couldn't fully remove — see
            # _distrusted_fub_ids' docstring. Without this, a plain by-name match would silently
            # re-adopt the still-contaminated plan the moment fub_data gets refreshed again.
            if fid not in self._distrusted_fub_ids and fub_info.get("Name") == plan_name:
                _LOGGER.info("[%s] Found cluster plan '%s' (fub_id=%s)", self.server_id, plan_name, fid_str)
                return fid, False

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

        if not await self._verify_new_plan_is_empty(plan_name, fub_id):
            return None

        # Seed the bulk plan-snapshot cache immediately instead of waiting for the next full
        # poll's function_plan_load_all_plans() to pick this fub_id up. Without this, a plan
        # created earlier in THIS SAME sync run (e.g. Full Sync creating "HA - KNX [1-100]"
        # before wiring bridge Markers into it) is invisible to _relevant_plans_loaded() for
        # the rest of the run — async_fresh_trigger_audit()/async_fresh_knx_bridge_audit() then
        # defer their whole check to "next poll" even though nothing is actually missing from
        # the data, silently pushing the wiring itself out to a second, separate button press.
        # {"elements": {}, "connections": {}} is the exact shape function_plan_load_elements()/
        # function_plan_load_all_plans() use per plan — just-verified-empty is accurate as of
        # this line; the "administrated by HA" comment element added a few lines below won't be
        # reflected here until the next real poll, but no consumer filters on comment elements
        # (all of them look for a specific reference type — Marker/IO/KNX), so that particular
        # gap is harmless. _SEEDED_EMPTY_PLAN_MARKER flags this entry as a placeholder rather
        # than real bulk data — see its own docstring for why _load_function_plan_check_data()
        # needs to tell the difference.
        self.function_plan_plans[fub_id] = {"elements": {}, "connections": {}, _SEEDED_EMPTY_PLAN_MARKER: True}

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

    async def _verify_new_plan_is_empty(self, plan_name: str, fub_id: int) -> bool:
        """Guard against building a managed plan on top of stale leftover elements.

        Comexio's element/connection storage (loadelements/saveelements) is keyed purely by
        fub_id and is NOT tied to a plan's actual registration lifetime — an id that was ever
        written to before (a plan deleted directly in Comexio, a client that wrote elements
        under an id before any plan existed there, ...) can still be handed back by
        create_fup() as a "new" plan while its element storage silently carries the old data.
        Building on top of that would corrupt the fresh plan with someone else's leftover
        elements/wiring instead of the pristine canvas the rest of this method assumes.
        Confirmed live 2026-09-15: a stale KNX-bridge test write under an unregistered fub_id
        got inherited wholesale by a brand-new "HA - Marker [301-400]" plan that reused that
        same id, mixing unrelated ghost elements (positioned far outside the visible canvas)
        into the real plan.

        Leftover elements are cleared in place first (function_plan_delete_elements also
        removes their connections) so the freshly created plan/fub_id itself stays usable —
        discarding it via delete_fup would only free the fub_id to be handed back by a later
        create_fup() still carrying the same leftover data, since delete_fup does not touch
        the fub_id-keyed element storage that caused the contamination in the first place.
        The cleanup result is re-verified with a second load (the delete endpoint's boolean
        result alone isn't proof the plan is actually empty afterwards — trusting it blindly
        could let a partially-cleaned plan through undetected). Only if cleanup isn't possible
        (load failure, no elements to delete despite orphaned connections, or the delete/
        re-verify fails) is the plan discarded — see _discard_contaminated_plan. Either success
        path lifts a prior quarantine entry for this fub_id (_distrusted_fub_ids): once
        genuinely re-verified empty, a reused id is trustworthy again.
        """
        existing = await self.api.function_plan_load_elements(fub_id)
        if self._plan_contents_clean(existing):
            self._distrusted_fub_ids.discard(fub_id)
            return True

        if existing is None:
            _LOGGER.error(
                "[%s] Could not verify new cluster plan '%s' (fub_id=%s) is empty — removing it and aborting",
                self.server_id,
                plan_name,
                fub_id,
            )
            await self._discard_contaminated_plan(plan_name, fub_id)
            return False

        elem_ids = [int(eid) for eid in existing.get("elements") or {}]
        conn_count = len(existing.get("connections") or {})
        if not elem_ids:
            # function_plan_delete_elements operates on element ids — orphaned connections
            # with no elements behind them aren't cleanable through this endpoint at all.
            _LOGGER.error(
                "[%s] New cluster plan '%s' (fub_id=%s) has %d orphaned connection(s) but no elements — "
                "not cleanable via the API; removing the plan and aborting instead of building on top of "
                "it. Inspect/clear fub_id=%s directly in Comexio Studio.",
                self.server_id,
                plan_name,
                fub_id,
                conn_count,
                fub_id,
            )
            await self._discard_contaminated_plan(plan_name, fub_id)
            return False

        _LOGGER.warning(
            "[%s] New cluster plan '%s' (fub_id=%s) unexpectedly already has %d element(s)/%d connection(s) — "
            "Comexio likely reused a stale fub_id still carrying leftover data; clearing it before use.",
            self.server_id,
            plan_name,
            fub_id,
            len(elem_ids),
            conn_count,
        )
        cleared = await self.api.function_plan_delete_elements(elem_ids)
        if cleared and self._plan_contents_clean(await self.api.function_plan_load_elements(fub_id)):
            _LOGGER.info(
                "[%s] Cleared %d leftover element(s) from '%s' (fub_id=%s) — plan is usable now",
                self.server_id,
                len(elem_ids),
                plan_name,
                fub_id,
            )
            self._distrusted_fub_ids.discard(fub_id)
            return True

        _LOGGER.error(
            "[%s] Could not clear leftover elements from new cluster plan '%s' (fub_id=%s) — removing it "
            "and aborting instead of building on top of it",
            self.server_id,
            plan_name,
            fub_id,
        )
        await self._discard_contaminated_plan(plan_name, fub_id)
        return False

    @staticmethod
    def _plan_contents_clean(existing: dict | None) -> bool:
        """True if a function_plan_load_elements() result has neither elements nor connections."""
        return existing is not None and not existing.get("elements") and not existing.get("connections")

    async def _discard_contaminated_plan(self, plan_name: str, fub_id: int) -> None:
        """Best-effort remove a plan that failed the new-plan emptiness check and couldn't be
        cleaned in place, and make sure it can't be silently re-adopted afterwards.

        delete_fup() only un-registers the plan in Comexio — the fub_id-keyed element storage
        that caused the contamination is untouched, so the id could still be handed back by a
        later create_fup() and reproduce the exact same corruption; there is no fix for that
        beyond a human clearing it in Comexio Studio. Dropping the cache entry alone is not
        durable either: the very next parse_config() (every poll, and the reload every sync
        ends with) repopulates fub_data wholesale from Comexio's still-live $Fubs listing if
        delete_fup failed, which would let _resolve_single_cluster_plan's by-name scan silently
        re-adopt the same poisoned plan with no re-check at all. The session-local quarantine
        in _distrusted_fub_ids closes that gap for the lifetime of this coordinator.

        Does not touch function_plan_plans: _create_managed_plan only seeds that cache AFTER
        _verify_new_plan_is_empty returns True, and every path that reaches this method returns
        False from there first — so a discarded fub_id can never have a cache entry to begin
        with. This method must not become reachable from anywhere else without re-checking that.
        """
        self._distrusted_fub_ids.add(fub_id)
        if not await self.api.delete_fup(fub_id):
            _LOGGER.error(
                "[%s] Could not remove contaminated/unverified plan '%s' (fub_id=%s) — quarantined for "
                "this session, but manual cleanup in Comexio Studio is still required to free the name/id",
                self.server_id,
                plan_name,
                fub_id,
            )
        self.api.fub_data.pop(str(fub_id), None)

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
        after renames); membership order defines each extension's column index. Excludes
        _distrusted_fub_ids — the single choke point resolve_io_clusters/_join_or_create_io_plan/
        managed_io_plan_members all go through, so a plan _verify_new_plan_is_empty rejected but
        couldn't fully remove can't be found-by-name-and-written-into or joined-as-having-free-
        capacity here either (see _resolve_single_cluster_plan's analogous check for Marker/KNX).
        """
        result: dict[int, list[str]] = {}
        for fid_str, fub_info in self.api.fub_data.items():
            fid = int(fid_str) if fid_str.lstrip("-").isdigit() else None
            if fid is None or fid in self._distrusted_fub_ids:
                continue
            members = self._io_plan_members(str(fub_info.get("Name", "")), prefix)
            if members:
                result[fid] = members
        return result

    def managed_io_plan_members(self, fub_id: int) -> list[str] | None:
        """Extension membership when fub_id is an HA-managed IO cluster plan, else None.

        Public counterpart of _io_plan_membership for the services module's grid/sort code.
        """
        return self._io_plan_membership(self._function_plan_prefix()).get(fub_id)

    def is_trigger_plan(self, fub_id: int) -> bool:
        """Whether fub_id is the single HA-managed trigger plan ("HA - TRIGGER").

        The sort/grid code needs this to pick FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP over the
        generic row pitch — the Flanke block renders taller than a plain marker/WebIO pair.
        """
        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map: dict[str, int] = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
        return plan_map.get(FUNCTION_PLAN_TRIGGER_PLAN_NAME) == fub_id

    def is_knx_cluster_plan(self, fub_id: int) -> bool:
        """Whether fub_id's live plan name is one of this coordinator's managed KNX cluster plans.

        The sort/grid code needs this UPFRONT — before plan elements are even loaded, at the
        same point it already asks is_trigger_plan() — to pick the tighter KNX bridge row pitch
        (FUNCTION_PLAN_KNX_LAYOUT_Y_STEP, see services/plan_actions.py's row_step selection)
        instead of the generic marker/WebIO pitch. A KNX bridge pair reserves TWO row-slots
        (K-object row + Phase 7 loopback hop — see services/_grid.py _assign_grid_positions), so
        packing those slots at the generic pitch leaves a full blank row's worth of unused
        vertical gap between one pair and the next (user report, 2026-09-20, screenshot comparing
        old K5-K6-K7 spacing against the desired tighter K8-K9-K10 stacking). Deliberately NOT the
        exact same value as the within-pair hop pitch (_KNX_LOOPBACK_Y_OFFSET) though — an earlier
        attempt at that made every row in the plan perfectly equidistant with no visual gap
        between separate pairs at all ("alles press an press", same-day follow-up report) — see
        FUNCTION_PLAN_KNX_LAYOUT_Y_STEP's own docstring in const.py for the resulting numbers.

        Matches by live plan name (like _is_managed_function_plan), not CONF_FUNCTION_PLAN_PLAN_MAP
        membership — cluster plans aren't necessarily cached there under a name reverse-lookup,
        and the name pattern alone ("{prefix} - {KNX label} [...]") is already the same
        authoritative check _cluster_plan_name's callers rely on elsewhere.
        """
        prefix = f"{self._function_plan_prefix()} - {SOURCE_CATEGORIES[WebioClass.KNX].label} ["
        return self.api.function_plan_name(fub_id).startswith(prefix)

    def _io_rows_needed(self, ext_name: str) -> int:
        """Column rows one extension package needs (IOs + header/blank separator rows)."""
        idents = [io["identifier"] for io in (self.data or {}).get("io", []) if io["ext_name"] == ext_name]
        rows = io_column_rows(idents)
        return (max(rows.values()) + 1) if rows else 0

    async def resolve_io_clusters(self, ext_names: list[str]) -> tuple[dict[str, tuple[int, int]], set[int], list[str]]:
        """Resolve/create the managed IO cluster plan of every extension (membership-true).

        Returns ({ext_name: (fub_id, column_index)}, {fub_ids of freshly created plans},
        [ext_names whose plan could not be resolved/created]). The third element lets callers
        surface a partial failure to the user — see resolve_marker_clusters' docstring for why
        an empty first dict is not a reliable "something went wrong" signal when several
        extensions are being resolved at once.
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
        failed_exts: list[str] = []

        for ext in ext_names:
            placed = next(
                ((fid, members.index(ext)) for fid, members in membership.items() if ext in members),
                None,
            )
            if placed is None:
                placed = await self._join_or_create_io_plan(ext, membership, capacity, prefix)
            if placed is None:
                _LOGGER.error("[%s] Failed to resolve/create IO cluster plan for '%s'", self.server_id, ext)
                failed_exts.append(ext)
                continue
            fub_id, _column = placed
            result[ext] = placed
            if fub_id not in membership:
                created_plans.add(fub_id)
                membership[fub_id] = [ext]
            plan_map_updates[self._io_plan_name(prefix, membership[fub_id])] = fub_id

        if plan_map_updates or stale:
            await self._persist_plan_map(plan_map_updates, removals=set(stale))
        return result, created_plans, failed_exts

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

    def _has_active_function_plan(self) -> bool:
        """Whether a Managed Function Plan is configured and its selector isn't disabled/gone.

        Shared has_active_plan guard for async_fresh_knx_bridge_audit and
        async_fresh_knx_bridge_loopback_audit — both need it for the same reason (a fresh
        re-audit against a plan nobody actually has active would be meaningless work), factored
        out so the boolean expression isn't duplicated a third time and to keep each caller's
        own cognitive complexity down (SonarQube S3776).
        """
        _lp_sel = self._active_plan_selector_state()
        return (_lp_sel is not None and _lp_sel.state not in ("unavailable", "unknown")) or (
            self.config_entry.options.get(CONF_FUNCTION_PLAN_FUB_ID) is not None
            or bool(self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP))
        )

    def _api_credentials_configured(self) -> bool:
        """Whether both optional API Basic-Auth credentials (username + password) are set.

        Several Phase 7 API-Loopback code paths need these (ensure_knx_loopback_webio aborts
        deterministically without them) — a shared check so the audit gates that decide whether
        to flag a loopback gap agree exactly with what the actual repair attempt requires,
        instead of two independently-written boolean expressions drifting apart over time.
        """
        return bool(self.api.api_user and self.api.api_pass)

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

        A _SEEDED_EMPTY_PLAN_MARKER entry (a plan _create_managed_plan created and verified
        empty earlier in this same run, not yet refreshed by a real bulk load) counts as a
        cache miss here too — this method feeds marker-cleanup/unwire lookups that need to see
        whatever was actually wired into the plan afterward, not the seed's now-possibly-stale
        empty snapshot.
        """
        plans: dict[int, dict] = {}
        for fub_id in self._function_plan_check_fub_ids():
            plan_data = self.function_plan_plans.get(fub_id)
            if plan_data is None or plan_data.get(_SEEDED_EMPTY_PLAN_MARKER):
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

    async def resolve_source_cleanup_plans(
        self, source_ids: list[int], preferred_fub_id: int | None = None, ref_type: int = 2
    ) -> dict[int, list[int]]:
        """Group markers/KNX objects by the managed plan they are wired in, for per-plan cleanup.

        A user-selected plan (preferred_fub_id) keeps the single-plan behaviour: all supplied
        ids are grouped under it as-is, without an upfront wiring check — the per-id cleanup
        call itself is a no-op for any id not actually wired there. Otherwise each id is
        looked up in all managed plans and unwired ones are omitted here instead. ref_type
        selects the source category's plan-element type (marker=2, KNX=11 — blind guess).
        """
        if preferred_fub_id is not None:
            return {preferred_fub_id: list(source_ids)}
        plans = await self._load_function_plan_check_data()
        plan_to_ids: dict[int, list[int]] = {}
        for source_id in source_ids:
            fub_id = self._check_source_function_plan_link(source_id, plans, ref_type)
            if fub_id is not None and self._is_managed_function_plan(fub_id):
                plan_to_ids.setdefault(fub_id, []).append(source_id)
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
                if not self._is_managed_function_plan(fub_id):
                    continue
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

    def _check_source_function_plan_link(self, source_id: int, plans: dict[int, dict], ref_type: int = 2) -> int | None:
        """Check if the source (marker=2/KNX=11) is wired in any of the pre-loaded managed plans.

        Returns the fub_id of the first plan in which the source element has a
        WebIO connection, else None.
        """
        for fub_id, plan_data in plans.items():
            if self._source_wired_in_plan(source_id, plan_data, ref_type):
                return fub_id
        return None

    @staticmethod
    def _source_wired_in_plan(source_id: int, plan_data: dict, ref_type: int = 2) -> bool:
        """Check if the source element (marker=2/KNX=11) in this plan has an outgoing connection."""
        all_matches = [
            elem_id
            for elem_id, elem_data in plan_data.get("elements", {}).items()
            # reference.type comes back as int or str depending on the response shape — normalize.
            if str((ref := elem_data.get("reference", {})).get("type")) == str(ref_type)
            and int(ref.get("ref_id", -1)) == source_id
        ]
        if len(all_matches) > 1:
            _LOGGER.debug("_source_wired_in_plan: M%s has MULTIPLE elements in this plan: %s", source_id, all_matches)
        marker_elem_id = ComexioAPI._find_source_element_id(plan_data.get("elements", {}), source_id, ref_type)
        if not marker_elem_id:
            _LOGGER.debug("_source_wired_in_plan: M%s not found as an element in this plan", source_id)
            return False

        # DEBUG: dump every connection touching ANY of the matched marker elements, in either role
        for conn_id, conn_data in plan_data.get("connections", {}).items():
            in_id = str(conn_data.get("input", {}).get("FubElementId", -1))
            out_ids = ComexioAPI._connection_output_ids(conn_data)
            if in_id in all_matches or any(oid in all_matches for oid in out_ids):
                _LOGGER.debug(
                    "_source_wired_in_plan: M%s conn=%s touches a marker element (input=%s, outputs=%s, raw=%s)",
                    source_id,
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
            "_source_wired_in_plan: M%s -> elem_id=%s (all_matches=%s), wired=%s",
            source_id,
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
    def _plan_knx_bridge_pairs(plan_data: dict) -> set[tuple[str, str]]:
        """(bridge Marker ref_id, KNX ref_id) pairs actually wired Marker -> KNX in one plan.

        Write-path counterpart of _plan_wired_pairs, but direction-sensitive unlike it:
        a Marker and a KNX object can legitimately be connected in EITHER direction on a
        plan (e.g. a K -> Marker wire built for unrelated custom logic, independent of
        Entwurf A "Merker-Brücke"), and only Marker (input) -> KNX (output) counts as a
        write bridge — the reverse must not be misread as one just because both element
        types touch the same connection.
        """
        elem_refs: dict[str, tuple[str, str]] = {}
        for elem_id, elem in (plan_data.get("elements") or {}).items():
            ref = elem.get("reference") or {}
            ref_type = str(ref.get("type"))
            if ref_type in ("2", "11"):
                elem_refs[str(elem_id)] = (ref_type, str(ref.get("ref_id")))
        pairs: set[tuple[str, str]] = set()
        for conn in (plan_data.get("connections") or {}).values():
            input_id = str((conn.get("input") or {}).get("FubElementId"))
            input_ref = elem_refs.get(input_id)
            if input_ref is None or input_ref[0] != "2":
                continue
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            output_ids = {str(o.get("FubElementId")) for o in outputs}
            knx_ids = {elem_refs[oid][1] for oid in output_ids if elem_refs.get(oid, ("", ""))[0] == "11"}
            pairs.update((input_ref[1], k) for k in knx_ids)
        return pairs

    def _audit_knx_bridge_items(
        self,
        has_active_plan: bool,
        knx_objects: list[dict[str, Any]],
        ha_map: dict[str, Any],
        wired_knx_webio_pairs: set[tuple[str, str]] | None,
        mismatches: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """KNX write path (Entwurf A "Merker-Brücke") audit: bridge-Marker + API-Loopback fan-out.

        A KNX object is blind/read-only in Comexio unless a dedicated bridge Marker is wired
        Marker -> KNX object in its cluster plan (the K -> WebIO leg is the pre-existing,
        separately-audited read path and is intentionally NOT duplicated here). Only imported/
        non-ignored KNX objects (present in ha_map) are candidates — one without an HA entity
        has nothing to bridge.

        Phase 7 (API-Loopback fan-out): once a bridge's Marker/K-Element wiring exists
        (knx_bridge_marker_by_k_id), the K-Element's own connection must ALSO reach the
        Comexio-internal API-Loopback Web-IO (wire_knx_bridge_loopback) or the bridge Marker
        gets stuck after a Bus-Rückänderung (see project_knx_write_path_design memory,
        "Punkt 4"). Detected purely from the already-loaded wired_knx_webio_pairs —
        _plan_wired_pairs collects EVERY (source, webio) pair a connection's sinks produce, so a
        bridged K-Element with fewer than 2 such pairs is missing the loopback leg. No extra
        HTTP round trip needed, and independent of the loopback device's/webIoId's concrete
        identity (irrelevant to a plain sink count).

        Mutates `mismatches` in place and may set self._lp_missing_recheck_pending — factored
        out of _async_update_data purely to keep that method's own cognitive complexity (already
        an accepted SonarQube S3776 outlier) from growing further with each Phase 7 addition.
        """
        knx_bridge_missing_items: list[dict[str, Any]] = []
        knx_bridge_loopback_missing_items: list[dict[str, Any]] = []
        if not has_active_plan:
            return knx_bridge_missing_items, knx_bridge_loopback_missing_items
        knx_bridge_marker_by_k_id = self._knx_bridge_marker_by_k_id()
        if knx_bridge_marker_by_k_id is None:
            self._lp_missing_recheck_pending = True
            return knx_bridge_missing_items, knx_bridge_loopback_missing_items
        knx_category = category_by_fub_module_type("11")
        for k in knx_objects:
            k_id = str(k["id"])
            if source_audit_key(knx_category, k["id"]) not in ha_map:
                continue
            if k.get("kind") == MarkerKind.READ_ONLY:
                # A read-only KNX object (explicit "[RO]" suffix, or auto-tagged by
                # _auto_suffix_unambiguous_knx) has no write path to bridge — without this
                # exclusion it would be flagged as "missing bridge Marker" on every poll
                # forever, since knx_bridge_marker_by_k_id legitimately never contains it. A
                # DPT-unambiguous item auto-tagged THIS same poll still briefly races past this
                # check once (renamed remotely, but this poll's already-parsed item dict is not
                # itself mutated — see _auto_suffix_unambiguous_knx's docstring); that one-poll
                # lag self-resolves on the next poll's fresh scrape, same as the accepted lag
                # documented there (Sourcery finding, review 2026-09-21).
                continue
            if k_id not in knx_bridge_marker_by_k_id:
                knx_bridge_missing_items.append(
                    {
                        "name": k["name"],
                        "ref_id": k_id,
                        "title": k["title"],
                        "type_raw": k["type_raw"],
                        "webio_class": WEBIO_CLASS_KNX,
                    }
                )
                mismatches.add(f"knx_bridge_missing_K{k_id}")

        if wired_knx_webio_pairs is None:
            self._lp_missing_recheck_pending = True
            return knx_bridge_missing_items, knx_bridge_loopback_missing_items

        knx_by_id = {str(k["id"]): k for k in knx_objects}
        knx_bridge_loopback_missing_items = self._audit_knx_bridge_loopback_items(
            knx_bridge_marker_by_k_id, knx_by_id, wired_knx_webio_pairs, mismatches
        )
        return knx_bridge_missing_items, knx_bridge_loopback_missing_items

    def _audit_knx_bridge_loopback_items(
        self,
        knx_bridge_marker_by_k_id: dict[str, str],
        knx_by_id: dict[str, dict[str, Any]],
        wired_knx_webio_pairs: set[tuple[str, str]],
        mismatches: set[str],
    ) -> list[dict[str, Any]]:
        """Phase 7 API-Loopback fan-out audit half of _audit_knx_bridge_items.

        Split out to keep _audit_knx_bridge_items' own cognitive complexity within SonarQube
        S3776's limit — see that method's docstring for the full semantics.

        Known limitation: "complete" is decided by sink COUNT (>=2) alone, not by verifying
        one sink is actually the API-Loopback device's own command — a K-element with two
        ordinary/non-loopback Web-IO connections would suppress this repair indefinitely.
        Fixing this needs a live identity lookup of which webIoId belongs to the loopback
        class' commands (it's deliberately outside WEBIO_CLASSES, so there's no cached
        per-poll mapping for it yet — see ensure_knx_loopback_webio's docstring). Tracked as
        an open Phase 7 follow-up (Sourcery finding, review 2026-09-21), not fixed here.
        """
        sink_counts: dict[str, int] = {}
        for k_ref_id, _webio_ref_id in wired_knx_webio_pairs:
            sink_counts[k_ref_id] = sink_counts.get(k_ref_id, 0) + 1
        # The repair for this gap (ensure_knx_loopback_webio) needs the OPTIONAL API username/
        # password (CONF_API_USERNAME/CONF_API_PASSWORD) — without them it aborts
        # deterministically every time. Flagging the gap anyway would raise a repair item that
        # can never be fixed until credentials are set, re-appearing on every single poll —
        # check once, not per candidate.
        has_api_credentials = self._api_credentials_configured()
        unfixable_without_credentials: list[str] = []
        knx_bridge_loopback_missing_items: list[dict[str, Any]] = []
        for k_id, marker_id in knx_bridge_marker_by_k_id.items():
            # Not in sink_counts at all -> the read-path wire itself is missing, a different,
            # already-audited gap (knx_bridge_missing/function_plan_missing) — nothing to fan
            # the loopback onto yet.
            if k_id not in sink_counts or sink_counts[k_id] >= 2:
                continue
            if not has_api_credentials:
                unfixable_without_credentials.append(k_id)
                continue
            k = knx_by_id.get(k_id)
            knx_bridge_loopback_missing_items.append(self._knx_bridge_loopback_missing_item(k, k_id, marker_id))
            mismatches.add(f"knx_bridge_loopback_missing_K{k_id}")
        if unfixable_without_credentials:
            # One aggregated WARNING per poll rather than one INFO per candidate (a large KNX
            # install would otherwise flood the log every ~15 min) — WARNING, not INFO, because
            # this hides a real gap from the repair UI entirely (see _api_credentials_configured's
            # docstring) and the only remaining visibility into it is this log line.
            _LOGGER.warning(
                "[%s] %d KNX bridge(s) are missing their API-Loopback fan-out (K%s), but no API "
                "credentials are configured (set them via the integration's Reconfigure dialog) "
                "— not flagging as a repair item until they are set, since the repair would fail "
                "deterministically without them",
                self.server_id,
                len(unfixable_without_credentials),
                ", K".join(sorted(unfixable_without_credentials)),
            )
        return knx_bridge_loopback_missing_items

    @staticmethod
    def _knx_bridge_loopback_missing_item(k: dict[str, Any] | None, k_id: str, marker_id: str) -> dict[str, Any]:
        """Build one knx_bridge_loopback_missing audit item.

        Shared by the live-poll computation in _async_update_data and the fresh mid-sync
        re-audit (async_fresh_knx_bridge_loopback_audit) — byte-identical dict shape in both,
        factored out to avoid the two drifting apart and to keep each caller's own cognitive
        complexity down (SonarQube S3776; the two ternaries below cost +2 each inside a
        for-loop). k is None when the K-Element vanished from the live config between the
        wired-pairs check and this lookup — both callers fall back to a synthetic name/
        binary-guess in that case rather than crash.
        """
        return {
            "name": k["name"] if k else f"K{k_id}",
            "ref_id": k_id,
            "marker_id": marker_id,
            "binary": k["type"] == "digital" if k else True,
            "webio_class": WEBIO_CLASS_KNX,
        }

    def _knx_dpt_suffix_ignored_ids(self) -> set[int]:
        """Ids the user chose "leave as writable switch" for in the knx_dpt_ambiguous repair
        flow (CONF_KNX_DPT_SUFFIX_IGNORED) — shared by both the auto-tag and the audit below
        so a manual opt-out (added by hand to the option, e.g. for an unambiguous DPT the
        installer actually wired as a real switch) is honored by both.
        """
        knx_prefix = SOURCE_CATEGORIES[WebioClass.KNX].audit_key_prefix
        raw = self.config_entry.options.get(CONF_KNX_DPT_SUFFIX_IGNORED, "").strip()
        return expand_ignored_marker_ids(raw, knx_prefix + knx_prefix.lower())

    async def _auto_suffix_unambiguous_knx(self, knx_items: list[dict[str, Any]]) -> None:
        """Auto-append "[RO]" to a digital KNX object whose DPT is semantically unambiguous
        (KNX_DPT_DIGITAL_DEVICE_CLASS's 3 entries: Alarm/Anwesenheit/Tür-Fenster) but whose
        title carries no classification suffix yet.

        A real ETS-imported KNX object never carries Comexio's own [RO]/[TRIG] convention —
        without this it would default to ComexioKnxSwitch (writable) even though these 3
        DPTs are near-universally read-only sensor telegrams in practice (user decision
        2026-09-20, see project_knx_write_path_design memory). Self-limiting on success: once
        renamed, the next poll sees the "[RO]" suffix and MarkerKind.READ_ONLY, so this never
        re-fires for the same item — unlike every other live-mutating action in this
        integration, that makes it safe to run unconditionally on every poll instead of gating
        it behind the Sync button or a Repair issue. Ignored KNX objects (ignored_knx_ids) and
        objects the user manually added to CONF_KNX_DPT_SUFFIX_IGNORED are skipped. A rename
        failure is retried on the next few polls (KNX_DPT_AUTOTAG_MAX_RETRIES) rather than
        given up on immediately — a one-off transient error (e.g. a momentary HTTP hiccup)
        would otherwise be treated the same as a genuinely persistent one (stale admin
        session, name collision); only past that many consecutive failures is the id skipped
        for the rest of this coordinator's lifetime.
        """
        ignored = self.ignored_knx_ids
        ignored_suffix = self._knx_dpt_suffix_ignored_ids()
        renamed_count = 0
        newly_failed: list[str] = []
        for item in knx_items:
            k_id = int(item["id"])
            if (
                item["type"] != "digital"
                or item.get("dpt_device_class") is None
                or item.get("kind") != MarkerKind.NORMAL
                or k_id in ignored
                or k_id in ignored_suffix
                or self._knx_dpt_autotag_fail_counts.get(k_id, 0) >= KNX_DPT_AUTOTAG_MAX_RETRIES
            ):
                continue
            if await self.api.rename_knx_object(item["id"], f"{item['title']} {MARKER_READ_ONLY_SUFFIX}"):
                renamed_count += 1
                self._knx_dpt_autotag_fail_counts.pop(k_id, None)
                continue
            fail_count = self._knx_dpt_autotag_fail_counts.get(k_id, 0) + 1
            self._knx_dpt_autotag_fail_counts[k_id] = fail_count
            if fail_count >= KNX_DPT_AUTOTAG_MAX_RETRIES:
                newly_failed.append(item["name"])
        if newly_failed:
            _LOGGER.warning(
                "[%s] Giving up auto-tagging %d KNX object(s) as read-only after %d failed "
                "attempts each (%s) — will not retry until the integration is reloaded; check "
                "the preceding error log for the cause",
                self.server_id,
                len(newly_failed),
                KNX_DPT_AUTOTAG_MAX_RETRIES,
                ", ".join(newly_failed),
            )
        if renamed_count:
            self._schedule_knx_dpt_reload(renamed_count)

    def _schedule_knx_dpt_reload(self, renamed_count: int) -> None:
        """Reload the integration shortly after _auto_suffix_unambiguous_knx renamed at least
        one KNX object, so the entity platforms rebuild with its new MarkerKind (switch ->
        sensor/binary_sensor) — entities are only created once, in each platform's
        async_setup_entry, so a coordinator refresh alone would leave the stale writable
        entity in place until the next full HA restart.

        Deliberately scheduled with a short delay via async_call_later rather than reloading
        synchronously from inside the poll that just wrote the rename: config_entries.
        async_reload() tears the coordinator down (async_shutdown, api.close()), which would
        race the still-running _async_update_data call this was triggered from if it fired
        immediately. Debounced to one pending timer (_knx_dpt_reload_cancel) no matter how
        many objects get renamed in this poll or across consecutive polls before it fires.
        """
        if self._knx_dpt_reload_cancel is not None:
            return
        _LOGGER.info(
            "[%s] Auto-tagged %d KNX object(s) as read-only; scheduling integration reload...",
            self.server_id,
            renamed_count,
        )
        self._knx_dpt_reload_cancel = async_call_later(self.hass, 5, self._async_fire_knx_dpt_reload)

    async def _async_fire_knx_dpt_reload(self, _now: Any) -> None:
        """async_call_later callback for _schedule_knx_dpt_reload.

        Fires from an unsupervised background timer, not a user-initiated action with its own
        status sensor to fall back on — so a failed reload is logged with full context here
        rather than left to bubble up into whatever generic handler async_call_later's executor
        uses. The renamed KNX object(s) stay on their stale entity platform until the next
        reload/restart in that case; nothing else in this coordinator retries it.
        """
        self._knx_dpt_reload_cancel = None
        try:
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        except Exception:
            _LOGGER.exception(
                "[%s] Reload after auto-tagging KNX object(s) as read-only failed — affected "
                "entities stay on their previous platform until the next reload or restart",
                self.server_id,
            )

    def _audit_knx_dpt_ambiguous(self, knx_items: list[dict[str, Any]]) -> None:
        """Repair-issue audit for digital KNX objects whose DPT is a physically ambivalent
        DPT1.x subtype (Schalter/Bool/Freigabe/Flanke/Binärwert — KNX_DPT_DIGITAL_AMBIGUOUS)
        with no classification suffix yet.

        Unlike the 3 unambiguous DPTs (_auto_suffix_unambiguous_knx), these need a human
        decision — the DPT alone can't tell a real toggle switch from a Taster (-> "[TRIG]")
        or a pure status readback (-> "[RO]"), see project_knx_write_path_design memory.
        Raises one combined Repair issue per server listing every currently-open item; the
        flow (repairs.py) lets the user classify each one individually, one at a time, or
        leave it unchanged (added to CONF_KNX_DPT_SUFFIX_IGNORED so it stops reappearing).
        """
        ignored_suffix = self._knx_dpt_suffix_ignored_ids()
        ignored_knx = self.ignored_knx_ids

        items = [
            {"id": item["id"], "title": item["title"], "name": item["name"]}
            for item in knx_items
            if item.get("dpt_ambiguous")
            and item.get("kind") == MarkerKind.NORMAL
            and int(item["id"]) not in ignored_knx
            and int(item["id"]) not in ignored_suffix
        ]

        issue_id = f"knx_dpt_ambiguous_{self.server_id}"
        if not items:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="knx_dpt_ambiguous",
            translation_placeholders={"server_id": self.server_id, "count": str(len(items))},
            data={"entry_id": self.config_entry.entry_id, "items": items},
        )

    def _knx_bridge_marker_by_k_id(self) -> dict[str, str] | None:
        """{k_id: bridge marker_id} for every KNX object already wired to a bridge Marker.

        Built from the same bulk plan snapshot / relevant-plans contract as
        _wired_source_webio_pairs: returns None while any plan relevant to the audit (see
        _function_plan_check_fub_ids) is not yet loaded, so the caller can defer the check to
        the next poll instead of misreporting a still-loading plan as "no bridge wired".

        Unlike _load_function_plan_check_data, a _SEEDED_EMPTY_PLAN_MARKER entry here is read
        as-is rather than treated as a miss — reading it *as* a real snapshot is deliberate: at
        seed time the plan genuinely had zero Marker->KNX pairs, and this method only ever looks
        for that one connection shape (see _plan_knx_bridge_pairs), which the K->WebIO pairs a
        Full Sync writes into the very same plan right beforehand never produce. That's still an
        incidental non-collision between two specific connection shapes, not a structural
        guarantee — a future caller reading this cache for anything else must not assume a
        seeded entry reflects everything written into the plan since it was created.

        An empty relevant_fub_ids (no active plan selected AND no CONF_FUNCTION_PLAN_PLAN_MAP
        entries — e.g. the legacy CONF_FUNCTION_PLAN_FUB_ID == "auto" case) is deliberately
        NOT delegated to _relevant_plans_loaded here: that method treats it as vacuously
        "loaded" (see its own docstring) once function_plan_plans is merely non-empty from an
        unrelated earlier snapshot, which would make this method return {} — read by
        ComexioKnxEntity._async_source_write() as "definitely no bridge wired, run Full Sync",
        a misleading instruction when the real problem is "no managed function plan configured
        at all" (confirmed 2026-09-16 review). Returning None here instead keeps the caller on
        its "data not loaded yet" branch, which — while not naming the actual cause — at least
        never tells the user to run a sync step that cannot fix this.
        """
        relevant_fub_ids = self._function_plan_check_fub_ids()
        if not relevant_fub_ids or not self._relevant_plans_loaded(relevant_fub_ids):
            return None
        by_k_id: dict[str, str] = {}
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id in relevant_fub_ids:
                for marker_id, k_id in self._plan_knx_bridge_pairs(plan_data):
                    by_k_id[k_id] = marker_id
        return by_k_id

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

    def _relevant_plans_loaded(self, relevant_fub_ids: set[int]) -> bool:
        """Whether every fub_id relevant to the wiring audit is already in the bulk snapshot.

        Checking only "is the snapshot non-empty" is not enough: right after startup/reload
        the backup cycle fills function_plan_plans incrementally, so a snapshot can already
        hold some plans while a relevant one (e.g. the marker cluster plan containing a
        marker's real Web-IO pair) has not landed yet. Treating that partial state as "ready"
        makes _wired_source_webio_pairs/_connected_source_ids scan only the fub_ids that
        happen to be loaded and silently miss the rest, which _function_plan_gap_item then
        misreports as a genuinely missing wire.

        relevant_fub_ids is narrowed to still-existing plans first: a fub_id can outlive its
        plan in CONF_FUNCTION_PLAN_PLAN_MAP (deleted/renamed directly in Comexio — the same
        stale-entry case _stale_plan_map_entries cleans up, but only on the sync-button path,
        never here). An unnarrowed check would wait forever for a fub_id that function_plan_
        load_all_plans() — itself filtered against fub_data — can never deliver, permanently
        returning False and, via _lp_missing_recheck_pending, retriggering a refresh every
        backup cycle without end. The empty-snapshot guard stays explicit so the original
        startup race (no plans loaded yet at all) is still caught even when relevant_fub_ids
        itself is empty (legacy CONF_FUNCTION_PLAN_FUB_ID == "auto").
        """
        if not self.function_plan_plans:
            return False
        existing_fub_ids = {int(fub_id) for fub_id in self.api.fub_data}
        return (relevant_fub_ids & existing_fub_ids) <= self.function_plan_plans.keys()

    async def _ensure_relevant_plans_cached(self, relevant_fub_ids: set[int], *, force: bool = False) -> bool:
        """Best-effort top up of self.function_plan_plans for a fresh (mid-sync) audit.

        _relevant_plans_loaded() only ever reports whether every relevant, still-existing
        fub_id is ALREADY cached — it never fetches anything itself, by design: the regular
        per-poll audit path deliberately trusts the passive backup-cycle snapshot rather than
        doing a live fetch on every tick (this cache exists specifically to avoid a per-poll
        cost of ~0.5s per plan — see project_logikplan_services memory).

        The "fresh" mid-sync audits (async_fresh_trigger_audit/async_fresh_knx_bridge_audit)
        cannot make that trade-off: right after an HA restart self.function_plan_plans starts
        completely empty, and the backup cycle that would normally fill it runs as a
        *background* task (see _async_update_data) that can still be mid-flight when Initial
        Setup is pressed moments after startup — this is what "the trigger plan re-audit
        skipped" and "the KNX bridge re-audit skipped" warnings in the same sync run actually
        trace back to, even for plans that already existed long before this run (most commonly
        the trigger plan itself). _create_managed_plan's own cache seed only ever covers the
        ONE plan it just created and cannot help here — every other plan relevant_fub_ids names
        stays missing until something actually fetches it. So this live-fetches every relevant,
        still-existing fub_id that is not in the cache yet (an entry already present — seeded
        placeholder or genuine bulk data — is left untouched) and stores the result as genuine
        data, so the audit that runs right after this call sees the real picture instead of
        deferring to "next poll" for a plan that has simply never been loaded since restart.

        Fetched results are collected in a local dict and merged into self.function_plan_plans
        in one final, non-awaiting step rather than written in directly per iteration: the
        backup cycle above replaces the whole attribute wholesale (self.function_plan_plans =
        plans, not a merge) whenever it finishes, and with an `await` between each fetch in
        this loop, that background reassignment can land in the middle of it — silently
        discarding an entry this loop already wrote into the dict object that reassignment just
        replaced. Merging once, after every fetch has completed, avoids that window entirely.

        A fub_id this couldn't fetch (load failure, or the plan vanished between the caller's
        own existence check and this one) is logged here by name/count — the caller's own
        "not yet in the bulk snapshot, will retry next poll" warning fires unconditionally
        whenever a plan is still missing afterwards, whether or not a live fetch was actually
        attempted, and would otherwise read as pure timing when a fetch genuinely just failed.

        force=True skips the "already cached" skip entirely and refetches every relevant,
        still-existing fub_id regardless of cache presence. Needed by
        async_fresh_knx_bridge_loopback_audit and (since 2026-09-21) async_fresh_trigger_audit:
        both can run in the same sync pass as _wire_knx_full/_wire_knx_cluster, right after
        those wrote new bridge Markers/sinks into the very plan this call is about to read
        back — by then that plan is virtually guaranteed to already be a cache entry (just a
        stale one, from before this run's writes), so the default "only fetch what's missing"
        behavior would silently skip the live refetch that specific audit needs to see its own
        run's writes (live-reproduced for the trigger case 2026-09-21: a K-object's trigger pair
        failed with "no write-path bridge marker yet" moments after its bridge was wired
        correctly in the same sync, once the KNX 3-leg consolidation removed the OLD design's
        own end-of-run re-audit that used to refresh this cache as an incidental side effect).
        async_fresh_knx_bridge_audit is the one caller that genuinely doesn't have this shape —
        it always runs FIRST in a sync pass (before any KNX writes happen), so the default
        (cache-preserving) behavior is correct and cheaper there.

        Returns True unless a force=True refetch could not confirm freshness for at least one
        candidate (its live fetch failed, leaving whatever was already cached — genuine data or,
        just as likely, nothing at all — untouched and possibly stale). Without this signal, a
        caller that specifically asked for force=True to guarantee a fresh view would silently
        keep trusting pre-write data on a transient fetch failure instead of deferring like the
        non-forced path already does when a fub_id is simply absent. Callers that don't pass
        force=True don't need this: a fub_id that was never fetched and stays absent from the
        cache is already correctly read as "not ready" by _knx_bridge_marker_by_k_id()/
        _wired_source_webio_pairs returning None, so they're free to ignore the return value.
        """
        existing_fub_ids = {int(fub_id) for fub_id in self.api.fub_data}
        fetched: dict[int, dict] = {}
        candidates = relevant_fub_ids & existing_fub_ids
        to_fetch = candidates if force else {fub_id for fub_id in candidates if fub_id not in self.function_plan_plans}
        for fub_id in to_fetch:
            try:
                plan_data = await self.api.function_plan_load_elements(fub_id)
            except Exception:
                _LOGGER.exception(
                    "[%s] Error loading function plan %s for fresh mid-sync audit", self.server_id, fub_id
                )
                continue
            if plan_data:
                fetched[fub_id] = plan_data
        if fetched:
            self.function_plan_plans.update(fetched)
        failed = to_fetch - fetched.keys()
        if failed:
            _LOGGER.warning(
                "[%s] Fresh mid-sync top-up: could not load plan(s) %s live — the next audit "
                "warning for these is a genuine fetch failure, not just backup-cycle timing",
                self.server_id,
                sorted(failed),
            )
        return not (force and failed)

    def _connected_source_ids(self, source_type: str) -> set[str] | None:
        """ref_ids of `source_type` elements with ANY connection at all, across every plan
        relevant to the wiring audit — see _plan_connected_source_ids. Mirrors
        _wired_source_webio_pairs' scoping (same relevant_fub_ids, same None-while-not-loaded
        contract) since both feed the same audit cycle.
        """
        relevant_fub_ids = self._function_plan_check_fub_ids()
        if not self._relevant_plans_loaded(relevant_fub_ids):
            return None
        connected: set[str] = set()
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id in relevant_fub_ids:
                connected.update(self._plan_connected_source_ids(plan_data, source_type))
        return connected

    def _wired_source_webio_pairs(self, source_type: str) -> set[tuple[str, str]] | None:
        """Return (ref_id, webIoId) pairs of source elements (marker "2" / IO "1") directly
        wired to a WebIO element in a managed plan.

        Built from the bulk snapshot of the backup cycle; returns None while any plan relevant
        to the audit (see _function_plan_check_fub_ids) is not loaded into that snapshot yet,
        including a partially-populated snapshot missing just one of them. A pair only counts
        as wired when the source element and the WebIO
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
        relevant_fub_ids = self._function_plan_check_fub_ids()
        if not self._relevant_plans_loaded(relevant_fub_ids):
            return None
        pairs: set[tuple[str, str]] = set()
        for fub_id, plan_data in self.function_plan_plans.items():
            if fub_id in relevant_fub_ids:
                pairs.update(self._plan_wired_pairs(plan_data, source_type))
        return pairs

    def _audit_wired_pairs(
        self, has_active_plan: bool, managed_io_exts: set[str]
    ) -> tuple[
        set[tuple[str, str]] | None,
        set[tuple[str, str]] | None,
        set[tuple[str, str]] | None,
        set[str] | None,
        set[str] | None,
        set[str] | None,
    ]:
        """Wired (ref_id, webIoId) pair sets for the audit: (markers, IOs, KNX objects), plus the
        any-connection-at-all ref_id sets _dangling_source_ids needs alongside them (see there).

        Each set is None when its check is disabled (no active plan / no managed IO
        extensions) or the bulk plan snapshot is not loaded yet (recheck scheduled).
        """
        wired_marker_pairs: set[tuple[str, str]] | None = None
        connected_marker_ids: set[str] | None = None
        wired_knx_pairs: set[tuple[str, str]] | None = None
        connected_knx_ids: set[str] | None = None
        if has_active_plan:
            wired_marker_pairs = self._wired_source_webio_pairs("2")
            connected_marker_ids = self._connected_source_ids("2")
            if wired_marker_pairs is None:
                self._lp_missing_recheck_pending = True
            # KNX cluster plans mirror marker cluster plans structurally, so they share the
            # same has_active_plan gate rather than a dedicated extension-scoped flag.
            wired_knx_pairs = self._wired_source_webio_pairs("11")
            connected_knx_ids = self._connected_source_ids("11")
            if wired_knx_pairs is None:
                self._lp_missing_recheck_pending = True
        wired_io_pairs: set[tuple[str, str]] | None = None
        connected_io_ids: set[str] | None = None
        if managed_io_exts:
            wired_io_pairs = self._wired_source_webio_pairs("1")
            connected_io_ids = self._connected_source_ids("1")
            if wired_io_pairs is None:
                self._lp_missing_recheck_pending = True
        return (
            wired_marker_pairs,
            wired_io_pairs,
            wired_knx_pairs,
            connected_marker_ids,
            connected_io_ids,
            connected_knx_ids,
        )

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
            if fub_id not in relevant_fub_ids or not self._is_managed_function_plan(fub_id):
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
            if not self._is_managed_function_plan(fub_id):
                continue
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
        wired_pairs: tuple[set[tuple[str, str]] | None, set[tuple[str, str]] | None, set[tuple[str, str]] | None],
        io_meta: dict | None,
        managed_io_exts: set[str],
    ) -> dict[str, Any] | None:
        """Missing-wiring item for one audit key, or None when the pair is wired or unchecked.

        wired_pairs = (marker pairs, IO pairs, KNX pairs); a set of None means that check is
        disabled or not yet loadable this run. Marker keys ("M<id>") are checked against the
        marker set, KNX keys ("K<id>") against the KNX set; IO keys only when their extension
        is opted into IO cluster management.
        """
        wired_marker_pairs, wired_io_pairs, wired_knx_pairs = wired_pairs
        web_id = str(best_match.get("webIoId"))
        if key.startswith("M"):
            if wired_marker_pairs is not None and (key[1:], web_id) not in wired_marker_pairs:
                return {"name": ha_name, "marker_id": int(key[1:])}
            return None
        if key.startswith("K"):
            if wired_knx_pairs is not None and (key[1:], web_id) not in wired_knx_pairs:
                return {"name": ha_name, "knx_id": int(key[1:])}
            return None
        if io_meta is None or io_meta["ext_name"] not in managed_io_exts or wired_io_pairs is None:
            return None
        if (str(io_meta["id"]), web_id) not in wired_io_pairs:
            return {"name": ha_name, "ext_name": io_meta["ext_name"], "identifier": io_meta["identifier"]}
        return None

    def _range_cluster_plan_name_for_item(self, item: dict, prefix: str, cluster_size: int) -> str | None:
        """Cluster-plan name for a missing-wiring item ({marker_id|knx_id: ...}), or None for an IO item."""
        for cat in (c for c in SOURCE_CATEGORIES.values() if c.range_clustered):
            if (id_key := f"{cat.key.value}_id") in item:
                return self._cluster_plan_name(item[id_key], prefix, cluster_size, cat.label)
        return None

    def _range_cluster_plan_name_for_key(self, key: str, prefix: str, cluster_size: int) -> str | None:
        """Cluster-plan name for an audit-map key ("M<id>"/"K<id>"), or None for an IO key."""
        for cat in (c for c in SOURCE_CATEGORIES.values() if c.range_clustered):
            if key.startswith(cat.audit_key_prefix):
                return self._cluster_plan_name(int(key[len(cat.audit_key_prefix) :]), prefix, cluster_size, cat.label)
        return None

    def _function_plan_missing_detail(
        self, missing_items: list[dict], ha_map: dict, io_meta_by_key: dict
    ) -> dict[str, Any]:
        """Split missing-wiring items into per-cluster marker/KNX gaps and per-extension IO gaps.

        The repair dialog uses this to word whole-cluster gaps (cluster plan never created,
        or deleted directly in Comexio — nothing placed/wired yet) differently from
        individual missing connections. Mirrors ios_by_ext (ext_name -> [gap, total]) with
        markers_by_plan (cluster plan name -> [gap, total]), bucketed the same way the cluster
        plans themselves are named (_cluster_plan_name). Marker and KNX cluster plans share this
        one dict — their plan-name strings never collide ("... - Marker [...]" vs "... - KNX
        [...]"), so no separate knx_by_plan bucket is needed.
        """
        prefix = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX)
        cluster_size = int(
            self.config_entry.options.get(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
            )
        )
        gaps_by_plan: dict[str, int] = {}
        for item in missing_items:
            if (plan_name := self._range_cluster_plan_name_for_item(item, prefix, cluster_size)) is not None:
                gaps_by_plan[plan_name] = gaps_by_plan.get(plan_name, 0) + 1
        totals_by_plan = dict.fromkeys(gaps_by_plan, 0)
        for key in ha_map:
            plan_name = self._range_cluster_plan_name_for_key(key, prefix, cluster_size)
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

    def _trigger_ids_by_ref(self, data: dict[str, Any]) -> dict[int, list[int]]:
        """Trigger source ids ([TRIG]/[TP]) grouped by plan-element ref_type, one bucket per
        active, trigger-capable source category (marker ref_type=2, KNX ref_type=11 — blind guess).

        Marker and KNX ids share one numeric space, so the audit has to run per category
        rather than over a flat merged list — this produces the per-ref_type input for it.

        Deliberately skips a category the user has opted out of (not in active_webio_classes)
        instead of producing an empty bucket for it: unlike the Web-IO wiring/dangling audit,
        which reads structure straight from the plan snapshot and stays correct regardless of
        opt-in, _audit_trigger_pairs' orphan detection depends on this method's *positive*
        list of still-legitimate trigger sources — every existing plan element not in that
        list is treated as orphaned and gets deleted by the next full sync. Since data[cat.
        data_key] is deliberately empty for an opted-out category (see _async_update_data),
        an unfiltered bucket for it would misread "we didn't look" as "none of these are
        trigger sources anymore" and delete every real trigger pair the moment the category
        is toggled off. Skipping the bucket entirely leaves that category's trigger wiring
        untouched while inactive, consistent with async_check_ignored_sources' same guard.
        """
        active = self.active_webio_classes
        return {
            int(cat.fub_module_type): [
                int(item["id"]) for item in data[cat.data_key] if item.get("kind") == MarkerKind.TRIGGER
            ]
            for cat in trigger_pair_categories()
            if cat.key in active
        }

    def _audit_all_trigger_pairs(
        self, trigger_ids_by_ref: dict[int, list[int]]
    ) -> tuple[dict[int, list[int]], dict[int, list[int]]] | None:
        """Run _audit_trigger_pairs once per source category, collecting the non-empty
        missing/orphan id lists keyed by ref_type.

        Returns None as soon as one per-category audit signals "trigger plan exists but is
        not in the bulk snapshot yet" — the caller then defers the whole trigger check to
        the next cycle instead of acting on a partial picture.
        """
        missing_by_ref: dict[int, list[int]] = {}
        orphan_by_ref: dict[int, list[int]] = {}
        for ref_type, ids in trigger_ids_by_ref.items():
            result = self._audit_trigger_pairs(ids, ref_type)
            if result is None:
                return None
            missing_ids, orphan_ids = result
            if missing_ids:
                missing_by_ref[ref_type] = missing_ids
            if orphan_ids:
                orphan_by_ref[ref_type] = orphan_ids
        return missing_by_ref, orphan_by_ref

    def _audit_trigger_pairs(self, trigger_source_ids: list[int], ref_type: int) -> tuple[list[int], list[int]] | None:
        """Compare trigger sources ([TRIG]/[TP]) of one category (ref_type) against the
        dedicated trigger plan's wiring.

        Returns (missing_ids, orphan_ids): missing = trigger source without a *complete*
        Source+Flanke round trip in the plan (plan may not even exist yet, or pair creation
        may have failed partway through, leaving a bare source element); orphan = any source
        element of this ref_type present in the plan — complete pair or not — whose source is
        no longer kind==TRIGGER (suffix removed, or the source was removed while its pair
        creation was still incomplete). Cleanup finds and removes whatever Flanke wiring exists
        for an orphaned source regardless of completeness, so an incomplete leftover pair must
        be reported here too, not just fully-wired ones — otherwise it never gets swept up. Uses
        the cached plan snapshot (self.function_plan_plans), consistent with the other
        Function Plan checks above — not a live reload on every audit tick. The mapped
        fub_id's live $Fubs name is checked (same freshness guard as
        _resolve_single_cluster_plan) so a renamed/repurposed/reused plan id is treated as
        "no trigger plan yet" instead of being audited as if it still were the trigger plan —
        otherwise a coincidentally-complete pair in that unrelated plan would make a trigger
        source look wired when resolve_trigger_plan() would actually create a fresh plan.

        Called once per source category (marker ref_type=2, KNX ref_type=11 — blind guess for
        KNX, unverified against real KNX plan data) — marker and KNX ids share the same numeric
        space, so a single merged call could not tell them apart. Mirrors the per-category
        _dangling_source_ids pattern above.

        KNX (ref_type=11) is a read-only label here: the *wired* plan element is always the
        source's write-path bridge Marker (type=2), never the K element itself — Comexio
        refuses to start a plan that wires a K element's Flanke self-reset loop back into
        that K element's own input, since the input is already driven by the bridge Marker's
        wiring ("mehrfach verwendete Ausgänge", confirmed live 2026-09-21; see
        button.py's _resolve_knx_trigger_bridge_markers). trigger_source_ids/missing_ids/
        orphan_ids stay in K-id space for the caller (display, audit-key prefixing); only the
        internal wired/existing lookups are translated to the bridge Marker's id.

        Returns None while the trigger plan's fub_id is a confirmed existing plan that simply
        has not landed in the bulk snapshot yet (same partial-snapshot startup/reload window
        _relevant_plans_loaded guards against for the generic Web-IO wiring check) — otherwise
        an unloaded-but-real plan would read as "zero wired pairs" and misreport every trigger
        source as missing until the next poll.
        """
        raw_map = self.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        plan_map = {k: int(v) for k, v in raw_map.items()} if isinstance(raw_map, dict) else {}
        fub_id = plan_map.get(FUNCTION_PLAN_TRIGGER_PLAN_NAME)
        if fub_id is None or self.api.fub_data.get(str(fub_id), {}).get("Name") != FUNCTION_PLAN_TRIGGER_PLAN_NAME:
            return list(trigger_source_ids), []
        if fub_id not in self.function_plan_plans:
            return None

        marker_ref_type = int(category_by_fub_module_type("2").fub_module_type)
        knx_ref_type = int(category_by_fub_module_type("11").fub_module_type)
        bridge_marker_by_k_id = self._knx_bridge_marker_by_k_id()
        if bridge_marker_by_k_id is None:
            return None

        plan_data = self.function_plan_plans[fub_id]
        existing_by_ref, _ = self.api._function_plan_existing_refs(plan_data)

        if ref_type == knx_ref_type:
            marker_by_k_id = {k_id: bridge_marker_by_k_id.get(str(k_id)) for k_id in trigger_source_ids}
            wired_marker_ids = self.api._function_plan_trigger_wired_source_ids(plan_data, marker_ref_type)
            missing_ids = [k_id for k_id, m in marker_by_k_id.items() if m is None or int(m) not in wired_marker_ids]

            all_wired_marker_ids = {ref_id for rt, ref_id in existing_by_ref if rt == marker_ref_type}
            reverse_bridge = {int(m): k_id for k_id, m in bridge_marker_by_k_id.items() if m is not None}
            trigger_id_set = set(trigger_source_ids)
            orphan_ids = [
                int(reverse_bridge[mid])
                for mid in all_wired_marker_ids
                if mid in reverse_bridge and int(reverse_bridge[mid]) not in trigger_id_set
            ]
            return missing_ids, orphan_ids

        # Plain-marker category: a KNX trigger's bridge Marker also shows up here as a bare
        # ref_type=2 plan element once wired above — its lifecycle belongs to the KNX branch
        # exclusively (kept even if KNX import is currently opted out, matching
        # _trigger_ids_by_ref's "leave inactive category's wiring untouched" rule), so it must
        # never be swept up as a plain-marker orphan.
        bridge_marker_ids = {int(m) for m in bridge_marker_by_k_id.values() if m is not None}
        all_marker_ids = {ref_id for rt, ref_id in existing_by_ref if rt == ref_type} - bridge_marker_ids
        wired_marker_ids = self.api._function_plan_trigger_wired_source_ids(plan_data, ref_type)

        missing_ids = [mid for mid in trigger_source_ids if mid not in wired_marker_ids]
        trigger_id_set = set(trigger_source_ids)
        orphan_ids = [mid for mid in all_marker_ids if mid not in trigger_id_set]
        return missing_ids, orphan_ids

    async def async_fresh_trigger_audit(self) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
        """Fetch Comexio's config directly and re-run the trigger-pair audit against it.

        async_request_refresh() is *not* an option here: _async_update_data() returns the
        existing (possibly stale) self.data as-is whenever self.in_sync is True, which covers
        the whole duration of a manual sync — the exact window this needs a fresh picture for.
        So this bypasses the coordinator update machinery entirely and fetches raw config
        straight from the API. live_states/referenced_markers are omitted deliberately: marker
        kind depends only on its title, and a trigger marker always has a real name (a suffix
        needs something to be suffixed to), so it's never affected by the referenced-marker
        cold-start fallback that only concerns unnamed markers.

        get_raw_config() returns {} on an HTTP failure rather than raising — indistinguishable
        from a genuinely empty config by shape alone. Proceeding anyway would derive empty
        trigger id lists and make _audit_trigger_pairs() read every existing source element
        in the trigger plan as orphaned, deleting valid self-reset pairs over what was really
        just a transient fetch failure. So an empty result skips the audit entirely
        ({}, {} — a safe no-op); the next successful poll or sync retries it.

        _audit_all_trigger_pairs() can itself return None (trigger plan exists but its data
        hasn't landed in the bulk snapshot yet) — no longer treated as an unavoidable dead end:
        _ensure_relevant_plans_cached() below live-fetches the trigger plan (and every other
        plan CONF_FUNCTION_PLAN_PLAN_MAP names) first, since the passive backup-cycle snapshot
        this check would otherwise rely on can still be completely empty moments after an HA
        restart (see that method's docstring). The None branch below is kept as a safety net for
        whatever it still can't resolve (e.g. the live fetch itself failing) rather than treated
        as unreachable.

        force=True on that call since 2026-09-21: a Full Sync's trigger step
        (button.py's _wire_trigger_pairs, called with refresh_audit=True) now runs right after
        _wire_knx_full wrote brand-new bridge Markers into the very KNX cluster plan(s) this
        audit's _knx_bridge_marker_by_k_id() lookup reads back (via _resolve_knx_trigger_bridge_markers,
        for a K-object that just got its [TRIG]/[TP] suffix wired) — the same
        write-then-read-back-in-the-same-pass shape _ensure_relevant_plans_cached's own
        docstring already documents for async_fresh_knx_bridge_loopback_audit. Before the KNX
        3-leg consolidation, this cache happened to already be fresh by the time this ran, as an
        incidental side effect of that OLD design's own (now-removed) end-of-run
        async_fresh_knx_bridge_loopback_audit() re-fetch. Without force=True here now, a
        just-bridged K-object's trigger pair fails with "no write-path bridge marker yet,
        cannot wire trigger pair" even though the bridge itself was wired correctly moments
        earlier in the same sync (live-reproduced 2026-09-21, K2).
        """
        raw_config = await self.api.get_raw_config()
        if not raw_config:
            _LOGGER.warning(
                "[%s] Trigger re-audit: direct config fetch failed — skipping rather than risk "
                "misreading it as zero trigger markers and deleting valid self-reset pairs",
                self.server_id,
            )
            return {}, {}
        # parse_config() itself doesn't know about import_* opt-in flags and returns every
        # category unfiltered — but that's fine here: _trigger_ids_by_ref() now does its own
        # active_webio_classes gating (an earlier version of this method instead blanked an
        # opted-out category's list to [] before handing it to _trigger_ids_by_ref, which
        # backfired — see that method's docstring for why an empty-but-present bucket reads
        # as "every existing trigger pair just got orphaned" rather than "category inactive,
        # don't touch its wiring").
        parsed = self.api.parse_config(raw_config)
        await self._ensure_relevant_plans_cached(self._function_plan_check_fub_ids(), force=True)
        trigger_audit_result = self._audit_all_trigger_pairs(self._trigger_ids_by_ref(parsed))
        if trigger_audit_result is None:
            _LOGGER.warning(
                "[%s] Trigger re-audit: trigger plan not yet in the bulk snapshot — skipping "
                "this sync's trigger-pair check, will retry on the next poll",
                self.server_id,
            )
            return {}, {}
        return trigger_audit_result

    async def async_fresh_knx_bridge_audit(self) -> list[dict[str, Any]]:
        """Fetch Comexio's config directly and re-run the knx_bridge_missing audit against it.

        Same staleness problem as async_fresh_trigger_audit (see its docstring for why
        async_request_refresh() cannot be used mid-sync): last_audit_results is the snapshot
        from the *previous* poll, which can still show 0 missing bridges even though every
        K-element genuinely lacks one now — e.g. import_knx was only just switched on, or the
        KNX cluster plan itself only came into existence earlier in *this same* sync run (a
        Full Sync's Initial-Setup pass creates the Web-IO class, wires K -> WebIO pairs, and
        wires Marker -> K bridges all in one press; without this refresh the last leg would
        silently see "no bridges missing" and the user would have to run a second, separate
        "Brücken-Merker anlegen" action afterwards to close the gap Full Sync already knew
        about).

        get_raw_config() returns {} on an HTTP failure rather than raising — an empty result
        here skips the audit entirely (safe no-op) instead of misreading a transient fetch
        failure as "no KNX objects, nothing to bridge".

        _knx_bridge_marker_by_k_id() still reads the cached bulk plan snapshot
        (self.function_plan_plans) for already-wired bridges, exactly like the live audit — a
        plan not yet in that snapshot defers the whole check (returns []) rather than risk
        misreading "not loaded yet" as "nothing wired, everything missing", mirroring
        async_fresh_trigger_audit's own None-handling. _ensure_relevant_plans_cached() below
        live-fetches every relevant, still-existing plan that isn't cached yet BEFORE that read,
        for the same reason async_fresh_trigger_audit now calls it: right after an HA restart
        the passive backup-cycle snapshot this otherwise depends on can still be completely
        empty when Initial Setup is pressed, deferring even for plans that already existed long
        before this sync (most commonly the trigger plan) — not just the KNX cluster plan this
        same sync may have just created.

        Gated on the same has_active_plan check the live knx_bridge_missing_items block uses
        (see _async_update_data) — _knx_bridge_marker_by_k_id() alone is not a substitute: with
        no active plan, _function_plan_check_fub_ids() returns an empty relevant-fub_id set,
        which _relevant_plans_loaded() then treats as vacuously satisfied (an empty set is a
        subset of anything) as long as *some* unrelated plan is cached in function_plan_plans —
        so it would return {} (not None) and every KNX object would be misreported as
        bridge-missing, potentially auto-creating a KNX cluster plan the user never opted into
        via Managed Function Plan.
        """
        if WebioClass.KNX not in self.active_webio_classes:
            return []
        if not self._has_active_function_plan():
            return []
        raw_config = await self.api.get_raw_config()
        if not raw_config:
            _LOGGER.warning(
                "[%s] KNX bridge re-audit: direct config fetch failed — skipping rather than "
                "risk misreading it as zero KNX objects",
                self.server_id,
            )
            return []
        parsed = self.api.parse_config(raw_config)
        await self._ensure_relevant_plans_cached(self._function_plan_check_fub_ids())
        knx_bridge_marker_by_k_id = self._knx_bridge_marker_by_k_id()
        if knx_bridge_marker_by_k_id is None:
            _LOGGER.warning(
                "[%s] KNX bridge re-audit: relevant Function Plan(s) not yet in the bulk "
                "snapshot — skipping this sync's bridge check, will retry on the next poll",
                self.server_id,
            )
            return []
        ignored_ids = self.ignored_ids_for(WebioClass.KNX)
        missing_items: list[dict[str, Any]] = []
        for k in parsed.get("knx", []):
            k_id = str(k["id"])
            if int(k_id) in ignored_ids or k_id in knx_bridge_marker_by_k_id:
                continue
            missing_items.append(
                {
                    "name": k["name"],
                    "ref_id": k_id,
                    "title": k["title"],
                    "type_raw": k["type_raw"],
                    "webio_class": WEBIO_CLASS_KNX,
                }
            )
        return missing_items

    async def async_fresh_knx_bridge_loopback_audit(self) -> list[dict[str, Any]]:
        """Fetch Comexio's config directly and re-run the knx_bridge_loopback_missing audit.

        Phase 7 counterpart of async_fresh_knx_bridge_audit — same staleness problem and same
        fix: a bridge Marker/K-Element pair created earlier in *this same* sync run (e.g. by
        _wire_knx_full/_wire_knx_cluster in button.py) is invisible to last_audit_results (a
        snapshot from the *previous* poll), so without this refresh the loopback fan-out would
        never get wired in the same Full Sync that just created the bridge — the user would
        need a second, separate run to close a gap this sync already knows about.

        Mirrors async_fresh_knx_bridge_audit's guards: bails out (returns []) rather than risk
        misreading a transient failure/not-yet-cached plan as "nothing to fan out", including
        the same has_active_plan gate for the same _function_plan_check_fub_ids() reason.
        """
        if WebioClass.KNX not in self.active_webio_classes:
            return []
        if not self._has_active_function_plan():
            return []
        if not self._api_credentials_configured():
            # Same reasoning as the live-poll counterpart in _async_update_data: the repair for
            # this gap needs the optional API username/password and aborts deterministically
            # without them — returning [] here (instead of a permanently-unfixable item) avoids
            # a repair flag that can never be resolved until credentials are configured.
            return []
        raw_config = await self.api.get_raw_config()
        if not raw_config:
            _LOGGER.warning(
                "[%s] KNX loopback re-audit: direct config fetch failed — skipping rather than "
                "risk misreading it as zero KNX objects",
                self.server_id,
            )
            return []
        parsed = self.api.parse_config(raw_config)
        # force=True: this audit runs right after (or, for a just-created bridge, from within)
        # _wire_knx_full/_wire_knx_cluster wrote new sinks into these same plans earlier in
        # this sync pass — by now they are virtually guaranteed to already be cache entries
        # (just stale ones), so the default
        # "only fetch what's missing" behavior would silently skip the live refetch this specific
        # audit needs. See _ensure_relevant_plans_cached's docstring.
        if not await self._ensure_relevant_plans_cached(self._function_plan_check_fub_ids(), force=True):
            _LOGGER.warning(
                "[%s] KNX loopback re-audit: forced refetch of a relevant plan failed — skipping "
                "this sync's loopback check rather than risk auditing stale (pre-write) plan data",
                self.server_id,
            )
            return []
        knx_bridge_marker_by_k_id = self._knx_bridge_marker_by_k_id()
        wired_knx_webio_pairs = self._wired_source_webio_pairs("11")
        if knx_bridge_marker_by_k_id is None or wired_knx_webio_pairs is None:
            _LOGGER.warning(
                "[%s] KNX loopback re-audit: relevant Function Plan(s) not yet in the bulk "
                "snapshot — skipping this sync's loopback check, will retry on the next poll",
                self.server_id,
            )
            return []
        sink_counts: dict[str, int] = {}
        for k_ref_id, _webio_ref_id in wired_knx_webio_pairs:
            sink_counts[k_ref_id] = sink_counts.get(k_ref_id, 0) + 1
        knx_by_id = {str(k["id"]): k for k in parsed.get("knx", [])}
        missing_items: list[dict[str, Any]] = []
        for k_id, marker_id in knx_bridge_marker_by_k_id.items():
            # Not in sink_counts at all -> the read-path wire itself is missing, a different,
            # already-audited gap — nothing to fan the loopback onto yet.
            if k_id not in sink_counts or sink_counts[k_id] >= 2:
                continue
            k = knx_by_id.get(k_id)
            missing_items.append(self._knx_bridge_loopback_missing_item(k, k_id, marker_id))
        return missing_items

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
            # Reuses the same registry-driven lookup as _function_plan_missing_detail
            # instead of a parallel "marker_id"/"knx_id" in item chain.
            plan_name = self._range_cluster_plan_name_for_item(
                item, prefix, cluster_size
            ) or self._io_cluster_plan_name_for_ext(item["ext_name"], prefix)
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
