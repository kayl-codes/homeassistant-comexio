# Version: 0.7.5
import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
import logging
import time
from typing import Any, NoReturn

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
    ComexioDataError,
    ComexioError,
    ComexioRequestRejectedError,
    ComexioResponseError,
    CreatedFunctionPlan,
    config as comexio_config,
    webio as comexio_webio,
)
from aiocomexio.config import ParseOptions, iter_group
from aiocomexio.const import WebioClass
from aiocomexio.reference_catalog import KIND_FUB_BASE, ReferenceCheck
from aiocomexio.session import session_kwargs
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

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
    FUB_BASE_KEY_FLANKE,
    FUNCTION_PLAN_FETCH_MARK_SLOTS,
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
    MARKER_KNX_BRIDGE_BLOCK_SIZE,
    MARKER_KNX_BRIDGE_SUFFIX_RE,
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_DEVICE_NAME_KNX_LOOPBACK,
    category_by_fub_module_type,
    io_column_rows,
    io_sort_key,
    is_valid_entity_name_schema,
    knx_loopback_command_name,
)
from .function_plan_block_settings import BlockSettings, block_settings_form, parse_block_settings

# Function-plan element reference types needing special handling in function_plan_rebuild_plan_from_snapshot.
FUNCTION_PLAN_COMMENT_TYPE = 14
FUNCTION_PLAN_CONSTANT_TYPE = 16
# Comexio's paper ids ($Fubs "Paper") per format aiocomexio accepts, for the plan cache after a settings save.
_PLAN_PAPER_IDS = {"A3": "2", "A4": "3", "A5": "4"}
_PLAN_PAPER_FALLBACK = "A4"
# Comexio's answer (as quoted in aiocomexio's refusal message) for a plan that is not running.
_PLAN_NOT_RUNNING_ANSWER = "0:not_found"
# Comexio's editor action that stores block settings ($FubBaseConfig rows) of one element.
_BLOCK_SETTINGS_SAVE_PATH = "/admin/function_function_module/savefubbaseconfig/"

_LOGGER = logging.getLogger(__name__)


def _webhook_path(server_id: str) -> str:
    """Path of HA's webhook that the Web-IO commands of this server POST to."""
    return f"/api/webhook/comexio_{server_id}"


def _raise_transport_error(err: ComexioConnectionError) -> NoReturn:
    """Re-raise the aiohttp.ClientError / TimeoutError behind err, else err itself.

    For the adapters whose callers have always handled transport failures as those two
    exception types (DataUpdateCoordinator reports them as a plain "Error requesting data"
    instead of an unexpected error with a traceback).
    """
    cause = err.__cause__
    if isinstance(cause, (aiohttp.ClientError, TimeoutError)):
        raise cause from None
    raise err


async def _attempt[T, F](
    what: str, call: Callable[[], Awaitable[T]], failed: F, *, transport_raises: bool = False
) -> T | F:
    """call()'s result, or `failed` after a warning naming `what` if it raised.

    The adapters for the aiocomexio write requests keep their callers' bool / id-or-None
    contract this way. TypeError / ValueError: aiocomexio checks ids, coordinates and plan
    settings before it sends anything, and the int() / float() casts the adapters apply to
    ids read from plan payloads happen inside call() as well. A TypeError is logged with its
    traceback — it may just as well be a call that no longer fits the library.

    transport_raises: re-raise a transport failure as aiohttp.ClientError / TimeoutError, for
    the adapters whose callers (the restore paths in services/backup.py) abort on those.
    """
    try:
        return await call()
    except ComexioConnectionError as err:
        if transport_raises:
            _raise_transport_error(err)
        _LOGGER.warning("%s failed: %s", what, err)
        return failed
    except (ComexioError, TypeError, ValueError) as err:
        # A note tells e.g. that a plan was created although reading back its id failed.
        notes = "".join(f" ({note})" for note in getattr(err, "__notes__", ()))
        _LOGGER.warning("%s failed: %s%s", what, err, notes, exc_info=isinstance(err, TypeError))
        return failed


def _plan_paper_and_orientation(paper_format: str, orientation: str) -> tuple[str, str]:
    """Paper format and orientation as aiocomexio accepts them.

    Comexio offers only A3/A4/A5; any other value (e.g. from a damaged backup) falls back to
    A4 / landscape, as the requests before aiocomexio did, instead of failing the whole restore.
    """
    paper = paper_format.upper()
    if paper not in _PLAN_PAPER_IDS:
        _LOGGER.warning("Paper format %r is not supported for plans — using %s", paper_format, _PLAN_PAPER_FALLBACK)
        paper = _PLAN_PAPER_FALLBACK
    return paper, "portrait" if orientation.lower() == "portrait" else "landscape"


async def _succeeded(what: str, call: Callable[[], Awaitable[object]], *, transport_raises: bool = False) -> bool:
    """_attempt for a request without an answer worth returning: True, or False after a warning."""

    async def run() -> bool:
        await call()
        return True

    return await _attempt(what, run, False, transport_raises=transport_raises)


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


def _reference_type(ref: Any) -> int | None:
    """An element reference's integer type (int or numeric string), or None if unreadable.

    A missing or malformed type must not pass as "not a marker" — the reference could still
    point at a marker, which the force gate would then treat as unplaced.
    """
    if not isinstance(ref, dict):
        return None
    ref_type = ref.get("type")
    if isinstance(ref_type, bool):
        return None
    if isinstance(ref_type, int):
        return ref_type
    if isinstance(ref_type, str):
        # isascii+isdecimal, not isdigit: "²".isdigit() is True but int("²") raises.
        text = ref_type.strip()
        if text.isascii() and text.isdecimal() and len(text) <= 10:
            return int(text)
    return None


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
        ref_type = _reference_type(ref)
        if ref_type is None:
            return None
        if ref_type != 2:
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


def _marker_category_is(record: dict[str, Any], expected: int) -> bool:
    """True if the marker record's CategoryId is exactly the int `expected`.

    Python's loose equality makes True == 1 and 1.0 == 1, so a plain "==" check would let a
    malformed CategoryId (e.g. a scraped JSON boolean) alias onto a real category — only a
    genuine int, not a bool, is accepted; a missing CategoryId, 1.0 or "1" never matches.
    """
    value = record.get("CategoryId")
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _is_api_created_marker(record: dict[str, Any]) -> bool:
    """True if the marker record carries CategoryId==1 (created via the admin API, not Studio)."""
    return _marker_category_is(record, 1)


def _is_studio_marker(record: dict[str, Any]) -> bool:
    """True if the marker record carries CategoryId==0 (factory or Studio-created).

    marker_delete's force path only applies to this known category — an unknown or malformed
    CategoryId stays protected even when the marker is untitled and unplaced.
    """
    return _marker_category_is(record, 0)


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
    - CategoryId==0 (factory or Studio-created): protected, unless force is active
      (placed_ids is not None) AND the marker has no title AND it is not placed in any
      function plan.
    - Anything else (unknown/malformed CategoryId): always protected, force or not.
    placed_ids=None means force is off — or the plans couldn't all be
      loaded, in which case placement is unknown and nothing may pass on that basis.
    """
    deletable: list[int] = []
    protected: list[int] = []
    for mid in marker_ids:
        record = records.get(mid)
        if (
            record is None
            or _is_api_created_marker(record)
            or (
                placed_ids is not None
                and _is_studio_marker(record)
                and not _marker_has_title(record)
                and mid not in placed_ids
            )
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


def _saved_schema(conf_data: Mapping[str, Any], key: str, default: str) -> str:
    """Saved entity-name schema, or default if aiocomexio would reject it.

    ParseOptions validates every schema up front, so a malformed one saved before the options
    flow validated schemas (possibly for a category the form now hides) would otherwise fail
    every poll and keep the whole entry from setting up.
    """
    schema = conf_data.get(key, default)
    if is_valid_entity_name_schema(schema):
        return schema
    _LOGGER.warning(
        "Ignoring the invalid saved %s %r and using the default %r, fix it in the options", key, schema, default
    )
    return default


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
        self._preview_client: ComexioClient | None = None
        # The host / credentials the preview session logged in with.
        self._preview_credentials: tuple[str, ...] = ()
        # aiocomexio client on the main session; see the client property.
        self._client: ComexioClient | None = None
        self._client_credentials: tuple[str, ...] = ()
        # The client whose last full login succeeded — login() skips the full login while its
        # session is still logged in (see there).
        self._logged_in_client: ComexioClient | None = None
        # Serializes full logins: two at once clear each other's cookie jar mid-login.
        self._login_lock = asyncio.Lock()
        # Bumped by every successful full login, so a caller that waited for the lock sees
        # that another one already logged the session in again.
        self._login_generation: int = 0
        # Plans whose connection values last came back in an unexpected shape (warned once).
        self._connection_values_shape_warned: set[int] = set()
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
        # Block settings of every plan element ($FubBaseConfig, see function_plan_block_settings)
        # from the last config fetch; None when that fetch did not carry a readable table, so a
        # backup then stores none rather than an ever older copy.
        self.block_settings: BlockSettings | None = None
        # Result of the last reference catalog reconciliation (reference_catalog.reconcile, set by
        # the coordinator each poll) — the only source of block-type ids such as the Flanke's.
        self.reference_check: ReferenceCheck | None = None
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
        # Called with no arguments when set_fub_active changed a plan's cached Active flag or
        # create_fup added a plan, so the coordinator can refresh the entities showing the plans
        # (see ComexioCoordinator.__init__) — a new plan gets its run-state sensor right away.
        self.run_state_listener: Callable[[], None] | None = None
        # Run states HA itself caused: fub_id_str → (running, mark). A config or run-state fetch
        # that started before such a change (run_state_mark) brings the older state and must not
        # write it back over HA's own — same idea as the webhook guard R1 in the coordinator.
        self._run_state_mark = 0
        self._ha_run_states: dict[str, tuple[bool, int]] = {}
        # fub_id_str → time.monotonic() of HA's own last stop of the plan (ha_stopped_within).
        self._ha_stopped_at: dict[str, float] = {}
        # (fetched $Fubs dict, run_state_mark taken before its get_raw_config fetch): lets every
        # path that writes a fetched plan list or plan into the cache apply that guard on its own.
        self._fetch_marks: deque[tuple[dict[str, Any], int]] = deque(maxlen=FUNCTION_PLAN_FETCH_MARK_SLOTS)
        # Bumped with every fetched plan list or plan written into the cache (fub_cache_epoch): a
        # run-state answer whose fetch began before such a write may be older than its Active flags.
        self._fub_cache_epoch = 0
        self._paper_data: dict[str, Any] = {}  # paper_id_str → {Id, Name, MMX, MMY}
        # Set by login() on failure so callers (setup) can tell a transient connection
        # problem (retry) apart from a genuine credential rejection (needs reauth).
        self.last_login_error: str | None = None

    def _build_session_kwargs(self) -> dict[str, Any]:
        """Session kwargs shared by the main session and the preview session (own cookie jar each)."""
        return session_kwargs(timeout=COMEXIO_HTTP_TIMEOUT_SEC, progress_log_interval=COMEXIO_PROGRESS_LOG_INTERVAL_SEC)

    async def ensure_preview_session(self) -> aiohttp.ClientSession | None:
        """Lazily create + log in the dedicated Stufe-2 preview session, reused after that.

        Returns None if the login fails — the caller falls back to the main session for that
        one poll tick rather than blocking the preview on a retry loop; the next tick tries
        the dedicated session again from scratch (a fresh session, since a stale/rejected
        cookie jar wouldn't fix itself).
        """
        self._drop_outdated_preview_session()
        if self._preview_session is not None:
            return self._preview_session
        async with self._preview_session_lock:
            # Re-check: another tick may have finished creating the session while this
            # one was waiting for the lock — and the settings may have changed since.
            self._drop_outdated_preview_session()
            if self._preview_session is not None:
                return self._preview_session
            if self._closed:
                return None
            session = async_create_clientsession(self.hass, **self._build_session_kwargs())
            credentials = self._credentials()
            client = self._new_client(session)
            login_ok = False
            try:
                login_ok = await self._login(client)
            finally:
                # Any non-success path (failed login, close() or new connection settings during
                # the await above, or _login() raising) must not leave an authenticated session
                # orphaned.
                settings_changed = credentials != self._credentials()
                if not login_ok or self._closed or settings_changed:
                    session.detach()
            if not login_ok:
                _LOGGER.warning("Preview session login failed — Stufe-2 poll falls back to the main session")
                return None
            if settings_changed:
                _LOGGER.info("Connection settings changed during the preview login — opening a new session next poll")
                return None
            if self._closed:
                return None
            self._preview_session = session
            self._preview_client = client
            self._preview_credentials = credentials
            return session

    def _credentials(self) -> tuple[str, ...]:
        return (self.host, self.username, self.password, self.api_user or "", self.api_pass or "")

    def _new_client(self, session: aiohttp.ClientSession) -> ComexioClient:
        host, username, password, api_user, api_pass = self._credentials()
        return ComexioClient(host, username, password, session=session, api_username=api_user, api_password=api_pass)

    @property
    def client(self) -> ComexioClient:
        """The aiocomexio client on the main session.

        Rebuilt whenever host or credentials change — the coordinator's reconfigure path
        assigns new values to host / username / password / api_user / api_pass and logs in again.
        A preview session that logged in with other settings is dropped as well — its client and
        cookie belong to the old ones — so ensure_preview_session opens a new one on the next poll.
        """
        self._drop_outdated_preview_session()
        if self._client is None or self._client_credentials != self._credentials():
            self._client = self._new_client(self.session)
            self._client_credentials = self._credentials()
        return self._client

    def _drop_outdated_preview_session(self) -> None:
        """Detach a preview session that logged in with other host / credentials than the current ones."""
        if self._preview_session is None or self._preview_credentials == self._credentials():
            return
        _LOGGER.info("Comexio connection settings changed — reopening the preview session")
        self._preview_session.detach()
        self._preview_session = None
        self._preview_client = None

    @property
    def _base_url(self) -> str:
        """Return the base URL for the Comexio IO-Server."""
        # The IO-Server is reached on the local network over plain HTTP; HTTPS would be a new feature.
        return f"http://{self.host}"  # NOSONAR

    @property
    def fub_data(self) -> dict[str, Any]:
        """Return function plan metadata (fub_id_str → {Id, Name, Paper, ...}), populated by parse_config()."""
        return self._fub_data

    def update_fub_cache_entry(self, fub_id: int | str, fub_info: dict[str, Any]) -> None:
        """Refresh a single plan's cached metadata (e.g. after an out-of-band get_raw_config() lookup).

        A plan HA started or stopped after that fetch began keeps HA's run state.
        """
        key = str(fub_id)
        # Looked up before the write: the cache is often the last poll's own fetched $Fubs, and
        # holding fub_info would make it match too — with that older poll's mark.
        since = self._fetch_mark(lambda fubs: fubs.get(key) is fub_info)
        self._fub_data[key] = fub_info
        self._fub_cache_epoch += 1
        if since is not None and (running := self._ha_run_states_since(since).get(int(fub_id))) is not None:
            self.apply_fub_run_states({int(fub_id): running})

    def _fetch_mark(self, matches: Callable[[dict[str, Any]], bool]) -> int | None:
        """run_state_mark taken before the newest remembered fetch whose $Fubs matches, None if unknown."""
        return next((mark for fubs, mark in reversed(self._fetch_marks) if matches(fubs)), None)

    def _replace_fub_data(self, fubs: dict[str, Any], run_state_mark: int | None = None) -> None:
        """Make a fetched plan list the cache, keeping the run states HA caused since its fetch began.

        run_state_mark defaults to the mark get_raw_config() remembered for this $Fubs dict.
        """
        if run_state_mark is None:
            run_state_mark = self._fetch_mark(lambda known: known is fubs)
        self._fub_data = fubs
        self._fub_cache_epoch += 1
        if run_state_mark is not None:
            self.apply_fub_run_states(self._ha_run_states_since(run_state_mark))

    def run_state_mark(self) -> int:
        """Mark to take before a config or run-state fetch; pass it on as `since` when applying the result."""
        return self._run_state_mark

    def fub_cache_epoch(self) -> int:
        """Counter of fetched plan lists/plans written into the cache; changes when one was written."""
        return self._fub_cache_epoch

    def _ha_run_states_since(self, since: int) -> dict[int, bool]:
        """Run states HA caused after the mark `since` — newer than any fetch that started before it."""
        return {int(key): running for key, (running, mark) in self._ha_run_states.items() if mark > since}

    def apply_fub_run_states(self, states: Mapping[int, bool], since: int | None = None) -> bool:
        """Write fetched run states into the cached plans' Active flags; True if any flag changed.

        Plans the cache does not know are skipped: a run state alone is no plan entry, the next
        poll brings the plan's metadata. Each changed entry is replaced, not mutated, since
        parse_config shares the dicts with the raw config it was given. With `since` (the
        run_state_mark taken before the fetch), plans HA started or stopped meanwhile keep that state.
        """
        if since is not None and (newer := self._ha_run_states_since(since)):
            states = {fub_id: running for fub_id, running in states.items() if fub_id not in newer}
        changed = False
        for fub_id, running in states.items():
            key = str(fub_id)
            fub = self._fub_data.get(key)
            if not isinstance(fub, dict) or self.get_fub_active(fub_id) is running:
                continue
            self._fub_data[key] = {**fub, "Active": int(running)}
            changed = True
        return changed

    def set_fub_active(self, fub_id: int | str, running: bool) -> None:
        """Record a run state HA itself just caused (run_fup / stop_fup) and tell the listener."""
        self._run_state_mark += 1
        self._ha_run_states[str(fub_id)] = (running, self._run_state_mark)
        if running:
            self._ha_stopped_at.pop(str(fub_id), None)
        else:
            self._ha_stopped_at[str(fub_id)] = time.monotonic()
        if self.apply_fub_run_states({int(fub_id): running}) and self.run_state_listener is not None:
            self.run_state_listener()

    def ha_stopped_within(self, fub_id: int, seconds: float) -> bool:
        """Whether HA itself stopped the plan less than `seconds` ago and has not started it since."""
        stopped_at = self._ha_stopped_at.get(str(fub_id))
        return stopped_at is not None and time.monotonic() - stopped_at < seconds

    async def login(self) -> bool:
        """Make sure the main session is logged in; a full RSA login only if it is not.

        Services call this before every run, while the coordinator poll or a sync may be using
        the same session. aiocomexio's login clears the session's cookie jar first, so an
        unconditional login would log a working session out for its duration — and for good if
        that login then fails on a transient error. A session is therefore first checked with
        the client's is_logged_in() (which already asks once more after a dropped keep-alive
        connection), and each of its outcomes is handled on its own:

        - True: the session is kept as it is.
        - False (Comexio served its login form): full login.
        - ComexioConnectionError / ComexioResponseError: unreachable or busy, not logged out —
          False with "connection"; a full login would clear the still valid session and most
          likely fail the same way.
        - ComexioDataError (neither the session's answer nor the login form): the session's
          state is unknown, and only a full login leads back to a known one. Should Comexio be
          broken rather than logged out, that login fails and reports "connection" itself.

        Returns False on failure and sets last_login_error to "rejected" (credentials refused
        — setup asks for reauth) or "connection" (server unreachable or answering garbage —
        setup retries).
        """
        client = self.client
        if client is self._logged_in_client:
            try:
                logged_in = await client.is_logged_in()
            except (ComexioConnectionError, ComexioResponseError) as err:
                _LOGGER.warning("Comexio admin session check failed: %s", err)
                self.last_login_error = "connection"
                return False
            except ComexioDataError as err:
                _LOGGER.warning("Comexio admin session check gave an unexpected answer, logging in again: %s", err)
            except ComexioError as err:
                _LOGGER.debug("Comexio admin session check failed, logging in again: %s", err)
            else:
                if logged_in:
                    self.last_login_error = None
                    return True
                _LOGGER.debug("Comexio admin session has expired, logging in again")
        return await self._full_login()

    async def _full_login(self) -> bool:
        """Full RSA login of the main session's client, one at a time.

        A caller that had to wait for a concurrent full login takes over its success instead of
        logging in again (which would clear the jar of the session just logged in).
        """
        generation = self._login_generation
        async with self._login_lock:
            client = self.client
            if self._login_generation != generation and client is self._logged_in_client:
                return True
            if not await self._login(client):
                self._logged_in_client = None
                return False
            self._logged_in_client = client
            self._login_generation += 1
            return True

    async def _login(self, client: ComexioClient) -> bool:
        """client.login() mapped onto login()'s bool / last_login_error contract."""
        try:
            await client.login()
        except ComexioAuthenticationError as err:
            _LOGGER.debug("Comexio login rejected: %s", err)
            self.last_login_error = "rejected"
            return False
        except ComexioError as err:
            _LOGGER.warning("Comexio login failed: %s", err)
            self.last_login_error = "connection"
            return False
        _LOGGER.info("Successfully logged into Comexio Admin interface")
        self.last_login_error = None
        return True

    async def get_raw_config(self) -> dict[str, Any]:
        """The function module page's config objects ($FubModules, $Fubs, ...), keyed without "$".

        Also refreshes io_types / io_input_types (from the main admin page) and comexio_version.
        A lapsed admin session (e.g. after a Comexio reboot) is logged in again once — this runs
        on every coordinator poll, so the main session heals itself here. Returns {} if the
        server answered but the scrape failed (HTTP error status, failed re-login, no parseable
        $FubModules) — callers check for FubModules. A transport failure raises
        aiohttp.ClientError / TimeoutError.
        """
        run_state_mark = self._run_state_mark
        try:
            try:
                raw = await self.client.get_raw_config()
            except ComexioAuthenticationError:
                _LOGGER.warning("Comexio admin session is no longer logged in — logging in again")
                if not await self._full_login():
                    _LOGGER.error("Re-login to Comexio failed (%s)", self.last_login_error)
                    return {}
                raw = await self.client.get_raw_config()
        except ComexioConnectionError as err:
            _raise_transport_error(err)
        except ComexioError as err:
            _LOGGER.error("Failed to fetch the Comexio configuration: %s", err)
            return {}
        self.io_types = raw.io_types
        self.io_input_types = raw.io_input_types
        if raw.comexio_version:
            self.comexio_version = raw.comexio_version
        block_settings = parse_block_settings(raw.variables.get("FubBaseConfig"))
        if block_settings is None and self.block_settings is not None:
            _LOGGER.warning("Comexio's config page no longer carries $FubBaseConfig — block settings are not backed up")
        self.block_settings = block_settings
        if isinstance(fubs := raw.variables.get("Fubs"), dict):
            self._fetch_marks.append((fubs, run_state_mark))
        return raw.variables

    async def get_knx_dpt_catalog(self) -> dict[str, Any]:
        """Fetch $KnxPoints/$KnxDevices/$KnxDpt from the KNX admin page.

        Resolves each existing K-element's real KNX DPT (KnxBaseTypeId.KnxSubId) via the
        Point -> Device -> Dpt chain (see aiocomexio.knx.resolve_knx_dpt) — neither $FubModules["11"] nor
        $IOTypesBinary carry a usable analog value range for KNX objects (both report a
        min=max=0 placeholder, see KNX_DPT_ANALOG_RANGES in const.py). Never raises: on any
        failure (transport, HTTP status, a page without parseable data) it returns the last
        known-good catalog (or {} if none exists yet); callers then fall back to the generic
        WEBIO_MARKER_ANALOG_MIN/MAX range, same as for an unresolved DPT — a transient network
        hiccup on this opt-in sub-page must not fail the whole coordinator poll (found in review
        2026-09-20). Falling back to the stale cache instead of {} matters specifically for
        DPT3.x composite pairing (aiocomexio parse_config): an empty catalog means no item gets
        tagged knx_composite this poll, which drops the composite's unique_id from __init__.py's
        active_unique_ids whitelist and gets its cover/light entity permanently deleted from the
        registry — a single transient failure (e.g. right at HA startup) must not have that
        effect (found in review 2026-09-20). A failed fetch is never cached, so the next poll
        retries.

        Cached on the instance and only re-fetched when comexio_version changes (see
        _knx_dpt_catalog docstring) — this method's only caller is once per poll in
        coordinator._async_update_data, but the underlying data is effectively static.
        """
        if self._knx_dpt_catalog is not None and self._knx_dpt_catalog_version == self.comexio_version:
            return self._knx_dpt_catalog

        try:
            result = await self.client.get_knx_dpt_catalog()
        except ComexioError as err:
            _LOGGER.warning("Failed to fetch KNX DPT catalog, keeping the last known-good one: %s", err)
            return self._knx_dpt_catalog or {}
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

    async def get_live_states(
        self, marker_count: int, knx_max_id: int = 0
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Live values of markers 1..marker_count and KNX objects 1..knx_max_id, in one request.

        Markers and KNX objects share the same plain numeric id space (a marker and a KNX
        object can both be "5"), so their values come back as two separate id-keyed dicts —
        merging them would silently let e.g. KNX object 5 pick up marker 5's value.

        Returns (None, None) on any fetch/parse failure — never ({}, {}) — so callers can tell
        "endpoint failed this cycle" apart from "nothing to report" and keep last-known values
        instead of overwriting them with a default (found in review 2026-09-20: an empty dict
        here made every marker/KNX value silently collapse to 0/off on a single transient
        HTTP hiccup, indistinguishable from a real reading).
        """
        try:
            states = await self.client.get_live_states(marker_count, knx_max_id)
        except ComexioError as err:
            _LOGGER.error("Live states fetch failed: %s", err)
            return None, None
        return states.markers, states.knx

    async def get_function_plan_connection_values(
        self, fub_id: int, session: aiohttp.ClientSession | None = None
    ) -> dict[str, list[Any]]:
        """Fetch live per-SOURCE-ELEMENT output values for one Function Plan — Studio's own
        "fupValueData" refresh action. Unlike markers/IOs/WebIOs (get_live_states, resolved to
        pill elements), this reports values for every block-internal output (an "Oder"
        gate, a Zeitglied, ...) that carries no marker/IO of its own — the ground truth
        behind the Function Plan preview's Stufe-2 wire coloring (see
        [[project-logikplan-preview]]). Returns {source_element_id: [value_per_output_row]},
        or {} while the plan is not running.

        session: None for the main session, or the dedicated, independently logged-in preview
        session from ensure_preview_session, so this high-frequency call can never queue
        behind — or block — the main coordinator poll on a shared connection.

        Transport failures, HTTP error statuses and a lapsed session raise (ComexioError): the
        caller (_async_poll_connection_values) counts consecutive failures and disarms the
        preview after _CONNECTION_POLL_MAX_FAILURES — returning {} for them would be
        indistinguishable from "plan not running" and bypass that circuit breaker (#75). A
        lapsed preview session is dropped first, so the next tick logs in a fresh one. Only an
        answer that is not in the expected shape returns {}: a body-shape surprise, not a
        connection failure.
        """
        client = self.client
        if session is not None:
            if session is not self._preview_session or self._preview_client is None:
                # A concurrent tick dropped it (_drop_preview_session) since this one got it.
                raise ComexioAuthenticationError("The preview session was dropped, its login lapsed")
            client = self._preview_client
        try:
            values = await client.get_function_plan_connection_values(fub_id)
        except ComexioDataError as err:
            # Polled every 0.5-2 s: warn once per plan, until it answers in shape again.
            level = logging.DEBUG if fub_id in self._connection_values_shape_warned else logging.WARNING
            self._connection_values_shape_warned.add(fub_id)
            _LOGGER.log(level, "Unexpected connection values response (fub=%s): %s", fub_id, err)
            return {}
        except ComexioAuthenticationError:
            if session is not None:
                self._drop_preview_session(session)
            raise
        self._connection_values_shape_warned.discard(fub_id)
        return values

    async def get_function_plan_run_states(
        self, fub_ids: Iterable[int], session: aiohttp.ClientSession | None = None
    ) -> dict[int, bool]:
        """Whether each plan runs, {fub_id: running}, for all fub_ids in one request.

        A plan Comexio did not answer clearly is left out — the caller keeps its last known
        state (see aiocomexio's get_function_plan_run_states). session: as for
        get_function_plan_connection_values, the preview session when called from the preview
        poll. Failures raise (ComexioError), so the caller can count them.
        """
        client = self.client
        if session is not None:
            if session is not self._preview_session or self._preview_client is None:
                raise ComexioAuthenticationError("The preview session was dropped, its login lapsed")
            client = self._preview_client
        try:
            return await client.get_function_plan_run_states(fub_ids)
        except ComexioAuthenticationError:
            if session is not None:
                self._drop_preview_session(session)
            raise

    def _drop_preview_session(self, session: aiohttp.ClientSession) -> None:
        """Forget a preview session whose login lapsed; ensure_preview_session opens a new one.

        Only if it is still the current one — a concurrent tick may already have replaced it.
        """
        if self._preview_session is not session:
            return
        _LOGGER.info("Preview session is no longer logged in — opening a new one on the next poll")
        self._preview_session = None
        self._preview_client = None
        session.detach()

    def parse_config(
        self,
        conf: dict[str, Any],
        live_states: dict[str, Any] | None = None,
        referenced_markers: set[str] | None = None,
        knx_live_states: dict[str, Any] | None = None,
        knx_dpt_catalog: dict[str, Any] | None = None,
        run_state_mark: int | None = None,
    ) -> dict[str, Any]:
        """
        Processes the raw configuration and performs a technical audit.
        Uses dynamic IO type mapping to determine binary vs analog states and units.

        live_states and knx_live_states are kept as two separate params (both id-keyed) rather
        than one merged dict — see get_live_states' docstring for why merging them would be
        unsafe (markers and KNX objects share the same plain numeric id space).
        run_state_mark (run_state_mark() taken before conf was fetched) keeps the run states HA
        caused since then over the older Active flags in conf; without it, the mark
        get_raw_config() remembered for conf does.
        """
        # Cache function plan + paper metadata for later use (e.g. auto canvas-format detection).
        # aiocomexio decodes each page variable on its own: a config whose $Fubs/$Paper did not
        # decode keeps the last known cache — wiping it would turn every plan's run-state sensor
        # unavailable and empty the plan selector until the next poll. An empty plan list still
        # decodes to a dict and replaces the cache.
        if isinstance(fubs := conf.get("Fubs"), dict):
            self._replace_fub_data(fubs, run_state_mark)
        if isinstance(paper := conf.get("Paper"), dict):
            self._paper_data = paper

        webio_name, schema_marker, schema_io, schema_knx, server_alias = self._load_config_names()
        data = comexio_config.parse_config(
            conf,
            io_types=self.io_types,
            io_input_types=self.io_input_types,
            options=ParseOptions(
                webio_name=webio_name,
                server_alias=server_alias,
                schema_marker=schema_marker,
                schema_io=schema_io,
                schema_knx=schema_knx,
            ),
            live_states=live_states,
            referenced_markers=referenced_markers,
            knx_live_states=knx_live_states,
            knx_dpt_catalog=knx_dpt_catalog,
        )
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
            schema_marker = _saved_schema(conf_data, CONF_SCHEMA_MARKER, schema_marker)
            schema_io = _saved_schema(conf_data, CONF_SCHEMA_IO, schema_io)
            schema_knx = _saved_schema(conf_data, CONF_SCHEMA_KNX, schema_knx)
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
        return None if fub is None else bool(int(fub.get("Active") or 0))

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

    # --- WEB-IO MANAGEMENT ---
    async def get_webio_base_info(self, webio_name: str) -> tuple[str, bool] | None:
        """(base_id, deletable) of the Web-IO class named webio_name, or None if there is none.

        None only for a fetched page that really lists no such class. Any failure — transport,
        HTTP status, a lapsed session — raises RuntimeError instead: callers treat None as "class
        absent" and upload a fresh class on that basis, so reporting "absent" on a failed check
        would create a duplicate class next to the one that is actually still present.
        """
        try:
            info = await self.client.get_webio_base_info(webio_name)
        except ComexioError as err:
            raise RuntimeError(f"Web-IO class lookup for {webio_name!r} failed: {err}") from err
        return None if info is None else (info.base_id, info.deletable)

    async def get_webio_device_info(self, device_name: str) -> str | None:
        """Id of the Web-IO device named device_name, or None if there is none.

        Same contract as get_webio_base_info: any failure raises RuntimeError instead of
        returning None — callers use None to mean "device absent" and force a destructive
        recreate on that basis (see button.py's `_decide_effective_action`), so reporting
        "absent" on a failed check would delete-and-reupload a class that is still present.
        """
        try:
            return await self.client.get_webio_device_id(device_name)
        except ComexioError as err:
            raise RuntimeError(f"Web-IO device lookup for {device_name!r} failed: {err}") from err

    async def delete_webio_device(self, device_id: str | int) -> bool:
        """Delete a Web-IO device; False if Comexio logic still uses it or the request failed."""
        return await self.webio_device_delete_error(device_id) is None

    async def webio_device_delete_error(self, device_id: str | int) -> str | None:
        """Delete a Web-IO device; None if Comexio accepted it, else why it did not.

        None only means Comexio reported no in-use error — get_webio_device_info shows whether
        the device is really gone.
        """
        try:
            if await self.client.delete_webio_device(device_id):
                return None
        except ComexioError as err:
            _LOGGER.warning("Deleting Web-IO device %s failed: %s", device_id, err)
            return f"request failed: {err}"
        return "still used in a function plan"

    async def delete_webio_base(self, base_id: str | int) -> bool:
        """Delete a Web-IO class; False if the request failed.

        True only means Comexio accepted the request — it refuses silently while a device of
        the class still exists (see get_webio_base_info's deletable flag).
        """
        return await self.webio_base_delete_error(base_id) is None

    async def webio_base_delete_error(self, base_id: str | int) -> str | None:
        """Delete a Web-IO class; None if Comexio accepted the request, else why it failed.

        Like delete_webio_base, None does not prove the class is gone — get_webio_base_info does.
        """
        try:
            await self.client.delete_webio_base(base_id)
        except ComexioError as err:
            _LOGGER.warning("Deleting Web-IO class %s failed: %s", base_id, err)
            return f"request failed: {err}"
        return None

    async def delete_fup(self, fub_id: int) -> bool:
        """Delete an entire function plan (not just elements within it); False if that failed."""
        _LOGGER.info("delete_fup: deleting Function Plan fub_id=%s", fub_id)
        return await _succeeded(
            f"Deleting function plan {fub_id}", lambda: self.client.delete_function_plan(int(fub_id))
        )

    async def update_webio_device_ip(self, device_id: str | int, ha_address: str, webio_name: str) -> bool:
        """
        Updates the server address (IP:Port) of an existing device.
        Uses the specific POST format required by Comexio's main save handler.

        webio_name must be the class-specific name (see const.webio_class_name) matching
        the device_id being updated — the caller resolves it, since a single config entry
        now maps to two Web-IO devices (marker/io).
        """
        _LOGGER.info("Updating Web-IO device %s address to %s", device_id, ha_address)
        try:
            await self.client.update_webio_device_address(device_id, ha_address, webio_name)
        except ComexioError as err:
            _LOGGER.warning("Updating the address of Web-IO device %s failed: %s", device_id, err)
            return False
        return True

    async def delete_single_command(self, cmd_id: str | int, device_id: str | int) -> bool:
        """Removes an individual command instance (Delta Sync); False if the request failed."""
        _LOGGER.info("Deleting individual Web-IO command ID: %s", cmd_id)
        try:
            await self.client.delete_webio_command(cmd_id, device_id)
        except ComexioError as err:
            _LOGGER.warning("Deleting Web-IO command %s failed: %s", cmd_id, err)
            return False
        return True

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
        try:
            await self.client.save_webio_command(
                device_id,
                cmd_payload,
                base_id=base_id,
                # Falsy ids ("" from a scrape without one) have always meant "new command".
                command_id=existing_cmd_id or None,
            )
        except (ComexioError, ValueError) as err:
            # ValueError: a command id that is no number, rejected before anything is sent.
            _LOGGER.warning("Saving Web-IO command %s failed: %s", cmd_payload.get("Name"), err)
            return False
        return True

    async def get_webio_command_range(
        self, cmd_id: str | int, device_id: str | int
    ) -> tuple[float | None, float | None]:
        """Reads a single Web-IO command's live Min/Max from its edit form.

        The bulk config scrape ($FubModules["10"], see aiocomexio.config._add_webhook_command) never returns
        Min/Max for HA's own Web-IO commands — confirmed live 2026-08-30 — so this per-command
        edit form is the only reliable source. Used by the nightly range check (see
        WEBIO_RANGE_CHECK_HOUR in const.py) to detect drift against
        WEBIO_MARKER_ANALOG_MIN/MAX. Returns (None, None) on a fetch failure or a form without
        min/max fields; a single missing or non-numeric field comes back as None.
        """
        try:
            return await self.client.get_webio_command_range(cmd_id, device_id)
        except ComexioError as err:
            _LOGGER.warning("Reading the range of Web-IO command %s failed: %s", cmd_id, err)
            return None, None

    def build_webio_commands(
        self,
        server_id: str,
        parsed_data: dict[str, Any],
        webio_class: WebioClass | None = None,
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
        return comexio_webio.build_webio_commands(
            _webhook_path(server_id), parsed_data, webio_class, ignored_marker_ids, ignored_knx_ids
        )

    def _build_knx_loopback_webio_command(self, *, k_id: int, marker_id: int, is_analog: bool) -> dict[str, Any]:
        """Web-IO command dict for one K-Element's Phase 7 API-Loopback command (aiocomexio.webio).

        The DPT range is resolved against the cached KNX DPT catalog — treated as stale once
        comexio_version has moved on since it was fetched (see get_knx_dpt_catalog).
        """
        return comexio_webio.build_knx_loopback_webio_command(
            k_id=k_id,
            marker_id=marker_id,
            is_analog=is_analog,
            knx_dpt_catalog=self._knx_dpt_catalog,
            catalog_stale=self._knx_dpt_catalog_version != self.comexio_version,
        )

    def generate_webio_json(
        self,
        server_id: str,
        webio_name: str,
        parsed_data: dict[str, Any],
        webio_class: WebioClass | None = None,
        ignored_marker_ids: set[int] | None = None,
        ignored_knx_ids: set[int] | None = None,
    ) -> str:
        """Generate the upload-ready JSON string for the Comexio Web-IO importer.

        webio_name here is already the class-specific name (see const.webio_class_name) —
        callers append the ' [M]'/' [IO]'/' [KNX]' suffix before calling this.
        ignored_marker_ids / ignored_knx_ids are forwarded to build_webio_commands() to
        exclude ignored items.
        """
        return comexio_webio.generate_webio_json(
            _webhook_path(server_id), webio_name, parsed_data, webio_class, ignored_marker_ids, ignored_knx_ids
        )

    async def upload_web_io(self, server_id: str, webio_name: str, web_io_json: str) -> tuple[bool, str]:
        """Uploads a JSON class template: (True, base_id), or (False, why it failed).

        Comexio's answer body is logged at debug level by aiocomexio, success or not.
        """
        try:
            base_id = await self.client.upload_webio_class(
                web_io_json, class_name=webio_name, filename=f"ha_{server_id}.json"
            )
        except ComexioError as err:
            _LOGGER.warning("Uploading Web-IO class %r failed: %s", webio_name, err)
            return False, str(err)
        if not base_id:
            # aiocomexio already raises on a missing id; kept so no caller ever creates a device on None.
            return False, "Comexio returned no base_id for the uploaded class"
        return True, base_id

    async def create_webio_device(
        self,
        name: str,
        base_id: str | int,
        ha_address: str,
        username: str = "",
        password: str = "",  # nosec B105
    ) -> bool:
        """Creates a device instance pointing at ha_address ("host:port", see ha_address.py).

        username/password default to empty, matching every existing HA-webhook device (which
        needs no Basic-Auth on its own commands). The Phase 7 API-Loopback device is the first
        caller to pass real credentials — gated on the Web-IO class' own Login=3 ("vom Geraet
        abhaengig") setting, see ensure_knx_loopback_webio.

        Returns False if the request failed. True only means Comexio accepted it — its answer
        carries no verdict (aiocomexio logs it at debug level), so a refusal answered as HTTP
        200 still reads as success; get_webio_device_info shows whether the device exists.
        """
        try:
            await self.client.create_webio_device(name, base_id, ha_address, username=username, password=password)
        except ComexioError as err:
            _LOGGER.warning("Creating Web-IO device %r failed: %s", name, err)
            return False
        return True

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
        project_knx_write_path_design memory). A full sync therefore calls this ONCE with every
        bridge of every KNX plan before any plan is wired (prestage_knx_loopback_class).
        Ignored once the class already exists; topping up an existing class with a few new
        bridges still goes through save_single_command in the caller
        (function_plan_add_knx_bridge_loopback_pairs), same as every other class' Delta-Sync.

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
        except RuntimeError as err:
            # get_webio_device_info raises RuntimeError on any failed check, precisely so
            # callers don't mistake "couldn't check" for "genuinely absent" — see its own
            # docstring. Must not escape uncaught out of a tuple[str, bool] | None-returning helper.
            _LOGGER.warning("ensure_knx_loopback_webio: device check failed: %s", err)
            return None

        try:
            base_info = await self.get_webio_base_info(WEBIO_CLASS_NAME_KNX_LOOPBACK)
        except RuntimeError as err:
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
                "knx_loopback", WEBIO_CLASS_NAME_KNX_LOOPBACK, comexio_webio.knx_loopback_class_json(initial_commands)
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

        Returns the fubElementId assigned by the server, or None on failure; a transport failure
        raises aiohttp.ClientError / TimeoutError.
        """
        return await _attempt(
            f"function_plan_add_element (fub={fub_id}, ref={ref_id}, type={element_type})",
            lambda: self.client.add_function_plan_element(
                int(fub_id), int(ref_id), int(element_type), x=float(x), y=float(y), connection=connection
            ),
            None,
            transport_raises=True,
        )

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
        Returns the connection ID assigned by the server, or None on failure; a transport failure
        raises aiohttp.ClientError / TimeoutError.
        """
        # Part of the warning: a failed save that resends "new" for a source that already HAS a
        # connection is exactly the corrupting case existing_conn_id exists to prevent.
        mode = "new" if existing_conn_id is None else f"update(id={existing_conn_id})"
        dst_ids = [dst for dst, _pos, _inv in outputs]
        return await _attempt(
            f"function_plan_save_connection (fub={fub_id}, {input_elem_id}→{dst_ids}, mode={mode})",
            lambda: self.client.save_function_plan_connection(
                int(fub_id),
                int(input_elem_id),
                [(int(dst), int(pos), bool(inverted)) for dst, pos, inverted in outputs],
                value_type=value_type,
                source_pos=int(input_pos),
                source_inverted=bool(input_inverted),
                connection_id=None if existing_conn_id is None else int(existing_conn_id),
            ),
            None,
            transport_raises=True,
        )

    async def function_plan_save_elements_pos(self, positions: list[tuple[int, float, float]]) -> bool:
        """Reposition multiple function plan elements in one call.

        positions: list of (fubElementId, x, y) tuples. Returns True on success, and for an
        empty list (nothing to move); a transport failure
        raises aiohttp.ClientError / TimeoutError.
        """
        if not positions:
            return True
        _LOGGER.info("function_plan_save_elements_pos: repositioning %d elements", len(positions))
        return await _succeeded(
            f"Repositioning {len(positions)} function plan element(s)",
            lambda: self.client.move_function_plan_elements(
                [(int(elem_id), float(x), float(y)) for elem_id, x, y in positions]
            ),
            transport_raises=True,
        )

    async def function_plan_save_block_settings(self, element_id: int | str, settings: Mapping[str, str]) -> bool:
        """Write block settings of one element ($FubBaseConfig) the way Comexio's editor saves them.

        True once Comexio answers {"saved": 1}; False (after a warning) for a refusal or any
        other answer. A transport failure raises aiohttp.ClientError / TimeoutError.
        """
        what = f"Saving block settings {sorted(settings)} of function plan element {element_id}"

        async def save() -> None:
            form = block_settings_form(element_id, settings, time.strftime("%a %b %d %Y %H:%M:%S GMT%z"))
            # aiocomexio 0.4.0 has no request for this editor action yet; _plan_json is its
            # generic editor POST (XHR headers, JSON object answer, ComexioError mapping).
            answer = await self.client._plan_json(_BLOCK_SETTINGS_SAVE_PATH, form, what=what)
            if str(answer.get("saved")) != "1":
                raise ComexioRequestRejectedError(f"{what} was not confirmed: {answer!r:.200}")

        return await _succeeded(what, save, transport_raises=True)

    async def function_plan_delete_elements(self, elem_ids: list[int]) -> bool:
        """Delete elements from a function plan (removes elements + their connections).

        elem_ids: list of fubElementId integers to delete. Returns True on success, and for an
        empty list (nothing to delete).
        """
        if not elem_ids:
            return True
        _LOGGER.info("function_plan_delete_elements: deleting %d element(s): %s", len(elem_ids), elem_ids)
        return await _succeeded(
            f"Deleting function plan elements {elem_ids}",
            lambda: self.client.delete_function_plan_elements([int(elem_id) for elem_id in elem_ids]),
        )

    async def delete_marker(self, marker_id: int) -> bool | None:
        """Delete a Marker directly from Comexio's marker list (not a function plan element).

        Tri-state return so the caller (marker_delete service) can tell a "nothing changed"
        outcome apart from a genuine request failure — collapsing both to one bool would let a
        session/HTTP/parse failure be misreported as "marker already absent" for what is an
        irreversible action:
        - True: Comexio reports the marker deleted.
        - False: Comexio answered but did not delete it — most often because the marker id
          doesn't (or no longer) exist, but the server could also be reporting a rejection this
          way; the caller cross-checks against a fresh presence lookup
          (get_marker_delete_eligibility's third return value) to tell those apart rather than
          assuming this is always the harmless case.
        - None: the request itself failed (HTTP error, unparsable or non-object answer,
          transport error, timeout) — a real failure, must NOT be reported as "already absent".
        """
        result = await _attempt(
            f"Deleting marker M{marker_id}", lambda: self.client.delete_marker(int(marker_id)), None
        )
        _LOGGER.info("delete_marker: marker_id=%s result=%s", marker_id, result)
        return result

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

    async def function_plan_load_elements(self, fub_id: int, strict: bool = False) -> dict | None:
        """Load elements and connections for a function plan (GET loadelements).

        Returns dict with 'elements' and 'connections' keys, each an id-keyed dict, or None on
        failure. strict=True also treats a payload without real elements and connections
        collections as a failure instead of an empty plan.

        List-shaped collections are re-keyed by aiocomexio (normalize_plan_payload): for
        connections the list position IS the server's real connection id, and that key is fed
        straight back into function_plan_save_connection's existing_conn_id (see
        _function_plan_find_connection_by_source) — so the re-keying must stay 1:1 with the
        server's ids, or connections get corrupted instead of just mislabeled.
        """
        try:
            data = await self.client.load_function_plan(fub_id, strict=strict)
        except ComexioError as err:
            _LOGGER.error("function_plan_load_elements fub=%s failed: %s", fub_id, err)
            return None
        _LOGGER.info(
            "function_plan_load_elements fub=%s: %d Elemente, %d Verbindungen",
            fub_id,
            len(data["elements"]),
            len(data["connections"]),
        )
        return data

    async def function_plan_load_all_plans(self, strict: bool = False) -> dict[int, dict]:
        """Load elements and connections for ALL known function plans in one bulk request.

        Uses the loadallelements endpoint (bulk variant of loadelements) instead of one
        request per plan — Comexio serializes requests server-side anyway, so N sequential
        per-plan calls gain nothing over a single bulk call. Result is filtered down to the
        fub list cached by parse_config (self._fub_data). strict=True drops entries without a
        real elements collection instead of treating them as empty plans. Unlike
        function_plan_load_elements(strict=True) it does not check connections, so a result here
        is no source for run_fup (a restore or rewrite).
        Returns {fub_id: {"elements": {...}, "connections": {...}}}, {} on failure.
        """
        fub_ids = {int(fid) for fid in self._fub_data}
        if not fub_ids:
            _LOGGER.warning("function_plan_load_all_plans: self._fub_data is empty — nothing to load")
            return {}

        t_start = time.monotonic()
        try:
            plans = await self.client.load_all_function_plans(fub_ids, strict=strict)
        except ComexioError as err:
            _LOGGER.error("function_plan_load_all_plans failed: %s", err)
            return {}
        _LOGGER.info(
            "function_plan_load_all_plans: %d/%d plans loaded in %.2fs (bulk request)",
            len(plans),
            len(fub_ids),
            time.monotonic() - t_start,
        )
        return plans

    async def function_plan_stop_fup(self, fub_id: int) -> bool:
        """Stop/pause a function plan (stop_fup); False if Comexio did not confirm it.

        A plan that is not running counts as stopped — see _stop_plan.
        """
        ok = await self._stop_plan(fub_id) is not False
        _LOGGER.info("function_plan_stop_fup: fub=%s result=%s", fub_id, ok)
        if ok:
            self.set_fub_active(fub_id, False)
        return ok

    async def _stop_plan(self, fub_id: int) -> bool | None:
        """Stop a function plan: True once it is stopped, None if it was not running, False on failure.

        A plan that is not running cannot be stopped: Comexio refuses with
        {"error": "stop_error", "state": 0, "return": "0:not_found"} (seen live 2026-09-29).
        That is routine for every restore or connect on a stopped plan, so it is logged at
        INFO instead of as a failed request. Callers that restart the plan afterwards need
        to tell it apart (None), or they would start a plan the user had stopped.
        """
        what = f"Stopping function plan {fub_id}"

        async def stop() -> bool | None:
            try:
                await self.client.stop_function_plan(int(fub_id))
            except ComexioRequestRejectedError as err:
                if _PLAN_NOT_RUNNING_ANSWER not in str(err):
                    raise
                _LOGGER.info("%s: the plan was not running", what)
                return None
            return True

        return await _attempt(what, stop, False)

    async def function_plan_add_comment_element(
        self,
        fub_id: int,
        text: str,
        x: float = 100.0,
        y: float = 7.5,
    ) -> int | None:
        """Place a text/comment block (type=14) on a function plan canvas, set to Comexio's widest width.

        Returns the fubElementId assigned by the server, or None on failure. A failed width
        update is only logged — the comment itself is placed.
        """
        elem_id = await _attempt(
            f"Placing a comment on function plan {fub_id}",
            lambda: self.client.add_function_plan_comment(int(fub_id), text, x=float(x), y=float(y)),
            None,
        )
        if elem_id is None:
            return None
        # add_element has no width parameter — the width lives in the comment
        # properties dialog, saved via a separate endpoint.
        await self._function_plan_set_comment_width(elem_id, text)
        return elem_id

    async def function_plan_add_constant_element(
        self,
        fub_id: int,
        value: str,
        x: float = 100.0,
        y: float = 100.0,
    ) -> int | None:
        """Place a Constant block (type=16) on a function plan canvas.

        Returns the fubElementId assigned by the server, or None on failure; a transport failure
        raises aiohttp.ClientError / TimeoutError.
        """
        return await _attempt(
            f"Placing a constant on function plan {fub_id}",
            lambda: self.client.add_function_plan_constant(int(fub_id), str(value), x=float(x), y=float(y)),
            None,
            transport_raises=True,
        )

    async def _function_plan_set_comment_width(self, elem_id: int, text: str, width: int = 5) -> bool:
        """Set a comment element's text width via savefupcommentelement (5 = 'Sehr Breit')."""
        return await _succeeded(
            f"Setting the width of comment element {elem_id}",
            lambda: self.client.save_function_plan_comment(int(elem_id), text, width=width),
        )

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
            plan_name: Name of the new plan (must not be in use yet)
            plan_comment: Optional comment/description
            paper_format: Paper size (A3, A4, A5; defaults to A4, and so does any other value)
            orientation: 'landscape' or 'portrait' (defaults to landscape, and so does any other value)
            dpi: Resolution in dots per inch, 45-120 (defaults to 90)

        The new plan's $Fubs entry is cached right away, so function_plan_update_paper and the
        canvas lookups find it before the next poll refreshes the whole cache. If Comexio
        confirmed the plan but its id could not be read back, the plan is looked up by name
        once more (_find_created_plan); None then means the plan may exist without an id.
        """
        paper, orient = _plan_paper_and_orientation(paper_format, orientation)
        known_ids = set(self._fub_data)

        async def create() -> CreatedFunctionPlan | None:
            try:
                return await self.client.create_function_plan(
                    plan_name, comment=plan_comment, paper_format=paper, orientation=orient, dpi=dpi
                )
            except ComexioCreatedWithoutIdError as err:
                _LOGGER.warning("create_fup: %s — looking the plan up by name once more", err)
                return await self._find_created_plan(plan_name, known_ids)

        created = await _attempt(f"Creating function plan {plan_name!r}", create, None)
        if created is None:
            return None
        _LOGGER.info("create_fup: plan '%s' created, fub_id=%s", plan_name, created.fub_id)
        self._fub_data[str(created.fub_id)] = created.fubs_entry
        if self.run_state_listener is not None:
            self.run_state_listener()
        return created.fub_id

    async def _find_created_plan(self, plan_name: str, known_ids: set[str]) -> CreatedFunctionPlan | None:
        """The plan Comexio confirmed under plan_name, found in a fresh $Fubs; None after an error log.

        Only a plan the cache did not know before the create counts, and only if exactly one
        such plan carries the name — a guessed id could make a restore write into another plan.
        """
        try:
            fubs = (await self.get_raw_config()).get("Fubs")
        except (aiohttp.ClientError, TimeoutError, ComexioConnectionError) as err:
            fubs, reason = None, f"reading the config failed: {err!r}"
        else:
            # get_raw_config() answers {} (after its own error log) when the scrape failed.
            reason = (
                "it is not listed exactly once among the new plans"
                if fubs is not None
                else "reading the config failed (see the error above)"
            )
        matches = [
            (fid, info)
            for fid, info in iter_group(fubs)
            if fid.isdigit() and fid not in known_ids and isinstance(info, dict) and info.get("Name") == plan_name
        ]
        if len(matches) == 1:
            fid, info = matches[0]
            return CreatedFunctionPlan(int(fid), info)
        _LOGGER.error(
            "create_fup: plan '%s' was created in Comexio, but its id is still unknown (%s) — "
            "it shows up with the next poll; do not create it again",
            plan_name,
            reason,
        )
        return None

    async def function_plan_update_paper(
        self, fub_id: int, paper_format: str, dpi: int, orientation: str, name: str | None = None
    ) -> bool:
        """Update an EXISTING plan's paper format/DPI/orientation (Comexio's plan settings save).

        Needed before an in-place restore whose snapshot's canvas settings differ from the
        live plan's current ones (e.g. force_override onto an unrelated plan) — otherwise
        element positions computed for the snapshot's original canvas can end up clipped or
        overlapping on the live plan's (different) canvas.

        name: if given, also renames the plan (force_override restores the snapshot's
        original name too, so the plan comes back exactly as it was — not just its content).
        None keeps the current live name unchanged.

        All other plan properties (comment, position, active state) are read from the
        current live data and passed through UNCHANGED. Known gap: Comexio's $Fubs dump does
        not expose "reset on close", so that flag is always sent as off (Comexio's own
        create-time default) rather than preserved — a cosmetic Comexio Studio setting this
        integration doesn't otherwise manage.
        """
        fub = self._fub_data.get(str(fub_id))
        if fub is None:
            _LOGGER.error("function_plan_update_paper: fub_id=%s not found in live data", fub_id)
            return False

        paper, orient = _plan_paper_and_orientation(paper_format, orientation)
        if not await _succeeded(
            f"Saving settings of function plan {fub_id}",
            lambda: self.client.update_function_plan(
                int(fub_id),
                name=name if name is not None else fub.get("Name", ""),
                comment=fub.get("Comment") or "",
                position=int(fub.get("Position", -1)),
                active=bool(int(fub.get("Active") or 0)),
                paper_format=paper,
                orientation=orient,
                dpi=int(dpi),
            ),
        ):
            return False

        # Keep the local cache in sync so get_fub_paper_format/dpi/orientation reflect the change
        fub["Paper"] = _PLAN_PAPER_IDS[paper]
        fub["Resolution"] = dpi
        fub["Orientation"] = 1 if orient == "portrait" else 0
        if name is not None:
            fub["Name"] = name
        _LOGGER.info(
            "function_plan_update_paper: fub=%s -> paper=%s dpi=%s orientation=%s name=%s",
            fub_id,
            paper,
            dpi,
            orient,
            name,
        )
        return True

    async def create_marker(self, binary: bool) -> int | None:
        """Create a new marker ('flag') and return its server-assigned numeric ID.

        The new marker starts unlabeled (empty Name, default value 0) — use rename_marker()
        afterwards to give it a title.

        IMPORTANT: the server assigns the new ID strictly sequentially (next free
        integer) — there is no way to request a specific target ID (confirmed live
        2026-09-14). Callers that need a specific ID (e.g. to align a block of
        bridge markers on a round boundary) must consume IDs one at a time via
        repeated calls until the desired ID comes back.

        binary: True for a digital marker, False for analog.

        Returns the new marker's Id, or None on failure.
        """
        marker_id = await _attempt(
            f"Creating a {'digital' if binary else 'analog'} marker",
            lambda: self.client.create_marker(binary=binary),
            None,
        )
        if marker_id is not None:
            _LOGGER.info("create_marker: created marker M%s (binary=%s)", marker_id, binary)
        return marker_id

    async def rename_marker(self, marker_id: int, name: str, binary: bool) -> bool:
        """Set an existing marker's title via Comexio's own save flow (after its uniqueness check).

        Comexio's marker save takes the whole form, so this also resets the marker's default
        value and "store in memory" flag to 0. Only safe to call on a marker whose current
        state is already known (e.g. one just created via create_marker()). Do NOT call this on
        a pre-existing user marker without first reading its live default/store_memory values.

        binary: True for digital, False for analog — must match the marker's actual type; it
        is not looked up here.

        Returns True on success; False if the name is taken or the save was not confirmed.
        """
        if not await _succeeded(
            f"Renaming marker M{marker_id}", lambda: self.client.rename_marker(int(marker_id), name, binary=binary)
        ):
            return False
        _LOGGER.info("rename_marker: M%s renamed to '%s' (binary=%s)", marker_id, name, binary)
        return True

    async def rename_knx_object(self, k_id: str | int, name: str) -> bool:
        """Set an existing KNX object's ("K-Element") title via Comexio's own save flow.

        Unlike rename_marker this is a genuine single-field patch — no other K-Element state
        is touched. Only ever called with the existing title plus a trailing "[RO]"/"[TRIG]"
        suffix (see coordinator._auto_suffix_unambiguous_knx / _audit_knx_dpt_ambiguous), never
        a full rename; Comexio's uniqueness check still runs first, as in its own admin UI.

        Returns True on success; False if the name is taken or the save was not confirmed.
        """
        if not await _succeeded(f"Renaming KNX object K{k_id}", lambda: self.client.rename_knx_object(int(k_id), name)):
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
        placed as an element in any function plan (plus, with keep_bridge_titles, stale
        bridge markers >= min_id — see below).

        keep_bridge_titles (optional): additionally treat every unplaced, bridge-titled
        marker >= min_id whose title is NOT in this set as free — a stale bridge marker left
        behind by a K-element rename or a deleted plan (see _stale_knx_bridge_marker_ids).
        Bridge titles are machine-given, so this never touches a user's own marker.
        Deliberately NOT capped at max_id: max_id guards blank markers (which could be a
        user's), a stale bridge marker is ours wherever it sits — and since the result is
        ascending, ids above the block are only used once the block itself is exhausted.

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
        A non-numeric plan id also returns None — checked before the cache is replaced, so a
        malformed $Fubs neither crashes the caller nor ends up in self._fub_data.
        """
        try:
            fub_ids = {int(fid) for fid in fubs}
        except (TypeError, ValueError):
            bad = [fid for fid in fubs if not str(fid).strip().lstrip("-").isdigit()]
            _LOGGER.error("_load_all_plans_verified: malformed plan id(s) %r in $Fubs — placement unknown", bad)
            return None
        self._replace_fub_data(fubs)
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
        lookup aiocomexio parse_config uses for KNX classification (self.io_types) — the
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

    async def allocate_knx_bridge_markers(
        self, missing_items: list[dict[str, Any]]
    ) -> tuple[dict[int, tuple[int, bool]], list[str]]:
        """Create + title the bridge Marker of every item up front, without touching any plan.

        Lets a full sync learn every (k_id, marker_id) pair BEFORE the first cluster plan is
        written, so the API-Loopback Web-IO class can be bulk-created with ALL its commands
        in one upload (prestage_knx_loopback_class) instead of 50 in the first cluster's
        upload plus one save_single_command per bridge of every later cluster — each of those
        blocks the Comexio server for ~35-40 s (live 28.09.2026: 9 commands for
        'HA - KNX [51-100]' took ~5.5 min). Same marker-selection rules as the in-plan path
        (block start, title reuse, free-marker reuse — see create_knx_bridge_marker);
        function_plan_add_knx_bridge_pairs then takes the result as `preallocated` and only
        wires.

        missing_items are {"ref_id", "title", "type_raw"} dicts. Returns
        ({k_id: (marker_id, binary)} for every marker obtained, error messages) — a K-element
        missing from the map is simply retried by the in-plan path.
        """
        batch_titles = {_knx_bridge_title(int(it["ref_id"]), it["title"]) for it in missing_items}
        fub_modules, free_marker_ids, error = await self._prepare_knx_bridge_batch(batch_titles)
        if error or fub_modules is None:
            return {}, [error or "could not prepare the KNX bridge marker block — see log"]
        allocated: dict[int, tuple[int, bool]] = {}
        errors: list[str] = []
        for item in sorted(missing_items, key=lambda it: int(it["ref_id"])):
            k_id = int(item["ref_id"])
            created = await self.create_knx_bridge_marker(
                k_id, item["title"], item["type_raw"], fub_modules, free_marker_ids
            )
            if created is None:
                errors.append(f"KNX bridge K{k_id}: create_knx_bridge_marker failed — see log")
                continue
            allocated[k_id] = (created[0], self.io_types.get(str(item["type_raw"]), {}).get("binary", False))
        return allocated, errors

    async def prestage_knx_loopback_class(
        self, api_username: str, api_password: str, bridges: list[tuple[int, int, bool]]
    ) -> set[str]:
        """Bulk-create the API-Loopback Web-IO class with every bridge's command, before any plan work.

        Only acts when the class does not exist yet — that is exactly the case where one
        upload is fast and per-command saves are slow. An already-existing class is left to
        the per-cluster path (function_plan_add_knx_bridge_loopback_pairs), where adding
        single commands is the intended way to top up a few new bridges.

        Returns the command names (knx_loopback_command_name, without the "{deviceId}. "
        prefix, so they stay valid even if the device lookup below fails) that went into the
        upload, so the per-cluster calls treat them as already present even if Comexio has not
        listed them yet (see _ensure_knx_loopback_commands' `preembedded`) — without that, a
        slow admin-page refresh would make them look missing and get them saved a second time.
        Empty set when nothing was uploaded here (no bridges, no API credentials, class
        already present, or any bootstrap failure — the per-cluster path reports those).
        """
        if not bridges:
            return set()
        bridges = sorted(bridges, key=lambda b: (b[0], b[1]))
        bootstrap = await self.ensure_knx_loopback_webio(api_username, api_password, bridges)
        if bootstrap is None or not bootstrap[1]:
            return set()
        names = {knx_loopback_command_name(k_id, marker_id) for k_id, marker_id, _ in bridges}
        try:
            device_id = await self.get_webio_device_info(WEBIO_DEVICE_NAME_KNX_LOOPBACK)
        except RuntimeError as err:
            _LOGGER.warning("prestage_knx_loopback_class: device check failed after upload: %s", err)
            return names
        if device_id is None:
            _LOGGER.error("prestage_knx_loopback_class: Loopback Web-IO device not found after upload")
            return names
        full_names = {f"{device_id}. {name}" for name in names}
        try:
            fresh_data = await self._reload_config_until_commands_ready(
                lambda _d: full_names, lambda d: {info["name"] for info in d.get("webio_names", {}).values()}
            )
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            # Same as the device check above: the upload already succeeded, so the names must
            # still reach the caller — raising here would drop them and get them saved twice.
            _LOGGER.warning("prestage_knx_loopback_class: readiness check failed after upload: %s", err)
            return names
        # Still return every uploaded name: they are part of the class template the upload
        # just created, so an unlisted one is only the admin page lagging — handing it to the
        # per-cluster path would save it a second time. Log it so the lag stays visible.
        if still_unlisted := full_names - {info["name"] for info in fresh_data.get("webio_names", {}).values()}:
            _LOGGER.warning(
                "prestage_knx_loopback_class: %d uploaded command(s) not yet listed by Comexio after "
                "the readiness wait, treating them as present (they are part of the uploaded class): %s",
                len(still_unlisted),
                sorted(still_unlisted),
            )
        return names

    async def function_plan_add_knx_bridge_pairs(
        self,
        fub_id: int,
        missing_items: list[dict[str, Any]],
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
        preallocated: dict[int, int] | None = None,
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
        preallocated ({k_id: marker_id}, from allocate_knx_bridge_markers) skips marker
        creation for those K-elements and only wires the given marker — their ids are also
        kept out of the free-marker pool, in case Comexio does not list the new titles yet.
        """
        preallocated = preallocated or {}
        batch_titles = {_knx_bridge_title(int(it["ref_id"]), it["title"]) for it in missing_items}
        fub_modules, free_marker_ids, error = await self._prepare_knx_bridge_batch(batch_titles)
        if error:
            return [], [error], {}
        if fub_modules is None:
            # Only reachable if _prepare_knx_bridge_batch's error-is-None contract is
            # violated — see its docstring. Fail loudly rather than silently proceed.
            raise AssertionError("_prepare_knx_bridge_batch returned fub_modules=None with error=None")
        reserved = set(preallocated.values())
        free_marker_ids = [m for m in free_marker_ids if m not in reserved]

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
                preallocated_marker_id=preallocated.get(k_id),
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
        preallocated_marker_id: int | None = None,
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
        function's docstring. preallocated_marker_id (see allocate_knx_bridge_markers) skips
        the marker creation and wires that marker directly.
        """
        if preallocated_marker_id is not None:
            marker_id = preallocated_marker_id
        else:
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
        command_progress_cb: Callable[[int, int, int, int], None] | None = None,
        preembedded: set[str] | None = None,
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
        preembedded: command names (knx_loopback_command_name, no device prefix) the caller
        already bulk-embedded in a fresh class upload of its own (prestage_knx_loopback_class)
        — handled exactly like freshly_created=True for those names, so they are never saved a
        second time.

        command_progress_cb(n, total, k_id, marker_id) fires right before each
        save_single_command call (n is 1-based, total counts only the commands this call
        actually saves) — each one blocks for ~35-40 s on the Comexio side (measured live
        28.09.2026), so the caller needs a per-command signal to show the sync is still moving.

        Returns (pending: cmd_name -> (k_id, marker_id), names_to_confirm: full_names not yet
        proven present in the caller's already-fetched config, errors).
        """
        pending: dict[str, tuple[int, int]] = {}
        names_to_confirm: set[str] = set()
        errors: list[str] = []
        bulk_embedded = preembedded or set()

        def _needs_save(cmd_name: str) -> bool:
            return (
                not freshly_created
                and f"{device_id}. {cmd_name}" not in existing_full_names
                and cmd_name not in bulk_embedded
            )

        to_save = sum(_needs_save(knx_loopback_command_name(k_id, marker_id)) for k_id, marker_id, _ in bridges)
        n_saved = 0
        for k_id, marker_id, binary in bridges:
            cmd_name = knx_loopback_command_name(k_id, marker_id)
            full_name = f"{device_id}. {cmd_name}"
            if full_name in existing_full_names:
                pending[cmd_name] = (k_id, marker_id)
                continue
            if not _needs_save(cmd_name):
                pending[cmd_name] = (k_id, marker_id)
                names_to_confirm.add(full_name)
                continue
            n_saved += 1
            if command_progress_cb is not None:
                command_progress_cb(n_saved, to_save, k_id, marker_id)
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
        command_progress_cb: Callable[[int, int, int, int], None] | None = None,
        preembedded: set[str] | None = None,
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
        KNX classes — see aiocomexio.config._build_webio_name_lexicon's docstring). Existence/readiness checks
        here go through webio_names instead (the all-devices lexicon), matched by the Studio
        pill-label convention "{deviceId}. {commandName}".

        fresh_plan=True places the pairs at FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK as an interim
        column, same as the fresh_plan=False off-canvas parking case below — this is NEVER the
        final column, a follow-up sort pass (see services/_grid.py) always runs afterward and
        moves each Loopback Web-IO into the SAME column as its read-path sibling, one row
        below it. progress_cb(done, total) fires after each item; command_progress_cb fires
        before each slow per-command Web-IO save (see _ensure_knx_loopback_commands).
        preembedded: command names already bulk-uploaded by prestage_knx_loopback_class
        before any plan was wired — never saved again here, only confirmed.
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
        except RuntimeError as err:
            # get_webio_device_info raises RuntimeError on any failed check — see its own
            # docstring. Must not escape this (added, skipped, errors)-returning batch as an
            # unhandled exception.
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
            bridges, device_id, base_id, existing_full_names, freshly_created, command_progress_cb, preembedded
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

        By default the CURRENT state is loaded first (aiocomexio refuses to run a plan whose
        load came back without elements or connections); pass an explicit plan_data (e.g. a
        backup snapshot with 'elements' and 'connections') to restore that state instead.
        Returns True only if Comexio confirmed the run.
        """
        return await self.function_plan_run_fup_outcome(fub_id, plan_data) is True

    async def function_plan_run_fup_outcome(self, fub_id: int, plan_data: dict | None = None) -> bool | None:
        """function_plan_run_fup telling a refusal apart from no answer.

        True: Comexio confirmed the run. False: Comexio refused it (result=false). None: no usable
        answer (connection, session or a malformed reply) — says nothing about the plan itself.
        """
        try:
            await self.client.run_function_plan(int(fub_id), plan_data)
        except ComexioRequestRejectedError as err:
            # After a restore payload Comexio routinely answers result=false although it applied
            # the plan (see services/backup.py); a plain reactivation refused is a stopped plan.
            level = logging.INFO if plan_data is not None else logging.WARNING
            _LOGGER.log(level, "function_plan_run_fup: fub=%s not confirmed: %s", fub_id, err)
            return False
        except (ComexioError, TypeError, ValueError) as err:
            _LOGGER.warning("function_plan_run_fup: fub=%s failed: %s", fub_id, err)
            return None
        _LOGGER.info("function_plan_run_fup: fub=%s result=True", fub_id)
        self.set_fub_active(fub_id, True)
        return True

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
        aiocomexio.config._build_webio_name_lexicon's docstring for why that dict is scoped that way). A
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
        for mid, marker in iter_group(fub_modules.get("2")):
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
        return f"{label}: add_element ({kind}) failed" if elem is None else int(elem)

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
    def _function_plan_trigger_wired_source_ids(
        plan_data: dict | None, flanke_ref_id: int, ref_type: int = 2
    ) -> set[int]:
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
            if ComexioAPI._function_plan_elem_is_flanke(elements, dst, str(flanke_ref_id)) and (dst, src) in edges
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

    def flanke_ref_id(self) -> int | None:
        """This server's $FubModules["5"] id of the Flanke block, or None when it can't be trusted.

        Resolved by the stable catalog key (reference_catalog), never by a hard-coded id: None
        while no reconciliation has run yet, and when the block is missing, ambiguous or has a
        different port layout on this server — the trigger-pair features then refuse to write
        instead of placing elements Comexio can't resolve ("Configuration fault ... 5 <ref>").
        """
        if self.reference_check is None:
            return None
        return self.reference_check.resolve(KIND_FUB_BASE, FUB_BASE_KEY_FLANKE)

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
        Refuses up front (no element written) when the Flanke block can't be resolved on this
        server — see flanke_ref_id.
        """
        flanke_ref_id = self.flanke_ref_id()
        if flanke_ref_id is None:
            _LOGGER.error(
                "trigger pairs: Flanke block (%s) not usable on this Comexio (%s) — nothing written, "
                "see the reference catalog check in the log",
                FUB_BASE_KEY_FLANKE,
                self.comexio_version,
            )
            return [], [f"Flanke block {FUB_BASE_KEY_FLANKE} not available on this Comexio"]
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
                fub_id, source_id, plan_data, existing_by_ref, _pair_pos(len(added), i), ref_type, flanke_ref_id
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
        ref_type: int,
        flanke_ref_id: int,
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

        existing_marker_elem = existing_by_ref.get((ref_type, source_id))
        existing_flanke_elem: int | None = None
        already_complete = False
        if existing_marker_elem and (
            paired_flanke_ids := self._function_plan_paired_flanke_ids(plan_data, [existing_marker_elem], flanke_ref_id)
        ):
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
            if all(eid not in marker_elem_id_set for eid in endpoint_ids):
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
        flanke_ref_id = self.flanke_ref_id()
        if flanke_ref_id is None:
            # Without the Flanke id the paired Flanken can't be told apart from other blocks —
            # deleting only the sources would strand their Flanken where no audit finds them.
            _LOGGER.error(
                "trigger pairs: Flanke block (%s) not usable on this Comexio (%s) — orphan removal skipped",
                FUB_BASE_KEY_FLANKE,
                self.comexio_version,
            )
            return 0, False
        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, _ = self._function_plan_existing_refs(plan_data)
        marker_elem_ids = [
            elem_id for source_id in source_ids if (elem_id := existing_by_ref.get((ref_type, source_id)))
        ]
        flanke_elem_ids = self._function_plan_paired_flanke_ids(plan_data, marker_elem_ids, flanke_ref_id)
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
    ) -> tuple[dict[str, int], int, list[str]]:
        """Recreate every element and connection from a snapshot on a freshly created (empty) plan.

        Snapshot element IDs are plan-local and meaningless on a new plan: pass 1 (re)creates
        every element regardless of type (Marker, WebIO, IO, any catalog function block,
        Comment, Constant), building an old-id -> new-id map; pass 2 redraws every connection
        using that map with its original port positions/polarity (see
        function_plan_catalog.py for why Constants need special handling: their value lives
        in the element's own "name" field, ref_id is always 0).

        Returns ({snapshot element id: new element id} of every re-created element,
        connections_created, warnings).
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
        return id_map, connections_created, warnings

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
        """Stop the plan, delete the given elements, restart it, and build the result dict.

        A plan that is not running is cleaned up all the same and left stopped: deleting
        elements needs a stopped plan, which it already is, and a restart would start a plan
        the user had stopped. Only a running plan that refuses to stop is a stop_failed.
        """
        stopped = await self._stop_plan(fub_id)
        if stopped is False:
            _LOGGER.warning(
                "_delete_plan_elements_and_restart: plan '%s' (fub=%s) could not be stopped, cleanup skipped",
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

        was_running = stopped is True
        success = await self.function_plan_delete_elements(elem_ids_to_delete)
        if not success:
            _LOGGER.error("_delete_plan_elements_and_restart: element deletion failed")
            restart_after_failure_ok = not was_running or await self.function_plan_run_fup(fub_id)
            return {
                "deleted_elem_count": 0,
                "webio_cmd_ids": [],
                "fub_id": fub_id,
                "plan_stopped": not restart_after_failure_ok,
                "plan_name": plan_name,
                "delete_failed": True,
                "was_running": was_running,
            }

        restart_ok = not was_running or await self.function_plan_run_fup(fub_id)
        if not was_running:
            restart_note = "was not running — left stopped"
        else:
            restart_note = "restarted" if restart_ok else "restart failed — left stopped"
        _LOGGER.info(
            "_delete_plan_elements_and_restart: deleted %d elements, webio_cmd_ids=%s (plan '%s' %s)",
            len(elem_ids_to_delete),
            webio_cmd_ids,
            plan_name,
            restart_note,
        )
        return {
            "deleted_elem_count": len(elem_ids_to_delete),
            "webio_cmd_ids": webio_cmd_ids,
            "fub_id": fub_id,
            "plan_stopped": not restart_ok,
            "plan_name": plan_name,
            "was_running": was_running,
        }

    async def set_value(
        self,
        target_type: str,
        target_id: str | int,
        value: float | int,
        ext: str | None = None,
        identifier: str | None = None,
    ) -> bool:
        """API write via Basic Auth; False if Comexio refused it or could not be reached."""
        try:
            if target_type == "marker":
                await self.client.set_marker_value(int(target_id), value)
            elif target_type == "knx":
                await self.client.set_knx_value(int(target_id), value)
            elif ext is None or identifier is None:
                _LOGGER.error("Missing 'ext' or 'identifier' for non-marker API write. Type: %s", target_type)
                return False
            else:
                await self.client.set_io_value(ext, identifier, value)
        except (ComexioError, ValueError) as err:
            # ValueError: a target id that is no number, rejected before anything is sent.
            _LOGGER.error("Comexio API write failed (%s %s): %s", target_type, target_id, err)
            return False
        return True

    async def get_bus_workload(self) -> dict[str, Any]:
        """Fetch the internal bus workload (%) and SD-card presence from the admin interface.

        Called on a fast, independent poll cadence (see coordinator's bus-load loop) —
        much more frequent than the main config audit, so failures are logged at debug
        level only to avoid log spam. Returns {} on failure.
        """
        try:
            return await self.client.get_bus_workload()
        except ComexioError as err:
            _LOGGER.debug("Bus workload fetch failed: %s", err)
            return {}

    async def system_emergency_reboot(self) -> bool:
        """Trigger an IMMEDIATE, unconfirmed full Comexio system reboot.

        Comexio has no confirmation dialog for this and returns no structured result — the
        request itself is the action. Only called by the Bus-Load-Watchdog's emergency path,
        gated behind CONF_BUS_WATCHDOG_AUTO_REBOOT (default off). True only means the request
        was accepted, not that the reboot completed cleanly.
        """
        try:
            await self.client.system_emergency_reboot()
        except ComexioError as err:
            _LOGGER.error("system_emergency_reboot failed: %s", err)
            return False
        return True

    async def check_extension_firmware(self) -> list[dict[str, Any]]:
        """Query the local extension bus for available firmware updates (BASE + all extensions).

        Comexio documents that this can briefly interrupt extension outputs while it runs, so
        it must only be called rarely — see the coordinator's version-gated nightly check, not
        a regular poll. Logged at warning level (not debug) since failures here are infrequent
        enough to matter, unlike the fast bus-workload poll. Returns [] on failure.
        """
        try:
            return await self.client.check_extension_firmware()
        except ComexioError as err:
            _LOGGER.warning("Extension firmware check failed: %s", err)
            return []

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

        Also flags the instance as closed so a login already in flight inside
        ensure_preview_session()'s lock detaches its freshly-authenticated session instead of
        assigning it to self._preview_session after this point — that session would otherwise
        never be detached (it was never visible here to begin with).
        """
        self._closed = True
        self.session.detach()
        if self._preview_session is not None:
            self._preview_session.detach()
            self._preview_session = None
            self._preview_client = None
