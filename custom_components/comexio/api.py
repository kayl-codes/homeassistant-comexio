# Version: 0.7.5
import asyncio
import base64
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
import io
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from multidict import MultiDict

# Mandatory DOMAIN import for Audit logic
from .const import (
    COMEXIO_HTTP_TIMEOUT_SEC,
    COMEXIO_PROGRESS_LOG_INTERVAL_SEC,
    CONF_SCHEMA_IO,
    CONF_SCHEMA_KNX,
    CONF_SCHEMA_MARKER,
    DEFAULT_SCHEMA_IO,
    DEFAULT_SCHEMA_KNX,
    DEFAULT_SCHEMA_MARKER,
    FLANKE_PORT_IN,
    FLANKE_PORT_OUT_RISING,
    FUB_BASE_REF_ID_FLANKE,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK,
    FUNCTION_PLAN_LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_X_WEBIO,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    FUNCTION_PLAN_PAIR_RELOAD_INITIAL_DELAY,
    FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS,
    FUNCTION_PLAN_TRIGGER_LAYOUT_X_FLANKE,
    FUNCTION_PLAN_TRIGGER_LAYOUT_X_MARKER,
    FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP,
    KNOWN_DOMAINS,
    KNX_DPT3_COMPOSITE_DOMAIN,
    KNX_DPT_ANALOG_RANGES,
    KNX_DPT_DEVICE_CLASS,
    KNX_DPT_DIGITAL_AMBIGUOUS,
    KNX_DPT_DIGITAL_DEVICE_CLASS,
    MARKER_KNX_BRIDGE_BLOCK_SIZE,
    MARKER_KNX_BRIDGE_SUFFIX_RE,
    MARKER_READ_ONLY_SUFFIX,
    MARKER_TRIGGER_SUFFIXES,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_CLASSES,
    WEBIO_DEVICE_NAME_KNX_LOOPBACK,
    WEBIO_INT16_DANGER_ZONE,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    MarkerKind,
    category_by_fub_module_type,
    io_column_rows,
    io_sort_key,
    knx_loopback_command_name,
    source_category,
    webio_class_label,
    webio_class_name,
)


class SafeDict(dict):
    """Safe dictionary for string formatting that doesn't crash on missing keys."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


LOCAL_HOSTNAME_RE = re.compile(r"^(?:localhost|[a-zA-Z0-9_-]+\.local|[a-zA-Z0-9_-]+\.lan|[a-zA-Z0-9_-]+\.home)\.?$")

# Function-plan element reference types needing special handling in function_plan_rebuild_plan_from_snapshot.
FUNCTION_PLAN_COMMENT_TYPE = 14
FUNCTION_PLAN_CONSTANT_TYPE = 16

# Module-level compiled patterns for get_raw_config (used on every coordinator refresh).
# $ioTypes = legacy (pre-v11); $IOTypesBinary = v11+ replacement with identical structure.
_IO_TYPES_DECL_RE = re.compile(r"var\s+\$ioTypes\s*=\s*")
_IO_BINARY_TYPES_DECL_RE = re.compile(r"var\s+\$IOTypesBinary\s*=\s*")
_IO_INPUT_TYPES_DECL_RE = re.compile(r"var\s+\$IOInputTypes\s*=\s*")
_SCRIPT_BLOCK_RE = re.compile(r"<script[^>]*>(.*?)</script[^>]{0,32}>", re.DOTALL | re.IGNORECASE)
# Comexio's own firmware/frontend version (e.g. "11.0.2"), from static asset paths
# (cache-busting), e.g. src="/11.0.2/js/cmb_admin.js" — cmb_admin.js is the generic
# admin-wide script, cmb_function_function_module.js is specific to the page we fetch;
# matching either is redundancy against a future filename change.
_COMEXIO_VERSION_RE = re.compile(
    r'src="/(\d+\.\d+\.\d+)/(?:js/cmb_admin\.js|'
    r'module/admin/function_function_module/js/cmb_function_function_module\.js)"'
)
_VAR_DECL_RE = re.compile(r"var\s+\$(\w+)\s*=\s*", re.DOTALL)
_EMPTY_JS_ARRAY_RE = re.compile(r"\[\s*\]")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_CONTENT_TYPE_JSON = "Content-Type: application/json"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

# get_webio_command_range: the min/max <input> tags on a Web-IO command's edit form, e.g.
# <input type="text" id="max_cmd_io_0" name="max_cmd_io_0" value="4294967296" .../>.
# Attribute order isn't guaranteed, so this captures the whole tag by its id and pulls the
# value out separately rather than assuming id comes before value.
_WEBIO_CMD_INPUT_RE = re.compile(r'<input\b[^>]*\bid="(min|max)_cmd_io_0"[^>]*>', re.IGNORECASE)
_WEBIO_CMD_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')


def _js_timestamp() -> str:
    """Return a millisecond-precision UTC timestamp in JS Date.toISOString() format."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)[:-3] + "Z"


def _is_local_address(host: str) -> bool:
    """Return True if host looks like a local IP or local hostname."""
    if not host:
        return False

    host = host.strip()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return False
        host = host[1:closing]
    elif ":" in host:
        host, _, _ = host.partition(":")

    with suppress(ValueError):
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    return bool(LOCAL_HOSTNAME_RE.match(host))


def _normalize_js_like_object(obj_str: str) -> str:
    """Remove trailing commas before closing braces/brackets to make JS objects JSON-compatible."""
    return _TRAILING_COMMA_RE.sub(r"\1", obj_str)


def _extract_js_object_literal(script_text: str, start_index: int) -> tuple[str | None, int]:
    """Extract a JS object literal starting at start_index (pointing at '{')."""
    if start_index >= len(script_text) or script_text[start_index] != "{":
        return None, start_index

    depth = 0
    i = start_index
    in_string: str | None = None
    escape = False

    while i < len(script_text):
        ch = script_text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script_text[start_index : i + 1], i + 1
        i += 1
    return None, start_index


_LOGGER = logging.getLogger(__name__)

# get_live_states' dashboard/refresh request/response key prefix for KNX objects ($FubModules
# type "11") — distinguishes them from markers, which share the same plain numeric id space.
_KNX_LIVE_KEY_PREFIX = "knxIo_11_"


def _is_extension_offline(identifier: str) -> bool:
    """Return True when identifier indicates an offline extension module.

    Online extensions report a serial number in 'XXXX-XXXX-XXXX' format;
    offline ones carry only a short model code without dashes (e.g. '5010').
    An empty string (missing field) is also treated as offline.
    """
    return "-" not in identifier


async def _tick_comexio_request_progress(method: str, path: str) -> None:
    """Log a 'still waiting' line every COMEXIO_PROGRESS_LOG_INTERVAL_SEC until cancelled.

    Cancelled by _on_comexio_request_end/_exception as soon as the request finishes, so a
    normal, fast call never logs anything -- only a request still open after the first
    interval does. This is what makes a long, otherwise-silent wait (e.g. a Comexio admin
    call that gets slow after a heavy write batch) visible instead of looking hung.
    """
    elapsed = 0
    while True:
        await asyncio.sleep(COMEXIO_PROGRESS_LOG_INTERVAL_SEC)
        elapsed += COMEXIO_PROGRESS_LOG_INTERVAL_SEC
        _LOGGER.info("Warte weiterhin auf Antwort von Comexio: %s %s (%ds)", method, path, elapsed)


# aiohttp TraceConfig requires an async callback signature regardless of body (python:S7503 false positive).
async def _on_comexio_request_start(_session: aiohttp.ClientSession, trace_ctx: Any, params: Any) -> None:  # NOSONAR
    trace_ctx.progress_task = asyncio.ensure_future(_tick_comexio_request_progress(params.method, params.url.path))


async def _cancel_comexio_progress_task(trace_ctx: Any) -> None:
    task = trace_ctx.progress_task
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _on_comexio_request_end(_session: aiohttp.ClientSession, trace_ctx: Any, _params: Any) -> None:
    await _cancel_comexio_progress_task(trace_ctx)


async def _on_comexio_request_exception(_session: aiohttp.ClientSession, trace_ctx: Any, _params: Any) -> None:
    await _cancel_comexio_progress_task(trace_ctx)


def _build_comexio_trace_config() -> aiohttp.TraceConfig:
    """TraceConfig that logs a periodic 'still waiting' line for any slow Comexio HTTP call.

    A single instance is shared by the whole session; aiohttp gives each individual request
    its own trace_ctx, so concurrent requests never interfere with each other's timers.
    """
    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_start.append(_on_comexio_request_start)
    trace_config.on_request_end.append(_on_comexio_request_end)
    trace_config.on_request_exception.append(_on_comexio_request_exception)
    return trace_config


def _balanced_rows_per_col(n_items: int, max_rows_per_col: int) -> int:
    """Rows per column that splits n_items evenly across the minimum number of columns.

    max_rows_per_col is how many rows physically fit in one column (canvas-height driven).
    Greedily filling each column to that capacity before spilling into the next (plain
    divmod(n, max_rows_per_col)) produces an uneven split for exact multiples — e.g. a
    50-item block in a 26-row-capacity column becomes 26/24 instead of the expected 25/25
    (user-reported live, 2026-09-21, in a fresh "HA - KNX [1-50]" cluster plan). This first
    picks the minimum column count that still fits (ceil(n_items / max_rows_per_col)), then
    divides n_items evenly across exactly that many columns.
    """
    if n_items <= 0:
        return max(1, max_rows_per_col)
    n_cols = -(-n_items // max_rows_per_col)
    return -(-n_items // n_cols)


def _blank_marker_id_candidates(items: Any, min_id: int, max_id: int | None) -> set[int]:
    """Untitled marker ids in [min_id, max_id) among $FubModules["2"]'s items.

    Split out of ComexioAPI._free_marker_ids to keep its own cognitive complexity within
    SonarQube S3776's limit — see that method's docstring for the reuse semantics.
    """
    candidates: set[int] = set()
    for m in items:
        if not isinstance(m, dict) or not isinstance(m.get("Id"), int) or m.get("Name"):
            continue
        m_id = m["Id"]
        if m_id >= min_id and (max_id is None or m_id < max_id):
            candidates.add(m_id)
    return candidates


def _placed_marker_ids(all_plans: dict[int, dict], ref_type: int) -> set[int]:
    """Marker ids referenced as a plan element of the given reference type, across all_plans.

    Split out of ComexioAPI._free_marker_ids to keep its own cognitive complexity within
    SonarQube S3776's limit — see that method's docstring for the reuse semantics.
    """
    placed_ids: set[int] = set()
    for plan_data in all_plans.values():
        for elem in (plan_data.get("elements") or {}).values():
            if not isinstance(elem, dict):
                continue
            ref = elem.get("reference") or {}
            if str(ref.get("type")) != str(ref_type):
                continue
            with suppress(TypeError, ValueError):
                placed_ids.add(int(ref.get("ref_id")))
    return placed_ids


def _plan_payload_has_elements(data: Any) -> bool:
    """True if a loadelements/loadallelements plan payload carries a real elements collection.

    An error object or truncated payload (no "elements", or "elements": null) must not pass as
    a loaded, empty plan wherever placement gates an irreversible action (marker_delete force).
    """
    return isinstance(data, dict) and isinstance(data.get("elements"), (dict, list))


def _plan_marker_refs_strict(plan_data: Any) -> set[int] | None:
    """Marker ids referenced in one plan, or None if any element/reference is unreadable."""
    elements = plan_data.get("elements") if isinstance(plan_data, dict) else None
    if not isinstance(elements, dict):
        return None
    refs: set[int] = set()
    for elem in elements.values():
        ref = elem.get("reference") if isinstance(elem, dict) else "malformed"
        if ref is None:
            continue
        if not isinstance(ref, dict):
            return None
        if str(ref.get("type")).strip() != "2":
            continue
        ref_id = ComexioAPI._parse_plausible_marker_id(ref.get("ref_id"))
        if ref_id is None:
            return None
        refs.add(ref_id)
    return refs


def _placed_marker_ids_strict(all_plans: dict[int, dict]) -> set[int] | None:
    """Fail-closed variant of _placed_marker_ids for marker_delete's force gate.

    _placed_marker_ids silently skips unreadable references — fine for picking free ids, but
    here a skipped reference would make a placed marker look unplaced and deletable. Any
    unreadable plan, element or marker reference returns None (placement unknown) instead.
    """
    placed: set[int] = set()
    for plan_data in all_plans.values():
        refs = _plan_marker_refs_strict(plan_data)
        if refs is None:
            return None
        placed |= refs
    return placed


_MARKER_CONFIG_UNREADABLE = "Comexio marker config could not be fetched or parsed — every id refused (see log)."
_FORCE_IGNORED = "force ignored: not every function plan could be loaded and verified (see log)."


def _is_api_created_marker(record: dict[str, Any]) -> bool:
    """True if the marker record carries CategoryId==1 (created via the admin API, not Studio).

    Python's loose equality makes True == 1 and 1.0 == 1, so a plain "== 1" check would let a
    malformed CategoryId (e.g. a scraped JSON boolean true) alias onto "1" — only a genuine
    int, not a bool, is accepted; anything else (including 1.0 or the string "1") is not.
    """
    value = record.get("CategoryId", 0)
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _marker_has_title(record: dict[str, Any]) -> bool:
    """True unless the marker's Name is missing/None or a blank string.

    A non-string Name (malformed record) counts as titled — the force path of marker_delete
    only removes markers it can positively identify as untitled.
    """
    name = record.get("Name")
    if name is None:
        return False
    return not isinstance(name, str) or bool(name.strip())


def _classify_marker_delete_ids(
    marker_ids: list[int], records: dict[int, dict[str, Any]], placed_ids: set[int] | None
) -> tuple[list[int], list[int]]:
    """Split marker_ids into (deletable, protected) for the marker_delete service.

    - Absent from records (already deleted / never existed): deletable — delete_marker then
      reports it as the harmless "already absent" case.
    - CategoryId==1 (created by this integration via the API): deletable.
    - Anything else (CategoryId==0 = factory or Studio-created): protected, unless force
      is active (placed_ids is not None) AND the marker has no title AND it is not placed in
      any function plan. placed_ids=None means force is off — or the plans couldn't all be
      loaded, in which case placement is unknown and nothing may pass on that basis.
    """
    deletable: list[int] = []
    protected: list[int] = []
    for mid in marker_ids:
        record = records.get(mid)
        if (
            record is None
            or _is_api_created_marker(record)
            or (placed_ids is not None and not _marker_has_title(record) and mid not in placed_ids)
        ):
            deletable.append(mid)
        else:
            protected.append(mid)
    return deletable, protected


def _knx_bridge_title(k_id: int, k_title: str) -> str:
    """Marker title of the KNX bridge marker for K-element k_id (matches MARKER_KNX_BRIDGE_SUFFIX_RE)."""
    return f"{k_title} [K{k_id}]"


def _is_knx_bridge_title(name: Any) -> bool:
    """True if name is a machine-given KNX bridge marker title ("... [K<id>]")."""
    return isinstance(name, str) and bool(MARKER_KNX_BRIDGE_SUFFIX_RE.search(name))


def _knx_bridge_run_end(items: Any, start: int, min_len: int = MARKER_KNX_BRIDGE_BLOCK_SIZE) -> int:
    """Exclusive end of the contiguous run of blank or bridge-titled markers beginning at start.

    Everything in that run is either a blank filler or a bridge marker — i.e. owned by the
    bridge block — so free-marker reuse may extend over the whole run instead of stopping
    after the first min_len ids (live 2026-09-25: a block that had grown to M300-M421 only
    ever reused M300-M349, every later rebuild appended fresh markers above M421). Never
    returns less than start + min_len, keeping the original single-block window as a floor.
    """
    names: dict[int, Any] = {
        m["Id"]: m.get("Name") for m in items if isinstance(m, dict) and isinstance(m.get("Id"), int)
    }
    end = start
    while end in names and (not names[end] or _is_knx_bridge_title(names[end])):
        end += 1
    return max(end, start + min_len)


def _knx_bridge_reset_candidates(items: Any, placed_ids: set[int]) -> tuple[list[tuple[int, bool]], int]:
    """Bridge-titled markers to reset (blank title) during a KNX cleanup.

    Returns ([(marker_id, binary), ...] ascending, number of bridge markers skipped because
    they are still placed in some function plan). A still-placed bridge marker is left alone:
    it is either wired into a plan the cleanup did not delete or the user reused it.
    """
    candidates: list[tuple[int, bool]] = []
    skipped = 0
    for m in items:
        if not isinstance(m, dict) or not isinstance(m.get("Id"), int) or not _is_knx_bridge_title(m.get("Name")):
            continue
        if m["Id"] in placed_ids:
            skipped += 1
            continue
        # Same default as _build_source_item: a marker without Type is digital.
        candidates.append((m["Id"], str(m.get("Type", 1)) == "1"))
    return sorted(candidates), skipped


def _stale_knx_bridge_marker_ids(items: Any, min_id: int, keep_titles: set[str]) -> set[int]:
    """Ids >= min_id of bridge-titled markers whose title is not in keep_titles.

    A bridge marker keeps its title after its K-element got a new one (e.g. a [TRIG]/[RO]
    suffix via the DPT repair flow) or after its plan was deleted — create_knx_bridge_marker
    only reuses exact title matches and blank markers, so such a leftover was never
    reclaimed (live 2026-09-25: M313-M363 still carried old "[K<id>]" titles next to their
    active replacements M364-M421). keep_titles holds the titles of the current batch, whose
    unwired remnants create_knx_bridge_marker reuses by exact title instead. Placement is
    NOT checked here — _free_marker_ids removes placed ids afterwards.
    """
    return {
        m["Id"]
        for m in items
        if isinstance(m, dict)
        and isinstance(m.get("Id"), int)
        and m["Id"] >= min_id
        and _is_knx_bridge_title(m.get("Name"))
        and m.get("Name") not in keep_titles
    }


class ComexioAPI:
    """
    Detailed interface to communicate with the Comexio API.

    Handles:
    - RSA Login for Admin tasks.
    - Dashboard Refresh for live values.
    - Full Web-IO Lifecycle management.
    - Smart Delta Sync for individual command updates.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
        api_user: str | None = None,
        api_pass: str | None = None,
    ) -> None:
        """Initialize the API class with all required credentials."""
        self.hass: HomeAssistant = hass
        self.host: str = host
        self.username: str = username
        self.password: str = password
        self.api_user: str | None = api_user
        self.api_pass: str | None = api_pass

        # This context is vital for the Audit logic to find the configured webio_name
        self.config_entry: ConfigEntry | None = None

        # Dedicated cookie management for Comexio session persistence.
        # unsafe=True is only enabled if the target host is a local address.
        # Explicit timeout: without it, a stalled Comexio response (e.g. mid-firmware-update)
        # can hang a caller indefinitely instead of surfacing as a catchable error — this
        # previously wedged the Function Plan backup cycle's lock forever, silently stopping
        # all future backups.
        self.session: aiohttp.ClientSession = async_create_clientsession(hass, **self._build_session_kwargs())

        # Second, independently logged-in session used exclusively by the Function Plan
        # preview's Stufe-2 connection-value poll (see coordinator._async_poll_connection_values).
        # Own cookie jar/connection so that poll can never queue behind (or be queued behind
        # by) the main coordinator's own requests on the shared session — see
        # [[project-logikplan-preview]] on why a stuck live-poll starved the whole coordinator.
        # Created lazily on first use, not here — most setups never open the preview.
        self._preview_session: aiohttp.ClientSession | None = None
        # Guards ensure_preview_session()'s check-then-act window: HA's interval scheduler
        # fires poll ticks without awaiting the previous one, so at the 0.5s fast-poll
        # cadence (debug box active) multiple ticks can see _preview_session is None before
        # the first login() completes — each would otherwise start its own redundant login.
        self._preview_session_lock = asyncio.Lock()
        # Set by close(); lets a login() already in flight inside the lock above notice
        # the instance was torn down while it was waiting and give up instead of leaking
        # a freshly-authenticated session past unload (see close() for the full race).
        self._closed: bool = False

        # Comexio's own firmware/frontend version (e.g. "11.0.2"), from static asset paths
        self.comexio_version: str | None = None
        # KNX DPT catalog cache (get_knx_dpt_catalog) — $KnxDevices/$KnxPoints only change on
        # an ETS edit and $KnxDpt is a firmware-static table, so refetching this admin
        # sub-page on every poll would be pointless load on a server that serializes requests
        # (see project_logikplan_api memory). Invalidated whenever comexio_version changes,
        # same pattern as LogikplanCatalogManager's own version-tracked cache.
        self._knx_dpt_catalog: dict[str, Any] | None = None
        self._knx_dpt_catalog_version: str | None = None
        # io_types: TypeId → {binary, min, max, unit}  (from $ioTypes or $IOTypesBinary)
        self.io_types: dict[str, Any] = {}
        # io_input_types: TypeId → {input: bool}  (from $IOInputTypes)
        self.io_input_types: dict[str, Any] = {}
        # Function plan + paper metadata (populated by parse_config)
        self._fub_data: dict[str, Any] = {}  # fub_id_str → {Id, Name, Paper, ...}
        self._paper_data: dict[str, Any] = {}  # paper_id_str → {Id, Name, MMX, MMY}
        self._auth_warned: bool = False
        self._login_warned: bool = False
        # Set by login() on failure so callers (setup) can tell a transient connection
        # problem (retry) apart from a genuine credential rejection (needs reauth).
        self.last_login_error: str | None = None

    def _build_session_kwargs(self) -> dict[str, Any]:
        """Session kwargs shared by the main session and the preview session (own cookie jar each)."""
        session_kwargs: dict[str, Any] = {
            "timeout": aiohttp.ClientTimeout(total=COMEXIO_HTTP_TIMEOUT_SEC),
            "trace_configs": [_build_comexio_trace_config()],
        }
        if _is_local_address(self.host):
            session_kwargs["cookie_jar"] = aiohttp.CookieJar(unsafe=True)
        return session_kwargs

    async def ensure_preview_session(self) -> aiohttp.ClientSession | None:
        """Lazily create + log in the dedicated Stufe-2 preview session, reused after that.

        Returns None if the login fails — the caller falls back to the main session for that
        one poll tick rather than blocking the preview on a retry loop; the next tick tries
        the dedicated session again from scratch (a fresh session, since a stale/rejected
        cookie jar wouldn't fix itself).
        """
        if self._preview_session is not None:
            return self._preview_session
        async with self._preview_session_lock:
            # Re-check: another tick may have finished creating the session while this
            # one was waiting for the lock.
            if self._preview_session is not None:
                return self._preview_session
            if self._closed:
                return None
            session = async_create_clientsession(self.hass, **self._build_session_kwargs())
            login_ok = False
            try:
                login_ok = await self.login(session=session)
            finally:
                # Any non-success path (failed login, close() during the await above, or
                # login() raising) must not leave an authenticated session orphaned.
                if not login_ok or self._closed:
                    session.detach()
            if not login_ok:
                _LOGGER.warning("Preview session login failed — Stufe-2 poll falls back to the main session")
                return None
            if self._closed:
                return None
            self._preview_session = session
            return session

    @property
    def _base_url(self) -> str:
        """Return the base URL for the Comexio IO-Server."""
        return f"http://{self.host}"

    @property
    def fub_data(self) -> dict[str, Any]:
        """Return function plan metadata (fub_id_str → {Id, Name, Paper, ...}), populated by parse_config()."""
        return self._fub_data

    def update_fub_cache_entry(self, fub_id: int | str, fub_info: dict[str, Any]) -> None:
        """Refresh a single plan's cached metadata (e.g. after an out-of-band get_raw_config() lookup)."""
        self._fub_data[str(fub_id)] = fub_info

    def _clean_value(self, val: Any) -> float:
        """Standardizes values: replaces German comma with dot and converts to numbers."""
        if val is None:
            return 0
        if isinstance(val, str):
            val = val.replace(",", ".")
        try:
            return float(val)
        except (ValueError, TypeError):
            _LOGGER.warning("Failed to clean value: %s", val)
            return 0

    async def get_ha_address(self) -> str:
        """
        Dynamically determines the Home Assistant address (DNS:Port or IP:Port)
        to be used for Comexio webhooks.
        """
        try:
            internal_url = self.hass.config.internal_url
            port = 8123
            fallback_ip = None

            if internal_url:
                parsed = urlparse(internal_url)
                port = parsed.port or 8123
                fallback_ip = parsed.hostname

            def resolve_dns():
                for domain in KNOWN_DOMAINS:
                    test_host = f"homeassistant.{domain}"
                    try:
                        socket.gethostbyname(test_host)
                        return test_host
                    except OSError:
                        continue
                return None

            hostname = await self.hass.async_add_executor_job(resolve_dns)

            if not hostname:
                if not fallback_ip or fallback_ip in ["localhost", "127.0.0.1", "::1"]:

                    def get_local_ip():
                        # Try private IPv4 routing first
                        with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                            s.connect(("10.255.255.255", 1))
                            return s.getsockname()[0]

                        # Fallback to IPv6 local routing (ULA prefix)
                        with suppress(OSError), socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
                            s.connect(("fd00::", 1))
                            return s.getsockname()[0]

                        with suppress(OSError):
                            return socket.gethostbyname(socket.gethostname())

                        return "127.0.0.1"

                    hostname = await self.hass.async_add_executor_job(get_local_ip)
                else:
                    hostname = fallback_ip

            # Wrap IPv6 addresses in brackets for URL compatibility
            with suppress(ValueError):
                if ipaddress.ip_address(hostname).version == 6:
                    hostname = f"[{hostname}]"

            return f"{hostname}:{port}"
        except Exception as e:
            _LOGGER.exception("Failed to determine HA address: %s", e)
            return "127.0.0.1:8123"

    def _encrypt_block(self, data_str: str, mod: int, exp: int) -> str:
        """RSA encryption logic matching Comexio v11 (PKCS1v15)."""
        try:
            pub_key = rsa.RSAPublicNumbers(exp, mod).public_key()
            encrypted = pub_key.encrypt(data_str.encode("iso-8859-1"), padding.PKCS1v15())
            required_len = ((pub_key.key_size + 7) // 8) * 2
            return encrypted.hex().zfill(required_len)
        except Exception:
            _LOGGER.exception("RSA Block encryption failed")
            raise

    async def login(self, session: aiohttp.ClientSession | None = None) -> bool:
        """Performs the RSA login procedure for admin access.

        session: defaults to the main session; pass the dedicated preview session
        (see ensure_preview_session) to log that one in independently instead.
        """
        sess = session if session is not None else self.session
        if not _is_local_address(self.host) and not self._login_warned:
            _LOGGER.warning(
                "Logging into Comexio over plain HTTP on a non-local address (%s). "
                "Credentials may be transmitted in clear text.",
                self.host,
            )
            self._login_warned = True

        _LOGGER.debug("Starting v11 RSA login procedure for host: %s", self.host)
        url = f"{self._base_url}/board/home/login/"

        sess.cookie_jar.update_cookies({"comexio-client-time": str(int(time.time()))})
        try:
            async with sess.post(url, data={"login_keys": "true"}) as resp:
                keys = await resp.json(content_type=None)

            salt_str = base64.b64decode(keys["salt"]).decode("iso-8859-1")
            mod, exp = int(keys["modulus"], 16), int(keys["exponent"], 16)
            nonce = "".join(secrets.choice("0123456789ABCDEF") for _ in range(20))

            pw_part1 = self._encrypt_block(salt_str + nonce + self.password, mod, exp)
            pw_part2 = self._encrypt_block(salt_str + nonce + "", mod, exp)
            pw = f"{pw_part1} {pw_part2}"
            payload = MultiDict(
                [
                    ("target", "/board/home/login"),
                    ("username", self.username),
                    ("password", pw),
                    ("loginsubmit", "Anmelden"),
                    ("encryption", "rsa"),
                ]
            )

            async with (
                sess.post(url, data=payload, headers={"Referer": url}) as resp,
                sess.get(f"{self._base_url}/admin/") as v_resp,
            ):
                html = await v_resp.text()
                if "Anmeldung" not in html and html != "":
                    _LOGGER.info("Successfully logged into Comexio Admin interface")
                    self.last_login_error = None
                    return True
            self.last_login_error = "rejected"
            return False
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as e:
            _LOGGER.exception("Critical error during Comexio login: %s", e)
            self.last_login_error = "connection"
            return False

    async def get_raw_config(self) -> dict[str, Any]:
        """
        Downloads JS config objects and global IO types from the admin interface.
        This provides the source of truth for all device properties and units.
        """
        # 1. Fetch the main admin page to get global variables like $ioTypes
        url_main = f"{self._base_url}/admin/"
        async with self.session.get(url_main) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch admin page for IO types (HTTP %s)", resp.status)
                return {}

            main_html = await resp.text()

        # Try $ioTypes (legacy) then $IOTypesBinary (Comexio v11+) — both have identical structure.
        self.io_types = {}
        for decl_re, var_name in (
            (_IO_TYPES_DECL_RE, "$ioTypes"),
            (_IO_BINARY_TYPES_DECL_RE, "$IOTypesBinary"),
        ):
            if assign_match := decl_re.search(main_html):
                brace_index = main_html.find("{", assign_match.end())
                if brace_index != -1:
                    raw_object, _ = _extract_js_object_literal(main_html, brace_index)
                    if raw_object:
                        try:
                            self.io_types = json.loads(_normalize_js_like_object(raw_object))
                            _LOGGER.debug("Loaded %d IO types from %s", len(self.io_types), var_name)
                            break
                        except json.JSONDecodeError as exc:
                            _LOGGER.warning("Failed to decode %s: %s", var_name, exc)
        else:
            _LOGGER.warning("No IO type data found ($ioTypes / $IOTypesBinary) — using identifier fallback")

        # Extract $IOInputTypes: TypeId → {input: bool}  (input=True means read-only sensor)
        self.io_input_types = {}
        if assign_match := _IO_INPUT_TYPES_DECL_RE.search(main_html):
            brace_index = main_html.find("{", assign_match.end())
            if brace_index != -1:
                raw_object, _ = _extract_js_object_literal(main_html, brace_index)
                if raw_object:
                    try:
                        self.io_input_types = json.loads(_normalize_js_like_object(raw_object))
                        _LOGGER.debug("Loaded %d IO input types", len(self.io_input_types))
                    except json.JSONDecodeError as exc:
                        _LOGGER.warning("Failed to decode $IOInputTypes: %s", exc)

        # 2. Fetch the function module page for the technical device configuration
        url_conf = f"{self._base_url}/admin/function_function_module/home"
        async with self.session.get(url_conf) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch function module page (HTTP %s)", resp.status)
                return {}
            html = await resp.text()

        if version_match := _COMEXIO_VERSION_RE.search(html):
            self.comexio_version = version_match.group(1)

        return self._scrape_js_vars(html, page_label="function module")

    @staticmethod
    def _scrape_js_vars(html: str, *, page_label: str) -> dict[str, Any]:
        """Extract every top-level `var $Name = {...}` JS object literal from an HTML page.

        Shared between get_raw_config (function module page) and get_knx_dpt_catalog (KNX
        admin page) — both pages embed their config as inline script-block JS objects in the
        same style.
        """
        # Restrict search to script tags to avoid scanning entire HTML with a single DOTALL regex
        script_blocks = _SCRIPT_BLOCK_RE.findall(html)

        result: dict[str, Any] = {}
        for script in script_blocks:
            for m in _VAR_DECL_RE.finditer(script):
                var_name = m.group(1)
                search_start = m.end()
                if _EMPTY_JS_ARRAY_RE.match(script, search_start):
                    # PHP json_encode renders an empty array as `[]` (e.g. $Fubs without any
                    # plan) — searching on for "{" would grab the NEXT variable's object.
                    result[var_name] = {}
                    continue
                brace_index = script.find("{", search_start)
                if brace_index == -1:
                    continue

                raw_obj, _ = _extract_js_object_literal(script, brace_index)
                if raw_obj is None:
                    continue

                normalized_obj = _normalize_js_like_object(raw_obj)

                try:
                    result[var_name] = json.loads(normalized_obj)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning(
                        "Failed to decode JSON for variable $%s on %s page: %s",
                        var_name,
                        page_label,
                        exc,
                    )
                    continue
        return result

    async def get_knx_dpt_catalog(self) -> dict[str, Any]:
        """Fetch $KnxPoints/$KnxDevices/$KnxDpt from the KNX admin page.

        Resolves each existing K-element's real KNX DPT (KnxBaseTypeId.KnxSubId) via the
        Point -> Device -> Dpt chain (see _resolve_knx_dpt) — neither $FubModules["11"] nor
        $IOTypesBinary carry a usable analog value range for KNX objects (both report a
        min=max=0 placeholder, see KNX_DPT_ANALOG_RANGES in const.py). Returns the last
        known-good catalog (or {} if none exists yet) on HTTP failure or connection error;
        callers then fall back to the generic WEBIO_MARKER_ANALOG_MIN/MAX range, same as for
        an unresolved DPT — a transient network hiccup on this opt-in sub-page must not fail
        the whole coordinator poll (found in review 2026-09-20: the bare aiohttp call
        previously let a connection error propagate uncaught into _async_update_data's
        poll-wide try/except). Falling back to the stale cache instead of {} matters
        specifically for DPT3.x composite pairing (_attach_knx_dpt3_composites): an empty
        catalog means no item gets tagged knx_composite this poll, which drops the
        composite's unique_id from __init__.py's active_unique_ids whitelist and gets its
        cover/light entity permanently deleted from the registry — a single transient
        failure (e.g. right at HA startup) must not have that effect (found in review
        2026-09-20).

        Cached on the instance and only re-fetched when comexio_version changes (see
        _knx_dpt_catalog docstring) — this method's only caller is once per poll in
        coordinator._async_update_data, but the underlying data is effectively static.
        """
        if self._knx_dpt_catalog is not None and self._knx_dpt_catalog_version == self.comexio_version:
            return self._knx_dpt_catalog

        url = f"{self._base_url}/admin/knx_one_wire/knx/"
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Failed to fetch KNX DPT catalog (HTTP %s)", resp.status)
                    return self._knx_dpt_catalog or {}
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Failed to fetch KNX DPT catalog: %s", err)
            return self._knx_dpt_catalog or {}
        result = self._scrape_js_vars(html, page_label="KNX DPT catalog")
        if not result:
            # HTTP 200 with no parseable `var $Name = {...}` block (changed/malformed page) —
            # same "couldn't get a real catalog this time" outcome as the HTTP-failure branches
            # above. Caching {} here would overwrite the last known-good catalog and, worse,
            # tag it as current for this comexio_version so a later poll never retries.
            _LOGGER.warning("KNX DPT catalog: HTTP 200 but no parseable data — keeping last known-good catalog")
            return self._knx_dpt_catalog or {}
        _LOGGER.debug(
            "KNX DPT catalog: %d points, %d devices, %d dpt entries",
            len(result.get("KnxPoints", {})),
            len(result.get("KnxDevices", {})),
            len(result.get("KnxDpt", {})),
        )
        self._knx_dpt_catalog = result
        self._knx_dpt_catalog_version = self.comexio_version
        return result

    def get_knx_dpt_catalog_snapshot(self) -> tuple[dict[str, Any], str | None] | None:
        """Current in-memory KNX DPT catalog + its comexio_version tag, or None if never fetched.

        Used by the coordinator to persist the last known-good catalog to disk (see
        seed_knx_dpt_catalog and coordinator.async_load_knx_dpt_catalog) — closes the
        cold-start gap where a freshly created instance's very first fetch failing would
        otherwise return {} instead of falling back to a real catalog, since the in-process
        fallback above has nothing cached yet on a fresh instance (Sourcery finding, review
        2026-09-21).
        """
        if self._knx_dpt_catalog is None:
            return None
        return self._knx_dpt_catalog, self._knx_dpt_catalog_version

    def seed_knx_dpt_catalog(self, catalog: dict[str, Any], version: str | None) -> None:
        """Restore a persisted KNX DPT catalog before the first poll (see get_knx_dpt_catalog_snapshot)."""
        self._knx_dpt_catalog = catalog
        self._knx_dpt_catalog_version = version

    @staticmethod
    def _resolve_knx_dpt(knx_dpt_catalog: dict[str, Any], k_id: str) -> tuple[int, int] | None:
        """Resolve a K-element's real KNX DPT (KnxBaseTypeId, KnxSubId) via Point -> Device -> Dpt.

        knx_dpt_catalog is get_knx_dpt_catalog()'s raw dict ($KnxPoints/$KnxDevices/$KnxDpt,
        each id-keyed like Comexio's other JS-object dumps — a K-element's own id IS its
        $KnxPoints entry's id, confirmed against $FubModules["11"]). Returns None if any link
        in the chain is missing or malformed, so callers can fall back to the generic range
        rather than crash on an unexpected shape.

        Unlike $FubModules groups (see _process_source_items), these three never need the
        JSON-array-vs-object handling: their ids are 1-based with gaps (verified against
        comexio-KNX-data.trace.txt 2026-09-20, e.g. $KnxDpt skips id 11), so the "keys are
        exactly 0..N-1" shape PHP's json_encode needs to emit an array can't occur here.
        """
        points = knx_dpt_catalog.get("KnxPoints")
        devices = knx_dpt_catalog.get("KnxDevices")
        dpts = knx_dpt_catalog.get("KnxDpt")
        if not isinstance(points, dict) or not isinstance(devices, dict) or not isinstance(dpts, dict):
            return None

        point = points.get(k_id)
        if not isinstance(point, dict):
            return None
        device = devices.get(str(point.get("KnxDeviceId")))
        if not isinstance(device, dict):
            return None
        dpt = dpts.get(str(device.get("KnxDptId")))
        if not isinstance(dpt, dict):
            return None

        base_type_id, sub_id = dpt.get("KnxBaseTypeId"), dpt.get("KnxSubId")
        if not isinstance(base_type_id, int) or not isinstance(sub_id, int):
            return None
        return base_type_id, sub_id

    async def get_live_states(
        self, marker_count: int, knx_max_id: int = 0
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fetches current live values for markers AND KNX objects, in a single dashboard
        refresh request.

        Markers and KNX objects share the same plain numeric id space (a marker and a KNX
        object can both be "5"), so their live values are kept in two separate returned dicts
        even though this is one HTTP round-trip — merging them into one id-keyed dict would
        silently let e.g. KNX object 5 pick up marker 5's value. Marker entries keep the
        long-standing bare-numeric-id request key ("5": {...}); KNX entries use a
        f"{_KNX_LIVE_KEY_PREFIX}<id>" request key instead specifically so the response can be
        split back apart by prefix afterwards. "11" is $FubModules' own KNX module type id
        (see _process_knx) — a Comexio-wide constant, not per-installation.

        Live-tested against a real KNX-equipped Comexio instance (2026-09-20, see
        project_knx_write_path_design memory): a bare "KnxIo": "K<id>" key returns that
        object's own value, confirmed distinct from the same numeric marker id — the "no known
        bulk live-value endpoint for KNX" assumption the KNX-objects feature originally
        shipped with (_process_knx docstring) was simply never tested against real hardware.

        Returns (None, None) on any fetch/parse failure — never ({}, {}) — so callers can tell
        "endpoint failed this cycle" apart from "nothing to report" and keep last-known values
        instead of overwriting them with a default (found in review 2026-09-20: an empty dict
        here made every marker/KNX value silently collapse to 0/off on a single transient
        HTTP hiccup, indistinguishable from a real reading).
        """
        url = f"{self._base_url}/board/dashboard/refresh/"
        refresh_dict: dict[str, Any] = {
            str(i): {"action": "get", "MarkerName": f"M{i}"} for i in range(1, marker_count + 1)
        }
        refresh_dict.update(
            {
                f"{_KNX_LIVE_KEY_PREFIX}{i}": {"action": "get", "KnxIo": f"K{i}", "Unit": "any"}
                for i in range(1, knx_max_id + 1)
            }
        )
        refresh_dict["messages"] = {"action": "messages"}

        form_data = aiohttp.FormData()
        form_data.add_field("json", json.dumps(refresh_dict))

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/",
            "User-Agent": "Mozilla/5.0",
        }

        try:
            async with self.session.post(url, data=form_data, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Live states fetch failed with HTTP status: %s", resp.status)
                    return None, None
                try:
                    data = await resp.json(content_type=None)
                    result = data.get("result", {})
                except Exception:
                    raw_text = await resp.text()
                    _LOGGER.exception(
                        "Failed to parse live states response as JSON; raw response: %s",
                        raw_text,
                    )
                    return None, None
        except aiohttp.ClientError as err:
            _LOGGER.exception("HTTP request error fetching live states: %s", err)
            return None, None
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching live states: %s", e)
            return None, None

        if not isinstance(result, dict):
            _LOGGER.error("Live states response had an unexpected shape: %r", type(result))
            return None, None

        knx_states = {
            key.removeprefix(_KNX_LIVE_KEY_PREFIX): value
            for key, value in result.items()
            if key.startswith(_KNX_LIVE_KEY_PREFIX)
        }
        marker_states = {
            key: value
            for key, value in result.items()
            if key != "messages" and not key.startswith(_KNX_LIVE_KEY_PREFIX)
        }
        return marker_states, knx_states

    async def get_function_plan_connection_values(
        self, fub_id: int, session: aiohttp.ClientSession | None = None
    ) -> dict[str, list[Any]]:
        """Fetch live per-SOURCE-ELEMENT output values for one Function Plan — Studio's own
        "fupValueData" refresh action, the same /board/dashboard/refresh/ endpoint
        get_live_states uses. Unlike markers/IOs/WebIOs (get_live_states, resolved to
        pill elements), this reports values for every block-internal output (an "Oder"
        gate, a Zeitglied, ...) that carries no marker/IO of its own — the ground truth
        behind the Function Plan preview's Stufe-2 wire coloring (see
        [[project-logikplan-preview]]). Returns {source_element_id: [value_per_output_row]}
        — the dict key is the SOURCE FubElementId (not a connection/wire id: a block with
        several outputs reports one array for all of them, indexed by output IOPos, and
        several wires from the same output row share that one value).

        session: defaults to the main session; the Stufe-2 poll passes its own dedicated,
        independently logged-in session (see ensure_preview_session) so this high-frequency
        call can never queue behind — or block — the main coordinator poll on a shared
        connection.
        """
        sess = session if session is not None else self.session
        url = f"{self._base_url}/board/dashboard/refresh/"
        payload = {"connection": {"action": "fupValueData", "fupId": fub_id}}
        form_data = aiohttp.FormData()
        form_data.add_field("json", json.dumps(payload))

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/",
            "User-Agent": "Mozilla/5.0",
        }

        _LOGGER.debug("Connection values request: POST %s payload=%s", url, payload)
        # No try/except around the request itself: a transient network failure (e.g.
        # ServerDisconnectedError) must propagate to the caller's own exception handler —
        # _async_poll_connection_values counts consecutive failures and disarms the preview
        # after _CONNECTION_POLL_MAX_FAILURES. Swallowing it here as a plain {} return made it
        # indistinguishable from the legitimate "plan not running" sentinel below, silently
        # bypassing that circuit breaker (#75).
        async with sess.post(url, data=form_data, headers=headers) as resp:
            # An HTTP error status is as much a poll failure as a network exception — e.g. a
            # 502 from a server that's mid-reconnect — and must propagate the same way instead
            # of returning the "plan not running" sentinel shape (#75).
            resp.raise_for_status()
            # resp.json() still performs the response body read — a connection drop mid-stream
            # (ClientPayloadError/ServerDisconnectedError/TimeoutError) must keep propagating to
            # the caller's circuit breaker, same reasoning as the removed outer try/except above.
            # json.JSONDecodeError is a genuine parse failure; AttributeError/TypeError cover a
            # response that parses fine but isn't the expected dict shape (e.g. top-level `null`
            # or a list) — a body-shape surprise, not a connection failure, so it must not
            # propagate to the circuit breaker either (#75).
            try:
                data = await resp.json(content_type=None)
                raw = (data.get("result") or {}).get("connection")
            except (json.JSONDecodeError, AttributeError, TypeError):
                _LOGGER.exception("Failed to parse connection values response (fub=%s)", fub_id)
                return {}
            _LOGGER.debug("Connection values raw response (fub=%s): %s", fub_id, raw)
            # Comexio returns the plain-text sentinel "0:not_found" (not JSON) instead
            # of a value dict when the plan isn't currently running — confirmed live
            # 2026-08-22: an active plan (fub=1) returns a real JSON dict every poll,
            # an inactive one (freshly restored/stopped plans included) always returns
            # this sentinel. Expected/frequent, not a parse failure — the old code
            # logged a full ERROR-level exception for it on every 2s poll tick.
            if isinstance(raw, str) and raw and not raw.lstrip().startswith(("{", "[")):
                _LOGGER.debug("Connection values: no live data for fub=%s (plan not active: %s)", fub_id, raw)
                return {}
            try:
                parsed = json.loads(raw) if raw else {}
                # Same PHP array/object ambiguity as function_plan_load_elements: an
                # associative array is serialized as a JSON list whenever its keys are
                # exactly 0..N-1 in order — meaning the list position IS the real source
                # FubElementId, not a guess (a small/quiet plan's element ids can easily
                # land on that sequential shape, e.g. fub=19 with 0 connections -> "[]").
                if isinstance(parsed, list):
                    parsed = {str(i): vals for i, vals in enumerate(parsed)}
                result = {elem_id: vals if isinstance(vals, list) else [vals] for elem_id, vals in parsed.items()}
                _LOGGER.debug("Connection values parsed (fub=%s): %s", fub_id, result)
                return result
            except (json.JSONDecodeError, AttributeError, TypeError):
                _LOGGER.exception("Failed to parse connection values response (fub=%s): %r", fub_id, raw)
                return {}

    def parse_config(
        self,
        conf: dict[str, Any],
        live_states: dict[str, Any] | None = None,
        referenced_markers: set[str] | None = None,
        knx_live_states: dict[str, Any] | None = None,
        knx_dpt_catalog: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Processes the raw configuration and performs a technical audit.
        Uses dynamic IO type mapping to determine binary vs analog states and units.

        live_states and knx_live_states are kept as two separate params (both id-keyed) rather
        than one merged dict — see get_live_states' docstring for why merging them would be
        unsafe (markers and KNX objects share the same plain numeric id space).
        """
        data = {
            "markers": [],
            "io": [],
            "io_all": [],
            "knx": [],
            "webio_commands": {},
            "webio_names": {},
            # One Web-IO device class per source category on the Comexio server — see const.webio_class_name.
            "webio_devices": {cls: {"device_id": None, "device_ip": None, "base_id": None} for cls in WEBIO_CLASSES},
            # Per-extension identity (name + stable serial), see _process_ios — used by the
            # coordinator's extension-rename migration to detect a Comexio-side rename.
            "extensions": {},
        }
        live_states = live_states or {}

        # Cache function plan + paper metadata for later use (e.g. auto canvas-format detection)
        self._fub_data = conf.get("Fubs", {})
        self._paper_data = conf.get("Paper", {})

        # Load configuration
        config_names = self._load_config_names()
        webio_name, schema_marker, schema_io, schema_knx, server_alias = config_names

        # Extract FubModules once
        fub_modules = conf.get("FubModules", {})

        # 2. Map Webhooks and device info
        self._process_device_info(conf, data, webio_name, fub_modules)

        # 3. Process Markers
        self._process_markers(data, live_states, schema_marker, server_alias, fub_modules, referenced_markers)

        # 4. Process IOs
        self._process_ios(data, schema_io, server_alias, fub_modules)

        # 5. Process KNX objects (opt-in via import_knx; coordinator drops the list when disabled).
        # No "wired but unnamed" import for KNX: marker and KNX ids share a numeric space, so the
        # marker reference set cannot be reused here without cross-contamination. A KNX object is
        # imported only when it carries a real Comexio label.
        self._process_knx(data, schema_knx, server_alias, fub_modules, knx_live_states, knx_dpt_catalog)

        _LOGGER.info(
            "Audit: %d Markers, %d IOs, %d KNX, %d Webhooks in Comexio for %s",
            len(data["markers"]),
            len(data["io"]),
            len(data["knx"]),
            len(data["webio_commands"]),
            webio_name,
        )
        return data

    def _load_config_names(self) -> tuple[str, str, str, str, str]:
        """Load configuration names from config_entry."""
        webio_name = "HomeAssistant"
        schema_marker = DEFAULT_SCHEMA_MARKER
        schema_io = DEFAULT_SCHEMA_IO
        schema_knx = DEFAULT_SCHEMA_KNX
        server_alias = "comexio"

        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", webio_name)
            schema_marker = conf_data.get(CONF_SCHEMA_MARKER, schema_marker)
            schema_io = conf_data.get(CONF_SCHEMA_IO, schema_io)
            schema_knx = conf_data.get(CONF_SCHEMA_KNX, schema_knx)
            server_alias = conf_data.get("server_id", server_alias)

        return webio_name, schema_marker, schema_io, schema_knx, server_alias

    # Reference canvas bounds: A4 landscape at 90 DPI (empirically measured on live Comexio)
    _CANVAS_REF_X: float = 870.0
    _CANVAS_REF_Y: float = 720.0
    _CANVAS_REF_MM_LONG: int = 297  # A4 long side (landscape width)
    _CANVAS_REF_MM_SHORT: int = 210  # A4 short side (landscape height)
    _CANVAS_REF_RES: int = 90
    # Paper name → (long side mm, short side mm) for explicit format override
    _PAPER_MM_BY_NAME: dict[str, tuple[int, int]] = {
        "A2": (594, 420),
        "A3": (420, 297),
        "A4": (297, 210),
        "A5": (210, 148),
    }

    def get_fub_paper_format(self, fub_id: int) -> str:
        """Return the paper format name (e.g. 'A4') for a function plan, defaulting to 'A4'."""
        fub = self._fub_data.get(str(fub_id), {})
        paper_id = str(fub.get("Paper", ""))
        paper = self._paper_data.get(paper_id, {})
        return str(paper.get("Name", "A4"))

    def get_fub_dpi(self, fub_id: int) -> int:
        """Return the configured resolution (DPI) for a function plan, defaulting to 90."""
        fub = self._fub_data.get(str(fub_id), {})
        return int(fub.get("Resolution", self._CANVAS_REF_RES))

    def get_fub_orientation(self, fub_id: int) -> str:
        """Return 'portrait' or 'landscape' for a function plan, defaulting to 'landscape'."""
        fub = self._fub_data.get(str(fub_id), {})
        return "portrait" if int(fub.get("Orientation", 0)) == 1 else "landscape"

    def get_fub_active(self, fub_id: int) -> bool | None:
        """Return a function plan's active flag, or None if the plan is not known live."""
        fub = self._fub_data.get(str(fub_id))
        if fub is None:
            return None
        return bool(int(fub.get("Active") or 0))

    def get_fub_canvas_bounds(
        self, fub_id: int, paper_name: str | None = None, orientation: str | None = None
    ) -> tuple[float, float]:
        """Return estimated (x_max, y_max) canvas bounds for a function plan.

        Scales proportionally from the A4-landscape-90-DPI reference (870×720).
        DPI (Resolution) is always taken from $Fubs plan data.
        paper_name overrides the format (A2/A3/A4/A5); None = read from $Fubs/$Paper.
        orientation overrides as "landscape"/"portrait" — needed for a plan that was just
        created and is not in the cached $Fubs data yet; None = read from $Fubs, where
        0 = landscape (long side → X), 1 = portrait (long side → Y).
        """
        fub = self._fub_data.get(str(fub_id), {})
        res = int(fub.get("Resolution", self._CANVAS_REF_RES))
        if orientation is None:
            orient_id = int(fub.get("Orientation", 0))
        else:
            orient_id = 1 if orientation.lower() == "portrait" else 0

        if paper_name and paper_name in self._PAPER_MM_BY_NAME:
            mm_long, mm_short = self._PAPER_MM_BY_NAME[paper_name]
        else:
            paper_id = str(fub.get("Paper", ""))
            paper = self._paper_data.get(paper_id, {})
            mm_long = paper.get("MMX", self._CANVAS_REF_MM_LONG)
            mm_short = paper.get("MMY", self._CANVAS_REF_MM_SHORT)

        if orient_id == 0:
            width_mm, height_mm = mm_long, mm_short
        else:
            width_mm, height_mm = mm_short, mm_long

        x_max = self._CANVAS_REF_X * (width_mm / self._CANVAS_REF_MM_LONG) * (res / self._CANVAS_REF_RES)
        y_max = self._CANVAS_REF_Y * (height_mm / self._CANVAS_REF_MM_SHORT) * (res / self._CANVAS_REF_RES)
        return x_max, y_max

    @staticmethod
    def _iter_group(group: Any) -> Iterable[tuple[str, Any]]:
        """Iterate a Comexio id group as (id, member) pairs, ids normalized to str.

        Comexio serializes a gap-free id group as a JSON array instead of an object
        (observed for both $FubModules groups and Web-IO command groups) — the array
        index then IS the id, so both shapes yield the same (id, member) pairs.
        """
        items = group.items() if isinstance(group, dict) else enumerate(group or [])
        return ((str(gid), member) for gid, member in items)

    def _process_device_info(
        self,
        conf: dict[str, Any],
        data: dict[str, Any],
        webio_name: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process device info and webhooks for both Web-IO classes (marker/io)."""
        web_devices = conf.get("WebDevices", {})
        fub_10 = fub_modules.get("10", {})
        fub_10_by_dev_id = dict(self._iter_group(fub_10))

        missing_classes = []
        for webio_class in WEBIO_CLASSES:
            target_dev_id = self._assign_webio_device_id(web_devices, data, webio_name, webio_class)
            if not target_dev_id:
                missing_classes.append(webio_class)
                continue
            commands = fub_10_by_dev_id.get(target_dev_id)
            if commands is None:
                continue
            for w_id, w_obj in self._iter_group(commands):
                if not isinstance(w_obj, dict):
                    _LOGGER.debug(
                        "Skipping non-dict Web-IO command entry %s in device %s (webio_class=%s): %r",
                        w_id,
                        target_dev_id,
                        webio_class,
                        w_obj,
                    )
                    continue
                self._add_webhook_command(data, w_id, w_obj, webio_class)

        if missing_classes and any(d.get("Name") == webio_name for d in web_devices.values()):
            # Pre-split installs have a single Web-IO device named exactly `webio_name`; it
            # won't match the new "<name> [M]"/"<name> [IO]" class names, so both classes look
            # missing right after upgrading. No automatic migration on purpose (see CLAUDE.md:
            # no backwards-compat shims) — a Full Sync creates the new class(es); the old device
            # is left untouched and can be removed manually once no longer needed.
            _LOGGER.warning(
                "Found a legacy Web-IO device named '%s' without a Marker/IO class suffix. "
                "This version splits Web-IO into separate classes ('%s' / '%s'); run a Full Sync "
                "to create the missing class(es): %s. The old device is left in place.",
                webio_name,
                webio_class_name(webio_name, WEBIO_CLASS_MARKER),
                webio_class_name(webio_name, WEBIO_CLASS_IO),
                ", ".join(webio_class_label(c) for c in missing_classes),
            )

        self._build_webio_name_lexicon(data, fub_10)

    def _assign_webio_device_id(
        self,
        web_devices: dict[str, Any],
        data: dict[str, Any],
        webio_name: str,
        webio_class: str,
    ) -> str | None:
        """Find the WebDevices entry for one Web-IO class, populate its device_info, return its id."""
        class_name = webio_class_name(webio_name, webio_class)
        for d_id, d_data in web_devices.items():
            if d_data.get("Name") != class_name:
                continue
            target_dev_id = str(d_id)
            dev_info = data["webio_devices"][webio_class]
            dev_info["device_id"] = target_dev_id
            # Comexio has been observed to scrape a leading space into the Ip field, which
            # broke the IP-mismatch audit (mismatch reported against an otherwise-identical
            # address) — stripped here, at the single point the value enters HA.
            raw_ip = d_data.get("Ip")
            dev_info["device_ip"] = raw_ip.strip() if isinstance(raw_ip, str) else raw_ip
            # The class id is WebDeviceBaseId in $WebDevices — "BaseId" only exists in the
            # upload/save payloads. Reading "BaseId" here left base_id None for every class, so
            # the uninstall cleanup never deleted a single Web-IO class (live-verified 2026-09-25).
            raw_base_id = d_data.get("WebDeviceBaseId")
            dev_info["base_id"] = str(raw_base_id) if raw_base_id is not None else None
            return target_dev_id
        return None

    def _build_webio_name_lexicon(self, data: dict[str, Any], fub_10: dict[str, Any]) -> None:
        """Build the webio_names label lexicon over ALL Web-IO classes (read-only, for Function Plan rendering).

        Plans may wire commands of foreign Web-IO devices, whose names are otherwise unknown to
        HA. Names mirror Comexio Studio's pill labels: '{deviceId}. {commandName}' (e.g.
        '16. R1 SZ Rollo % IST') — Studio does NOT include the device name in the pill (verified
        against the Netzteil plan). Kept separate from webio_commands on purpose — that dict
        drives the sync/audit logic and must only ever contain HA's own class.
        """
        for dev_id, dev_commands in self._iter_group(fub_10):
            prefix = f"{dev_id}. "
            for w_id, w_obj in self._iter_group(dev_commands):
                if not isinstance(w_obj, dict):
                    continue
                name = w_obj.get("Name")
                if name:
                    data["webio_names"][w_id] = {
                        "name": f"{prefix}{name}",
                        "analog": w_obj.get("TypeId") in {2, "2"},
                    }

    def _add_webhook_command(self, data: dict[str, Any], w_id: str, w_obj: dict[str, Any], webio_class: str) -> None:
        """Add a webhook command to data."""
        raw_type = w_obj.get("TypeId")
        try:
            val_type = int(raw_type) if raw_type is not None else 1
        except (ValueError, TypeError):
            val_type = 1

        # webIoId (w_id) is a global counter across ALL Web-IO devices on the server (verified
        # live) — safe to key this single flat dict by command name regardless of webio_class.
        data["webio_commands"][w_obj.get("Name")] = {
            "webIoId": w_id,
            "cmdId": w_obj.get("WebCommandId"),
            "typeId": val_type,
            "webioClass": webio_class,
        }

    # Placeholder title for a Comexio marker with an empty label that is still wired into a
    # plan (see _process_markers) — kept out of the entity/name comparison logic nowhere
    # special-cased on purpose: once imported, it behaves exactly like any other marker name,
    # so a later real rename in Comexio is picked up by the normal sync/rename detection.
    _NO_NAME_MARKER_TITLE = "#nn"

    def _process_markers(
        self,
        data: dict[str, Any],
        live_states: dict[str, Any],
        schema_marker: str,
        server_alias: str,
        fub_modules: dict[str, Any],
        referenced_marker_ids: set[str] | None = None,
    ) -> None:
        """Process markers from config ($FubModules["2"]).

        A marker without a Comexio label is normally excluded entirely — but one that is
        actually wired into a function plan (referenced_marker_ids) is imported anyway with
        a synthetic "#nn" title, so it gets a real HA entity/webhook/Web-IO command and the
        plan preview knows its type + live value. If the marker later gets a real name in
        Comexio, or drops out of every plan, it naturally reverts to the normal path (named
        marker, or orphaned like any other unused marker) — no special-case cleanup needed.
        """
        data["markers"].extend(
            self._process_source_items(
                fub_modules,
                module_key="2",
                schema=schema_marker,
                id_prefix="M",
                id_placeholder="MarkerId",
                title_placeholder="MarkerTitle",
                server_alias=server_alias,
                live_states=live_states,
                referenced_ids=referenced_marker_ids,
            )
        )

    def _process_knx(
        self,
        data: dict[str, Any],
        schema_knx: str,
        server_alias: str,
        fub_modules: dict[str, Any],
        live_states: dict[str, Any] | None = None,
        knx_dpt_catalog: dict[str, Any] | None = None,
    ) -> None:
        """Process KNX objects from config ($FubModules["11"], "knxIo" per $FubTypes).

        Structurally modeled 1:1 on markers (see _process_markers) — same title-suffix kind
        heuristic ([RO]/[TRIG]/[TP]), same analog/digital Type mapping.

        live_states here is the SEPARATE knx-keyed dict get_live_states() now also returns
        (its "knxIo_11_<id>" query, live-tested 2026-09-20 against a real KNX-equipped
        Comexio instance — see project_knx_write_path_design memory) — never the marker dict,
        since markers and KNX objects share the same plain numeric id space and mixing them
        would silently hand e.g. KNX object 5 marker 5's live value.

        knx_dpt_catalog (get_knx_dpt_catalog()'s result, only fetched when import_knx is on)
        is used to attach each analog item's real KNX-standard value range AND step (native
        resolution) — see _resolve_knx_dpt / KNX_DPT_ANALOG_RANGES. Items whose DPT can't be
        resolved, or whose DPT has no entry in the table, are left without dpt_min/dpt_max/
        dpt_unit/dpt_step so ComexioKnxNumber falls back to its generic heuristic. The same
        resolved DPT also drives dpt_device_class — KNX_DPT_DEVICE_CLASS (a plain HA
        NumberDeviceClass value string) for analog items, or KNX_DPT_DIGITAL_DEVICE_CLASS (a
        plain HA BinarySensorDeviceClass value string, only meaningful once ComexioKnxBinarySensor
        picks it up for a "[RO]" item) for digital ones — absent from either when the DPT has no
        matching device class (e.g. the DPT3.x step/direction values). A digital item whose DPT
        is instead one of the physically ambivalent DPT1.x subtypes (KNX_DPT_DIGITAL_AMBIGUOUS)
        gets dpt_ambiguous=True — see coordinator._auto_suffix_unambiguous_knx /
        _audit_knx_dpt_ambiguous for what consumes these two flags. An item whose raw Type
        has no entry at all in self.io_types (_source_item_type's dpt_type_unresolved) also
        gets dpt_ambiguous=True unconditionally, routing it into the same human-review repair
        flow instead of _build_source_item silently guessing "analog" for a possibly-digital
        object (Sourcery finding, review 2026-09-21).
        """
        items = self._process_source_items(
            fub_modules,
            module_key="11",
            schema=schema_knx,
            id_prefix="K",
            id_placeholder="KnxId",
            title_placeholder="KnxTitle",
            server_alias=server_alias,
            live_states=live_states or {},
        )
        for item in items:
            if item.pop("dpt_type_unresolved", False):
                # Set by _source_item_type: no $IOTypesBinary entry at all for this KNX
                # object's raw Type, so its digital/analog split is genuinely unknown rather
                # than merely unresolved-but-analog. Routed into the same human-review repair
                # flow as a physically ambivalent DPT1.x subtype (_audit_knx_dpt_ambiguous)
                # regardless of knx_dpt_catalog availability below — that catalog is unrelated
                # to io_types, and this must not depend on a second, independent fetch
                # succeeding too.
                item["dpt_ambiguous"] = True
        if knx_dpt_catalog:
            for item in items:
                self._apply_knx_dpt_metadata(item, knx_dpt_catalog)
            self._attach_knx_dpt3_composites(items, knx_dpt_catalog)
        data["knx"].extend(items)

    def _apply_knx_dpt_metadata(self, item: dict[str, Any], knx_dpt_catalog: dict[str, Any]) -> None:
        """Resolve one KNX item's DPT and attach unit/device_class/ambiguous metadata in place.

        Split out of _process_knx to keep its own cognitive complexity within SonarQube
        S3776's limit — see _process_knx's docstring for the full semantics implemented here.
        """
        if item.get("dpt_ambiguous"):
            # Already forced True in _process_knx because io_types had no entry for this
            # object's raw Type at all (dpt_type_unresolved) — a device_class this DPT chain
            # might independently resolve to is not corroborating evidence worth auto-tagging
            # on (KNX_DPT_DIGITAL_DEVICE_CLASS below would otherwise make it eligible for
            # coordinator._auto_suffix_unambiguous_knx's auto-rename, racing the human-review
            # repair flow this item is already queued for). Leave it on the generic fallback.
            return
        dpt = self._resolve_knx_dpt(knx_dpt_catalog, item["id"])
        if dpt is None:
            _LOGGER.debug("KNX item %s: could not resolve DPT chain, using generic fallback", item["id"])
            return
        if item["type"] != "analog":
            if device_class := KNX_DPT_DIGITAL_DEVICE_CLASS.get(dpt):
                item["dpt_device_class"] = device_class
            elif dpt in KNX_DPT_DIGITAL_AMBIGUOUS:
                # Physically ambivalent DPT1.x subtype (see KNX_DPT_DIGITAL_AMBIGUOUS
                # docstring) — flagged for coordinator._audit_knx_dpt_ambiguous rather
                # than auto-classified.
                item["dpt_ambiguous"] = True
            return
        dpt_range = KNX_DPT_ANALOG_RANGES.get(dpt)
        if dpt_range is None:
            _LOGGER.debug(
                "KNX item %s: resolved DPT%s.%s has no entry in KNX_DPT_ANALOG_RANGES, using generic fallback range",
                item["id"],
                dpt[0],
                dpt[1],
            )
            return
        item["dpt_min"], item["dpt_max"], item["dpt_unit"], item["dpt_step"] = dpt_range
        if device_class := KNX_DPT_DEVICE_CLASS.get(dpt):
            item["dpt_device_class"] = device_class

    def _attach_knx_dpt3_composites(self, items: list[dict[str, Any]], knx_dpt_catalog: dict[str, Any]) -> None:
        """Tag DPT3.x (Dimmer 3.007 / Blinds 3.008) K-element pairs with composite metadata.

        Comexio splits each DPT3.x KNX object into two K-elements sharing one KnxDeviceId —
        a digital control bit (direction) and an analog 3-bit step code (0=break, 1-7=move),
        see dev-tools/knx_seed_test_matrix.py's save_device()/points[0]/points[1]. Tags each
        item in place with a "knx_composite" dict ({"role": "direction"|"stepcode", "domain":
        "light"|"cover", "partner_id": <other K-element's id>}) so cover.py/light.py can build
        one composite entity per pair, and switch.py/number.py can skip the pair's individual
        generic entities. Only annotates `items` — sync/audit/wiring logic (button.py,
        coordinator.py) is untouched, since both K-elements keep their own bridge Marker
        exactly as before.
        """
        points = knx_dpt_catalog.get("KnxPoints")
        if not isinstance(points, dict):
            return

        by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            point = points.get(item["id"])
            device_id = point.get("KnxDeviceId") if isinstance(point, dict) else None
            if device_id is not None:
                by_device[str(device_id)].append(item)

        for device_id, pair in by_device.items():
            self._tag_knx_dpt3_pair(device_id, pair, knx_dpt_catalog)

    def _tag_knx_dpt3_pair(self, device_id: str, pair: list[dict[str, Any]], knx_dpt_catalog: dict[str, Any]) -> None:
        """Tag one KnxDeviceId's 2-point group with knx_composite metadata, if it qualifies.

        Split out of _attach_knx_dpt3_composites to keep its own cognitive complexity within
        SonarQube S3776's limit — see that method's docstring for the full DPT3.x pairing
        semantics.
        """
        if len(pair) != 2:
            if len(pair) > 2:
                _LOGGER.debug(
                    "KNX device %s has %d points sharing one KnxDeviceId (expected at most 2), "
                    "skipping composite grouping",
                    device_id,
                    len(pair),
                )
            return
        dpt = self._resolve_knx_dpt(knx_dpt_catalog, pair[0]["id"])
        domain = KNX_DPT3_COMPOSITE_DOMAIN.get(dpt) if dpt else None
        if domain is None:
            _LOGGER.debug(
                "KNX device %s has 2 points but resolved DPT %s isn't a DPT3.x composite, skipping composite grouping",
                device_id,
                dpt,
            )
            return
        direction_item = next((i for i in pair if i["type"] == "digital"), None)
        stepcode_item = next((i for i in pair if i["type"] == "analog"), None)
        if direction_item is None or stepcode_item is None:
            _LOGGER.debug(
                "KNX device %s resolved to DPT%s.%s but its 2 points aren't one digital + "
                "one analog K-element, skipping composite grouping",
                device_id,
                dpt[0],
                dpt[1],
            )
            return
        direction_item["knx_composite"] = {
            "role": "direction",
            "domain": domain,
            "partner_id": stepcode_item["id"],
        }
        stepcode_item["knx_composite"] = {
            "role": "stepcode",
            "domain": domain,
            "partner_id": direction_item["id"],
        }

    def _process_source_items(
        self,
        fub_modules: dict[str, Any],
        *,
        module_key: str,
        schema: str,
        id_prefix: str,
        id_placeholder: str,
        title_placeholder: str,
        server_alias: str,
        live_states: dict[str, Any],
        referenced_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Shared Marker/KNX item processing over one $FubModules category.

        Both categories carry the same relevant fields (Id, Name, Type) and are exposed to
        HA identically, so the per-item build is a single code path parametrized by the
        id prefix and the entity-name schema placeholder keys.
        """
        referenced_ids = referenced_ids or set()
        items: list[dict[str, Any]] = []
        group = fub_modules.get(module_key)
        # Comexio serializes gap-free id groups as JSON arrays instead of objects (same
        # quirk as $FubModules["10"], see _build_webio_name_lexicon) — group members are
        # values either way, so the array case just iterates it directly.
        group_items = group.values() if isinstance(group, dict) else (group or [])
        for raw in group_items:
            if not isinstance(raw, dict) or raw.get("Id") is None:
                continue

            item_id = str(raw.get("Id"))
            has_name = bool(raw.get("Name"))
            if not has_name and item_id not in referenced_ids:
                continue

            items.append(
                self._build_source_item(
                    raw,
                    item_id=item_id,
                    has_name=has_name,
                    module_key=module_key,
                    schema=schema,
                    id_prefix=id_prefix,
                    id_placeholder=id_placeholder,
                    title_placeholder=title_placeholder,
                    server_alias=server_alias,
                    live_states=live_states,
                )
            )
        return items

    def _build_source_item(
        self,
        raw: dict[str, Any],
        *,
        item_id: str,
        has_name: bool,
        module_key: str,
        schema: str,
        id_prefix: str,
        id_placeholder: str,
        title_placeholder: str,
        server_alias: str,
        live_states: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one Marker/KNX item dict from its raw $FubModules entry.

        Split out of _process_source_items to keep its own cognitive complexity within
        SonarQube S3776's limit — see that method's docstring for the shared semantics.
        """
        type_raw = raw.get("Type", 1)
        type_str, type_unresolved = self._source_item_type(module_key, type_raw)
        title = raw.get("Name") or self._NO_NAME_MARKER_TITLE

        ha_name = schema.format_map(
            SafeDict(ServerAlias=server_alias, **{id_placeholder: item_id, title_placeholder: title})
        )

        item = {
            "id": item_id,
            "ha_name": " ".join(ha_name.split()),
            "name": f"{id_prefix}{item_id} {title}",
            # Bare Comexio title, without the id prefix "name" carries — needed e.g.
            # by create_knx_bridge_marker() to build the bridge marker's own title.
            "title": title,
            # Unnamed-but-referenced item ("#nn"): the plan preview greys it out
            # like an inactive IO as a visual hint that it has no label in Comexio.
            "no_name": not has_name,
            "type": type_str,
            "type_raw": type_raw,
            "value": self._clean_value(live_states.get(item_id, 0)),
            "kind": self._marker_kind(title, module_key=module_key),
        }
        if type_unresolved:
            # Only ever True for module_key=="11" (see _source_item_type) — a transient
            # signal, popped by _process_knx right after building these items (converted into
            # dpt_ambiguous=True there) and never present on a marker item.
            item["dpt_type_unresolved"] = True
        return item

    def _source_item_type(self, module_key: str, type_raw: Any) -> tuple[str, bool]:
        """digital/analog classification for one raw Marker/KNX Type value.

        Returns (type_str, unresolved) — unresolved is only ever True for a KNX item whose
        raw Type has no entry at all in self.io_types (Comexio hasn't provided an
        $IOTypesBinary entry for it yet, e.g. a very new/uncommon DPT). Split out of
        _process_source_items for SonarQube S3776.
        """
        if module_key == "11":
            # KNX ($FubModules["11"]): Type is a rich catalog code (same value space as
            # normal IOs' $IOTypesBinary), NOT the simple {1,2,3} scale markers use — the
            # marker-only heuristic below would e.g. misclassify Type=121 (DPT17 scene
            # number, analog) as digital. Reuse the same self.io_types lookup
            # _add_io_entry() already uses for IOs (confirmed live 2026-09-14 against real
            # KNX wiring on a function plan — see project_knx_write_path_design memory).
            entry = self.io_types.get(str(type_raw))
            if entry is None:
                # Genuinely unresolved, not just "resolved and analog" — defaulting to
                # "analog" here would silently expose a possibly-digital object as a
                # writable number entity (Sourcery finding, review 2026-09-21). "digital"
                # is the safer default of the two: it only risks a spurious switch/[RO]
                # sensor rather than pushing an out-of-range analog write to what might
                # be a binary KNX datapoint, and dpt_type_unresolved=True routes it
                # through the same human-review repair flow as a physically ambivalent
                # DPT1.x subtype either way.
                return "digital", True
            return ("digital" if entry.get("binary", False) else "analog"), False
        return ("analog" if type_raw in [2, 3] else "digital"), False

    @staticmethod
    def _marker_kind(m_title: str, *, module_key: str) -> MarkerKind:
        """Derive a source item's HA exposure kind from its Comexio-side title suffix.

        A trailing "[K<id>]" (auto-created write-path bridge Marker, see
        MARKER_KNX_BRIDGE_SUFFIX_RE) is checked first: it is machine-titled by
        create_knx_bridge_marker() as "<k_title> [K<k_id>]", and since all three suffix
        checks below are end-anchored they're mutually exclusive anyway — the ordering
        itself has no effect on a Marker. It matters only for module_key: this classification
        is Marker-only (module_key "2") — a KNX object (module_key "11") can never itself be
        a bridge, only be fed by one, so a KNX object whose own (user/ETS-given) title
        happens to end in the same bracket-and-digits shape must not be swept into
        KNX_BRIDGE, which would silently drop it from the audit and from every HA platform.
        Failing that, "[RO]" wins over a simultaneous "[TRIG]"/"[TP]" suffix (nonsensical
        combination, but must resolve to exactly one kind rather than crash).
        """
        title = m_title.rstrip()
        if MARKER_KNX_BRIDGE_SUFFIX_RE.search(title):
            if module_key != "2":
                _LOGGER.warning(
                    "KNX object '%s' has a title ending in '[K<id>]' — that suffix is reserved for "
                    "auto-created write-path bridge Markers and is ignored here (treating as normal).",
                    title,
                )
            else:
                return MarkerKind.KNX_BRIDGE
        if title.endswith(MARKER_READ_ONLY_SUFFIX):
            if any(suffix in title for suffix in MARKER_TRIGGER_SUFFIXES):
                _LOGGER.warning("Marker '%s' has both [RO] and a trigger suffix — treating as read-only.", title)
            return MarkerKind.READ_ONLY
        if title.endswith(MARKER_TRIGGER_SUFFIXES):
            return MarkerKind.TRIGGER
        return MarkerKind.NORMAL

    def _process_ios(
        self,
        data: dict[str, Any],
        schema_io: str,
        server_alias: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process IOs from config.

        Inactive IOs (Active=False, e.g. an extension slot the user prepared but hasn't
        wired up yet) get no entity/webhook — Comexio itself refuses to wire a connection
        to an inactive IO, so there is nothing meaningful to read/write. They still get a
        proper label in "io_all" (unfiltered) so the Function Plan preview can resolve their
        name instead of falling back to a bare "IO ref=N".
        """
        for ext_id, ext_content in fub_modules.get("1", {}).items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")
            ext_serial = ext_meta.get("Identifier", "")
            ext_offline = _is_extension_offline(ext_serial)
            data["extensions"][ext_id] = {"name": ext_name, "serial": ext_serial}

            for io_item in ext_content.get("inoutput", {}).values():
                if not io_item:
                    continue

                io_type_id = str(io_item.get("InOutputTypeId"))
                type_info = self.io_types.get(io_type_id, {})

                ident = io_item.get("Identifier") or str(io_item.get("Id", "unknown"))
                desc = io_item.get("Description") or ident

                self._add_io_entry(
                    data, io_item, ext_name, ident, desc, type_info, schema_io, server_alias, ext_offline
                )

    @staticmethod
    def _normalize_io_unit(unit: str) -> str:
        """Normalize Comexio IO unit strings to HA-compatible values."""
        if unit in ("\\u00b0C", "°C", "°C", "C"):
            return "°C"
        return "" if unit in ("0/1", "1/0", "?") else unit

    def _add_io_entry(
        self,
        data: dict[str, Any],
        io_item: dict[str, Any],
        ext_name: str,
        ident: str,
        desc: str,
        type_info: dict[str, Any],
        schema_io: str,
        server_alias: str,
        ext_offline: bool = False,
    ) -> None:
        """Add an IO entry to data."""
        is_binary = type_info.get("binary", False)
        v_min = type_info.get("min", 0)
        v_max = type_info.get("max", 1)
        unit = type_info.get("unit", "")
        ident_upper = ident.upper()

        try:
            type_id_raw = int(io_item.get("InOutputTypeId", 1))
        except (ValueError, TypeError):
            type_id_raw = 1

        # Fallback classification when $IOTypesBinary unavailable.
        if not self.io_types:
            if re.match(r"^QI\d+$", ident_upper):
                is_binary, v_max = False, 0
            elif re.match(r"^Q\d+$", ident_upper) or re.match(r"^I\d+$", ident_upper):
                is_binary, v_max = True, 1

        # is_input=True → read-only sensor/binary_sensor; False → writable switch/number.
        # Identifier prefix is the reliable source: Q* are relay/dimmer outputs (writable),
        # I*/AI*/QI* and special names are inputs. $IOInputTypes cannot be used here because
        # the same TypeId (e.g. 2 = binary 0/1) is shared by both inputs and outputs.
        if re.match(r"^Q\d+$", ident_upper):
            is_input = False
        elif re.match(r"^(?:I|AI|QI)\d+$", ident_upper):
            is_input = True
        elif self.io_input_types:
            is_input = self.io_input_types.get(str(type_id_raw), {}).get("input", True)
        else:
            is_input = True

        unit = self._normalize_io_unit(unit)

        if desc and desc.strip() and desc != ident:
            io_name = f"{ext_name} {ident} {desc.strip()}"
        else:
            io_name = f"{ext_name} {ident}"

        ha_name = schema_io.format_map(
            SafeDict(ServerAlias=server_alias, ExtName=ext_name, IoId=ident, IoTitle=desc or "")
        )

        entry = {
            "id": str(io_item.get("Id")),
            "ext_name": ext_name,
            "identifier": ident,
            "ha_name": " ".join(ha_name.split()),
            "name": io_name,
            "is_binary": is_binary,
            "is_input": is_input,
            "unit": unit,
            "min": v_min,
            "max": v_max,
            "type_id_raw": type_id_raw,
            "value": self._clean_value(io_item.get("Value", 0)),
            "offline": ext_offline,
            # Inactive IOs get no entity/webhook (see _process_ios) but still need a label
            # for the Function Plan preview — "io_all" carries every IO, this flag tells the
            # renderer to grey the pill (Studio's own convention for an inactive element).
            "inactive": not io_item.get("Active"),
        }
        data["io_all"].append(entry)
        if not entry["inactive"]:
            data["io"].append(entry)

    # --- WEB-IO MANAGEMENT ---
    async def get_webio_base_info(self, webio_name: str) -> tuple[str, bool] | None:
        """Scans classes via add-page.

        Returns (base_id, deletable), or `None` if `webio_name` genuinely has no class in a
        successfully-fetched page. A failed fetch raises RuntimeError instead of returning
        `None`, mirroring get_webio_device_info — callers treat `None` as "class absent" and
        upload a fresh class on that basis, so silently reporting "absent" on a transient HTTP
        error would create a duplicate class next to the one that is actually still present.
        """
        url_add = f"{self._base_url}/admin/web_io/add"
        async with self.session.get(url_add) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch Web-IO add page (HTTP %s)", resp.status)
                raise RuntimeError(f"Failed to fetch Web-IO add page (HTTP {resp.status})")
            html = await resp.text()
            pattern = rf'<option value="(\d+)"[^>]*>{re.escape(webio_name)}</option>'
            if match := re.search(pattern, html, re.IGNORECASE):
                b_id = match[1]
                url_win = f"{self._base_url}/admin/web_io/baseDeviceWindow/"
                async with self.session.get(url_win) as win_resp:
                    if win_resp.status != 200:
                        _LOGGER.error("Failed to fetch Web-IO base window (HTTP %s)", win_resp.status)
                        raise RuntimeError(f"Failed to fetch Web-IO base window (HTTP {win_resp.status})")
                    win_html = await win_resp.text()

                    return (b_id, f"delete_web_device_base/?id={b_id}" in win_html)
        return None

    async def get_webio_device_info(self, device_name: str) -> str | None:
        """Checks instance existence via HTML tabs.

        Returns the tab's device id, or `None` if `device_name` genuinely has no tab in a
        successfully-fetched page. A failed fetch raises instead of returning `None` — callers
        use `None` to mean "device absent" and force a destructive recreate on that basis
        (see button.py's `_decide_effective_action`), so silently reporting "absent" here on a
        transient HTTP error would delete-and-reupload a class that is actually still present.
        """
        url_home = f"{self._base_url}/admin/web_io/home"
        async with self.session.get(url_home) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch Web-IO home page (HTTP %s)", resp.status)
                raise RuntimeError(f"Failed to fetch Web-IO home page (HTTP {resp.status})")
            html = await resp.text()
            pattern = rf'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
            return m[1] if (m := re.search(pattern, html, re.IGNORECASE)) else None

    async def delete_webio_device(self, device_id: str | int) -> bool:
        """
        Tries to delete the device instance.
        Returns True if successful, False if blocked by Comexio logic.
        """
        url = f"{self._base_url}/admin/web_io/delete_device/?id={device_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        _LOGGER.debug("Deleting Web-IO device %s via GET: %s", device_id, url)
        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO device %s (HTTP %s)", device_id, resp.status)
                return False
            html = await resp.text()

            # Check for jQuery UI error state which indicates the device is in use
            if 'class="ui-state-error' in html or "ui-state-error" in html:
                _LOGGER.warning("Device %s is in use within Comexio logic and cannot be deleted.", device_id)
                return False
            return True

    async def delete_webio_base(self, base_id: str | int) -> bool:
        """Sends DELETE command for device class template."""
        url = f"{self._base_url}/admin/web_io/delete_web_device_base/?id={base_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO base %s (HTTP %s)", base_id, resp.status)
            return resp.status == 200

    async def delete_fup(self, fub_id: int) -> bool:
        """Delete an entire Function Plan (not just elements within it).

        Same redirect-verification pattern as create_fup: the server responds with a
        302 redirect to the plan overview, carrying 'delete=ok' in the Location header
        on success.
        """
        url = f"{self._base_url}/admin/function_function_module/delete/?id={fub_id}"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("delete_fup: deleting Function Plan fub_id=%s", fub_id)
        try:
            async with self.session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("delete_fup: unexpected HTTP status %s for fub_id=%s", resp.status, fub_id)
                    return False
                redirect_location = resp.headers.get("Location", "")
                success = "delete=ok" in redirect_location
                if not success:
                    _LOGGER.error(
                        "delete_fup: redirect missing 'delete=ok' (fub_id=%s, location: %s)",
                        fub_id,
                        redirect_location,
                    )
                return success
        except aiohttp.ClientError:
            _LOGGER.exception("delete_fup: HTTP request error deleting fub_id=%s", fub_id)
            return False
        except Exception:
            _LOGGER.exception("delete_fup: unexpected error deleting fub_id=%s", fub_id)
            return False

    async def update_webio_device_ip(self, device_id: str | int, ha_address: str, webio_name: str) -> bool:
        """
        Updates the server address (IP:Port) of an existing device.
        Uses the specific POST format required by Comexio's main save handler.

        webio_name must be the class-specific name (see const.webio_class_name) matching
        the device_id being updated — the caller resolves it, since a single config entry
        now maps to two Web-IO devices (marker/io).
        """
        _LOGGER.info("Updating Web-IO device %s address to %s", device_id, ha_address)
        url = f"{self._base_url}/admin/web_io/save"

        # Construct the payload based on user observations.
        device_data = {
            "web_device_id": str(device_id),
            f"name_{device_id}": webio_name,
            f"ip_{device_id}": ha_address,
            f"username_{device_id}": "",
            f"password_{device_id}": "",  # nosec B105
            f"checkca_{device_id}": "0",
            f"pinnedpubkey_{device_id}": "",
            f"form_login_{device_id}": "2",
        }

        payload = {"no_reload": "true", "JSON": json.dumps(device_data)}

        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}

        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status == 200:
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    raw_text = await resp.text()
                    _LOGGER.exception(
                        "Failed to parse Web-IO device IP update response as JSON; raw response: %s",
                        raw_text,
                    )
                    return False
                return result.get("save") == 1

            _LOGGER.error("Failed to update Web-IO device IP, HTTP status: %s", resp.status)
            return False

    async def delete_single_command(self, cmd_id: str | int, device_id: str | int) -> bool:
        """Removes an individual command instance (Delta Sync)."""
        _LOGGER.info("Deleting individual Web-IO command ID: %s", cmd_id)
        url = f"{self._base_url}/admin/web_io/delete_web_command/?id={cmd_id}&dev={device_id}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO command %s (HTTP %s)", cmd_id, resp.status)
            return resp.status == 200

    async def save_single_command(
        self,
        base_id: str | int | None,
        device_id: str | int,
        cmd_payload: dict[str, Any],
        existing_cmd_id: str | int | None = None,
    ) -> bool:
        """
        Adds or updates a single command in an existing device (Delta Sync).
        If existing_cmd_id is provided, an UPDATE is performed.

        header_modifier/post_get/authentication are read from cmd_payload (defaulting to the
        JSON-POST-no-auth shape every HA-webhook command uses) rather than hardcoded — the
        Phase 7 API-Loopback command needs PostGet=0/Authentication=1/no header modifier
        instead (see _build_knx_loopback_webio_command), and reusing this method unmodified
        for it would silently strip that combination back to the auth-less POST shape,
        reproducing the exact 401 Unauthorized bug the Phase 7 field combination fixes.
        """
        _LOGGER.info("Applying command: %s (Update: %s)", cmd_payload.get("Name"), existing_cmd_id is not None)
        url = f"{self._base_url}/admin/web_io/save_command"

        # Base64 identifier for Comexio
        if existing_cmd_id:
            # Update format exactly like original Comexio trace: {"src":"command","id":1324}
            cmd_ref = json.dumps({"src": "command", "id": int(existing_cmd_id)}, separators=(",", ":"))
            cmd_id_b64 = base64.b64encode(cmd_ref.encode()).decode()
        else:
            # New format: {"src":"command","id":null}
            cmd_id_b64 = "eyJzcmMiOiJjb21tYW5kIiwiaWQiOm51bGx9"

        payload = {
            "dlg_web_device_id": str(device_id),
            "protocol": 0,
            "parameter": cmd_payload["Parameter"],
            "header_modifier": cmd_payload.get("HeaderModifier", _CONTENT_TYPE_JSON),
            "data": cmd_payload["Data"],
            "port": "",
            "post_get": cmd_payload.get("PostGet", 1),
            "authentication": cmd_payload.get("Authentication", 0),
            "req_freq": "",
            "reply_interpreter": "",
            "id_cmd_io_0": cmd_id_b64,
            "name_cmd_io_0": cmd_payload["Name"],
            "function_cmd_io_0": "1_1_0",
            "input_cmd_io_0": 1,
            "type_cmd_io_0": cmd_payload["TypeId"],
            "send_on_one_cmd_io_0": 0,
            "min_cmd_io_0": cmd_payload["Min"],
            "max_cmd_io_0": cmd_payload["Max"],
            "default_value_cmd_io_0": "",
            "id_cmd_io_sample": "",
            "name_cmd_io_sample": "",
            "function_cmd_io_sample": "0_1_0",
            "input_cmd_io_sample": 1,
            "type_cmd_io_sample": 2,
            "send_on_one_cmd_io_sample": 0,
            "min_cmd_io_sample": 0,
            "max_cmd_io_sample": 1,
            "default_value_cmd_io_sample": "",
            "DefaultActive": 1,
        }

        if existing_cmd_id:
            payload["id"] = str(existing_cmd_id)
        else:
            payload["deviceBaseId"] = str(base_id) if base_id is not None else "0"

        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}

        # _LOGGER.debug("Sending save_single_command payload: %s", payload)

        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                await resp.text()
                # _LOGGER.debug("save_single_command response [%s]: %s", resp.status, resp_text)
                return resp.status == 200
        except aiohttp.ClientError as err:
            _LOGGER.warning("HTTP request error saving Web-IO command %s: %s", cmd_payload.get("Name"), err)
            return False
        except Exception:
            _LOGGER.exception("Unexpected error saving Web-IO command %s", cmd_payload.get("Name"))
            return False

    async def get_webio_command_range(
        self, cmd_id: str | int, device_id: str | int
    ) -> tuple[float | None, float | None]:
        """Reads a single Web-IO command's live Min/Max from its edit form.

        The bulk config scrape ($FubModules["10"], see _add_webhook_command) never returns
        Min/Max for HA's own Web-IO commands — confirmed live 2026-08-30 — so this per-command
        edit form is the only reliable source. Used by the nightly range check (see
        WEBIO_RANGE_CHECK_HOUR in const.py) to detect drift against
        WEBIO_MARKER_ANALOG_MIN/MAX. Returns (None, None) on a fetch failure or if a field is
        missing from the form.
        """
        url = f"{self._base_url}/admin/web_io/edit_command/?Id={cmd_id}&TestDevice={device_id}"
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.error("Failed to fetch Web-IO command %s edit form (HTTP %s)", cmd_id, resp.status)
                    return None, None
                html = await resp.text()
        except aiohttp.ClientError as err:
            _LOGGER.warning("HTTP request error fetching Web-IO command %s edit form: %s", cmd_id, err)
            return None, None
        except Exception:
            _LOGGER.exception("Unexpected error fetching Web-IO command %s edit form", cmd_id)
            return None, None

        values: dict[str, float | None] = {"min": None, "max": None}
        matched = False
        for tag_match in _WEBIO_CMD_INPUT_RE.finditer(html):
            matched = True
            field = tag_match[1].lower()
            if value_match := _WEBIO_CMD_VALUE_RE.search(tag_match[0]):
                try:
                    values[field] = float(value_match[1])
                except ValueError:
                    _LOGGER.warning(
                        "Web-IO command %s edit form has a non-numeric %s value: %r",
                        cmd_id,
                        field,
                        value_match[1],
                    )
        if not matched:
            _LOGGER.warning("Web-IO command %s edit form has no min/max input tags (page layout changed?)", cmd_id)
        return values["min"], values["max"]

    def build_webio_commands(
        self,
        server_id: str,
        parsed_data: dict[str, Any],
        webio_class: str | None = None,
        ignored_marker_ids: set[int] | None = None,
        ignored_knx_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the list of Web-IO command dicts for the given parsed configuration.

        webio_class restricts the result to one Web-IO class ("marker"/"io"/"knx"), for the
        bulk class-upload path (generate_webio_json); None returns all (delta-sync payload
        lookup, where the destination device is chosen separately per command).
        ignored_marker_ids / ignored_knx_ids exclude items the user configured as ignored —
        they have no HA entity, so they need no Web-IO command pushing values back via webhook.
        Returns the list directly so callers can use it without a json.dumps/json.loads roundtrip.
        """
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands: list[dict[str, Any]] = []

        # 1. Create Web-IO for markers
        markers = parsed_data.get("markers", []) if webio_class in (None, WEBIO_CLASS_MARKER) else []
        for m in markers:
            if ignored_marker_ids and int(m["id"]) in ignored_marker_ids:
                continue
            commands.append(self._build_marker_webio_command(m, webhook_path))

        # 2. Create Web-IO for IOs
        io_entries = parsed_data.get("io", []) if webio_class in (None, WEBIO_CLASS_IO) else []
        for io_item in io_entries:
            commands.append(self._build_io_webio_command(io_item, webhook_path))

        # 3. Create Web-IO for KNX objects
        knx_entries = parsed_data.get("knx", []) if webio_class in (None, WEBIO_CLASS_KNX) else []
        for knx_item in knx_entries:
            if ignored_knx_ids and int(knx_item["id"]) in ignored_knx_ids:
                continue
            commands.append(self._build_marker_webio_command(knx_item, webhook_path, source_type=WEBIO_CLASS_KNX))

        return commands

    @staticmethod
    def _lua_escape(value: Any) -> str:
        """Escape a value for embedding in a double-quoted Lua string literal."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _webio_data_lua(payload: str) -> str:
        """Build the Lua `data(a)` webhook body for a Web-IO command, given its JSON payload fields."""
        return f"function data(a)\r\n  local d = {{ {payload} }}\r\n  return json_stringify(d)\r\nend"

    @staticmethod
    def _webio_command(
        *, name: str, type_id: int, min_v: float, max_v: float, data: str, webhook_path: str
    ) -> dict[str, Any]:
        """Build a Web-IO command dict, filling in the fields shared by markers and IOs."""
        return {
            "Name": name,
            "TypeId": type_id,
            "Min": min_v,
            "Max": max_v,
            "Parameter": webhook_path,
            "HeaderModifier": _CONTENT_TYPE_JSON,
            "Data": data,
            "Protocol": 0,
            "PostGet": 1,
            "WebDeviceId": 0,
            "Authentication": 0,
            "Input": 1,
            "ReqFreq": "",
            "ReplyInterpreter": "",
            "Port": "",
            "SendOnOne": 0,
            "Changed": 1,
            "BaseId": 0,
            "DefaultValue": "",
            "DefaultActive": 1,
            "io": [],
        }

    @staticmethod
    def _knx_webio_range(dpt_min: float | None, dpt_max: float | None) -> tuple[float, float]:
        """Resolve the Min/Max to embed in an analog KNX Web-IO command from a resolved DPT range.

        Uses the real DPT range verbatim (only the pre-existing _safe_webio_range int16
        danger-zone guard still applies) rather than capping it to WEBIO_MARKER_ANALOG_MIN/MAX —
        deliberate per user decision 2026-09-20 after live-testing both ends of the scale: K8
        (DPT9.004, 0..670760) round-trips fine, K5 (DPT12.001, 0..4294967295) gets corrupted by a
        still-open Comexio firmware bug that rounds analog values above ~1,000,000 (see README for
        the documented limitation). Capping the range here would dodge that bug silently today but
        would need undoing again once Comexio ships a fix — the user chose to expose the true DPT
        range and document the caveat instead ("dann sind wir damit aus dem Boot").
        None (DPT unresolved, see _resolve_knx_dpt) falls back to the fully generic
        WEBIO_MARKER_ANALOG_MIN/MAX range, unchanged from before this method existed.
        """
        if dpt_min is None or dpt_max is None:
            return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
        return ComexioAPI._safe_webio_range(dpt_min, dpt_max)

    def _resolve_knx_loopback_range(self, k_id: int, marker_id: int) -> tuple[float, float]:
        """Resolve the Min/Max for one analog K-Element's Phase 7 loopback Web-IO command.

        Unlike _build_marker_webio_command's KNX branch (which reads dpt_min/dpt_max already
        resolved during the current poll, via _process_knx), this command is built from
        on-demand Sync-button wiring code, so it must resolve the DPT itself against whatever
        self._knx_dpt_catalog happens to hold right now — which can be None (no poll has ever
        populated it yet) or stale (comexio_version has moved on since the cached fetch; see
        get_knx_dpt_catalog's own freshness check, not applied by _resolve_knx_dpt itself).
        Both degrade to the generic WEBIO_MARKER_ANALOG_MIN/MAX range rather than risk trusting
        a stale chain, same as a genuinely-unresolvable DPT — but each case is logged
        separately (mirroring _process_knx's own split for the identical situation on the HA
        Number entity's side, found missing here in review 2026-09-20) since this path creates
        a permanent Web-IO command that nothing later re-checks against a fresher catalog.
        """
        catalog = self._knx_dpt_catalog
        catalog_fresh = catalog is not None and self._knx_dpt_catalog_version == self.comexio_version
        dpt = self._resolve_knx_dpt(catalog or {}, str(k_id)) if catalog_fresh else None
        if dpt is None:
            if catalog is None:
                reason = "catalog not yet fetched"
            elif not catalog_fresh:
                reason = "catalog stale (comexio_version changed since last fetch)"
            else:
                reason = "DPT chain resolution failed"
            _LOGGER.debug(
                "KNX loopback command K%s->M%s: could not resolve DPT (%s), using generic fallback range",
                k_id,
                marker_id,
                reason,
            )
            return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX

        dpt_range = KNX_DPT_ANALOG_RANGES.get(dpt)
        if dpt_range is None:
            _LOGGER.debug(
                "KNX loopback command K%s->M%s: resolved DPT%s.%s has no entry in "
                "KNX_DPT_ANALOG_RANGES, using generic fallback range",
                k_id,
                marker_id,
                dpt[0],
                dpt[1],
            )
            return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX

        return self._knx_webio_range(dpt_range[0], dpt_range[1])

    @staticmethod
    def _build_marker_webio_command(
        m: dict[str, Any], webhook_path: str, source_type: str = WEBIO_CLASS_MARKER
    ) -> dict[str, Any]:
        """Build the Web-IO command dict for a single marker or KNX object.

        source_type is the literal written into the webhook Lua payload's ``type=`` field
        ("marker" or "knx"); it decides which coordinator update path the pushed value hits.
        For a KNX object, m may already carry dpt_min/dpt_max (set by _process_knx) — see
        _knx_webio_range for how those replace the generic ±500,000 range with the real DPT range.
        """
        is_ana = m["type"] == "analog"
        if is_ana and source_type == WEBIO_CLASS_KNX:
            min_v, max_v = ComexioAPI._knx_webio_range(m.get("dpt_min"), m.get("dpt_max"))
        elif is_ana:
            min_v, max_v = WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
        else:
            min_v, max_v = 0, 1
        safe_id = ComexioAPI._lua_escape(m["id"])
        safe_type = ComexioAPI._lua_escape(source_type)
        lua = ComexioAPI._webio_data_lua(f'id="{safe_id}", value=a, type="{safe_type}"')
        return ComexioAPI._webio_command(
            name=f"HA {m['name']}",
            type_id=2 if is_ana else 1,
            min_v=min_v,
            max_v=max_v,
            data=lua,
            webhook_path=webhook_path,
        )

    def _build_knx_loopback_webio_command(self, *, k_id: int, marker_id: int, is_analog: bool) -> dict[str, Any]:
        """Build the Web-IO command dict for one K-Element's Phase 7 API-Loopback command.

        Unlike _webio_command's HA-webhook shape (POST, JSON body, no auth, target = HA's own
        webhook), this command GETs Comexio's OWN /api/?action=set endpoint on every change of
        the wired K-Element's output, writing the bridge Marker directly and closing the
        "Punkt 4" stuck-marker loop entirely inside Comexio (see WEBIO_CLASS_NAME_KNX_LOOPBACK
        in const.py for the full rationale). Field combination confirmed live 17.09.2026 after
        three rounds of debugging (see project_knx_write_path_design memory, "Phase 7"):
          - PostGet=0 (GET, not POST)
          - Authentication=1 ("Ja") — the actual root cause found; without it the device's
            Basic-Auth credentials (see create_webio_device) never attach to this command's
            own outgoing request, even with the class Login=3 and correct device credentials
            (401 Unauthorized).
        Min/Max: resolved via _resolve_knx_loopback_range, same DPT chain and _knx_webio_range
        logic as _build_marker_webio_command's KNX branch — see that method's own docstring for
        exactly which situations fall back to the generic WEBIO_MARKER_ANALOG_MIN/MAX range, and
        why each is logged.
        """
        min_v: float
        max_v: float
        min_v, max_v = (0, 1) if not is_analog else self._resolve_knx_loopback_range(k_id, marker_id)
        param_lua = f'function parameter(a)\r\n  return "/api/?action=set&marker=M{marker_id}&value="..a\r\nend'
        return {
            "Name": knx_loopback_command_name(k_id, marker_id),
            "TypeId": 2 if is_analog else 1,
            "Min": min_v,
            "Max": max_v,
            "Parameter": param_lua,
            "HeaderModifier": "",
            "Data": "",
            "Protocol": 0,
            "PostGet": 0,
            "WebDeviceId": 0,
            "Authentication": 1,
            "Input": 1,
            "ReqFreq": "",
            "ReplyInterpreter": "",
            "Port": "",
            "SendOnOne": 0,
            "Changed": 1,
            "BaseId": 0,
            "DefaultValue": "",
            "DefaultActive": 1,
            "io": [],
        }

    @staticmethod
    def _safe_webio_range(v_min: float, v_max: float) -> tuple[float, float]:
        """Widen a Web-IO Min/Max pair whose bounds land in the int16 clamp-bug danger zone.

        See WEBIO_INT16_DANGER_ZONE / WEBIO_MARKER_ANALOG_MIN docstrings for the underlying
        Comexio bug this guards against.
        """
        lo, hi = WEBIO_INT16_DANGER_ZONE
        if lo <= abs(v_min) <= hi or lo <= abs(v_max) <= hi:
            return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
        return v_min, v_max

    @staticmethod
    def _build_io_webio_command(io_item: dict[str, Any], webhook_path: str) -> dict[str, Any]:
        """Build the Web-IO command dict for a single physical IO."""
        is_ana = not io_item.get("is_binary", False)
        # Use the authentic min/max from the Comexio type definition, unless it lands in the
        # int16 danger zone (see _safe_webio_range). io_item["min"/"max"] can be present but
        # None (scraped $ioTypes value missing) — .get()'s default only covers a missing key,
        # so None is coerced explicitly before the abs() calls in _safe_webio_range.
        raw_min = io_item.get("min")
        raw_max = io_item.get("max")
        v_min = 0 if raw_min is None else raw_min
        default_max = 100 if is_ana else 1
        v_max = default_max if raw_max is None else raw_max
        v_min, v_max = ComexioAPI._safe_webio_range(v_min, v_max)
        safe_ext = ComexioAPI._lua_escape(io_item["ext_name"])
        safe_io_id = ComexioAPI._lua_escape(io_item["identifier"])
        lua = ComexioAPI._webio_data_lua(f'ext="{safe_ext}", io="{safe_io_id}", value=a, type="io"')
        return ComexioAPI._webio_command(
            name=f"HA IO {io_item['ext_name']} {io_item['identifier']}",
            type_id=2 if is_ana else 1,
            min_v=v_min,
            max_v=v_max,
            data=lua,
            webhook_path=webhook_path,
        )

    def generate_webio_json(
        self,
        server_id: str,
        webio_name: str,
        parsed_data: dict[str, Any],
        webio_class: str | None = None,
        ignored_marker_ids: set[int] | None = None,
        ignored_knx_ids: set[int] | None = None,
    ) -> str:
        """Generate the upload-ready JSON string for the Comexio Web-IO importer.

        webio_name here is already the class-specific name (see const.webio_class_name) —
        callers append the ' [M]'/' [IO]'/' [KNX]' suffix before calling this.
        ignored_marker_ids / ignored_knx_ids are forwarded to build_webio_commands() to
        exclude ignored items.
        """
        return json.dumps(
            {
                "data": "web_io",
                "format": 1,
                "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 2, "BaseId": 0},
                "commands": self.build_webio_commands(
                    server_id, parsed_data, webio_class, ignored_marker_ids, ignored_knx_ids
                ),
            }
        )

    async def upload_web_io(self, server_id: str, webio_name: str, web_io_json: str) -> tuple[bool, str]:
        """Uploads JSON class template."""
        url = f"{self._base_url}/admin/web_io/upload_device_settings"
        file_data = io.BytesIO(web_io_json.encode("utf-8"))
        form = aiohttp.FormData()
        form.add_field("file", file_data, filename=f"ha_{server_id}.json", content_type="application/json")
        form.add_field("set_name", webio_name)
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        async with self.session.post(url, data=form, headers=headers) as resp:
            raw_text = await resp.text()
            # TEMPORARY diagnostic (18.09.2026): a KNX class upload was reported successful here
            # (ok=True/base_id returned) yet neither the class nor its device ever showed up on the
            # live server afterward — logged unconditionally (not just on the failure branch below)
            # to see the exact body Comexio sent back on the "successful" call too. Remove once the
            # KNX Web-IO class creation gap (see create_webio_device below) is understood.
            _LOGGER.debug("upload_web_io('%s'): HTTP %s, body=%r", webio_name, resp.status, raw_text)
            if resp.status == 200:
                try:
                    result = json.loads(raw_text)
                except (json.JSONDecodeError, TypeError):
                    return False, raw_text
                if result.get("ok"):
                    return True, result.get("base_id")
            return False, raw_text

    async def create_webio_device(
        self,
        name: str,
        base_id: str | int,
        ha_address: str | None = None,
        username: str = "",
        password: str = "",  # nosec B105
    ) -> bool:
        """Creates a device instance. Automatically determines HA address if not provided.

        username/password default to empty, matching every existing HA-webhook device (which
        needs no Basic-Auth on its own commands). The Phase 7 API-Loopback device is the first
        caller to pass real credentials — gated on the Web-IO class' own Login=3 ("vom Geraet
        abhaengig") setting, see ensure_knx_loopback_webio.
        """
        if not ha_address:
            ha_address = await self.get_ha_address()

        url = f"{self._base_url}/admin/web_io/saveDeviceWindow"

        payload = {
            "name": name,
            "ip": ha_address,
            "web_device_base": base_id,
            "username": username,
            "password": password,
            "web_device_base_sample": "none",
            "identifier": "",
            "form_login": "2",
        }

        async with self.session.post(url, data=payload, headers={"X-Requested-With": "XMLHttpRequest"}) as resp:
            # TEMPORARY diagnostic (18.09.2026): see upload_web_io's comment above — this call only
            # ever checked resp.status, never the body, so a server-side rejection returned as HTTP
            # 200 (e.g. validation error, name/slot conflict) would silently read as success. Logging
            # the raw body unconditionally to find out what a real failure here actually looks like
            # before deciding how to validate it properly. Remove once that's known.
            raw_text = await resp.text()
            _LOGGER.debug(
                "create_webio_device('%s', base_id=%s): HTTP %s, body=%r", name, base_id, resp.status, raw_text
            )
            return resp.status == 200

    @staticmethod
    def _knx_loopback_class_json(commands: list[dict[str, Any]] | None = None) -> str:
        """Upload-ready JSON for the ComexioAPI Loopback Web-IO class.

        Login=3 ("vom Geraet abhaengig") is required so the device's own username/password
        (see create_webio_device) get attached as Basic-Auth to this class' commands — gated
        additionally by each individual command's own Authentication=1 field (see
        _build_knx_loopback_webio_command's docstring for why both are needed).

        commands: the full initial command set to embed right away, same bulk-upload
        pattern every per-category Marker/IO/KNX class uses (generate_webio_json ->
        upload_web_io) — see ensure_knx_loopback_webio, whose caller already knows every
        K-Element that needs a command at bootstrap time. Defaults to empty only for a
        caller with nothing to embed yet; growing an existing class afterward still goes
        through save_single_command one command at a time (button.py's Delta-Sync), same
        as every other Web-IO class.
        """
        return json.dumps(
            {
                "data": "web_io",
                "format": 1,
                "base": {"Identifier": WEBIO_CLASS_NAME_KNX_LOOPBACK, "UseCookies": 0, "Login": 3, "BaseId": 0},
                "commands": commands or [],
            }
        )

    async def ensure_knx_loopback_webio(
        self,
        api_username: str,
        api_password: str,
        bridges: list[tuple[int, int, bool]] | None = None,
    ) -> tuple[str, bool] | None:
        """Idempotently create the once-per-server ComexioAPI Loopback Web-IO class + device.

        Unlike the per-category Marker/IO/KNX classes (one per opted-in source category,
        generated via generate_webio_json/upload_web_io and pointed at HA's own webhook), this
        class/device is created ONCE per Comexio server and points back at the server's OWN
        address (self.host, not HA).

        bridges (k_id, marker_id, binary triples), if given, are embedded as the class' full
        initial command set on first creation ONLY — the same bulk-upload pattern every other
        Web-IO class uses, instead of creating an empty class and adding every command one at
        a time afterwards via save_single_command (measured live 17.09.2026: >5 min for 10
        K-Elements vs. instant bulk upload for the other classes — see
        project_knx_write_path_design memory). Ignored once the class already exists; growing
        an existing class with newly-added bridges still goes through save_single_command in
        the caller (function_plan_add_knx_bridge_loopback_pairs), same as every other class'
        Delta-Sync.

        Returns (base_id, freshly_created) on success — freshly_created tells the caller
        whether bridges was just bulk-embedded (so it must wait for the commands to become
        visible via reload rather than re-creating them one by one) or the class already
        existed beforehand. Returns None if api_username/api_password aren't configured, the
        class upload or device creation call reports failure (server-side "no", not a network
        error), the device or class check itself fails (HTTP error, timeout, unreachable host
        — both get_webio_device_info and get_webio_base_info raise rather than report
        "absent", so a flaky check can't trigger a duplicate class upload that would orphan
        the existing one's already-embedded commands), or the device is present while its
        class is not (see the inline comment where this is decided). A connection-level
        failure during the upload/create calls propagates as an exception instead of
        returning None — matching how every other Web-IO class's own
        bootstrap path (button.py's `_recreate_class`) behaves; the top-level sync handler is
        the catch-all for that case, same as for them.

        Known limitations:
        - Once the device exists, this never re-checks its username/password/ip against the
          current config — unlike WEBIO_CLASSES devices, this class isn't covered by the
          IP-mismatch/credential audit (it isn't a member of WEBIO_CLASSES), so a later
          api_username/api_password rotation or host change leaves the loopback device silently
          writing with stale credentials (401s with no repair issue raised). Tracked as an open
          Phase 7 follow-up (project_knx_write_path_design memory).
        """
        if not api_username or not api_password:
            _LOGGER.warning(
                "ensure_knx_loopback_webio: api_username/api_password not configured — cannot "
                "create the ComexioAPI Loopback Web-IO (Phase 7 KNX write-path)"
            )
            return None

        try:
            device_id = await self.get_webio_device_info(WEBIO_DEVICE_NAME_KNX_LOOPBACK)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # get_webio_device_info raises RuntimeError on a non-200 response, precisely so
            # callers don't mistake "couldn't check" for "genuinely absent" — see its own
            # docstring. Its own session.get() call is unwrapped though, so a connection
            # failure/timeout propagates as aiohttp.ClientError/TimeoutError instead — must not
            # let either escape uncaught out of a tuple[str, bool] | None-returning helper.
            _LOGGER.warning("ensure_knx_loopback_webio: device check failed: %s", err)
            return None

        try:
            base_info = await self.get_webio_base_info(WEBIO_CLASS_NAME_KNX_LOOPBACK)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # Same raise contract as get_webio_device_info above: a failed check must not be
            # read as "class absent", which would bulk-upload only *this* call's bridges as a
            # brand-new class and orphan any others the existing class already carries.
            _LOGGER.warning("ensure_knx_loopback_webio: class check failed: %s", err)
            return None
        # base_info is None now means the class is genuinely absent (a failed check raised
        # above). A device can only exist if create_webio_device below already succeeded for
        # it once, which itself requires a base_id from a prior successful class creation — so
        # a device without its class is an inconsistent server state, not a fresh install.
        # Abort rather than upload a new class next to the orphaned device.
        if device_id is not None and base_info is None:
            _LOGGER.error(
                "ensure_knx_loopback_webio: device '%s' exists but its Web-IO class '%s' was "
                "not found — aborting rather than uploading a new class next to the orphaned "
                "device",
                WEBIO_DEVICE_NAME_KNX_LOOPBACK,
                WEBIO_CLASS_NAME_KNX_LOOPBACK,
            )
            return None

        freshly_created = base_info is None
        if base_info:
            base_id, _deletable = base_info
        else:
            initial_commands = [
                self._build_knx_loopback_webio_command(k_id=k_id, marker_id=marker_id, is_analog=not binary)
                for k_id, marker_id, binary in (bridges or [])
            ]
            success, res_id = await self.upload_web_io(
                "knx_loopback", WEBIO_CLASS_NAME_KNX_LOOPBACK, self._knx_loopback_class_json(initial_commands)
            )
            if not success:
                _LOGGER.error("ensure_knx_loopback_webio: class upload failed: %s", res_id)
                return None
            base_id = res_id
            _LOGGER.info(
                "ensure_knx_loopback_webio: class '%s' created with %d initial command(s) (base_id=%s)",
                WEBIO_CLASS_NAME_KNX_LOOPBACK,
                len(initial_commands),
                base_id,
            )

        if device_id is None:
            if not await self.create_webio_device(
                WEBIO_DEVICE_NAME_KNX_LOOPBACK, base_id, self.host, username=api_username, password=api_password
            ):
                _LOGGER.error("ensure_knx_loopback_webio: device creation failed (base_id=%s)", base_id)
                return None
            _LOGGER.info(
                "ensure_knx_loopback_webio: device '%s' created @ %s", WEBIO_DEVICE_NAME_KNX_LOOPBACK, self.host
            )

        return str(base_id), freshly_created

    # --- LOGIKPLAN (FUNCTION PLAN) ---

    async def function_plan_add_element(
        self,
        fub_id: int,
        ref_id: int,
        element_type: int,
        x: float = 100.0,
        y: float = 100.0,
        connection: dict | None = None,
    ) -> int | None:
        """Place a Marker, WebIO, IO, or catalog function block on a plan canvas.

        Pass `connection` to wire the element in the same API call (skips saveconnection).
        For type=10 (WebIO): connection = {"0": {"id":"new","fub_id":...,"type":"binary|analog",
          "input":{"element":"<marker_elem_id>","pos":"0","inverted":false},
          "output":{"0":{"element":"new","pos":"0","inverted":false}}}}
        Comment (type=14) and Constant (type=16) blocks aren't backed by a catalog ref_id —
        use function_plan_add_comment_element / function_plan_add_constant_element instead.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": "",
            "ref_id": str(ref_id),
            "type": str(element_type),
            "id": "undefined",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        if connection is not None:
            payload["connection"] = json.dumps(connection, separators=(",", ":"))
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error(
                    "function_plan_add_element failed (HTTP %s, fub=%s, ref=%s, type=%s)",
                    resp.status,
                    fub_id,
                    ref_id,
                    element_type,
                )
                return None
            try:
                result = await resp.json(content_type=None)
                elem_id = result.get("id")
                if elem_id is None:
                    _LOGGER.error(
                        "function_plan_add_element: no id in response (fub=%s, ref=%s, type=%s): %s",
                        fub_id,
                        ref_id,
                        element_type,
                        result,
                    )
                    return None
                _LOGGER.debug(
                    "function_plan_add_element: fub=%s ref=%s type=%s → elem_id=%s",
                    fub_id,
                    ref_id,
                    element_type,
                    elem_id,
                )
                return int(elem_id)
            except Exception:
                _LOGGER.exception("function_plan_add_element: failed to parse response")
                return None

    async def function_plan_save_connection(
        self,
        fub_id: int,
        input_elem_id: int,
        outputs: list[tuple[int, int, bool]],
        value_type: str = "binary",
        input_pos: int = 0,
        input_inverted: bool = False,
        existing_conn_id: int | None = None,
    ) -> int | None:
        """Draw a wire (or fan-out) from input_elem (source) to one or more output elements.

        value_type: "binary" for digital, "analog" for analog.
        input_pos: source output-port index, for elements with more than one port — 0 for
        single-port elements.
        outputs: (output_elem_id, output_pos, output_inverted) per sink. Comexio models a
        fan-out as ONE connection record with multiple "output" entries, not as several
        independent connections from the same source pin — sending them separately caused
        both wires to silently vanish for IO/Constant sources (confirmed live 2026-08-22 on a
        restored copy of a real plan; catalog function-block sources tolerated it, IO/Constant
        sources did not), so every sink for a given source must be sent in a single call.
        existing_conn_id: pass the EXISTING connection's id when this call is re-saving a
        source's outputs (e.g. unioning a new sink onto ones already there) rather than
        creating a brand-new wire — omit (None) only for a source that has no connection yet.
        Sending "id":"new" for a source that already has a connection makes Comexio fold the
        new save into the existing record (same id kept, outputs correctly merged) but silently
        drop that record's "input" field entirely, leaving a connection with valid outputs but
        no recorded source — breaks both the visual wire in Comexio Studio and our own
        visualizer ("TypNone ref=?"). Confirmed live 2026-09-18 on a throwaway test plan:
        resaving with "id":"new" twice from the same source reproduced the missing-"input"
        connection 1:1; resaving the second time with the real id instead preserved it.
        Callers that read an existing connection via _function_plan_find_connection_by_source
        MUST pass its id back here.
        Returns the connection ID assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/saveconnection/"
        timestamp = _js_timestamp()
        output_dict = {
            str(i): {"element": str(dst), "pos": str(pos), "inverted": inverted}
            for i, (dst, pos, inverted) in enumerate(outputs)
        }
        # Logged verbatim below on both the error and success path — a save that resends "new"
        # for a source that already HAS a connection is exactly the corrupting case this whole
        # existing_conn_id mechanism exists to prevent, so a failure here is materially riskier
        # (see function_plan_save_connection's docstring) than a failure creating a fresh wire.
        mode = "new" if existing_conn_id is None else f"update(id={existing_conn_id})"
        conn_json = json.dumps(
            {
                "id": "new" if existing_conn_id is None else str(existing_conn_id),
                "fub_id": fub_id,
                "input": {"element": str(input_elem_id), "pos": str(input_pos), "inverted": input_inverted},
                "type": value_type,
                "output": output_dict,
            },
            separators=(",", ":"),
        )
        payload = {"JSON": conn_json, "timestamp": timestamp}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        dst_ids = [dst for dst, _pos, _inv in outputs]
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error(
                    "function_plan_save_connection failed (HTTP %s, fub=%s, %s→%s, mode=%s)",
                    resp.status,
                    fub_id,
                    input_elem_id,
                    dst_ids,
                    mode,
                )
                return None
            try:
                result = await resp.json(content_type=None)
                saved_conn_id = result.get("id")
                if saved_conn_id is None:
                    # HTTP 200 with a parseable body but no "id" — Comexio accepted the request
                    # but didn't return a connection id (e.g. a rejected/invalid save). Distinct
                    # from the except-block below (which is a genuine parse failure): log the
                    # raw body at ERROR so this is diagnosable later instead of looking identical
                    # to a normal successful save on DEBUG.
                    _LOGGER.error(
                        "function_plan_save_connection: HTTP 200 but no 'id' in response "
                        "(fub=%s, %s→%s, mode=%s, body=%r)",
                        fub_id,
                        input_elem_id,
                        dst_ids,
                        mode,
                        result,
                    )
                    return None
                _LOGGER.debug(
                    "function_plan_save_connection: fub=%s %s→%s mode=%s conn_id=%s",
                    fub_id,
                    input_elem_id,
                    dst_ids,
                    mode,
                    saved_conn_id,
                )
                return int(saved_conn_id)
            except Exception:
                _LOGGER.exception("function_plan_save_connection: failed to parse response (mode=%s)", mode)
                return None

    async def function_plan_save_elements_pos(self, positions: list[tuple[int, float, float]]) -> bool:
        """Reposition multiple function plan elements in one call.

        positions: list of (fubElementId, x, y) tuples.
        Returns True on success.
        """
        url = f"{self._base_url}/admin/function_function_module/saveelementspos/"
        timestamp = _js_timestamp()
        pos_dict = {str(i): {"x": x, "y": y, "id": elem_id} for i, (elem_id, x, y) in enumerate(positions)}
        payload = {
            "Json": json.dumps(pos_dict, separators=(",", ":")),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("function_plan_save_elements_pos: repositioning %d elements", len(positions))
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_save_elements_pos failed (HTTP %s)", resp.status)
                return False
            try:
                result = await resp.json(content_type=None)
                success = result.get("result") == 1
                _LOGGER.info("function_plan_save_elements_pos: result=%s (raw: %s)", success, result)
                return success
            except Exception:
                _LOGGER.exception("function_plan_save_elements_pos: failed to parse response")
                return False

    async def function_plan_delete_elements(self, elem_ids: list[int]) -> bool:
        """Delete elements from a function plan (removes elements + their connections).

        elem_ids: list of fubElementId integers to delete.
        Returns True on success.
        """
        url = f"{self._base_url}/admin/function_function_module/deleteelements/"
        timestamp = _js_timestamp()
        payload = {
            "Json": json.dumps([str(eid) for eid in elem_ids]),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("function_plan_delete_elements: %d Elemente löschen: %s", len(elem_ids), elem_ids)
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_delete_elements failed (HTTP %s)", resp.status)
                    return False
                try:
                    result = await resp.json(content_type=None)
                    success = result.get("delete") is True
                    _LOGGER.info("function_plan_delete_elements: result=%s", success)
                    return success
                except Exception:
                    _LOGGER.exception("function_plan_delete_elements: failed to parse response")
                    return False
        except aiohttp.ClientError:
            _LOGGER.exception("function_plan_delete_elements: HTTP request error")
            return False

    async def delete_marker(self, marker_id: int) -> bool | None:
        """Delete a Marker directly from Comexio's marker list (not a function plan element).

        POSTs to delete_element/ with elementId=<marker_id>, type=<Marker's fub_module_type,
        "2">, full=true. Tri-state return so the caller (marker_delete service) can tell a
        "nothing changed" outcome apart from a genuine request failure — collapsing both to
        one bool would let a session/HTTP/parse failure be misreported as "marker already
        absent" for what is an irreversible action:
        - True: {"result": "1"} — deleted.
        - False: request completed (HTTP 200, valid JSON object) but result wasn't "1" — most
          often because the marker id doesn't (or no longer) exist, but the server could also
          be reporting a rejection this way; the caller cross-checks against a fresh presence
          lookup (get_marker_delete_eligibility's third return value) to tell those apart
          rather than assuming this is always the harmless case.
        - None: the request itself failed (non-200, unparsable or non-object JSON body,
          transport error, timeout) — a real failure, must NOT be reported as "already absent".
        """
        url = f"{self._base_url}/admin/function_function_module/delete_element/"
        payload = {
            "elementId": str(marker_id),
            "type": source_category(WEBIO_CLASS_MARKER).fub_module_type,
            "full": "true",
            "timestamp": _js_timestamp(),
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("delete_marker: HTTP %s deleting marker_id=%s", resp.status, marker_id)
                    return None
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    _LOGGER.exception("delete_marker: failed to parse response for marker_id=%s", marker_id)
                    return None
                if not isinstance(result, dict):
                    _LOGGER.error("delete_marker: unexpected response shape for marker_id=%s: %r", marker_id, result)
                    return None
                success = str(result.get("result")) == "1"
                _LOGGER.info("delete_marker: marker_id=%s result=%s", marker_id, success)
                return success
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.exception("delete_marker: HTTP request error deleting marker_id=%s", marker_id)
            return None

    async def get_marker_delete_eligibility(
        self, marker_ids: list[int], force: bool = False
    ) -> tuple[list[int], list[int], set[int], str | None]:
        """Split marker_ids into (deletable, protected, known_ids, gate_error) via a fresh config lookup.

        Safety gate for marker_delete: by default only markers this integration created itself
        via the API carry CategoryId==1 and may be deleted. CategoryId==0 covers both
        factory-provisioned markers (M1 "System rebooted", M2 "TRUE", M3 "FALSE") and anything
        a human created via Comexio Studio — protected. With force=True a CategoryId==0 marker
        becomes deletable too, but only if it has no title AND is not placed in any function
        plan (checked against freshly loaded plans — see _classify_marker_delete_ids). If not
        every plan can be loaded, force grants nothing: placement would be unknown.

        Protected ids no longer abort the batch — the caller deletes the deletable ones and
        reports the protected ones. A marker_id absent from the current config entirely is
        deletable (delete_marker then reports it as "already absent").

        A config fetch or parse failure (see _parse_marker_records) refuses every requested id
        rather than defaulting them all to deletable — a blind failure must never silently open
        this gate for an irreversible action.

        The third return value, `known_ids`, is the set of ids actually found in this lookup —
        the caller uses it to tell a delete_marker "False" result for a confirmed-absent id
        (harmless) apart from one for an id we just saw present (suspicious). The fourth,
        `gate_error`, names a load failure that made the gate stricter than requested (config
        unreadable, or force ignored because the plans couldn't be verified) — None otherwise,
        so the caller can tell that apart from genuine protection.
        """
        try:
            conf = await self.get_raw_config()
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.exception("get_marker_delete_eligibility: config fetch failed — refusing all ids")
            return [], list(marker_ids), set(), _MARKER_CONFIG_UNREADABLE
        records = self._parse_marker_records(conf)
        if records is None:
            return [], list(marker_ids), set(), _MARKER_CONFIG_UNREADABLE
        placed_ids = await self._placed_marker_ids_for_force(conf) if force else None
        gate_error = _FORCE_IGNORED if force and placed_ids is None else None
        deletable, protected = _classify_marker_delete_ids(marker_ids, records, placed_ids)
        return deletable, protected, set(records), gate_error

    async def _placed_marker_ids_for_force(self, conf: dict) -> set[int] | None:
        """Marker ids placed in any function plan, from a fresh load of EVERY plan, or None.

        None (force grants nothing) whenever the plan list or any single plan can't be loaded —
        a missing plan would make the markers placed in it look unplaced.
        """
        fubs = conf.get("Fubs")
        if not isinstance(fubs, dict):
            _LOGGER.error("get_marker_delete_eligibility: no plan list in config — force ignored")
            return None
        all_plans = await self._load_all_plans_verified(fubs, strict=True)
        if all_plans is None:
            _LOGGER.error("get_marker_delete_eligibility: not every plan could be loaded — force ignored")
            return None
        placed = _placed_marker_ids_strict(all_plans)
        if placed is None:
            _LOGGER.error("get_marker_delete_eligibility: unreadable marker reference in a plan — force ignored")
        return placed

    @staticmethod
    def _marker_group_items(group: Any) -> list[Any] | None:
        """Normalize FubModules["2"] into a flat list of marker records, or None if unusable.

        The group is a dict keyed by Id under normal conditions, but a gap-free 0-based
        group can come back from Comexio as a JSON array instead (see the WebIO Command-Group
        array quirk elsewhere in this file) — accept list/tuple too. A missing group (None)
        is not itself malformed, just empty. Any other shape (e.g. a bare scalar) is refused
        rather than crashing on `list(group)`.
        """
        if isinstance(group, dict):
            return list(group.values())
        if isinstance(group, (list, tuple)):
            return list(group)
        if group is None:
            return []
        _LOGGER.error(
            "get_marker_delete_eligibility: malformed marker group (%s) — refusing all ids",
            type(group).__name__,
        )
        return None

    @staticmethod
    def _parse_marker_records(conf: dict) -> dict[int, dict[str, Any]] | None:
        """Extract {marker_id: marker record} from a raw config dict, or None if unusable.

        Split out of get_marker_delete_eligibility to keep that function's cognitive
        complexity in check (SonarQube S3776) — this is a self-contained parse step with
        the same all-or-nothing fail-closed contract as the rest of that gate: any
        malformed shape anywhere in the marker group (bad FubModules type, non-iterable
        group, a non-dict record, an unparseable or duplicate Id) returns None rather
        than a partial dict, which the caller treats identically to a config fetch
        failure — refusing every requested id rather than guessing from incomplete data.
        """
        if not isinstance(conf, dict) or not isinstance(conf.get("FubModules"), dict):
            _LOGGER.error("get_marker_delete_eligibility: malformed config response — refusing all ids")
            return None
        items = ComexioAPI._marker_group_items(conf["FubModules"].get("2"))
        if items is None:
            return None
        if not items:
            _LOGGER.error("get_marker_delete_eligibility: no marker config available — refusing all ids")
            return None
        records: dict[int, dict[str, Any]] = {}
        for m in items:
            if not isinstance(m, dict):
                # A non-dict entry (e.g. a bare int/string/null from a scraping regression)
                # can't be read for an Id either, so a requested marker_id that belonged to it
                # would just as silently fall through to the absent-id default — refuse the
                # whole batch here too rather than dropping it unnoticed.
                _LOGGER.error("get_marker_delete_eligibility: non-dict marker record %r — refusing all ids", m)
                return None
            item_id = ComexioAPI._parse_plausible_marker_id(m.get("Id"))
            if item_id is None:
                # A record with an Id we can't parse can't be entered into `records` at all —
                # dropping it with just a warning and moving on would let a requested id that
                # actually belongs to THIS record fall through the absent-id default
                # and be treated as "already deleted, so deletable" purely because we couldn't
                # read its real (possibly CategoryId==0, protected) identity. There's no way to
                # know in advance whether the unparseable record was for one of the requested
                # ids or an unrelated one, so — same all-or-nothing posture as every other
                # malformed-data case here — any single unparseable Id refuses the whole batch.
                _LOGGER.error(
                    "get_marker_delete_eligibility: marker record with non-numeric Id %r — refusing all ids",
                    m.get("Id"),
                )
                return None
            if item_id in records:
                # Two marker records resolving to the same Id (malformed config / upstream
                # parsing regression) would otherwise let whichever one is iterated last win —
                # if a protected CategoryId==0 record is silently overwritten by a duplicate
                # CategoryId==1 record for the same Id, the protected marker ends up classified
                # as deletable. Can't tell which of the two (if either) is the real record, so
                # — same all-or-nothing posture as every other malformed-data case here — a
                # duplicate Id refuses the whole batch rather than picking one silently.
                _LOGGER.error("get_marker_delete_eligibility: duplicate marker Id %r — refusing all ids", item_id)
                return None
            records[item_id] = m
        if not records:
            _LOGGER.error("get_marker_delete_eligibility: no marker had a usable Id — refusing all ids")
            return None
        return records

    @staticmethod
    def _parse_plausible_marker_id(raw_id: Any) -> int | None:
        """Parse a marker record's raw Id field into a plain int, or None if unparseable.

        int() also accepts bools (True -> 1) and truncates fractional floats (1.9 -> 1),
        either of which would silently alias a malformed record onto a real marker id and
        let its CategoryId override that real marker's protection — only a genuine integer
        or a plain ASCII decimal-digit string is accepted (str.isdecimal() alone also
        passes non-ASCII digits, e.g. Arabic-Indic "١٢٣", which int() happily aliases the
        same way). int() on a non-finite float (e.g. a JSON "Infinity" literal, which
        json.loads accepts) raises OverflowError, and on a 4300+ digit string raises
        ValueError via CPython's integer string conversion limit — neither is a case this
        gate may crash on, so int() itself stays wrapped below rather than assumed safe
        just because the shape check passed.
        """
        is_plausible_id = isinstance(raw_id, int) or (
            isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 10
        )
        if isinstance(raw_id, bool) or not is_plausible_id:
            return None
        try:
            return int(raw_id)
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _keyed_by_list_position(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Re-key a list-shaped loadelements collection back into an {id: item} dict.

        For connections (no "id" field of their own, see the caller below), the resulting key
        IS the real connection id — PHP only serializes an associative array as a JSON list when
        its keys are exactly 0..n-1, so enumerate() here reproduces the server's original ids
        1:1. This used to matter only for display; since Bug #2's fix (2026-09-18) that key is
        also fed straight back into function_plan_save_connection's existing_conn_id (see
        _function_plan_find_connection_by_source) — a future change here that broke this
        invariant would silently start corrupting connections instead of just mislabeling them
        (code-reviewer finding, 2026-09-18).
        """
        return {str(item.get("id", i)): item for i, item in enumerate(items)}

    async def function_plan_load_elements(self, fub_id: int, strict: bool = False) -> dict | None:
        """Load elements and connections for a function plan (GET loadelements).

        Returns dict with 'elements' and 'connections' keys, or None on failure. strict=True
        also treats a payload without a real elements collection as a failure instead of an
        empty plan (see _plan_payload_has_elements).
        """
        url = f"{self._base_url}/admin/function_function_module/loadelements/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.get(url, params={"fubid": fub_id}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_load_elements failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return None
                data = await resp.json(content_type=None)
                if strict and not _plan_payload_has_elements(data):
                    _LOGGER.error("function_plan_load_elements fub=%s: payload without elements", fub_id)
                    return None
                # Comexio's PHP backend serializes an associative array as a JSON array
                # (not object) whenever its keys happen to be sequential integers from 0 —
                # a shape coincidence, not a signal that the collection is empty. A small,
                # rarely-edited plan's connection ids can easily stay sequential, so treating
                # "is a list" as "is empty" (the old assumption here) silently discarded real
                # elements/connections. Re-key by each item's own "id" when present (elements
                # carry one); connections don't, so fall back to the list position.
                elements = data.get("elements")
                data["elements"] = (
                    self._keyed_by_list_position(elements) if isinstance(elements, list) else (elements or {})
                )
                connections = data.get("connections")
                data["connections"] = (
                    self._keyed_by_list_position(connections) if isinstance(connections, list) else (connections or {})
                )
                elem_count = len(data.get("elements", {}))
                conn_count = len(data.get("connections", {}))
                _LOGGER.info(
                    "function_plan_load_elements fub=%s: %d Elemente, %d Verbindungen", fub_id, elem_count, conn_count
                )
                return data
        except Exception:
            _LOGGER.exception("function_plan_load_elements fub_id=%s failed", fub_id)
            return None

    async def function_plan_load_all_plans(self, strict: bool = False) -> dict[int, dict]:
        """Load elements and connections for ALL known function plans in one bulk request.

        Uses the loadallelements endpoint (bulk variant of loadelements) instead of one
        request per plan — Comexio serializes requests server-side anyway, so N sequential
        per-plan calls gain nothing over a single bulk call. Result is filtered down to the
        fub list cached by parse_config (self._fub_data). strict=True drops entries without a
        real elements collection instead of treating them as empty plans.
        Returns {fub_id: {"elements": {...}, "connections": {...}}}.
        """
        fub_ids = {int(fid) for fid in self._fub_data}
        if not fub_ids:
            _LOGGER.warning("function_plan_load_all_plans: self._fub_data is empty — nothing to load")
            return {}

        url = f"{self._base_url}/admin/function_function_module/loadallelements"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        t_start = time.monotonic()
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_load_all_plans failed (HTTP %s)", resp.status)
                    return {}
                raw = await resp.json(content_type=None)
        except Exception:
            _LOGGER.exception("function_plan_load_all_plans failed")
            return {}

        if not isinstance(raw, dict):
            _LOGGER.error("function_plan_load_all_plans: unexpected response shape (%s)", type(raw).__name__)
            return {}

        plans: dict[int, dict] = {}
        for fid_str, data in raw.items():
            try:
                fid = int(fid_str)
                if fid not in fub_ids:
                    continue
                if strict and not _plan_payload_has_elements(data):
                    _LOGGER.warning("function_plan_load_all_plans: entry fid=%s without elements — dropped", fid)
                    continue
                elements = data.get("elements")
                data["elements"] = (
                    self._keyed_by_list_position(elements) if isinstance(elements, list) else (elements or {})
                )
                connections = data.get("connections")
                data["connections"] = (
                    self._keyed_by_list_position(connections) if isinstance(connections, list) else (connections or {})
                )
                plans[fid] = data
            except (ValueError, TypeError, AttributeError):
                _LOGGER.exception("function_plan_load_all_plans: skipping malformed entry fid=%r", fid_str)
                continue

        duration = time.monotonic() - t_start
        _LOGGER.info(
            "function_plan_load_all_plans: %d/%d plans loaded in %.2fs (bulk request)",
            len(plans),
            len(fub_ids),
            duration,
        )
        return plans

    async def function_plan_stop_fup(self, fub_id: int) -> bool:
        """Stop/pause a function plan (stop_fup)."""
        url = f"{self._base_url}/admin/function_function_module/stop_fup/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(url, data={"id": str(fub_id)}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_stop_fup failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                result = await resp.json(content_type=None)
                success = result.get("result") is True
                _LOGGER.info("function_plan_stop_fup: fub=%s result=%s state=%s", fub_id, success, result.get("state"))
                return success
        except Exception:
            _LOGGER.exception("function_plan_stop_fup: fub_id=%s failed", fub_id)
            return False

    async def function_plan_add_comment_element(
        self,
        fub_id: int,
        text: str,
        x: float = 100.0,
        y: float = 7.5,
    ) -> int | None:
        """Place a text/comment block (type=14, ref_id=3) on a function plan canvas.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": text,
            "ref_id": "3",
            "type": "14",
            "id": "0",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_add_comment_element failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return None
                try:
                    result = await resp.json(content_type=None)
                    elem_id = result.get("id")
                    if elem_id is None:
                        _LOGGER.error(
                            "function_plan_add_comment_element: no id in response (fub=%s): %s", fub_id, result
                        )
                        return None
                    _LOGGER.debug("function_plan_add_comment_element: fub=%s → elem_id=%s", fub_id, elem_id)
                except Exception:
                    _LOGGER.exception("function_plan_add_comment_element: failed to parse response")
                    return None
        except aiohttp.ClientError:
            _LOGGER.exception("function_plan_add_comment_element: HTTP request error (fub=%s)", fub_id)
            return None
        # add_element has no width parameter — the width lives in the comment
        # properties dialog, saved via a separate endpoint.
        await self._function_plan_set_comment_width(int(elem_id), text)
        return elem_id

    async def function_plan_add_constant_element(
        self,
        fub_id: int,
        value: str,
        x: float = 100.0,
        y: float = 100.0,
    ) -> int | None:
        """Place a Constant block (type=16) on a function plan canvas.

        The server always normalizes a saved Constant's reference.ref_id back to 0 (see
        function_plan_catalog.py's docstring — $FubModules["16"] is empty, nothing to
        reference), but the *create* call itself rejects ref_id="0" with
        {"error": "data faulty"} — confirmed live 2026-08-22 via a throwaway test plan.
        Any positive placeholder (ref_id="1") is accepted and gets normalized away.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": value,
            "ref_id": "1",
            "type": "16",
            "id": "undefined",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_add_constant_element failed (HTTP %s, fub=%s)", resp.status, fub_id)
                return None
            try:
                result = await resp.json(content_type=None)
                elem_id = result.get("id")
                if elem_id is None:
                    _LOGGER.error("function_plan_add_constant_element: no id in response (fub=%s): %s", fub_id, result)
                    return None
                _LOGGER.debug("function_plan_add_constant_element: fub=%s → elem_id=%s", fub_id, elem_id)
                return int(elem_id)
            except Exception:
                _LOGGER.exception("function_plan_add_constant_element: failed to parse response")
                return None

    async def _function_plan_set_comment_width(self, elem_id: int, text: str, width: int = 5) -> bool:
        """Set a comment element's text width via savefupcommentelement (5 = 'Sehr Breit')."""
        url = f"{self._base_url}/admin/function_function_module/savefupcommentelement/"
        payload = {
            "id": str(elem_id),
            "use_base_64": "1",
            "name": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "width": str(width),
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.warning("savefupcommentelement failed (HTTP %s, elem=%s)", resp.status, elem_id)
                    return False
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    _LOGGER.exception("savefupcommentelement: failed to parse response (elem=%s)", elem_id)
                    return False
        except aiohttp.ClientError:
            _LOGGER.exception("savefupcommentelement: HTTP request error (elem=%s)", elem_id)
            return False
        if result.get("result") != 1:
            _LOGGER.warning("savefupcommentelement rejected (elem=%s): %s", elem_id, result)
            return False
        _LOGGER.debug("savefupcommentelement: elem=%s width=%s → %s", elem_id, width, result.get("data"))
        return True

    async def create_fup(
        self,
        plan_name: str,
        plan_comment: str = "",
        paper_format: str = "A4",
        orientation: str = "landscape",
        dpi: int = 90,
    ) -> int | None:
        """Create a new function plan. Returns the new fub_id on success, None on failure.

        Args:
            plan_name: Name of the new plan
            plan_comment: Optional comment/description
            paper_format: Paper size (A3, A4, A5; defaults to A4)
            orientation: 'landscape' or 'portrait' (defaults to landscape)
            dpi: Resolution in dots per inch, 45-120 (defaults to 90)

        Steps:
        1. Check uniqueness via /admin/_helper/isunique
        2. POST to /admin/function_function_module/save_fub
        3. Verify plan was created by checking the response redirect
        """
        # Step 1: Unique check
        url_check = f"{self._base_url}/admin/_helper/isunique"
        try:
            async with self.session.post(url_check, data={"model": "fub", "field": "name", "value": plan_name}) as resp:
                if resp.status != 200:
                    _LOGGER.error("create_fup: uniqueness check failed (HTTP %s)", resp.status)
                    return None
                result = await resp.json(content_type=None)
                if not result.get("result"):
                    _LOGGER.error("create_fup: plan name '%s' already exists", plan_name)
                    return None
        except Exception:
            _LOGGER.exception("create_fup: uniqueness check failed")
            return None

        # Step 2: Create the plan
        paper_map = {"A3": "2", "A4": "3", "A5": "4"}
        paper_id = paper_map.get(paper_format.upper(), "3")
        orient_id = "1" if orientation.lower() == "portrait" else "0"

        url_create = f"{self._base_url}/admin/function_function_module/save_fub"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        payload = {
            "fub_position": "-1",
            "fub_type": "1",
            "fub_page_count_x": "1",
            "fub_page_count_y": "1",
            "fub_name": plan_name,
            "fub_comment": plan_comment,
            "fub_active": "0",
            "fub_reset_on_close": "0",
            "fub_paper": paper_id,
            "fub_orientation": orient_id,
            "fub_resolution": str(dpi),
            "fub_create": "Erzeugen",
        }

        try:
            async with self.session.post(url_create, data=payload, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("create_fup: save_fub failed (HTTP %s)", resp.status)
                    return None

                redirect_location = resp.headers.get("Location", "")
                if "added=1" not in redirect_location:
                    _LOGGER.error("create_fup: redirect missing 'added=1' (location: %s)", redirect_location)
                    return None

                _LOGGER.info("create_fup: plan '%s' created successfully (redirect: %s)", plan_name, redirect_location)
        except Exception:
            _LOGGER.exception("create_fup: save_fub request failed")
            return None

        # Step 3: Verify plan was created by reloading config and checking $Fubs
        # (same key parse_config() uses for _fub_data — "FubModules" holds
        # markers/IOs, not plans, and would never see the new entry here)
        try:
            raw_config = await self.get_raw_config()
            fub_data = raw_config.get("Fubs", {})
            for fub_id_str, fub_info in fub_data.items():
                if fub_info.get("Name") == plan_name:
                    new_fub_id = int(fub_id_str)
                    _LOGGER.info("create_fup: verification successful, new fub_id=%s", new_fub_id)
                    # Update internal _fub_data
                    if not hasattr(self, "_fub_data"):
                        self._fub_data = {}
                    self._fub_data[fub_id_str] = fub_info
                    return new_fub_id
            _LOGGER.error("create_fup: verification failed — plan '%s' not found in $Fubs after creation", plan_name)
            return None
        except Exception:
            _LOGGER.exception("create_fup: verification (config reload) failed")
            return None

    async def function_plan_update_paper(
        self, fub_id: int, paper_format: str, dpi: int, orientation: str, name: str | None = None
    ) -> bool:
        """Update an EXISTING plan's paper format/DPI/orientation (same save_fub endpoint as
        create_fup, but with fub_id set and fub_save='Speichern' instead of fub_create).

        Needed before an in-place restore whose snapshot's canvas settings differ from the
        live plan's current ones (e.g. force_override onto an unrelated plan) — otherwise
        element positions computed for the snapshot's original canvas can end up clipped or
        overlapping on the live plan's (different) canvas.

        name: if given, also renames the plan (force_override restores the snapshot's
        original name too, so the plan comes back exactly as it was — not just its content).
        None keeps the current live name unchanged.

        All other plan properties (comment, position, active state) are read from the
        current live data and passed through UNCHANGED. Known gap: Comexio's $Fubs dump does
        not expose "reset on close", so that flag is always sent as "0" (Comexio's own
        create-time default) rather than preserved — a cosmetic Comexio Studio setting this
        integration doesn't otherwise manage.
        """
        fub = self._fub_data.get(str(fub_id))
        if fub is None:
            _LOGGER.error("function_plan_update_paper: fub_id=%s not found in live data", fub_id)
            return False

        paper_map = {"A3": "2", "A4": "3", "A5": "4"}
        paper_id = paper_map.get(paper_format.upper(), "3")
        orient_id = "1" if orientation.lower() == "portrait" else "0"
        target_name = name if name is not None else fub.get("Name", "")

        url = f"{self._base_url}/admin/function_function_module/save_fub"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        payload = {
            "fub_id": str(fub_id),
            "fub_position": str(fub.get("Position", "-1")),
            "fub_type": "1",
            "fub_page_count_x": "1",
            "fub_page_count_y": "1",
            "fub_name": target_name,
            "fub_comment": fub.get("Comment", ""),
            "fub_active": str(int(bool(fub.get("Active", False)))),
            "fub_reset_on_close": "0",
            "fub_paper": paper_id,
            "fub_orientation": orient_id,
            "fub_resolution": str(dpi),
            "fub_save": "Speichern",
        }

        try:
            async with self.session.post(url, data=payload, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("function_plan_update_paper: save_fub failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                redirect_location = resp.headers.get("Location", "")
                if "saved=1" not in redirect_location:
                    _LOGGER.error(
                        "function_plan_update_paper: redirect missing 'saved=1' (fub=%s, location: %s)",
                        fub_id,
                        redirect_location,
                    )
                    return False
        except Exception:
            _LOGGER.exception("function_plan_update_paper: save_fub request failed (fub=%s)", fub_id)
            return False

        # Keep the local cache in sync so get_fub_paper_format/dpi/orientation reflect the change
        fub["Paper"] = paper_id
        fub["Resolution"] = dpi
        fub["Orientation"] = int(orient_id)
        if name is not None:
            fub["Name"] = name
        _LOGGER.info(
            "function_plan_update_paper: fub=%s -> paper=%s dpi=%s orientation=%s name=%s",
            fub_id,
            paper_format,
            dpi,
            orientation,
            name,
        )
        return True

    async def create_marker(self, binary: bool) -> int | None:
        """Create a new marker ('flag') and return its server-assigned numeric ID.

        Wraps POST /admin/flag/add/. The new marker starts unlabeled (empty Name,
        default value 0) — use rename_marker() afterwards to give it a title.

        IMPORTANT: the server assigns the new ID strictly sequentially (next free
        integer) — there is no way to request a specific target ID (confirmed live
        2026-09-14). Callers that need a specific ID (e.g. to align a block of
        bridge markers on a round boundary) must consume IDs one at a time via
        repeated calls until the desired ID comes back.

        binary: True for a digital marker (type=1), False for analog (type=2).

        Returns the new marker's Id, or None on failure.
        """
        url = f"{self._base_url}/admin/flag/add/"
        payload = {"type": "1" if binary else "2"}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/flag/home",
        }
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("create_marker: flag/add failed (HTTP %s, binary=%s)", resp.status, binary)
                    return None
                result = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("create_marker: HTTP request error (binary=%s): %s", binary, err)
            return None
        except Exception:
            _LOGGER.exception("create_marker: failed to parse response (binary=%s)", binary)
            return None

        if not result.get("ok"):
            _LOGGER.error("create_marker: server rejected flag/add (binary=%s): %s", binary, result)
            return None
        marker_id = result.get("saved")
        if marker_id is None:
            _LOGGER.error("create_marker: no 'saved' id in response (binary=%s): %s", binary, result)
            return None
        _LOGGER.info("create_marker: created marker M%s (binary=%s)", marker_id, binary)
        return int(marker_id)

    async def rename_marker(self, marker_id: int, name: str, binary: bool) -> bool:
        """Set an existing marker's title via Comexio's own save flow.

        Mirrors what Comexio's own admin UI does when saving a marker:
        1. POST /admin/_helper/isunique to check the name isn't already taken.
        2. POST /admin/flag/saveOne with the full form payload — Comexio's saveOne
           looks like a full form save rather than a title-only patch, so
           default/store_memory/value_<id> must be sent along even though we only
           intend to change the name.

        Only safe to call on a marker whose current state is already known (e.g.
        one just created via create_marker()). Do NOT call this on a pre-existing
        user marker without first reading its live default/store_memory values —
        this would silently reset them to the values sent here.

        binary: True for digital (type=1), False for analog (type=2) — must match
        the marker's actual type; it is not looked up here.

        Returns True on success.
        """
        marker_type = "1" if binary else "2"

        url_check = f"{self._base_url}/admin/_helper/isunique"
        check_payload = {"model": "memory", "field": "name", "value": name, "id": str(marker_id)}
        try:
            async with self.session.post(url_check, data=check_payload) as resp:
                if resp.status != 200:
                    _LOGGER.error("rename_marker: isunique check failed (HTTP %s, id=%s)", resp.status, marker_id)
                    return False
                result = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("rename_marker: isunique HTTP error (id=%s): %s", marker_id, err)
            return False
        except Exception:
            _LOGGER.exception("rename_marker: isunique request failed (id=%s)", marker_id)
            return False

        if not result.get("result"):
            _LOGGER.error("rename_marker: name '%s' already in use (id=%s)", name, marker_id)
            return False

        url_save = f"{self._base_url}/admin/flag/saveOne"
        save_payload = {
            "id": str(marker_id),
            "default_default": "0",
            "default_type": marker_type,
            "name": name,
            "type": marker_type,
            f"value_{marker_id}": "0",
            "default": "0",
            "store_memory": "0",
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/flag/home",
        }
        try:
            async with self.session.post(url_save, data=save_payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("rename_marker: saveOne failed (HTTP %s, id=%s)", resp.status, marker_id)
                    return False
                result = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("rename_marker: saveOne HTTP error (id=%s): %s", marker_id, err)
            return False
        except Exception:
            _LOGGER.exception("rename_marker: saveOne request failed (id=%s)", marker_id)
            return False

        if str(result.get("saved")) != str(marker_id):
            _LOGGER.error("rename_marker: saveOne response mismatch (id=%s): %s", marker_id, result)
            return False

        _LOGGER.info("rename_marker: M%s renamed to '%s' (binary=%s)", marker_id, name, binary)
        return True

    async def rename_knx_object(self, k_id: str | int, name: str) -> bool:
        """Set an existing KNX object's ("K-Element") title via Comexio's own save flow.

        Mirrors rename_marker but targets the KNX one-wire object endpoint instead:
        1. POST /admin/_helper/isunique (model=oneWire) to check the name isn't already taken.
        2. POST /admin/knx_one_wire/saveKnx/ with field=name&value=<name> — unlike
           flag/saveOne this is a genuine single-field patch, not a full-form save, so no
           other K-Element state needs to be read/resent first.

        Only ever called with the existing title plus a trailing "[RO]"/"[TRIG]" suffix
        (see coordinator._auto_suffix_unambiguous_knx / _audit_knx_dpt_ambiguous), never a
        full rename — collisions should be rare in practice, but this still guards against
        one exactly like Comexio's own admin UI would.

        Returns True on success.
        """
        url_check = f"{self._base_url}/admin/_helper/isunique"
        check_payload = {"model": "oneWire", "field": "name", "value": name, "id": str(k_id)}
        try:
            async with self.session.post(url_check, data=check_payload) as resp:
                if resp.status != 200:
                    _LOGGER.error("rename_knx_object: isunique check failed (HTTP %s, id=%s)", resp.status, k_id)
                    return False
                result = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("rename_knx_object: isunique HTTP error (id=%s): %s", k_id, err)
            return False
        except Exception:
            _LOGGER.exception("rename_knx_object: isunique request failed (id=%s)", k_id)
            return False

        if not isinstance(result, dict) or not result.get("result"):
            _LOGGER.error("rename_knx_object: name '%s' already in use (id=%s): %s", name, k_id, result)
            return False

        url_save = f"{self._base_url}/admin/knx_one_wire/saveKnx/"
        save_payload = {"id": str(k_id), "field": "name", "value": name}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/knx_one_wire/home",
        }
        try:
            async with self.session.post(url_save, data=save_payload, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("rename_knx_object: saveKnx failed (HTTP %s, id=%s)", resp.status, k_id)
                    return False
                result = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("rename_knx_object: saveKnx HTTP error (id=%s): %s", k_id, err)
            return False
        except Exception:
            _LOGGER.exception("rename_knx_object: saveKnx request failed (id=%s)", k_id)
            return False

        if not isinstance(result, dict) or str(result.get("Ok")) != "1":
            _LOGGER.error("rename_knx_object: saveKnx response mismatch (id=%s): %s", k_id, result)
            return False

        _LOGGER.info("rename_knx_object: K%s renamed to '%s'", k_id, name)
        return True

    @staticmethod
    def _highest_marker_id(fub_modules: dict[str, Any], *, only_original_titled: bool = False) -> int:
        """Highest marker Id currently present in $FubModules["2"].

        only_original_titled=True restricts to 'original' (CategoryId==0, factory-
        provisioned) markers that also carry a real title — used to find the KNX bridge
        block's boundary basis (see _next_block_boundary). False (default) considers every
        marker regardless of category/title, including ones this integration itself created
        (CategoryId==1) — used to know how many ids are actually already consumed.
        """
        group = fub_modules.get("2")
        items = group.values() if isinstance(group, dict) else (group or [])
        highest = 0
        for m in items:
            if not isinstance(m, dict):
                continue
            marker_id = m.get("Id")
            if not isinstance(marker_id, int):
                continue
            if only_original_titled and (m.get("CategoryId", 0) != 0 or not m.get("Name")):
                continue
            highest = max(highest, marker_id)
        return highest

    @staticmethod
    def _next_block_boundary(highest_id: int, block_size: int = MARKER_KNX_BRIDGE_BLOCK_SIZE) -> int:
        """Round up to the next multiple of block_size strictly greater than highest_id.

        E.g. 252 -> 300, 201 -> 250, 250 -> 300 — a marker sitting exactly AT a boundary
        still rounds up to the NEXT one, the boundary itself is reserved for the bridge
        block (user decision 2026-09-14, see project_knx_write_path_design memory).
        """
        return ((highest_id // block_size) + 1) * block_size

    @staticmethod
    def _existing_knx_bridge_block_start(fub_modules: dict[str, Any]) -> int | None:
        """Round-50 boundary of the already-established KNX bridge marker block, or None if
        no bridge marker (title matching MARKER_KNX_BRIDGE_SUFFIX_RE) exists anywhere yet.

        ensure_knx_bridge_block_start() uses this to tell "the block already exists, keep
        using its boundary" apart from "no block yet, pick a fresh one" — without it, every
        call recomputes _next_block_boundary from whatever the highest ORIGINAL marker
        happens to be *right now*, which only ever grows as unrelated real markers get
        created elsewhere. Confirmed live 2026-09-16: an established M300-M305 bridge block
        (from an earlier session) had room to simply continue at M306, but the highest
        original marker had since grown past M300, pushing the recomputed boundary to M350
        and burning 39 marker ids (M311-M349) as pure blank filler just to reach it.
        """
        group = fub_modules.get("2")
        items = group.values() if isinstance(group, dict) else (group or [])
        bridge_ids = [
            m["Id"]
            for m in items
            if isinstance(m, dict)
            and isinstance(m.get("Id"), int)
            and MARKER_KNX_BRIDGE_SUFFIX_RE.search(m.get("Name") or "")
        ]
        return None if not bridge_ids else (min(bridge_ids) // 50) * 50

    @staticmethod
    def _marker_id_by_title(fub_modules: dict[str, Any], title: str) -> int | None:
        """Id of an existing marker with this exact title, if any ($FubModules["2"] scan).

        Used by create_knx_bridge_marker to recognize an unwired remnant of an earlier,
        partially-failed bridge attempt (create_marker + rename_marker both succeeded, but
        the subsequent wire_knx_bridge_pair call failed) — rename_marker enforces title
        uniqueness, so blindly creating another marker with the same bridge title would
        just fail every retry forever instead of finishing the original attempt.
        """
        group = fub_modules.get("2")
        items = group.values() if isinstance(group, dict) else (group or [])
        for m in items:
            if isinstance(m, dict) and m.get("Name") == title:
                marker_id = m.get("Id")
                return marker_id if isinstance(marker_id, int) else None
        return None

    @staticmethod
    def _free_marker_ids(
        fub_modules: dict[str, Any],
        all_plans: dict[int, dict],
        min_id: int,
        ref_type: int = 2,
        max_id: int | None = None,
        keep_bridge_titles: set[str] | None = None,
    ) -> list[int]:
        """Ascending ids in [min_id, max_id) of markers that are 'free': no title AND not
        placed as an element in any function plan.

        keep_bridge_titles (optional): additionally treat every unplaced, bridge-titled
        marker >= min_id whose title is NOT in this set as free — a stale bridge marker left
        behind by a K-element rename or a deleted plan (see _stale_knx_bridge_marker_ids).
        Bridge titles are machine-given, so this never touches a user's own marker.

        Mirrors the same relevant-vs-free distinction Comexio's own Studio validation uses
        (see project_knx_write_path_design memory, 15.09.2026 discussion): a blank, unplaced
        marker left over from an earlier create_knx_bridge_marker attempt (its plan since
        deleted or its title cleared) is safe to rename and reuse. Without this, every
        create/reset test cycle would permanently burn a fresh block of sequential ids —
        Comexio hands them out strictly sequentially server-side and never reclaims one on
        its own (confirmed live 2026-09-14/15).

        max_id (exclusive) matters since ensure_knx_bridge_block_start() started reusing an
        already-established block's boundary indefinitely (2026-09-16 fix) instead of
        recomputing a fresh one above every current marker on each call: min_id can now stay
        low for a long time while unrelated real markers keep accumulating above it, so an
        unbounded scan would risk sweeping up a blank, unplaced marker the user created for
        an entirely different purpose. Callers pass the block size as max_id - min_id to keep
        reuse confined to the bridge block itself.
        """
        group = fub_modules.get("2")
        items = group.values() if isinstance(group, dict) else (group or [])
        candidate_ids = _blank_marker_id_candidates(items, min_id, max_id)
        if keep_bridge_titles is not None:
            candidate_ids |= _stale_knx_bridge_marker_ids(items, min_id, keep_bridge_titles)
        if not candidate_ids:
            return []
        placed_ids = _placed_marker_ids(all_plans, ref_type)
        return sorted(candidate_ids - placed_ids)

    async def _load_all_plans_verified(self, fubs: dict[str, Any], strict: bool = False) -> dict[int, dict] | None:
        """Elements/connections of EVERY plan in fubs (a fresh $Fubs), or None if any is missing.

        Placement checks that decide whether a bridge marker may be renamed or blanked need
        the complete picture: function_plan_load_all_plans silently skips malformed entries
        and filters against the cached plan list. This refreshes that cache (self._fub_data)
        from fubs, then loads each plan the bulk response lacked individually (e.g. a plan
        without elements, should the bulk endpoint omit those). Returns {} if there are no
        plans at all. strict is passed on to both loaders (see function_plan_load_elements).
        """
        self._fub_data = fubs
        fub_ids = {int(fid) for fid in fubs}
        if not fub_ids:
            return {}
        plans = await self.function_plan_load_all_plans(strict=strict)
        for fid in sorted(fub_ids - set(plans)):
            data = await self.function_plan_load_elements(fid, strict=strict)
            if data is None:
                _LOGGER.error("_load_all_plans_verified: plan fub=%s could not be loaded — placement unknown", fid)
                return None
            plans[fid] = data
        return plans

    async def reset_knx_bridge_markers(
        self, progress_cb: Callable[[int, int], None] | None = None
    ) -> tuple[list[int], list[int], int, str | None]:
        """Blank the title of every unplaced KNX bridge marker ("... [K<id>]").

        Part of the KNX cleanup: markers are never deleted (Comexio would not hand their ids
        out again), only un-titled, so _free_marker_ids treats them as free and the next sync
        rebuilds the bridge block compactly from its start. Clearing a title via isunique +
        saveOne with name="" was verified live 2026-09-25 (M300: type and category kept).
        Must run AFTER the KNX cluster plans were deleted — markers still placed in any plan
        are skipped.

        Reads $Fubs from the same fresh config fetch (the cached plan list still contains the
        plans deleted moments ago). Returns (reset ids, failed ids, skipped-as-placed count,
        error) — error is set, and nothing is touched, when the config or any plan could not
        be loaded: a missing plan would make the bridge markers wired into it look unplaced.
        progress_cb(done, total) is called after every marker (one HTTP round trip each).
        """
        conf = await self.get_raw_config()
        fub_modules = conf.get("FubModules")
        fubs = conf.get("Fubs")
        if not fub_modules or not isinstance(fubs, dict):
            return [], [], 0, "could not fetch current Comexio config"
        all_plans = await self._load_all_plans_verified(fubs)
        if all_plans is None:
            return [], [], 0, "could not load every function plan"

        group = fub_modules.get("2")
        items = group.values() if isinstance(group, dict) else (group or [])
        candidates, skipped = _knx_bridge_reset_candidates(items, _placed_marker_ids(all_plans, 2))
        reset: list[int] = []
        failed: list[int] = []
        for i, (marker_id, binary) in enumerate(candidates, start=1):
            (reset if await self.rename_marker(marker_id, "", binary) else failed).append(marker_id)
            if progress_cb:
                progress_cb(i, len(candidates))
        _LOGGER.info("reset_knx_bridge_markers: reset=%d failed=%s skipped_placed=%d", len(reset), failed, skipped)
        return reset, failed, skipped, None

    async def _fill_marker_gap(self, current_highest_id: int, target_id: int) -> int:
        """Consume marker ids via blank create_marker() calls until the next created
        marker would land exactly on target_id.

        Comexio assigns marker ids strictly sequentially, server-side — there is no way to
        request target_id directly (confirmed live 2026-09-14), so every id in the gap must
        be actively created as an untitled filler marker ("fehlende müssen angelegt werden
        (ohne Werte)!" — explicit user requirement, not a side effect to avoid).

        Returns the number of filler markers actually created. Stops early (with a logged
        error) if create_marker() ever fails, or if it ever returns an id that does not
        strictly increase — Comexio is documented to hand out ids sequentially, but trusting
        that blindly would turn a server-side glitch into an infinite loop here.
        """
        created = 0
        highest = current_highest_id
        while highest < target_id - 1:
            new_id = await self.create_marker(binary=True)
            if new_id is None:
                _LOGGER.error(
                    "_fill_marker_gap: create_marker failed after %d filler(s) (highest=%s, target=%s)",
                    created,
                    highest,
                    target_id,
                )
                break
            if new_id <= highest:
                _LOGGER.error(
                    "_fill_marker_gap: create_marker returned non-increasing id %s (highest=%s, target=%s) — "
                    "aborting to avoid an infinite loop",
                    new_id,
                    highest,
                    target_id,
                )
                break
            created += 1
            highest = new_id
        return created

    async def ensure_knx_bridge_block_start(self, fub_modules: dict[str, Any]) -> int | None:
        """Make sure the next marker created via create_marker() lands on/past the KNX
        bridge block's round-50 boundary, filling any gap with blank filler markers first.

        Idempotent/safe to call before every bridge-marker creation, not just the first:
        once the boundary has been reached or passed — whether by a bridge marker created
        earlier, or by a marker the user happened to create manually in the meantime — this
        is a no-op, since Comexio hands out ids sequentially regardless of who asked.

        The boundary itself is only ever *freshly computed* (via _next_block_boundary) for
        the very first bridge marker a Comexio instance ever gets — once a block already
        exists (_existing_knx_bridge_block_start finds a marker titled [K<id>] anywhere),
        its boundary is reused as-is instead. Recomputing a fresh boundary every time would
        keep chasing whatever the highest ORIGINAL marker happens to be at that moment, which
        only ever grows as unrelated real markers get created — pushing an established,
        far-from-full block onto a brand new boundary and burning every id in between as
        blank filler for nothing (see _existing_knx_bridge_block_start's own docstring for
        the live incident this fixes).

        Returns the boundary marker id (informational/for logging) once actually reached,
        or None if _fill_marker_gap stopped short of it (its own error is already logged) —
        callers must treat None as "abort", since creating a bridge marker right after would
        silently land it off the intended round-50 boundary instead of on it.
        """
        existing_block_start = self._existing_knx_bridge_block_start(fub_modules)
        target = (
            existing_block_start
            if existing_block_start is not None
            else self._next_block_boundary(self._highest_marker_id(fub_modules, only_original_titled=True))
        )
        _LOGGER.debug(
            "ensure_knx_bridge_block_start: target=M%d (source=%s)",
            target,
            "existing block" if existing_block_start is not None else "freshly computed",
        )
        current_highest = self._highest_marker_id(fub_modules)
        if current_highest >= target - 1:
            _LOGGER.debug(
                "ensure_knx_bridge_block_start: boundary M%d already reached/passed (highest=M%d)",
                target,
                current_highest,
            )
            return target
        filled = await self._fill_marker_gap(current_highest, target)
        if current_highest + filled < target - 1:
            _LOGGER.error(
                "ensure_knx_bridge_block_start: failed to reach boundary M%d (stuck at M%d) — aborting",
                target,
                current_highest + filled,
            )
            return None
        _LOGGER.info("ensure_knx_bridge_block_start: filled %d gap marker(s) to reach boundary M%d", filled, target)
        return target

    async def create_knx_bridge_marker(
        self,
        k_id: int,
        k_title: str,
        k_type_raw: int,
        fub_modules: dict[str, Any],
        free_marker_ids: list[int] | None = None,
    ) -> tuple[int, str] | None:
        """Create + title a bridge marker for one K-element (Entwurf A — see
        project_knx_write_path_design memory).

        binary/analog is derived from k_type_raw via the same $IOTypesBinary catalog
        lookup _process_source_items() uses for KNX classification (self.io_types) — the
        bridge marker's type must match the K-element's own pin class, nothing else is
        wireable onto it (confirmed live 2026-09-14).

        Reuses an existing marker with the exact bridge title, if one is already sitting
        in fub_modules unwired: an earlier attempt for the same K-element can have created
        and titled the marker successfully but then failed at the wire_knx_bridge_pair
        step, and rename_marker enforces title uniqueness — without this check, every
        retry would call create_marker again, rename would collide with that leftover
        marker's title, and the K-element could never be bridged.

        Failing that, pops the lowest id off free_marker_ids (see _free_marker_ids) if the
        caller supplied one — a blank, unplaced leftover marker from an earlier attempt or
        test cycle — and renames it instead of calling create_marker(), so repeated
        create/reset cycles reuse the existing block instead of permanently consuming new
        sequential ids (2026-09-15 design fix). Callers must pop from the SAME list object
        across every item of a batch so two K-elements never race for the same free id.

        Does NOT wire the marker to the K-element or create its Web-IO — that FUP-wiring
        step is a separate piece (see wire_knx_bridge_pair).

        Caller is responsible for calling ensure_knx_bridge_block_start() once before the
        very first bridge marker of a session, so a newly-created one (no free id available)
        lands on the round-50 boundary rather than wherever the marker pool currently ends.

        Returns (marker_id, title) on success, None on failure (create or rename failed).
        """
        binary = self.io_types.get(str(k_type_raw), {}).get("binary", False)
        title = _knx_bridge_title(k_id, k_title)

        existing_id = self._marker_id_by_title(fub_modules, title)
        if existing_id is not None:
            _LOGGER.info(
                "create_knx_bridge_marker: reusing existing M%s '%s' for K%s (unwired remnant of an earlier attempt)",
                existing_id,
                title,
                k_id,
            )
            return existing_id, title

        if free_marker_ids:
            marker_id = free_marker_ids.pop(0)
            _LOGGER.info(
                "create_knx_bridge_marker: reusing free M%s for K%s instead of creating a new marker",
                marker_id,
                k_id,
            )
        else:
            marker_id = await self.create_marker(binary)
            if marker_id is None:
                _LOGGER.error("create_knx_bridge_marker: create_marker failed for K%s (%s)", k_id, k_title)
                return None

        if not await self.rename_marker(marker_id, title, binary):
            _LOGGER.error(
                "create_knx_bridge_marker: rename_marker failed for M%s -> '%s' (K%s)", marker_id, title, k_id
            )
            return None

        _LOGGER.info("create_knx_bridge_marker: M%s '%s' created for K%s (binary=%s)", marker_id, title, k_id, binary)
        return marker_id, title

    async def wire_knx_bridge_pair(
        self,
        fub_id: int,
        marker_id: int,
        k_id: int,
        binary: bool,
        pos: tuple[float, float, float],
        existing_by_ref: dict[tuple[int, int], int] | None = None,
        conn_endpoints: list[set[int]] | None = None,
        plan_data: dict | None = None,
    ) -> str | None:
        """Wire a bridge Marker (source) to its K-Element/KNX object (sink) on a plan.

        This is the write-path counterpart of _function_plan_wire_ref_pair (which wires a
        Source->Web-IO pair for the read path): here the Marker is the source and the KNX
        object (type=11) is the sink, so Comexio pushes the marker's value onto the bus.
        Existing elements are reused, mirroring _function_plan_wire_ref_pair's orphan-pair
        handling; pass a pre-loaded existing_by_ref/conn_endpoints/plan_data (from
        _function_plan_existing_refs / function_plan_load_elements) when wiring several pairs
        on the same plan to avoid reloading it for every pair.
        plan_data: needed to union onto the marker's CURRENT sinks (silent-failure-hunter
        finding, 2026-09-18) whenever elem_marker turns out to be a REUSED element — e.g. an
        "unwired remnant" left over from a previous failed/partial run — which can already
        carry its own connection record from something else, regardless of whether the
        K-element side is reused or freshly created. An earlier version of this function only
        checked this when BOTH sides pre-existed, silently missing the (more common) case of a
        reused marker paired with a brand-new K-element; that gap is now closed by resolving
        elem_marker/elem_knx first and always doing the existing-connection lookup on
        elem_marker afterward — a freshly created elem_marker simply has no match, so the same
        code path is safe for both cases. Reload happens whenever plan_data itself is missing
        (checked independently of existing_by_ref/conn_endpoints, code-reviewer finding
        2026-09-18 — a caller supplying those two but forgetting plan_data would otherwise
        silently disable this check instead of erroring). Passing conn_id="new" onto a marker
        that already has a connection would hit the exact Comexio server quirk
        function_plan_save_connection's docstring documents: the new save merges into the
        existing record but silently drops that record's "input" field.
        Returns None on success, "" if the pair is already wired (skip, not an error), or an
        error message.
        """
        x_marker, x_knx, y = pos
        conn_type = "binary" if binary else "analog"
        label = f"KNX bridge M{marker_id}->K{k_id}"

        if plan_data is None:
            plan_data = await self.function_plan_load_elements(fub_id)
        if existing_by_ref is None or conn_endpoints is None:
            existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        existing_marker_elem = existing_by_ref.get((2, marker_id))
        existing_knx_elem = existing_by_ref.get((11, k_id))

        # Fast path: both elements already existed and are already wired together — nothing to do.
        # is not None (not truthy) to stay consistent with _resolve_or_create_element below — an
        # element id of 0 would otherwise be misread as "doesn't exist" (silent-failure-hunter /
        # code-reviewer finding, 2026-09-18, Round 4). Harmless here specifically (the success
        # path below always runs elem_marker/elem_knx through _function_plan_union_sink
        # regardless of reuse-vs-fresh), but kept consistent anyway.
        if (
            existing_marker_elem is not None
            and existing_knx_elem is not None
            and any(existing_marker_elem in eps and existing_knx_elem in eps for eps in conn_endpoints)
        ):
            _LOGGER.info("%s already wired on fub=%s, skipping", label, fub_id)
            return ""

        elem_marker = await self._resolve_or_create_element(
            fub_id, existing_marker_elem, marker_id, 2, x_marker, y, label, "marker"
        )
        if isinstance(elem_marker, str):
            return elem_marker

        elem_knx = await self._resolve_or_create_element(fub_id, existing_knx_elem, k_id, 11, x_knx, y, label, "KNX")
        if isinstance(elem_knx, str):
            return elem_knx

        # Union-safe save: elem_marker may be a reused element that already carries a
        # connection from something else (see docstring above) — fold this sink into it rather
        # than blind-resaving with conn_id="new". A brand-new elem_marker naturally has no
        # match here, so this is safe for both the reuse and fresh-create case.
        union = self._function_plan_union_sink(plan_data, elem_marker, elem_knx, label, fub_id)
        if union is None:
            return ""
        if isinstance(union, str):
            return union
        outputs, input_pos, input_inverted, existing_conn_id = union

        conn_id = await self.function_plan_save_connection(
            fub_id,
            elem_marker,
            outputs,
            conn_type,
            input_pos=input_pos,
            input_inverted=input_inverted,
            existing_conn_id=existing_conn_id,
        )
        if conn_id is None:
            return f"{label}: save_connection failed"

        _LOGGER.info(
            "%s wired (fub=%s, marker_elem=%s, knx_elem=%s, conn=%s)",
            label,
            fub_id,
            elem_marker,
            elem_knx,
            conn_id,
        )
        return None

    async def wire_knx_bridge_loopback(
        self,
        fub_id: int,
        k_id: int,
        marker_id: int,
        loopback_web_ref_id: int,
        existing_by_ref: dict[tuple[int, int], int],
        plan_data: dict | None,
        pos: tuple[float, float],
    ) -> str | None:
        """Fan the K-Element's EXISTING read-path wire out to ALSO include the API-Loopback Web-IO.

        Phase 7 (see project_knx_write_path_design memory): besides the K-Element (source,
        type=11) -> HA-Web-IO (sink) wire _function_plan_wire_ref_pair already drew
        (function_plan_add_source_pairs, ref_type=11), the same K-Element output must ALSO
        reach a second Web-IO whose command writes the bridge Marker directly via Comexio's
        own /api/ endpoint — closing the "Punkt 4" stuck-marker loop entirely inside Comexio.

        Must NOT call _function_plan_wire_ref_pair a second time for the same K-Element:
        function_plan_save_connection replaces the FULL sink list for a source pin on every
        save (see its own docstring) rather than adding to it — a second single-sink call
        would silently drop the existing HA-Web-IO wire, breaking the read-path entity. Instead
        this reads the K-Element's CURRENT connection (_function_plan_find_connection_by_source),
        keeps its type and existing sinks, adds the loopback Web-IO as one more, and saves that
        union in a single call.

        The reverse hazard (an unrelated later run of the read-path repair silently overwriting
        this fan-out back down to the HA-Web-IO sink alone) is closed on the caller side:
        _function_plan_wire_ref_pair's orphan-pair AND element-recreate branches read the
        K-Element's current sinks via plan_data and save the union, same technique as here.
        All three _function_plan_add_single_*_pair batch callers (Marker/IO/KNX, ref_type=2/1/11)
        now thread plan_data through unconditionally — the IO path used to be the one exception
        (silent-failure-hunter finding, 2026-09-18: it always called with plan_data=None, which
        would have reproduced Bug #2 for an IO source that already had a connection, not just
        skipped a no-op union) and has since been fixed to match the other two.

        Requires the K-Element's read-path wire to already exist — this only ever EXTENDS it,
        it never creates it from scratch (run the normal KNX bridge repair first if missing).
        Also serves as the retrofit path for bridges predating Phase 7 (e.g. M300-M306): the
        caller just needs to already know their k_id/marker_id, the wiring itself is identical.
        Returns None on success, "" if the loopback sink is already present (skip, not an
        error), or an error message.
        """
        x_webio, y = pos
        label = f"KNX loopback K{k_id}->M{marker_id}"

        k_elem = existing_by_ref.get((11, k_id))
        if k_elem is None:
            return f"{label}: K-Element not found in plan (read-path wire missing, run KNX bridge repair first)"

        try:
            found = self._function_plan_find_connection_by_source(plan_data, k_elem)
        except ValueError as exc:
            return f"{label}: {exc}, aborting to avoid resaving it as a brand-new connection"
        if found is None:
            return f"{label}: K-Element has no existing connection (read-path wire missing, run repair first)"
        existing_conn_id, conn = found
        # loadelements' raw shape uses CamelCase IOPos/Inverted (as opposed to the plain
        # pos/inverted keys function_plan_save_connection's own payload uses on save) — same
        # load/save key asymmetry _rebuild_one_connection already accounts for. Getting this
        # wrong wouldn't show up against a single-sink source like today's live test (falls
        # back to the same pos=0/inverted=False either way), only once a source fans out to a
        # non-default input port or an inverted sink.
        conn_type = "analog" if conn.get("type") in (1, "analog") else "binary"

        existing_outputs = self._read_connection_outputs(conn, label, fub_id)
        if existing_outputs is None:
            # Parsing already logged which sink was malformed — saving a truncated union here
            # would permanently drop the existing read-path wire this method exists to preserve.
            return f"{label}: existing connection has a malformed sink, aborting to avoid dropping it"

        loopback_elem = existing_by_ref.get((10, loopback_web_ref_id))
        if loopback_elem is not None and any(dst == loopback_elem for dst, _p, _i in existing_outputs):
            _LOGGER.info("%s already wired on fub=%s, skipping", label, fub_id)
            return ""

        if loopback_elem is None:
            loopback_elem = await self.function_plan_add_element(
                fub_id=fub_id, ref_id=loopback_web_ref_id, element_type=10, x=x_webio, y=y
            )
            if loopback_elem is None:
                return f"{label}: add_element (Loopback Web-IO, webIoId={loopback_web_ref_id}) failed"

        outputs = [*existing_outputs, (int(loopback_elem), 0, False)]
        input_pos, input_inverted = self._connection_input_pin(conn)
        conn_id = await self.function_plan_save_connection(
            fub_id,
            k_elem,
            outputs,
            conn_type,
            input_pos=input_pos,
            input_inverted=input_inverted,
            existing_conn_id=existing_conn_id,
        )
        if conn_id is None:
            return f"{label}: save_connection (fan-out union, {len(outputs)} sinks) failed"

        _LOGGER.info(
            "%s added (fub=%s, k_elem=%s, loopback_elem=%s, conn_id=%s, total_sinks=%d)",
            label,
            fub_id,
            k_elem,
            loopback_elem,
            conn_id,
            len(outputs),
        )
        return None

    async def _prepare_knx_bridge_batch(
        self, batch_titles: set[str]
    ) -> tuple[dict[str, Any] | None, list[int], str | None]:
        """Fetch config, locate the KNX bridge marker block, and collect reusable free markers.

        Split out of function_plan_add_knx_bridge_pairs to keep its own cognitive complexity
        within SonarQube S3776's limit. Returns (fub_modules, free_marker_ids, error) — on
        failure fub_modules is None and error carries the message the caller returns verbatim.

        batch_titles: bridge titles of the current batch — stale bridge markers carrying one
        of them stay out of the free list, create_knx_bridge_marker reuses them by title.
        Reuse spans the whole contiguous blank/bridge run from the block start
        (_knx_bridge_run_end), not just its first MARKER_KNX_BRIDGE_BLOCK_SIZE ids.
        """
        conf = await self.get_raw_config()
        fub_modules = conf.get("FubModules")
        if not fub_modules:
            # get_raw_config() returns {} on a failed HTTP fetch — proceeding with an empty
            # $FubModules would make ensure_knx_bridge_block_start think NO marker exists yet
            # and fill dozens of bogus filler markers to reach a wrong "boundary".
            _LOGGER.error(
                "function_plan_add_knx_bridge_pairs: could not fetch current Comexio config "
                "(FubModules missing) — aborting"
            )
            return None, [], "could not fetch current Comexio config — aborting KNX bridge wiring, see log"
        target = await self.ensure_knx_bridge_block_start(fub_modules)
        if target is None:
            return None, [], "failed to reach the KNX bridge marker block boundary — aborting, see log"

        fubs = conf.get("Fubs")
        all_plans = await self._load_all_plans_verified(fubs) if isinstance(fubs, dict) else None
        if not all_plans:
            # None = some plan could not be loaded; {} = no plan at all, which cannot happen
            # here (the target cluster plan exists) and so also points at a bad fetch. Reuse
            # now also reclaims stale BRIDGE markers, which are wired into some plan far more
            # often than blank ones — treating an unseen plan as "nothing placed there" would
            # rename a live bridge of another cluster. Fall back to always creating fresh
            # markers instead (still correct, just skips the reuse optimization for this run).
            _LOGGER.warning(
                "function_plan_add_knx_bridge_pairs: could not load every function plan "
                "— skipping free-marker reuse for this run (creating fresh markers instead)"
            )
            return fub_modules, [], None

        group = fub_modules.get("2")
        items = list(group.values() if isinstance(group, dict) else (group or []))
        free_marker_ids = self._free_marker_ids(
            fub_modules,
            all_plans,
            target,
            max_id=_knx_bridge_run_end(items, target),
            keep_bridge_titles=batch_titles,
        )
        return fub_modules, free_marker_ids, None

    async def function_plan_add_knx_bridge_pairs(
        self,
        fub_id: int,
        missing_items: list[dict[str, Any]],
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[list[int], list[str], dict[int, tuple[int, bool]]]:
        """Create a bridge Marker + wire it to its KNX object, for every item, on a stopped plan.

        Write-path counterpart of function_plan_add_source_pairs (Entwurf A "Merker-Brücke"):
        unlike that read-path pairing, the source element does not exist yet — a fresh
        bridge Marker is created per K-element (ensure_knx_bridge_block_start once per call
        keeps the whole batch on/after the round-50 boundary, then create_knx_bridge_marker
        per item, reusing free markers already sitting in the block before creating new ones
        — see _free_marker_ids) before wire_knx_bridge_pair draws the Marker->KNX connection.
        Reusing a stale existing_by_ref/conn_endpoints snapshot across the loop is safe here
        — every item gets a distinct marker_id (newly created, or popped from
        free_marker_ids, which only ever contains markers placed on no plan at all) and
        targets a different k_id, so no lookup in this batch can collide with one from an
        earlier item in the same batch.
        missing_items are {"ref_id", "title", "type_raw"} dicts (coordinator's
        knx_bridge_missing audit items). fresh_plan=True places pairs at their final grid
        slots (no later sort pass needed); progress_cb(done, total) fires after each item.
        Returns (added K ref_ids, error messages, {k_id: (marker_id, binary)} for every
        newly-added K — the caller uses this to wire the API-Loopback fan-out (leg 3) for a
        brand-new bridge immediately, in the same cycle, instead of re-auditing for the
        marker_id later (see _add_single_knx_bridge's docstring)).
        """
        batch_titles = {_knx_bridge_title(int(it["ref_id"]), it["title"]) for it in missing_items}
        fub_modules, free_marker_ids, error = await self._prepare_knx_bridge_batch(batch_titles)
        if error:
            return [], [error], {}
        if fub_modules is None:
            # Only reachable if _prepare_knx_bridge_batch's error-is-None contract is
            # violated — see its docstring. Fail loudly rather than silently proceed.
            raise AssertionError("_prepare_knx_bridge_batch returned fub_modules=None with error=None")

        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        items = sorted(missing_items, key=lambda it: int(it["ref_id"])) if fresh_plan else missing_items
        _, y_max = self.get_fub_canvas_bounds(fub_id)
        max_rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))
        rows_per_col = _balanced_rows_per_col(len(items), max_rows_per_col)

        def _pair_pos(n_added: int, n_loop: int) -> tuple[float, float, float]:
            """(x_marker, x_knx, y): final grid slot for fresh plans, placeholder otherwise."""
            if fresh_plan:
                col, row = divmod(n_added, rows_per_col)
                x_off = col * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
                return (
                    FUNCTION_PLAN_LAYOUT_X_MARKER + x_off,
                    FUNCTION_PLAN_LAYOUT_X_WEBIO + x_off,
                    FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_LAYOUT_Y_STEP,
                )
            # Off-canvas parking row: the follow-up sort pass assigns the real slots.
            return (
                FUNCTION_PLAN_LAYOUT_X_MARKER,
                FUNCTION_PLAN_LAYOUT_X_WEBIO,
                10000.0 + n_loop * FUNCTION_PLAN_LAYOUT_Y_STEP,
            )

        added: list[int] = []
        errors: list[str] = []
        bridged: dict[int, tuple[int, bool]] = {}
        for i, item in enumerate(items):
            k_id = int(item["ref_id"])
            marker_id, err = await self._add_single_knx_bridge(
                fub_id,
                k_id,
                item["title"],
                item["type_raw"],
                fub_modules,
                existing_by_ref,
                conn_endpoints,
                _pair_pos(len(added), i),
                free_marker_ids,
                plan_data=plan_data,
            )
            if err is None:
                added.append(k_id)
                if marker_id is None:
                    # Only reachable if _add_single_knx_bridge's err-is-None contract is
                    # violated — see its docstring. Fail loudly rather than silently drop
                    # this K-element out of the returned `bridged` map.
                    raise AssertionError(f"_add_single_knx_bridge returned marker_id=None with err=None for K{k_id}")
                binary = self.io_types.get(str(item["type_raw"]), {}).get("binary", False)
                bridged[k_id] = (marker_id, binary)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(items))

        return added, errors, bridged

    async def _add_single_knx_bridge(
        self,
        fub_id: int,
        k_id: int,
        k_title: str,
        k_type_raw: int,
        fub_modules: dict[str, Any],
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        pos: tuple[float, float, float],
        free_marker_ids: list[int],
        plan_data: dict | None = None,
    ) -> tuple[int | None, str | None]:
        """Create (or reuse an unwired remnant/free marker for) the bridge Marker for one
        K-element, then wire it in.

        free_marker_ids is shared (and mutated via pop) across the whole batch — see
        create_knx_bridge_marker. plan_data is passed through to wire_knx_bridge_pair's
        orphan-pair branch, which needs it to union onto an already-wired reused marker
        instead of resaving with conn_id="new" (see that function's docstring).

        Returns (marker_id, error). marker_id is the bridge Marker's id whenever
        create_knx_bridge_marker succeeded (even on a later wire failure, so the caller can
        still log which marker a failed wire left behind); the caller only trusts it once
        error is None. error is None on success, "" if a reused marker turned out to already
        be fully wired (stale audit entry — skip, not an error), else an error message.
        Returning marker_id here (instead of the caller re-discovering it via a fresh audit
        later) is what lets function_plan_add_knx_bridge_pairs' caller wire the API-Loopback
        fan-out (leg 3) for a brand-new bridge in the SAME stop/write/sort cycle — see that
        function's docstring.
        """
        created = await self.create_knx_bridge_marker(k_id, k_title, k_type_raw, fub_modules, free_marker_ids)
        if created is None:
            return None, f"KNX bridge K{k_id}: create_knx_bridge_marker failed — see log"
        marker_id, _title = created
        binary = self.io_types.get(str(k_type_raw), {}).get("binary", False)
        err = await self.wire_knx_bridge_pair(
            fub_id,
            marker_id,
            k_id,
            binary,
            pos,
            existing_by_ref=existing_by_ref,
            conn_endpoints=conn_endpoints,
            plan_data=plan_data,
        )
        return marker_id, err

    async def _ensure_knx_loopback_commands(
        self,
        bridges: list[tuple[int, int, bool]],
        device_id: str | int,
        base_id: str | int,
        existing_full_names: set[str],
        freshly_created: bool,
    ) -> tuple[dict[str, tuple[int, int]], set[str], list[str]]:
        """Resolve which loopback Web-IO commands still need creating for this batch.

        freshly_created=True: every bridge's command SHOULD already be bulk-embedded in the
        class' initial upload (see ensure_knx_loopback_webio) — but freshly_created can also be
        a false positive (see ensure_knx_loopback_webio's own docstring on the device-vs-class
        cross-check it now runs before reporting this), so existing_full_names is still checked
        first rather than assuming every name is new. Never calls save_single_command in this
        branch — that would duplicate whatever the bulk upload already created.
        freshly_created=False: per-bridge existing-check + save_single_command fallback, same
        as growing any other Web-IO class' Delta-Sync.

        Returns (pending: cmd_name -> (k_id, marker_id), names_to_confirm: full_names not yet
        proven present in the caller's already-fetched config, errors).
        """
        pending: dict[str, tuple[int, int]] = {}
        names_to_confirm: set[str] = set()
        errors: list[str] = []
        for k_id, marker_id, binary in bridges:
            cmd_name = knx_loopback_command_name(k_id, marker_id)
            full_name = f"{device_id}. {cmd_name}"
            if full_name in existing_full_names:
                pending[cmd_name] = (k_id, marker_id)
                continue
            if freshly_created:
                pending[cmd_name] = (k_id, marker_id)
                names_to_confirm.add(full_name)
                continue
            command = self._build_knx_loopback_webio_command(k_id=k_id, marker_id=marker_id, is_analog=not binary)
            if await self.save_single_command(base_id, device_id, command):
                pending[cmd_name] = (k_id, marker_id)
                names_to_confirm.add(full_name)
            else:
                errors.append(f"KNX loopback K{k_id}->M{marker_id}: save_single_command failed")
        return pending, names_to_confirm, errors

    async def function_plan_add_knx_bridge_loopback_pairs(
        self,
        fub_id: int,
        bridges: list[tuple[int, int, bool]],
        api_username: str,
        api_password: str,
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[list[int], list[int], list[str]]:
        """Add the Phase 7 API-Loopback Web-IO fan-out for a batch of already-wired KNX bridges.

        bridges: (k_id, marker_id, binary) triples for K-Elements whose Marker<->K-Element wire
        (write path, wire_knx_bridge_pair) and K-Element->HA-Web-IO wire (read path,
        function_plan_add_source_pairs ref_type=11) already exist — this call only ADDS the
        loopback fan-out via wire_knx_bridge_loopback, it never creates the bridge itself. Also
        doubles as the retrofit path for bridges predating Phase 7 (e.g. M300-M306): pass
        their existing (k_id, marker_id, binary) triples the same way as for freshly created
        ones.

        Bootstraps the once-per-server ComexioAPI Loopback Web-IO class/device first (see
        ensure_knx_loopback_webio) and aborts the whole batch if that fails — no loopback
        command can be created without it. On first bootstrap (class didn't exist yet), every
        bridge's command was already bulk-embedded in that class' initial upload, so this
        skips save_single_command entirely and just waits for the reload below to confirm
        them. Otherwise (class already existed), skips save_single_command for any bridge
        whose command already exists (idempotent against retries/retrofits — without this, a
        repeated call would keep creating same-named duplicate commands, since this class
        isn't part of the normal orphan/rename audit that would otherwise catch that).

        The Phase 7 Loopback class lives outside WEBIO_CLASSES, so its commands are never
        keys of parse_config()'s webio_commands (that dict only ever holds HA's own Marker/IO/
        KNX classes — see _build_webio_name_lexicon's docstring). Existence/readiness checks
        here go through webio_names instead (the all-devices lexicon), matched by the Studio
        pill-label convention "{deviceId}. {commandName}".

        fresh_plan=True places the pairs at FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK as an interim
        column, same as the fresh_plan=False off-canvas parking case below — this is NEVER the
        final column, a follow-up sort pass (see services/_grid.py) always runs afterward and
        moves each Loopback Web-IO into the SAME column as its read-path sibling, one row
        below it. progress_cb(done, total) fires after each item.
        Returns (added K ref_ids, already-wired K ref_ids skipped as a routine no-op, error
        messages) — len(added) + len(skipped) + len(errors) always accounts for every item in
        bridges once the batch reaches the per-item wiring loop.
        """
        # Sort by (k_id, marker_id) before anything below reads bridges — both the bulk
        # initial-embed order (ensure_knx_loopback_webio, fresh class) and the per-item
        # save_single_command append order (_ensure_knx_loopback_commands, existing class)
        # follow this list's order verbatim, unlike the HA-webhook KNX class whose commands
        # come pre-sorted straight out of parse_config's admin-page scrape order. Only sorts
        # *this batch* among itself — Comexio has no reorder-in-place API, so a batch added to
        # an already-populated class still lands after whatever an earlier, differently-ordered
        # batch created (only a full class recreate could fix that retroactively); flagged as
        # cosmetic by the user 2026-09-20 after recreating all test bridges in one batch.
        bridges = sorted(bridges, key=lambda b: (b[0], b[1]))
        bootstrap = await self.ensure_knx_loopback_webio(api_username, api_password, bridges)
        if bootstrap is None:
            return [], [], ["ComexioAPI Loopback Web-IO class/device not available — aborting, see log"]
        base_id, freshly_created = bootstrap
        try:
            device_id = await self.get_webio_device_info(WEBIO_DEVICE_NAME_KNX_LOOPBACK)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # get_webio_device_info raises RuntimeError on a non-200 response — see its own
            # docstring — but its session.get() call is unwrapped, so a connection failure/
            # timeout propagates as aiohttp.ClientError/TimeoutError instead. Must not let
            # either escape this (added, skipped, errors)-returning batch as an unhandled
            # exception.
            return [], [], [f"ComexioAPI Loopback Web-IO device check failed: {err}"]
        if device_id is None:
            return [], [], ["ComexioAPI Loopback Web-IO device not found after bootstrap — aborting, see log"]

        def _present_names(data: dict) -> set[str]:
            return {info["name"] for info in data.get("webio_names", {}).values()}

        raw_config = await self.get_raw_config()
        if not raw_config.get("FubModules"):
            # Mirrors function_plan_add_knx_bridge_pairs' own guard: get_raw_config() returns {}
            # on a failed HTTP fetch, which would otherwise make existing_full_names look like
            # "nothing exists yet" and cause every already-created loopback command in this
            # batch to be silently duplicated via save_single_command below.
            _LOGGER.error(
                "function_plan_add_knx_bridge_loopback_pairs: could not fetch current Comexio config — aborting"
            )
            return [], [], ["could not fetch current Comexio config — aborting KNX loopback wiring, see log"]
        current = self.parse_config(raw_config)
        existing_full_names = _present_names(current)

        pending, names_to_confirm, errors = await self._ensure_knx_loopback_commands(
            bridges, device_id, base_id, existing_full_names, freshly_created
        )

        if not pending:
            return [], [], errors or ["no loopback Web-IO command could be saved — aborting, see log"]

        fresh_data = (
            await self._reload_config_until_commands_ready(lambda _d: names_to_confirm, _present_names)
            if names_to_confirm
            else current
        )
        name_to_id = {info["name"]: wid for wid, info in fresh_data.get("webio_names", {}).items()}

        plan_data = await self.function_plan_load_elements(fub_id)
        if plan_data is None:
            # function_plan_load_elements returns None (and already logs the real cause) on a
            # failed fetch — treating that as "no elements exist" would make every bridge below
            # get the misleading "K-Element not found, run repair first" error instead of the
            # actual "couldn't load the plan" one. Prepend, don't replace: `errors` may already
            # hold per-item save_single_command failures from the loop above — losing those here
            # would under-report the batch's real error count to the caller/sync summary.
            return [], [], [*errors, f"could not load function plan {fub_id} — aborting KNX loopback wiring, see log"]
        existing_by_ref, _ = self._function_plan_existing_refs(plan_data)

        return await self._wire_knx_bridge_loopback_batch(
            fub_id, pending, name_to_id, device_id, existing_by_ref, plan_data, errors, fresh_plan, progress_cb
        )

    async def _wire_knx_bridge_loopback_batch(
        self,
        fub_id: int,
        pending: dict[str, tuple[int, int]],
        name_to_id: dict[str, Any],
        device_id: str | int,
        existing_by_ref: dict[tuple[int, int], int],
        plan_data: dict,
        errors: list[str],
        fresh_plan: bool,
        progress_cb: Callable[[int, int], None] | None,
    ) -> tuple[list[int], list[int], list[str]]:
        """Place and wire each pending loopback command's function-plan element.

        Split out of function_plan_add_knx_bridge_loopback_pairs to keep its own cognitive
        complexity within SonarQube S3776's limit — see that method's docstring for the full
        semantics (grid placement convention, added/skipped/errors contract).
        """
        _, y_max = self.get_fub_canvas_bounds(fub_id)
        rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))

        def _pos(n: int) -> tuple[float, float]:
            if fresh_plan:
                _col, row = divmod(n, rows_per_col)
                y = FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_LAYOUT_Y_STEP
                return FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK, y
            # Off-canvas parking row — the follow-up sort pass assigns the real slot, same
            # convention as function_plan_add_knx_bridge_pairs' own non-fresh-plan branch.
            return FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK, 10000.0 + n * FUNCTION_PLAN_LAYOUT_Y_STEP

        added: list[int] = []
        skipped: list[int] = []
        for i, (cmd_name, (k_id, marker_id)) in enumerate(pending.items()):
            webio_id = name_to_id.get(f"{device_id}. {cmd_name}")
            err = await self._add_single_knx_bridge_loopback(
                fub_id, k_id, marker_id, webio_id, existing_by_ref, plan_data, _pos(i)
            )
            if err is None:
                added.append(k_id)
            elif err:
                errors.append(err)
            else:
                skipped.append(k_id)  # "" == already wired, a routine no-op, not an error
            if progress_cb:
                progress_cb(i + 1, len(pending))

        return added, skipped, errors

    async def _add_single_knx_bridge_loopback(
        self,
        fub_id: int,
        k_id: int,
        marker_id: int,
        webio_id: str | int | None,
        existing_by_ref: dict[tuple[int, int], int],
        plan_data: dict | None,
        pos: tuple[float, float],
    ) -> str | None:
        """Fan one already-resolved loopback command's webIoId out onto its K-Element's wire.

        Converts any unexpected exception (e.g. a non-numeric webio_id from a malformed
        webio_names entry) into an error string rather than letting it propagate and abort the
        whole batch in function_plan_add_knx_bridge_loopback_pairs, discarding every result
        already collected for earlier, successfully-processed bridges in the same run.
        """
        if webio_id is None:
            return f"KNX loopback K{k_id}->M{marker_id}: Web-IO command not found after config reload"
        try:
            return await self.wire_knx_bridge_loopback(
                fub_id, k_id, marker_id, int(webio_id), existing_by_ref, plan_data, pos
            )
        except Exception:
            _LOGGER.exception("KNX loopback K%s->M%s: unexpected error while wiring", k_id, marker_id)
            return f"KNX loopback K{k_id}->M{marker_id}: unexpected error, see log"

    async def function_plan_run_fup(self, fub_id: int, plan_data: dict | None = None) -> bool:
        """Save and activate a function plan (run_fup).

        By default the CURRENT state is loaded via loadelements; pass an explicit
        plan_data (e.g. a backup snapshot with 'elements' and 'connections') to
        restore that state instead.
        The output field in connections is converted from list (loadelements)
        to indexed dict (run_fup expectation).
        """
        if plan_data is None:
            plan_data = await self.function_plan_load_elements(fub_id)
        if plan_data is None:
            _LOGGER.error("function_plan_run_fup: could not load plan %s", fub_id)
            return False

        connections_transformed = {
            conn_id: {
                **conn,
                "output": {str(i): item for i, item in enumerate(conn.get("output", []))},
            }
            for conn_id, conn in plan_data.get("connections", {}).items()
        }
        data_payload = {"elements": plan_data.get("elements", {}), "connections": connections_transformed}

        url = f"{self._base_url}/admin/function_function_module/run_fup/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(
                url, data={"id": str(fub_id), "data": json.dumps(data_payload)}, headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_run_fup failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                result = await resp.json(content_type=None)
                success = result.get("result") is True
                _LOGGER.info("function_plan_run_fup: fub=%s result=%s state=%s", fub_id, success, result.get("state"))
                return success
        except Exception:
            _LOGGER.exception("function_plan_run_fup: fub_id=%s failed", fub_id)
            return False

    async def _reload_config_until_commands_ready(
        self,
        expected_names_fn: Callable[[dict], set[str]],
        present_names_fn: Callable[[dict], set[str]] | None = None,
    ) -> dict:
        """Reload Comexio config, retrying with backoff until the expected names are ready.

        A freshly uploaded Web-IO command does not always appear in the very next
        `/admin/function_function_module/home` response — Comexio seems to regenerate
        that page on its own cycle rather than synchronously per write. expected_names_fn
        derives the Web-IO command names to wait for from each reload's own parsed data
        (marker names/IO identifiers are stable; only webio_commands is expected to lag).

        present_names_fn overrides what counts as "already visible" — defaults to
        webio_commands' keys (HA's own Marker/IO/KNX classes only, see
        _build_webio_name_lexicon's docstring for why that dict is scoped that way). A
        command living in a Web-IO class HA doesn't own the audit for (e.g. the Phase 7
        API-Loopback class) is never a key of webio_commands and would wait out every retry
        here regardless of how fast it actually appears — such callers must pass a
        present_names_fn reading webio_names (the all-devices lexicon) instead.
        Returns the last parsed config regardless of outcome — callers report per-item
        errors for any names still missing after the final attempt.
        """
        delay = FUNCTION_PLAN_PAIR_RELOAD_INITIAL_DELAY
        fresh_data: dict = {}
        for attempt in range(FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS):
            raw = await self.get_raw_config()
            fresh_data = self.parse_config(raw)
            present = present_names_fn(fresh_data) if present_names_fn else fresh_data.get("webio_commands", {}).keys()
            missing = expected_names_fn(fresh_data) - present
            if not missing:
                return fresh_data
            if attempt < FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS - 1:
                _LOGGER.info(
                    "function plan pairing: %d Web-IO command(s) not yet visible after reload "
                    "(attempt %d/%d), retrying in %.1fs",
                    len(missing),
                    attempt + 1,
                    FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        return fresh_data

    @staticmethod
    def _function_plan_elem_id(value: Any) -> int | None:
        """Cast a raw FubElementId to int, tolerating the string-or-int shapes Comexio mixes.

        Every trigger-pair helper below matches these against elem_ids sourced from
        _function_plan_existing_refs, which are always int — comparing an un-cast string
        against that int set would silently never match.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _read_connection_outputs(conn: dict, label: str, fub_id: int) -> list[tuple[int, int, bool]] | None:
        """Parse an existing connection's sinks into (dst_elem_id, pos, inverted) tuples.

        Shared by wire_knx_bridge_loopback and _function_plan_wire_ref_pair's orphan-pair
        repair path — both must UNION a new sink onto a source's existing ones rather than
        overwrite, since function_plan_save_connection always replaces the full sink list for
        a source pin (see its own docstring). Uses loadelements' raw CamelCase keys
        (FubElementId/IOPos/Inverted) — NOT the lowercase element/pos/inverted keys
        function_plan_save_connection's own payload uses on save (same load/save key
        asymmetry _rebuild_one_connection already accounts for).
        Returns None (not []) if any sink can't be parsed, so the caller aborts instead of
        silently saving a truncated sink list that would drop that wire.
        """
        outputs_raw = conn.get("output") or []
        if isinstance(outputs_raw, dict):
            outputs_raw = list(outputs_raw.values())
        outputs: list[tuple[int, int, bool]] = []
        for sink in outputs_raw:
            dst_id = ComexioAPI._function_plan_elem_id(sink.get("FubElementId"))
            if dst_id is None:
                _LOGGER.warning(
                    "%s: existing sink has unparsable FubElementId %r on fub=%s — refusing to "
                    "save (would silently drop this wire)",
                    label,
                    sink.get("FubElementId"),
                    fub_id,
                )
                return None
            outputs.append((dst_id, sink.get("IOPos", 0), bool(sink.get("Inverted", False))))
        return outputs

    @staticmethod
    def _connection_input_pin(conn: dict) -> tuple[int, bool]:
        """(input_pos, input_inverted) of an existing connection's INPUT pin, loadelements shape.

        Sibling of _read_connection_outputs for the one field that helper deliberately leaves
        alone (the source side, not the sinks). A read-then-union save that only carries the new
        sink list forward but not this would silently reset a non-default input port or an
        inverted input wire back to (0, False) on every union-save — same CamelCase IOPos/
        Inverted raw shape as the output sinks, same load/save key asymmetry.
        """
        input_pin = conn.get("input") or {}
        return input_pin.get("IOPos", 0), bool(input_pin.get("Inverted", False))

    @staticmethod
    def _function_plan_existing_refs(plan_data: dict | None) -> tuple[dict[tuple[int, int], int], list[set[int]]]:
        """Index a plan's elements by (ref_type, ref_id) and collect connection endpoint sets.

        Keys are normalized to int — the raw JSON may carry type/ref_id as strings.
        """
        existing_by_ref: dict[tuple[int, int], int] = {}
        conn_endpoints: list[set[int]] = []
        if not plan_data:
            return existing_by_ref, conn_endpoints
        for elem_id_str, elem in plan_data.get("elements", {}).items():
            ref = elem.get("reference") or {}
            with suppress(TypeError, ValueError, KeyError):
                existing_by_ref[(int(ref["type"]), int(ref["ref_id"]))] = int(elem_id_str)
        for conn in (plan_data.get("connections") or {}).values():
            endpoints: set[int] = set()
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            for endpoint in [conn.get("input") or {}, *outputs]:
                with suppress(TypeError, ValueError, KeyError):
                    endpoints.add(int(endpoint["FubElementId"]))
            conn_endpoints.append(endpoints)
        return existing_by_ref, conn_endpoints

    async def fetch_marker_titles(self) -> dict[int, str] | None:
        """Current marker titles {id: title} from a fresh config fetch; None if it failed."""
        conf = await self.get_raw_config()
        fub_modules = conf.get("FubModules")
        if not fub_modules:
            return None
        titles: dict[int, str] = {}
        for mid, marker in self._iter_group(fub_modules.get("2")):
            with suppress(TypeError, ValueError):
                titles[int(mid)] = str((marker or {}).get("Name") or "")
        return titles

    @staticmethod
    def function_plan_element_refs(plan_data: dict | None) -> list[tuple[int, int]]:
        """(ref_type, ref_id) of every element in plan_data — public view of the index
        _function_plan_existing_refs builds, for callers outside the API."""
        existing_by_ref, _ = ComexioAPI._function_plan_existing_refs(plan_data)
        return list(existing_by_ref)

    @staticmethod
    def _function_plan_find_connection_by_source(plan_data: dict | None, src_elem_id: int) -> tuple[int, dict] | None:
        """(conn_id, raw connection record) whose input pin is src_elem_id, or None if none exists.

        A source pin has at most one outgoing connection record in Comexio (see
        function_plan_save_connection's docstring — fan-out is modeled as multiple "output"
        entries on ONE record, never as several records from the same source) — its "type"
        and "output" fields are exactly what a fan-out extension (wire_knx_bridge_loopback)
        must read and preserve before adding one more sink. The id (the dict's own key in
        plan_data["connections"], not part of the record's value) MUST be threaded back into
        function_plan_save_connection's conn_id param on the resave — see that function's
        docstring for the silently-dropped-"input" bug this avoids. Returns None if no such
        connection (or plan_data) exists.

        Raises ValueError if a connection DOES match but its own dict key can't be parsed as an
        id — deliberately NOT folded into the "no connection" None case (silent-failure-hunter
        finding, 2026-09-18): treating an unparsable-but-matched connection as "none exists"
        would make callers fall back to conn_id=None ("id":"new"), resaving over a connection
        that already exists and reproducing the exact silently-dropped-"input" corruption this
        whole conn_id mechanism exists to prevent. Callers must catch and turn this into an
        explicit abort, same shape as the existing "malformed sink" guards below.
        """
        if not plan_data:
            return None
        for conn_id_str, conn in (plan_data.get("connections") or {}).items():
            if ComexioAPI._function_plan_elem_id((conn.get("input") or {}).get("FubElementId")) == src_elem_id:
                conn_id = ComexioAPI._function_plan_elem_id(conn_id_str)
                if conn_id is None:
                    raise ValueError(
                        f"connection matched for src_elem_id={src_elem_id} but its own key "
                        f"{conn_id_str!r} is unparsable"
                    )
                return conn_id, conn
        return None

    async def _resolve_or_create_element(
        self,
        fub_id: int,
        existing_elem: int | None,
        ref_id: int,
        element_type: int,
        x: float,
        y: float,
        label: str,
        kind: str,
    ) -> int | str:
        """Reuse existing_elem if it's already in the plan, else create a fresh element_type element.

        Extracted (code-reviewer finding, 2026-09-18) from the "reuse-or-create" pattern repeated
        in wire_knx_bridge_pair (marker/KNX) and _function_plan_wire_ref_pair (source/Web-IO) —
        pure boilerplate around function_plan_add_element, but its inlined if/error-check was
        contributing to both functions' cognitive complexity overshoot past the project's budget
        of 15.
        Returns the resolved element id, or an f"{label}: add_element ({kind}) failed" error string.
        """
        if existing_elem is not None:
            return int(existing_elem)
        elem = await self.function_plan_add_element(fub_id=fub_id, ref_id=ref_id, element_type=element_type, x=x, y=y)
        if elem is None:
            return f"{label}: add_element ({kind}) failed"
        return int(elem)

    @staticmethod
    def _function_plan_union_sink(
        plan_data: dict | None, src_elem: int, sink_elem: int, label: str, fub_id: int
    ) -> tuple[list[tuple[int, int, bool]], int, bool, int | None] | str | None:
        """Fold sink_elem into src_elem's existing connection (if any), ready to hand straight
        to function_plan_save_connection.

        Single implementation of the "never resave conn_id='new' over a source that already has
        a connection" rule established live 2026-09-18 (Bug #2) — extracted (code-reviewer
        finding, 2026-09-18) after the same ~15-line try/except+union block got copy-pasted into
        four places (wire_knx_bridge_pair, wire_knx_bridge_loopback, _function_plan_wire_ref_pair
        ×2), pushing two of those functions' cognitive complexity past the project's budget of 15
        and duplicating both the logic and its error strings (S1192). Three of the four now
        delegate here; wire_knx_bridge_loopback (code-reviewer finding, 2026-09-18, Round 4) keeps
        its own inline variant because it must hard-error when the source has NO existing
        connection yet (this helper instead falls back to a fresh single-sink pair in that case —
        wrong semantics for a function whose whole job is extending an existing fan-out). Callers
        now do only a two-way dispatch: `if union is None: return ""`,
        `if isinstance(union, str): return union`, else unpack the tuple into
        function_plan_save_connection's input_pos/input_inverted/existing_conn_id kwargs.

        Returns:
        - (outputs, input_pos, input_inverted, existing_conn_id): pass straight through to
          function_plan_save_connection. If src_elem had no existing connection, this is a
          fresh single-sink pair (outputs=[sink_elem], existing_conn_id=None); otherwise
          sink_elem is appended to the existing sinks and the source's input pin is preserved.
        - None if src_elem already has a connection that already includes sink_elem — the pair is
          already wired; this is logged here (single canonical message, code-reviewer finding
          2026-09-18: the three call sites used to each log their own copy of one of two near-
          identical literals, tripping S1192 on both). Callers just return "" (skip, not an
          error) without logging again.
        - a non-empty str error message if the existing connection couldn't be safely read (a
          malformed sink, or an unparsable connection id) — callers must also return this
          verbatim rather than falling back to conn_id="new", which would reproduce the exact
          silently-dropped-"input" corruption this whole mechanism exists to prevent.
        """
        try:
            found = ComexioAPI._function_plan_find_connection_by_source(plan_data, src_elem)
        except ValueError as exc:
            return f"{label}: {exc}, aborting to avoid resaving it as a brand-new connection"
        if found is None:
            return [(sink_elem, 0, False)], 0, False, None
        existing_conn_id, existing_conn = found
        existing_outputs = ComexioAPI._read_connection_outputs(existing_conn, label, fub_id)
        if existing_outputs is None:
            return f"{label}: existing connection has a malformed sink, aborting to avoid dropping it"
        if any(dst == sink_elem for dst, _p, _i in existing_outputs):
            _LOGGER.info("%s: sink already present on existing connection (fub=%s), skipping", label, fub_id)
            return None
        outputs = [*existing_outputs, (sink_elem, 0, False)]
        input_pos, input_inverted = ComexioAPI._connection_input_pin(existing_conn)
        return outputs, input_pos, input_inverted, existing_conn_id

    @staticmethod
    def _function_plan_elem_ref(elements: dict, elem_id: int) -> dict:
        return (elements.get(str(elem_id)) or {}).get("reference") or {}

    @staticmethod
    def _function_plan_elem_is_flanke(elements: dict, elem_id: int, flanke_ref_id: str) -> bool:
        ref = ComexioAPI._function_plan_elem_ref(elements, elem_id)
        return str(ref.get("type")) == "5" and str(ref.get("ref_id")) == flanke_ref_id

    @staticmethod
    def _function_plan_trigger_wired_source_ids(plan_data: dict | None, ref_type: int = 2) -> set[int]:
        """Source (marker=2/KNX=11) ref_ids in plan_data with a complete <->Flanke round trip.

        A source element that exists but is missing either the ->Flanke or the
        Flanke-> connection — e.g. left behind by a pair creation that failed
        partway through — must not count as wired: the trigger audit needs to see it
        as still incomplete so sync repairs it, instead of treating a bare source
        element as proof the self-reset wiring is already in place.
        """
        if not plan_data:
            return set()
        elements = plan_data.get("elements", {})
        flanke_ref_id = str(FUB_BASE_REF_ID_FLANKE)

        edges: set[tuple[int, int]] = set()
        for conn in (plan_data.get("connections") or {}).values():
            src_id = ComexioAPI._function_plan_elem_id((conn.get("input") or {}).get("FubElementId"))
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            for sink in outputs:
                dst_id = ComexioAPI._function_plan_elem_id(sink.get("FubElementId"))
                if src_id is not None and dst_id is not None:
                    edges.add((src_id, dst_id))

        wired_marker_elem_ids = {
            src
            for src, dst in edges
            if ComexioAPI._function_plan_elem_is_flanke(elements, dst, flanke_ref_id) and (dst, src) in edges
        }
        return {
            int(ComexioAPI._function_plan_elem_ref(elements, elem_id)["ref_id"])
            for elem_id in wired_marker_elem_ids
            if str(ComexioAPI._function_plan_elem_ref(elements, elem_id).get("type")) == str(ref_type)
        }

    async def _function_plan_wire_ref_pair(
        self,
        fub_id: int,
        src_type: int,
        src_ref_id: int,
        web_ref_id: int,
        conn_type: str,
        label: str,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        pos: tuple[float, float, float],
        plan_data: dict | None = None,
    ) -> str | None:
        """Wire one source element (Marker type=2 / IO type=1) to its Web-IO (type=10) element.

        Existing elements are reused: if both already sit in the plan but the wire
        between them is missing (orphan pair, e.g. a connection lost during a
        restore cycle), only the connection is drawn. conn_endpoints holds the
        FubElementId endpoint set of every existing connection to detect that case.
        Returns None on success, "" when the pair is already wired in the plan
        (skip, not an error), or an error message.

        plan_data: needed to avoid the exact Bug #2 corruption (function_plan_save_connection's
        docstring) whenever src_ref_id already carries a connection — not just for the
        KNX-specific "reverse hazard" (see wire_knx_bridge_loopback's docstring) where a
        K-Element can carry a SECOND sink (the Phase-7 API-Loopback Web-IO). A Marker/IO source
        never has a second sink today, but it CAN still already have its ordinary single-sink
        connection (e.g. a reused/orphaned element from a previous partial run) — omitting
        plan_data there doesn't just skip a no-op union, it disables the existing-connection
        lookup entirely and lets a resave fall back to conn_id="new", silently dropping that
        connection's "input" field. An earlier version of this docstring claimed the omission
        was safe for Marker/IO precisely because of that "no second sink" reasoning, which
        conflated "no second sink" with "no existing connection at all"; the IO-pairs callers
        (function_plan_add_io_pairs → _function_plan_add_single_io_pair) had accordingly never
        threaded plan_data through at all until this was caught (silent-failure-hunter finding,
        2026-09-18) — always pass plan_data when available. None (the pre-Phase-7 callers/tests)
        falls back to the original single-sink save.
        """
        x_src, x_webio, y = pos

        existing_src_elem = existing_by_ref.get((src_type, src_ref_id))
        existing_webio_elem = existing_by_ref.get((10, web_ref_id))

        # is not None (not truthy) everywhere below, matching _resolve_or_create_element's own
        # check (silent-failure-hunter / code-reviewer finding, 2026-09-18, Round 4): the two
        # branches below this ("existing_webio_elem" and the final fresh-conn_payload fallback)
        # assume elem_src is guaranteed freshly created and skip _function_plan_union_sink
        # entirely — an element id of 0 misread as falsy-"doesn't exist" would have sent that
        # element's real existing connection through the unprotected "id":"new" path,
        # reproducing Bug #2 with no error, no log, and no entry in the batch's errors list.
        if existing_src_elem is not None and existing_webio_elem is not None:
            return await self._wire_ref_pair_orphan(
                fub_id, existing_src_elem, existing_webio_elem, conn_type, label, conn_endpoints, plan_data
            )

        elem_src = await self._resolve_or_create_element(
            fub_id, existing_src_elem, src_ref_id, src_type, x_src, y, label, f"source, type={src_type}"
        )
        if isinstance(elem_src, str):
            return elem_src

        if existing_webio_elem is not None:
            # Web-IO element already in the plan — wire the (possibly fresh) source to it.
            # Reaching this branch with existing_src_elem also non-None is impossible (that
            # combination is fully handled, and returned from, by the orphan-pair branch
            # above) — elem_src here is always a source freshly created a few lines up,
            # so it cannot carry any pre-existing sinks a single-sink save would clobber.
            conn_id = await self.function_plan_save_connection(
                fub_id, elem_src, [(existing_webio_elem, 0, False)], conn_type
            )
            if conn_id is None:
                return f"{label}: save_connection to existing Web-IO element failed"
            return None

        if existing_src_elem is not None:
            return await self._wire_ref_pair_reattach_source(
                fub_id, existing_src_elem, web_ref_id, conn_type, label, x_webio, y, plan_data
            )

        conn_payload = {
            "0": {
                "id": "new",
                "fub_id": fub_id,
                "type": conn_type,
                "input": {"element": str(elem_src), "pos": "0", "inverted": False},
                "output": {"0": {"element": "new", "pos": "0", "inverted": False}},
            }
        }
        elem_webio = await self.function_plan_add_element(
            fub_id=fub_id,
            ref_id=web_ref_id,
            element_type=10,
            x=x_webio,
            y=y,
            connection=conn_payload,
        )
        if elem_webio is None:
            return f"{label}: add_element (Web-IO, webIoId={web_ref_id}) failed"

        _LOGGER.info(
            "function plan pair %s → src_elem=%s webio_elem=%s (fub=%s, conn=%s)",
            label,
            elem_src,
            elem_webio,
            fub_id,
            conn_type,
        )
        return None

    async def _wire_ref_pair_orphan(
        self,
        fub_id: int,
        existing_src_elem: int,
        existing_webio_elem: int,
        conn_type: str,
        label: str,
        conn_endpoints: list[set[int]],
        plan_data: dict | None,
    ) -> str | None:
        """Both source and Web-IO elements already exist in the plan — draw/repair the wire.

        Extracted from _function_plan_wire_ref_pair (code-reviewer finding, 2026-09-18:
        cognitive complexity ~28, split into per-branch sub-coroutines to get under the
        project's budget of 15). Covers the ordinary orphan-pair case (wire lost, e.g. during a
        restore cycle) plus the "already wired, conn_endpoints just didn't reflect it" edge case
        _function_plan_union_sink itself detects and logs.
        """
        if any(existing_src_elem in eps and existing_webio_elem in eps for eps in conn_endpoints):
            _LOGGER.info("function plan pair %s already wired in plan fub=%s, skipping", label, fub_id)
            return ""
        # Unioned onto any sinks already present (e.g. a KNX loopback fan-out) rather than
        # replacing them, since function_plan_save_connection always saves the FULL sink list.
        union = self._function_plan_union_sink(plan_data, existing_src_elem, existing_webio_elem, label, fub_id)
        if union is None:
            return ""
        if isinstance(union, str):
            return union
        outputs, input_pos, input_inverted, existing_conn_id = union
        conn_id = await self.function_plan_save_connection(
            fub_id,
            existing_src_elem,
            outputs,
            conn_type,
            input_pos=input_pos,
            input_inverted=input_inverted,
            existing_conn_id=existing_conn_id,
        )
        if conn_id is None:
            return f"{label}: save_connection between existing elements failed"
        _LOGGER.info(
            "function plan pair %s rewired existing elements %s→%s (fub=%s, conn_id=%s, total_sinks=%d)",
            label,
            existing_src_elem,
            existing_webio_elem,
            fub_id,
            conn_id,
            len(outputs),
        )
        return None

    async def _wire_ref_pair_reattach_source(
        self,
        fub_id: int,
        existing_src_elem: int,
        web_ref_id: int,
        conn_type: str,
        label: str,
        x_webio: float,
        y: float,
        plan_data: dict | None,
    ) -> str | None:
        """Source element already exists but its Web-IO is missing — place a fresh one and reattach.

        Extracted from _function_plan_wire_ref_pair (see _wire_ref_pair_orphan's docstring for
        why). E.g. the Web-IO class was deleted+recreated under a new webIoId, orphaning the plan
        element for the old one. existing_src_elem may already carry other sinks (a KNX loopback
        fan-out), so the new Web-IO element is placed bare first, then unioned onto whatever
        connection the source already has — baking the connection into the same add_element call
        (like the fresh-source case) would hand Comexio a second, separate connection record for
        this source pin (function_plan_save_connection's docstring documents that as destructive).
        """
        elem_webio = await self.function_plan_add_element(
            fub_id=fub_id, ref_id=web_ref_id, element_type=10, x=x_webio, y=y
        )
        if elem_webio is None:
            return f"{label}: add_element (Web-IO, webIoId={web_ref_id}) failed"
        union = self._function_plan_union_sink(plan_data, existing_src_elem, int(elem_webio), label, fub_id)
        if union is None:
            return ""
        if isinstance(union, str):
            return union
        outputs, input_pos, input_inverted, existing_conn_id = union
        conn_id = await self.function_plan_save_connection(
            fub_id,
            existing_src_elem,
            outputs,
            conn_type,
            input_pos=input_pos,
            input_inverted=input_inverted,
            existing_conn_id=existing_conn_id,
        )
        if conn_id is None:
            return f"{label}: save_connection to newly placed Web-IO element failed"
        _LOGGER.info(
            "function plan pair %s → existing src_elem=%s, newly placed webio_elem=%s "
            "(fub=%s, conn_type=%s, total_sinks=%d)",
            label,
            existing_src_elem,
            elem_webio,
            fub_id,
            conn_type,
            len(outputs),
        )
        return None

    async def function_plan_add_source_pairs(
        self,
        fub_id: int,
        source_ids: list[int],
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
        ref_type: int = 2,
    ) -> tuple[list[int], list[str]]:
        """Add Marker/KNX (type=2/11) + Web-IO (type=10) element pairs to a stopped plan.

        Reloads Comexio config to pick up freshly created Web-IO commands (webIoId),
        retrying with backoff (see _reload_config_until_commands_ready) since a
        just-uploaded command does not always show up on the very next reload.
        fresh_plan=True places the pairs directly at their final grid positions
        (sorted by source ID) — no sort pass is needed afterwards. Otherwise the
        elements get placeholder positions and a sort run must follow.
        progress_cb(done, total) is invoked after every processed source.
        ref_type selects the source category (marker=2 / KNX=11 — blind guess),
        driving the source data key and the plan-element type.
        Returns (added_source_ids, error_messages).
        """
        category = category_by_fub_module_type(ref_type)

        def _expected_names(data: dict) -> set[str]:
            sources = {int(m["id"]): m for m in data.get(category.data_key, [])}
            return {f"HA {sources[mid]['name']}" for mid in source_ids if mid in sources}

        fresh_data = await self._reload_config_until_commands_ready(_expected_names)
        webio_commands = fresh_data.get("webio_commands", {})
        sources_by_id = {int(m["id"]): m for m in fresh_data.get(category.data_key, [])}

        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        if fresh_plan:
            source_ids = sorted(source_ids)
        _, y_max = self.get_fub_canvas_bounds(fub_id)
        # Always the generic pitch, even for a fresh KNX read-only cluster plan (ref_type=11) —
        # unlike async_sort_function_plan's is_knx_cluster_plan branch, that tighter pitch
        # (FUNCTION_PLAN_KNX_LAYOUT_Y_STEP) exists only to close the gap the write-bridge's extra
        # Phase-7-loopback slot leaves per pair. A read-only pair here takes exactly one row slot,
        # so the generic pitch already places it with no gap — reviewed and confirmed harmless
        # 2026-09-20, not a bug to fix.
        max_rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))
        rows_per_col = _balanced_rows_per_col(len(source_ids), max_rows_per_col)

        def _pair_pos(n_added: int, n_loop: int) -> tuple[float, float, float]:
            """(x_source, x_webio, y): final grid slot for fresh plans, placeholder otherwise."""
            if fresh_plan:
                col, row = divmod(n_added, rows_per_col)
                x_off = col * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
                return (
                    FUNCTION_PLAN_LAYOUT_X_MARKER + x_off,
                    FUNCTION_PLAN_LAYOUT_X_WEBIO + x_off,
                    FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_LAYOUT_Y_STEP,
                )
            # Off-canvas parking row: the follow-up sort pass assigns the real slots.
            return (
                FUNCTION_PLAN_LAYOUT_X_MARKER,
                FUNCTION_PLAN_LAYOUT_X_WEBIO,
                10000.0 + n_loop * FUNCTION_PLAN_LAYOUT_Y_STEP,
            )

        added: list[int] = []
        errors: list[str] = []
        for i, source_id in enumerate(source_ids):
            err = await self._function_plan_add_single_pair(
                fub_id,
                source_id,
                sources_by_id,
                webio_commands,
                existing_by_ref,
                conn_endpoints,
                _pair_pos(len(added), i),
                ref_type,
                plan_data,
            )
            if err is None:
                added.append(source_id)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(source_ids))

        return added, errors

    async def _function_plan_add_single_pair(
        self,
        fub_id: int,
        source_id: int,
        sources_by_id: dict,
        webio_commands: dict,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        pos: tuple[float, float, float],
        ref_type: int = 2,
        plan_data: dict | None = None,
    ) -> str | None:
        """Add one Source+Web-IO pair at pos=(x_source, x_webio, y).

        Return semantics as _function_plan_wire_ref_pair (None = added, "" = already wired).
        plan_data: see _function_plan_wire_ref_pair — needed for every ref_type, not just KNX:
        the source may already carry an ordinary single-sink connection (a reused/orphaned
        element from a previous partial run), and omitting plan_data disables the
        existing-connection lookup entirely, reproducing Bug #2 (code-reviewer finding,
        2026-09-18, caught after an earlier version of this docstring made the same "only
        relevant for ref_type=11" claim _function_plan_wire_ref_pair's own docstring was just
        corrected for). ref_type=11 (KNX) additionally needs it for the Phase-7 API-Loopback
        fan-out this repair must not clobber.
        """
        label = f"{category_by_fub_module_type(ref_type).audit_key_prefix}{source_id}"
        source = sources_by_id.get(source_id)
        if not source:
            return f"{label}: not found in fresh config"

        expected_cmd_name = f"HA {source['name']}"
        webio_cmd = webio_commands.get(expected_cmd_name)
        if not webio_cmd:
            _LOGGER.warning("function_plan_add_source_pairs: %s — Web-IO '%s' not found", label, expected_cmd_name)
            return f"{label}: Web-IO '{expected_cmd_name}' not found after config reload"

        web_ref_id = webio_cmd.get("webIoId")
        if web_ref_id is None:
            return f"{label}: no webIoId for '{expected_cmd_name}'"

        conn_type = "binary" if source["type"] == "digital" else "analog"
        return await self._function_plan_wire_ref_pair(
            fub_id,
            ref_type,
            source_id,
            int(web_ref_id),
            conn_type,
            label,
            existing_by_ref,
            conn_endpoints,
            pos,
            plan_data,
        )

    async def function_plan_add_trigger_pairs(
        self,
        fub_id: int,
        source_ids: list[int],
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
        ref_type: int = 2,
    ) -> tuple[list[int], list[str]]:
        """Add Source (marker=2/KNX=11) + Flanke (type=5) self-reset pairs to the trigger plan.

        No Web-IO element is involved — that wiring stays in the source's normal cluster
        plan, a separate fub_id. See MARKER_TRIGGER_SUFFIXES / FUNCTION_PLAN_TRIGGER_PLAN_NAME
        in const.py for why the two are kept apart.
        fresh_plan=True places the pairs directly at their final grid positions
        (sorted by source ID), matching function_plan_add_source_pairs.
        Returns (added_source_ids, error_messages).
        """
        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, _ = self._function_plan_existing_refs(plan_data)

        if fresh_plan:
            source_ids = sorted(source_ids)
        _, y_max = self.get_fub_canvas_bounds(fub_id)
        max_rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP))
        rows_per_col = _balanced_rows_per_col(len(source_ids), max_rows_per_col)

        def _pair_pos(n_added: int, n_loop: int) -> tuple[float, float, float]:
            """(x_source, x_flanke, y): final grid slot for fresh plans, placeholder otherwise.

            Uses FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP (not the generic, single-row-tall
            FUNCTION_PLAN_LAYOUT_Y_STEP) — the Flanke block renders 4 row-heights tall, so the
            generic step would stack consecutive pairs' Flanke blocks on top of each other.
            """
            if fresh_plan:
                col, row = divmod(n_added, rows_per_col)
                x_off = col * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
                return (
                    FUNCTION_PLAN_TRIGGER_LAYOUT_X_MARKER + x_off,
                    FUNCTION_PLAN_TRIGGER_LAYOUT_X_FLANKE + x_off,
                    FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP,
                )
            return (
                FUNCTION_PLAN_TRIGGER_LAYOUT_X_MARKER,
                FUNCTION_PLAN_TRIGGER_LAYOUT_X_FLANKE,
                10000.0 + n_loop * FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP,
            )

        added: list[int] = []
        errors: list[str] = []
        for i, source_id in enumerate(source_ids):
            err = await self._function_plan_add_single_trigger(
                fub_id, source_id, plan_data, existing_by_ref, _pair_pos(len(added), i), ref_type
            )
            if err is None:
                added.append(source_id)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(source_ids))

        return added, errors

    async def _function_plan_add_single_trigger(
        self,
        fub_id: int,
        source_id: int,
        plan_data: dict | None,
        existing_by_ref: dict[tuple[int, int], int],
        pos: tuple[float, float, float],
        ref_type: int = 2,
    ) -> str | None:
        """Add one Source+Flanke self-reset pair at pos=(x_source, x_flanke, y).

        Flanke element is created before the marker element (matches the user's Studio layout
        preference — creation order affects auto-placement even though explicit x/y is passed).

        Wiring verified live 2026-08-29 (TestPlan fub_id 33, M6): marker output[0] -> Flanke
        input "In" (FLANKE_PORT_IN); Flanke output "+" (FLANKE_PORT_OUT_RISING, fires once on
        a rising edge) -> marker input[0]. The marker's plan input behaves as a toggle, so this
        loop alone (no Web-IO) makes an external write to the marker self-clear back to 0.
        Return semantics as _function_plan_wire_ref_pair (None = added, "" = already wired).

        existing_by_ref collapses same-(ref_type, ref_id) elements to one arbitrary elem_id —
        every Flanke in this plan shares the same (type=5, ref_id) block-type reference, so
        looking a Flanke up there would silently reuse one marker's Flanke for every other
        trigger marker. The marker's own Flanke (if any) is found via its connections instead
        (_function_plan_paired_flanke_ids), and reused only if the round trip is complete
        (_function_plan_flanke_wires_back) — a one-directional leftover from a failed previous
        attempt must not be reported as already wired.
        """
        label = f"{category_by_fub_module_type(ref_type).audit_key_prefix}{source_id}"
        x_source, x_flanke, y = pos
        flanke_ref_id = int(FUB_BASE_REF_ID_FLANKE)

        existing_marker_elem = existing_by_ref.get((ref_type, source_id))
        existing_flanke_elem: int | None = None
        already_complete = False
        if existing_marker_elem:
            paired_flanke_ids = self._function_plan_paired_flanke_ids(plan_data, [existing_marker_elem], flanke_ref_id)
            if paired_flanke_ids:
                existing_flanke_elem = paired_flanke_ids[0]
                already_complete = self._function_plan_flanke_wires_back(
                    plan_data, existing_flanke_elem, existing_marker_elem
                )
        if already_complete:
            _LOGGER.info("trigger pair %s already wired in plan fub=%s, skipping", label, fub_id)
            return ""

        elem_flanke = existing_flanke_elem or await self.function_plan_add_element(
            fub_id=fub_id, ref_id=flanke_ref_id, element_type=5, x=x_flanke, y=y
        )
        if elem_flanke is None:
            return f"{label}: add_element (Flanke) failed"
        flanke_is_new = existing_flanke_elem is None

        elem_marker = existing_marker_elem or await self.function_plan_add_element(
            fub_id=fub_id, ref_id=source_id, element_type=ref_type, x=x_source, y=y
        )
        if elem_marker is None:
            return await self._function_plan_trigger_add_failed(
                elem_flanke, flanke_is_new, f"{label}: add_element (marker) failed"
            )

        if not self._function_plan_connection_exists(plan_data, int(elem_marker), int(elem_flanke)):
            conn_out = await self.function_plan_save_connection(
                fub_id, int(elem_marker), [(int(elem_flanke), FLANKE_PORT_IN, False)], "binary"
            )
            if conn_out is None:
                return await self._function_plan_trigger_add_failed(
                    elem_flanke, flanke_is_new, f"{label}: save_connection (marker→Flanke) failed"
                )

        if not self._function_plan_flanke_wires_back(plan_data, int(elem_flanke), int(elem_marker)):
            conn_back = await self.function_plan_save_connection(
                fub_id,
                int(elem_flanke),
                [(int(elem_marker), 0, False)],
                "binary",
                input_pos=FLANKE_PORT_OUT_RISING,
            )
            if conn_back is None:
                return await self._function_plan_trigger_add_failed(
                    elem_flanke, flanke_is_new, f"{label}: save_connection (Flanke→marker) failed"
                )

        _LOGGER.info("trigger pair %s created: marker=%s flanke=%s (fub=%s)", label, elem_marker, elem_flanke, fub_id)
        return None

    async def _function_plan_trigger_add_failed(self, elem_flanke: int, flanke_is_new: bool, error: str) -> str:
        """On a failed trigger-pair step, delete a just-created Flanke instead of leaving debris.

        A bare, disconnected Flanke has no marker to key off of, so the marker-based trigger
        audit can never find and clean it up later — it would silently accumulate across
        repeated failed sync attempts. The caller (button.py) already stops the trigger plan
        before calling function_plan_add_trigger_pairs and restarts it afterwards, so this
        needs no stop/restart of its own. A *reused* existing Flanke (flanke_is_new=False)
        predates this call and must not be deleted — it may still complete on a later retry.
        """
        if flanke_is_new and not await self.function_plan_delete_elements([int(elem_flanke)]):
            _LOGGER.error(
                "trigger pair rollback: failed to delete orphaned Flanke elem=%s — "
                "will not be found by the marker-based trigger audit; needs manual plan cleanup",
                elem_flanke,
            )
        return error

    @staticmethod
    def _function_plan_paired_flanke_ids(
        plan_data: dict | None, marker_elem_ids: list[int], flanke_ref_id: int
    ) -> list[int]:
        """Find each Flanke element connected to one of marker_elem_ids, in either direction.

        A pair's Marker->Flanke or Flanke->Marker connection alone can be the only one present
        (e.g. left behind by a failed pair creation), so both directions must be checked here —
        otherwise orphan cleanup can delete the marker but leave its Flanke behind. Connection
        outputs may be a dict or a list (Comexio returns either shape — see
        _function_plan_existing_refs, which normalizes the same way); missing that here would
        make orphan cleanup silently fail to find (and delete) the paired Flanke whenever a
        plan happens to use the list shape.
        """
        if not plan_data:
            return []
        elements = plan_data.get("elements", {})
        marker_elem_id_set = set(marker_elem_ids)

        def _is_flanke(elem_id: int | None) -> bool:
            if elem_id is None:
                return False
            ref = (elements.get(str(elem_id)) or {}).get("reference") or {}
            return str(ref.get("type")) == "5" and str(ref.get("ref_id")) == str(flanke_ref_id)

        flanke_elem_ids: set[int] = set()
        for conn in (plan_data.get("connections") or {}).values():
            src_id = ComexioAPI._function_plan_elem_id((conn.get("input") or {}).get("FubElementId"))
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            dst_ids = [ComexioAPI._function_plan_elem_id(sink.get("FubElementId")) for sink in outputs]
            endpoint_ids = [src_id, *dst_ids]
            if not any(eid in marker_elem_id_set for eid in endpoint_ids):
                continue
            other_ids = [eid for eid in endpoint_ids if eid is not None and eid not in marker_elem_id_set]
            flanke_elem_ids.update(eid for eid in other_ids if _is_flanke(eid))
        return list(flanke_elem_ids)

    @staticmethod
    def _function_plan_connection_exists(plan_data: dict | None, src_elem_id: int, dst_elem_id: int) -> bool:
        """True if plan_data already has a connection from src_elem_id to dst_elem_id."""
        if not plan_data:
            return False
        for conn in (plan_data.get("connections") or {}).values():
            src_id = ComexioAPI._function_plan_elem_id((conn.get("input") or {}).get("FubElementId"))
            if src_id != src_elem_id:
                continue
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            if any(ComexioAPI._function_plan_elem_id(sink.get("FubElementId")) == dst_elem_id for sink in outputs):
                return True
        return False

    @staticmethod
    def _function_plan_flanke_wires_back(plan_data: dict | None, flanke_elem_id: int, marker_elem_id: int) -> bool:
        """True if flanke_elem_id has a Flanke->Marker connection back to marker_elem_id."""
        return ComexioAPI._function_plan_connection_exists(plan_data, flanke_elem_id, marker_elem_id)

    async def function_plan_remove_trigger_pairs(
        self, fub_id: int, source_ids: list[int], ref_type: int = 2
    ) -> tuple[int, bool]:
        """Remove orphaned Source+Flanke pairs (source no longer [TRIG]/[TP]) from the trigger plan.

        Returns (deleted element count, plan_stopped) — mirrors _delete_plan_elements_and_restart.
        Each trigger source has its own dedicated Flanke element (never shared), so it is
        always safe to remove alongside its source (marker=2/KNX=11).
        """
        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, _ = self._function_plan_existing_refs(plan_data)
        marker_elem_ids = [
            elem_id for source_id in source_ids if (elem_id := existing_by_ref.get((ref_type, source_id)))
        ]
        flanke_elem_ids = self._function_plan_paired_flanke_ids(plan_data, marker_elem_ids, int(FUB_BASE_REF_ID_FLANKE))
        elem_ids = marker_elem_ids + flanke_elem_ids
        if not elem_ids:
            return 0, False
        result = await self._delete_plan_elements_and_restart(fub_id, elem_ids, [], self.function_plan_name(fub_id))
        return result.get("deleted_elem_count", 0), bool(result.get("plan_stopped"))

    async def function_plan_add_io_pairs(
        self,
        fub_id: int,
        ext_name: str,
        identifiers: list[str],
        column_index: int = 0,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Add IO (type=1) + Web-IO (type=10) element pairs for one extension column.

        identifiers: the IOs to wire in this run. Row slots derive from ALL of the
        extension's HA-relevant IOs (io_column_rows), so a pair retrofitted later lands
        exactly in the slot the initial layout reserved for it — IO plans therefore never
        need a sort pass. Should a column outgrow the canvas anyway, it wraps into a
        sub-column right next to it as a defensive fallback (the plan-creation side is
        expected to pick a canvas tall enough to avoid this).
        Returns (added_identifiers, error_messages).
        """

        def _expected_names(data: dict) -> set[str]:
            ext_idents = {io["identifier"] for io in data.get("io", []) if io["ext_name"] == ext_name}
            return {f"HA IO {ext_name} {ident}" for ident in identifiers if ident in ext_idents}

        fresh_data = await self._reload_config_until_commands_ready(_expected_names)
        webio_commands = fresh_data.get("webio_commands", {})
        ext_ios = {io["identifier"]: io for io in fresh_data.get("io", []) if io["ext_name"] == ext_name}
        rows = io_column_rows(list(ext_ios))

        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        _, y_max = self.get_fub_canvas_bounds(fub_id)
        rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))

        added: list[str] = []
        errors: list[str] = []
        todo = sorted(identifiers, key=io_sort_key)
        for i, ident in enumerate(todo):
            err = await self._function_plan_add_single_io_pair(
                fub_id,
                ext_name,
                ident,
                ext_ios,
                webio_commands,
                existing_by_ref,
                conn_endpoints,
                (rows.get(ident, 0), column_index, rows_per_col),
                plan_data,
            )
            if err is None:
                added.append(ident)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(todo))

        return added, errors

    async def _function_plan_add_single_io_pair(
        self,
        fub_id: int,
        ext_name: str,
        ident: str,
        ext_ios: dict[str, dict],
        webio_commands: dict,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        slot: tuple[int, int, int],
        plan_data: dict | None = None,
    ) -> str | None:
        """Add one IO+Web-IO pair at its deterministic slot=(row, column_index, rows_per_col).

        Return semantics as _function_plan_wire_ref_pair (None = added, "" = already wired).
        plan_data: see _function_plan_wire_ref_pair — needed so the orphan-pair/reattach branches
        there union onto the IO source's CURRENT sinks instead of resaving conn_id="new" over an
        existing connection (silent-failure-hunter finding, 2026-09-18: this path used to omit
        plan_data entirely, reproducing Bug #2 for IO sources even though the structurally
        identical Marker/KNX path — function_plan_add_source_pairs/_function_plan_add_single_pair
        — already threaded it through).
        """
        label = f"{ext_name} {ident}"
        io_entry = ext_ios.get(ident)
        if not io_entry:
            return f"{label}: not found in fresh config"

        expected_cmd_name = f"HA IO {ext_name} {ident}"
        webio_cmd = webio_commands.get(expected_cmd_name)
        if not webio_cmd:
            _LOGGER.warning("function_plan_add_io_pairs: %s — Web-IO '%s' not found", label, expected_cmd_name)
            return f"{label}: Web-IO '{expected_cmd_name}' not found after config reload"

        web_ref_id = webio_cmd.get("webIoId")
        if web_ref_id is None:
            return f"{label}: no webIoId for '{expected_cmd_name}'"

        try:
            io_ref_id = int(io_entry["id"])
        except (TypeError, ValueError):
            return f"{label}: invalid internal io id '{io_entry.get('id')}'"

        row, column_index, rows_per_col = slot
        sub_col, row_in_col = divmod(row, rows_per_col)
        x_off = (column_index + sub_col) * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
        pos = (
            FUNCTION_PLAN_LAYOUT_X_MARKER + x_off,
            FUNCTION_PLAN_LAYOUT_X_WEBIO + x_off,
            FUNCTION_PLAN_LAYOUT_Y_START + row_in_col * FUNCTION_PLAN_LAYOUT_Y_STEP,
        )
        conn_type = "binary" if io_entry.get("is_binary") else "analog"
        return await self._function_plan_wire_ref_pair(
            fub_id, 1, io_ref_id, int(web_ref_id), conn_type, label, existing_by_ref, conn_endpoints, pos, plan_data
        )

    async def function_plan_rebuild_plan_from_snapshot(
        self, fub_id: int, snapshot: dict[str, Any]
    ) -> tuple[int, int, list[str]]:
        """Recreate every element and connection from a snapshot on a freshly created (empty) plan.

        Snapshot element IDs are plan-local and meaningless on a new plan: pass 1 (re)creates
        every element regardless of type (Marker, WebIO, IO, any catalog function block,
        Comment, Constant), building an old-id -> new-id map; pass 2 redraws every connection
        using that map with its original port positions/polarity (see
        function_plan_catalog.py for why Constants need special handling: their value lives
        in the element's own "name" field, ref_id is always 0).

        Returns (elements_created, connections_created, warnings).
        """
        elements = snapshot.get("elements", {})
        connections = snapshot.get("connections", {})

        id_map, warnings = await self._rebuild_all_elements(fub_id, elements)
        connections_created = 0
        for conn_id, conn in connections.items():
            c_delta, conn_warnings = await self._rebuild_one_connection(fub_id, conn_id, conn, id_map)
            connections_created += c_delta
            warnings.extend(conn_warnings)

        _LOGGER.info(
            "function_plan_rebuild_plan_from_snapshot: fub=%s elements=%d connections=%d warnings=%d",
            fub_id,
            len(id_map),
            connections_created,
            len(warnings),
        )
        return len(id_map), connections_created, warnings

    async def _rebuild_all_elements(self, fub_id: int, elements: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
        """(Re)create every snapshot element on a fresh plan. Returns ({old_id: new_id}, warnings)."""
        id_map: dict[str, int] = {}
        warnings: list[str] = []
        for old_id, elem in elements.items():
            ref = elem.get("reference", {})
            elem_type = ref.get("type")
            ref_id = ref.get("ref_id", 0)
            x, y = elem.get("position_x", 0.0), elem.get("position_y", 0.0)
            if elem_type == FUNCTION_PLAN_COMMENT_TYPE:
                text = (elem.get("name") or "").strip() or FUNCTION_PLAN_MANAGED_PLAN_COMMENT
                new_id = await self.function_plan_add_comment_element(fub_id, text, x=x, y=y)
            elif elem_type == FUNCTION_PLAN_CONSTANT_TYPE:
                new_id = await self.function_plan_add_constant_element(fub_id, elem.get("name", "0"), x=x, y=y)
            else:
                new_id = await self.function_plan_add_element(
                    fub_id=fub_id, ref_id=ref_id, element_type=elem_type, x=x, y=y
                )
            if new_id is None:
                warnings.append(f"element {old_id} (type={elem_type}, ref_id={ref_id}) failed to recreate")
                continue
            id_map[old_id] = new_id
        return id_map, warnings

    async def _rebuild_one_connection(
        self, fub_id: int, conn_id: str, conn: dict[str, Any], id_map: dict[str, int]
    ) -> tuple[int, list[str]]:
        """Recreate one snapshot connection (one source -> one or more sinks) via the id_map.

        Comexio's own semantics: "input" is the source element, "output" is the list of
        sink elements it fans out to (see project memory project_logikplan_api.md). All sinks
        for one source MUST be sent in a single saveconnection call — sending them as
        separate calls silently drops every wire from that source when it's an IO or Constant
        element (confirmed live 2026-08-22; catalog function-block sources tolerated the
        split, IO/Constant sources did not).

        Returns (connections_created, warnings) — created is 0 or 1 (one API call per entry).
        """
        inp = conn.get("input", {})
        old_src = str(inp.get("FubElementId"))
        new_src = id_map.get(old_src)
        if new_src is None:
            return 0, [f"connection {conn_id}: source element {old_src} was not recreated — skipped"]
        conn_type = "analog" if conn.get("type") in (1, "analog") else "binary"

        raw_outputs = conn.get("output", [])
        raw_outputs = list(raw_outputs.values()) if isinstance(raw_outputs, dict) else raw_outputs

        warnings: list[str] = []
        outputs: list[tuple[int, int, bool]] = []
        for out in raw_outputs:
            old_dst = str(out.get("FubElementId"))
            new_dst = id_map.get(old_dst)
            if new_dst is None:
                warnings.append(f"connection {conn_id}: sink element {old_dst} was not recreated — skipped")
                continue
            outputs.append((new_dst, out.get("IOPos", 0), out.get("Inverted", False)))

        if not outputs:
            return 0, warnings

        result = await self.function_plan_save_connection(
            fub_id,
            new_src,
            outputs,
            conn_type,
            input_pos=inp.get("IOPos", 0),
            input_inverted=inp.get("Inverted", False),
        )
        if result is None:
            warnings.append(f"connection {conn_id}: {old_src}->{[o[0] for o in outputs]} save_connection failed")
            return 0, warnings
        return 1, warnings

    def function_plan_name(self, fub_id: int) -> str:
        """Display name of a Function Plan for logging/reporting; falls back to the id."""
        return next(
            (fd.get("Name", str(fub_id)) for fid, fd in self._fub_data.items() if int(fid) == fub_id), str(fub_id)
        )

    @staticmethod
    def _find_source_element_id(elements: dict[str, Any], source_id: int, ref_type: int = 2) -> str | None:
        """Return the plan-local element id of the given source ref (marker=2/KNX=11), or None if not wired."""
        for elem_id, elem_data in elements.items():
            ref = elem_data.get("reference", {})
            # reference.type comes back as int or str depending on the response shape — normalize.
            if str(ref.get("type")) == str(ref_type) and int(ref.get("ref_id", -1)) == source_id:
                return str(elem_id)
        return None

    @staticmethod
    def _connection_output_ids(conn_data: dict[str, Any]) -> list[str]:
        """Normalize a connection's output endpoints (server may serialize as dict or list)."""
        outputs = conn_data.get("output") or []
        if isinstance(outputs, dict):
            outputs = list(outputs.values())
        return [str(o.get("FubElementId")) for o in outputs if isinstance(o, dict)]

    @staticmethod
    def _element_has_any_wiring(elem_id: str, plan_data: dict) -> bool:
        """True if elem_id is a source (input) or sink (output) of any connection in this plan.

        Broader than a WebIO-specific check — used to tell "not wired to a WebIO" apart from
        "not wired at all", since only the latter is safe to delete outright.
        """
        for conn_data in plan_data.get("connections", {}).values():
            if str(conn_data.get("input", {}).get("FubElementId", -1)) == elem_id:
                return True
            if elem_id in ComexioAPI._connection_output_ids(conn_data):
                return True
        return False

    @staticmethod
    def _find_wired_webio_ids_for_marker(marker_id: int, marker_elem_id: str, plan_data: dict) -> list[int]:
        """Return the webIoIds of all WebIO elements directly wired to the given marker element."""
        elements = plan_data.get("elements", {})
        webio_ids: list[int] = []
        for conn_data in plan_data.get("connections", {}).values():
            input_elem = conn_data.get("input", {})
            if str(input_elem.get("FubElementId", -1)) != marker_elem_id:
                continue
            for out_elem_id in ComexioAPI._connection_output_ids(conn_data):
                ref = elements.get(out_elem_id, {}).get("reference", {})
                if ref.get("type") != 10:
                    continue
                raw_ref_id = ref.get("ref_id")
                if raw_ref_id is None:
                    continue
                try:
                    webio_ids.append(int(raw_ref_id))
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "_find_wired_webio_ids_for_marker: malformed WebIO ref_id %r on elem=%s (M%d)",
                        raw_ref_id,
                        out_elem_id,
                        marker_id,
                    )
        return webio_ids

    @staticmethod
    def _find_webio_wiring(webio_id: int, plan_data: dict) -> list[int] | None:
        """Return [webio_elem_id, connected_source_elem_id] if webio_id is wired in this plan.

        The source element is whatever is wired to the WebIO element's input side (marker or
        raw IO — the type doesn't matter here, it's simply the other end of the same wire).
        Returns None if this webIoId has no element in the plan, or the element isn't wired.
        """
        elements = plan_data.get("elements", {})
        webio_elem_id = next(
            (
                eid
                for eid, elem in elements.items()
                if elem.get("reference", {}).get("type") == 10
                and str(elem.get("reference", {}).get("ref_id")) == str(webio_id)
            ),
            None,
        )
        if webio_elem_id is None:
            return None
        for conn_data in plan_data.get("connections", {}).values():
            if webio_elem_id not in ComexioAPI._connection_output_ids(conn_data):
                continue
            input_elem_id = conn_data.get("input", {}).get("FubElementId")
            if input_elem_id is None:
                continue
            return [int(webio_elem_id), int(input_elem_id)]
        return None

    async def _delete_plan_elements_and_restart(
        self, fub_id: int, elem_ids_to_delete: list[int], webio_cmd_ids: list[int], plan_name: str
    ) -> dict:
        """Stop the plan, delete the given elements, restart it, and build the result dict."""
        stop_ok = await self.function_plan_stop_fup(fub_id)
        if not stop_ok:
            _LOGGER.error(
                "_delete_plan_elements_and_restart: failed to stop plan '%s' (fub=%s), aborting cleanup",
                plan_name,
                fub_id,
            )
            return {
                "deleted_elem_count": 0,
                "webio_cmd_ids": [],
                "fub_id": fub_id,
                "plan_stopped": False,
                "plan_name": plan_name,
                "stop_failed": True,
            }

        success = await self.function_plan_delete_elements(elem_ids_to_delete)
        if not success:
            _LOGGER.error("_delete_plan_elements_and_restart: element deletion failed")
            restart_after_failure_ok = await self.function_plan_run_fup(fub_id)
            return {
                "deleted_elem_count": 0,
                "webio_cmd_ids": [],
                "fub_id": fub_id,
                "plan_stopped": not restart_after_failure_ok,
                "plan_name": plan_name,
            }

        restart_ok = await self.function_plan_run_fup(fub_id)
        _LOGGER.info(
            "_delete_plan_elements_and_restart: deleted %d elements, webio_cmd_ids=%s (plan '%s' %s)",
            len(elem_ids_to_delete),
            webio_cmd_ids,
            plan_name,
            "restarted" if restart_ok else "restart failed — left stopped",
        )
        return {
            "deleted_elem_count": len(elem_ids_to_delete),
            "webio_cmd_ids": webio_cmd_ids,
            "fub_id": fub_id,
            "plan_stopped": not restart_ok,
            "plan_name": plan_name,
        }

    async def set_value(
        self,
        target_type: str,
        target_id: str | int,
        value: float | int,
        ext: str | None = None,
        identifier: str | None = None,
    ) -> bool:
        """API write via Basic Auth."""
        auth = aiohttp.BasicAuth(self.api_user, self.api_pass or "") if self.api_user else None

        if auth is not None and not self._auth_warned and not _is_local_address(self.host):
            _LOGGER.warning(
                "Using Basic Auth over plain HTTP on a non-local address. Credentials may be transmitted in clear text."
            )
            self._auth_warned = True

        url = f"{self._base_url}/api/"
        params: dict[str, Any] = {"action": "set", "value": value}

        if target_type == "marker":
            params["marker"] = f"M{target_id}"
        elif target_type == "knx":
            # BLIND GUESS pending real KNX hardware: mirrors the marker path with a "K" prefix.
            # Verify the actual /api/ query-param name against a live Comexio server once available.
            params["knx"] = f"K{target_id}"
        else:
            if ext is None or identifier is None:
                _LOGGER.error("Missing 'ext' or 'identifier' for non-marker API write. Type: %s", target_type)
                return False
            params["ext"] = ext
            params["io"] = identifier

        try:
            async with self.session.get(url, params=params, auth=auth) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Comexio API write failed: HTTP %s for %s with params=%s",
                        resp.status,
                        url,
                        {k: v for k, v in params.items() if k != "value"},
                    )
                    return False
                return True
        except aiohttp.ClientError as err:
            _LOGGER.exception(
                "Comexio API write request error for %s with params=%s: %s",
                url,
                {k: v for k, v in params.items() if k != "value"},
                err,
            )
            return False
        except Exception:
            _LOGGER.exception(
                "Unexpected error during Comexio API write for %s with params=%s",
                url,
                {k: v for k, v in params.items() if k != "value"},
            )
            return False

    async def get_bus_workload(self) -> dict[str, Any]:
        """Fetch the internal bus workload (%) and SD-card presence from the admin interface.

        Called on a fast, independent poll cadence (see coordinator's bus-load loop) —
        much more frequent than the main config audit, so failures are logged at debug
        level only to avoid log spam.
        """
        url = f"{self._base_url}/admin/in_output/inoutputinfo"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/in_output/home"}
        try:
            async with self.session.post(url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Bus workload fetch failed with HTTP status: %s", resp.status)
                    return {}
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.debug("HTTP request error fetching bus workload: %s", err)
            return {}
        except Exception as err:
            _LOGGER.debug("Unexpected error fetching bus workload: %s", err)
            return {}

    async def system_emergency_reboot(self) -> bool:
        """Trigger an IMMEDIATE, unconfirmed full Comexio system reboot.

        Comexio has no confirmation dialog for this and returns no structured result — the
        request itself is the action. Only called by the Bus-Load-Watchdog's emergency path,
        gated behind CONF_BUS_WATCHDOG_AUTO_REBOOT (default off). HTTP 200 only means the
        request was accepted, not that the reboot completed cleanly.
        """
        url = f"{self._base_url}/admin/admin_dashboard/home/"
        try:
            async with self.session.get(url, params={"id": "system", "restart": "1"}) as resp:
                _LOGGER.warning("system_emergency_reboot: request sent, HTTP status %s", resp.status)
                return resp.status == 200
        except aiohttp.ClientError as err:
            _LOGGER.exception("system_emergency_reboot: HTTP request error: %s", err)
            return False
        except Exception:
            _LOGGER.exception("system_emergency_reboot: unexpected error")
            return False

    async def check_extension_firmware(self) -> list[dict[str, Any]]:
        """Query the local extension bus for available firmware updates (BASE + all extensions).

        Comexio documents that this can briefly interrupt extension outputs while it runs, so
        it must only be called rarely — see the coordinator's version-gated nightly check, not
        a regular poll. Logged at warning level (not debug) since failures here are infrequent
        enough to matter, unlike the fast bus-workload poll.
        """
        url = f"{self._base_url}/admin/extension/checkextension_fwupdate/"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/"}
        try:
            async with self.session.post(url, data={"pos": "local"}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Extension firmware check failed with HTTP status: %s", resp.status)
                    return []
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("HTTP request error checking extension firmware: %s", err)
            return []
        except Exception as e:
            _LOGGER.exception("Unexpected error checking extension firmware: %s", e)
            return []
        if payload.get("ok") != "ok":
            _LOGGER.warning("Extension firmware check returned an error payload: %s", payload)
            return []
        return payload.get("data", [])

    def close(self) -> None:
        """Detach the main session and the dedicated preview session, if one was ever opened.

        The main session is created during config entry setup, so async_create_clientsession
        already registers it for auto-cleanup on entry unload/HA shutdown. The preview session
        (see ensure_preview_session) is created lazily at runtime, outside that setup context,
        so it only gets HA's homeassistant_stop cleanup — not entry-unload cleanup. Detaching
        both explicitly here (already called from async_unload_entry and the setup-failure path)
        avoids leaking the preview session's connection across integration reloads.

        Uses detach() rather than close(): HA replaces close() on its own sessions with a
        warn-only stub (warn_use), so await session.close() never released anything — it only
        logged the "closes the Home Assistant aiohttp session" deprecation. detach() is what
        HA's own cleanup does and actually unlinks the session from the pooled, hass-scoped
        connector (keyed by verify_ssl/family/ssl_cipher) shared with every other session.

        Also flags the instance as closed so a login() already in flight inside
        ensure_preview_session()'s lock detaches its freshly-authenticated session instead of
        assigning it to self._preview_session after this point — that session would otherwise
        never be detached (it was never visible here to begin with).
        """
        self._closed = True
        self.session.detach()
        if self._preview_session is not None:
            self._preview_session.detach()
            self._preview_session = None
