from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
import logging
import re
from typing import Any

from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

# Version: 0.8.1
DOMAIN = "comexio"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # nosec B105
CONF_SERVER_ID = "server_id"

CONF_SCHEMA_MARKER = "schema_marker"
CONF_SCHEMA_IO = "schema_io"
CONF_SCHEMA_KNX = "schema_knx"
DEFAULT_SCHEMA_MARKER = "M{MarkerId} {MarkerTitle}"
DEFAULT_SCHEMA_IO = "{ExtName} {IoId} {IoTitle}"
DEFAULT_SCHEMA_KNX = "K{KnxId} {KnxTitle}"

# keys for API access
CONF_API_USERNAME = "api_username"
CONF_API_PASSWORD = "api_password"  # nosec B105

CONF_ENABLE_NOTIFICATIONS = "enable_notifications"
DEFAULT_ENABLE_NOTIFICATIONS = True

CONF_COVER_KEYWORDS = "cover_keywords"
DEFAULT_COVER_KEYWORDS = "rollo, jalousie, blind"

CONF_ENTITY_ID_MIGRATION_IGNORED = "entity_id_migration_ignored"
CONF_STATISTICS_CLEANUP_IGNORED = "statistics_cleanup_ignored"
CONF_INCLUDE_OFFLINE_EXTENSIONS = "include_offline_extensions"
CONF_IGNORED_MARKERS = "ignored_markers"
CONF_IGNORED_KNX = "ignored_knx"

# The option-key VALUES below keep the legacy "logikplan" spelling — they are persisted
# in entry.options (and mirrored as field keys in translations/*.json); renaming them
# would silently drop every user's saved settings.
CONF_FUNCTION_PLAN_FUB_ID = "logikplan_fub_id"  # user-selected single plan (select entity)
# Legacy persisted CONF_FUNCTION_PLAN_FUB_ID value from the removed "Auto" option — read as
# "no selection" so an old entry doesn't resolve to a bogus plan.
FUNCTION_PLAN_FUB_ID_AUTO = "auto"
CONF_FUNCTION_PLAN_PLAN_MAP = "logikplan_plan_map"  # dict: plan_name → fub_id (HA-managed cluster plans)
CONF_FUNCTION_PLAN_PLAN_PREFIX = "logikplan_plan_prefix"
DEFAULT_FUNCTION_PLAN_PLAN_PREFIX = "HA"
CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN = "logikplan_max_pairs_per_plan"
DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN = 100
# IO cluster plans: physical extension IOs wired to their Web-IO commands. Only extensions
# whose names are listed here are managed (staged rollout — the user opts extensions in one
# by one). Extensions-per-plan capacity derives from CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN:
# 50 → 1, 100 → 2, 150 → 3 extensions per plan; each extension fills exactly one column.
CONF_FUNCTION_PLAN_IO_EXTENSIONS = "logikplan_io_extensions"


# Web-IO class split: HA maintains separate Web-IO device classes on the Comexio
# server — one per source category (Marker, physical IO, KNX object) — derived from the
# single webio_name field in the config/options flow by appending a suffix. The webIoId
# (the per-command key inside $FubModules["10"]) is a global counter across ALL Web-IO
# devices on a Comexio server, never reused per-device (verified live 2026-07-27), so
# commands from every class can share one flat webio_commands lookup without ambiguity.
class WebioClass(StrEnum):
    """The Web-IO device classes HA manages on the Comexio server.

    A StrEnum so every existing string-based usage (dict keys, equality checks,
    f-string interpolation, JSON payloads sent to/scraped back from Comexio)
    keeps working unchanged, while call sites gain typo-safety via the members.
    """

    MARKER = "marker"
    IO = "io"
    KNX = "knx"


WEBIO_CLASS_MARKER = WebioClass.MARKER
WEBIO_CLASS_IO = WebioClass.IO
WEBIO_CLASS_KNX = WebioClass.KNX
WEBIO_CLASSES = tuple(WebioClass)

# Internal audit-map key convention (coordinator._async_update_data): IO entries are keyed
# as "IO_{ext_name}_{identifier}", markers as bare "M{id}", KNX objects as bare "K{id}" —
# centralized here so the build and classify sides can't drift apart.
_IO_AUDIT_KEY_PREFIX = "IO_"
_MARKER_AUDIT_KEY_PREFIX = "M"
_KNX_AUDIT_KEY_PREFIX = "K"


@dataclass(frozen=True, kw_only=True)
class SourceCategory:
    """Table-driven description of one Comexio source type HA mirrors as entities.

    Markers and KNX objects are near-identical twins: server-side state variables
    with numeric ids, mirrored 1:1 and clustered into "HA - <label> [n-m]" id-range
    function plans. Physical IOs are the odd one out (ext/identifier composite keys,
    one cluster plan per extension, binary_sensor support) and keep some special
    handling at the call sites where genericity isn't reachable. Adding a further
    Comexio module type = one more entry in SOURCE_CATEGORIES.
    """

    key: WebioClass
    fub_module_type: str  # $FubModules key holding the raw source items
    data_key: str  # parse_config() output-dict key ("markers"/"io"/"knx")
    audit_key_prefix: str  # coordinator audit-map key prefix
    webio_name_suffix: str  # appended to the configured webio_name (" [M]"/" [IO]"/" [KNX]")
    label: str  # short display label for sync/audit messages (English — used in UI/log text)
    german_label: str  # German equivalent, used only in options_flow.py's German-only inline
    # validation errors ("Merker" is the correct German automation term, distinct from the
    # English/code "Marker" — see [[feedback_persistent_notification_language]] for why
    # everything else stays English)
    schema_conf_key: str  # entry.options key for the entity-name schema
    schema_default: str  # default entity-name schema
    id_placeholder: str  # entity-name schema format key for the id
    title_placeholder: str  # entity-name schema format key for the title
    unique_id_infix: str  # HA unique_id infix ("m"/""/"k")
    range_clustered: bool  # True → id-range cluster plans; False → one plan per extension
    supports_trigger_pairs: bool  # True → [TRIG]/[TP] items get a shared-trigger-plan self-reset pair
    import_conf_key: str  # entry.options opt-in flag
    import_default: bool  # default for the opt-in flag (KNX ships OFF)
    ignored_conf_key: str | None = None  # entry.options key for the ignore-list (None → not supported)


# KNX objects live under $FubModules["11"] ("knxIo" per $FubTypes). They are implemented
# blind (no real KNX hardware / sample JSON) — see project_knx_objects memory. Every
# blind-guessed code point elsewhere (set_value params, webhook routing, trigger ref_type)
# carries an inline marker.
SOURCE_CATEGORIES: dict[WebioClass, SourceCategory] = {
    WebioClass.MARKER: SourceCategory(
        key=WebioClass.MARKER,
        fub_module_type="2",
        data_key="markers",
        audit_key_prefix=_MARKER_AUDIT_KEY_PREFIX,
        webio_name_suffix=" [M]",
        label="Marker",
        german_label="Merker",
        schema_conf_key=CONF_SCHEMA_MARKER,
        schema_default=DEFAULT_SCHEMA_MARKER,
        id_placeholder="MarkerId",
        title_placeholder="MarkerTitle",
        unique_id_infix="m",
        range_clustered=True,
        supports_trigger_pairs=True,
        import_conf_key="import_markers",
        import_default=True,
        ignored_conf_key=CONF_IGNORED_MARKERS,
    ),
    WebioClass.IO: SourceCategory(
        key=WebioClass.IO,
        fub_module_type="1",
        data_key="io",
        audit_key_prefix=_IO_AUDIT_KEY_PREFIX,
        webio_name_suffix=" [IO]",
        label="IO",
        german_label="IO",
        schema_conf_key=CONF_SCHEMA_IO,
        schema_default=DEFAULT_SCHEMA_IO,
        id_placeholder="IoId",
        title_placeholder="IoTitle",
        unique_id_infix="",
        range_clustered=False,
        supports_trigger_pairs=False,
        import_conf_key="import_ios",
        import_default=True,
        ignored_conf_key=None,
    ),
    WebioClass.KNX: SourceCategory(
        key=WebioClass.KNX,
        fub_module_type="11",
        data_key="knx",
        audit_key_prefix=_KNX_AUDIT_KEY_PREFIX,
        webio_name_suffix=" [KNX]",
        label="KNX",
        german_label="KNX-Objekt",
        schema_conf_key=CONF_SCHEMA_KNX,
        schema_default=DEFAULT_SCHEMA_KNX,
        id_placeholder="KnxId",
        title_placeholder="KnxTitle",
        unique_id_infix="k",
        range_clustered=True,
        supports_trigger_pairs=True,
        import_conf_key="import_knx",
        import_default=False,
        ignored_conf_key=CONF_IGNORED_KNX,
    ),
}


def source_category(webio_class: str) -> SourceCategory:
    """Registry entry for a Web-IO class; raises ValueError on an unknown class."""
    try:
        return SOURCE_CATEGORIES[WebioClass(webio_class)]
    except (ValueError, KeyError) as err:
        raise ValueError(f"Unknown Web-IO class {webio_class!r}, expected one of {WEBIO_CLASSES}") from err


def active_webio_classes(conf: Mapping[str, Any]) -> tuple[WebioClass, ...]:
    """Web-IO classes currently opted into via each category's import_conf_key flag.

    A category the user has opted out of (e.g. KNX, opt-in and OFF by default) is never
    expected to have a Web-IO device/class on the Comexio server — audit and full-sync
    logic must use this instead of the unconditional WEBIO_CLASSES, or an opted-out
    category's absence gets flagged as "missing" (spurious repair issue, wipes
    last_audit_results every poll) and a full sync force-creates it anyway. WEBIO_CLASSES
    itself stays the right choice for uninstall/cleanup, which must still catch a class
    left behind from before the user opted out.
    """
    return tuple(
        cls
        for cls in WEBIO_CLASSES
        if conf.get(SOURCE_CATEGORIES[cls].import_conf_key, SOURCE_CATEGORIES[cls].import_default)
    )


def classify_audit_key(key: str) -> WebioClass:
    """Map an internal audit-map key back to its Web-IO class by longest-matching prefix.

    Every audit key built via io_audit_key()/source_audit_key() carries a registered
    prefix, so the loop below always finds a match in practice; the MARKER fallback only
    guards against a malformed/unexpected key reaching this function and is logged rather
    than applied silently, so such a case doesn't masquerade as a genuine Marker key.
    """
    best, best_len = None, -1
    for cat in SOURCE_CATEGORIES.values():
        if key.startswith(cat.audit_key_prefix) and len(cat.audit_key_prefix) > best_len:
            best, best_len = cat.key, len(cat.audit_key_prefix)
    if best is None:
        _LOGGER.warning("classify_audit_key: no category matches key %r, defaulting to MARKER", key)
        return WebioClass.MARKER
    return best


def category_by_fub_module_type(fub_module_type: int | str) -> SourceCategory:
    """Registry entry matching a $FubModules key / plan-element ref_type (2/11/...).

    The reverse of source_category(): trigger-pair and function-plan-link call sites
    only carry a bare ref_type int (see FUB_BASE_REF_ID_FLANKE / ref_type params in
    coordinator.py), not a WebioClass — this lets them derive the source's audit
    prefix/label registry-style instead of hard-coding a second "2"->"M" mapping.
    Raises ValueError for any fub_module_type not present in SOURCE_CATEGORIES.
    """
    fub_module_type = str(fub_module_type)
    for cat in SOURCE_CATEGORIES.values():
        if cat.fub_module_type == fub_module_type:
            return cat
    raise ValueError(f"No SourceCategory for fub_module_type {fub_module_type!r}")


def trigger_pair_categories() -> list[SourceCategory]:
    """Source categories whose [TRIG]/[TP] items get a shared-trigger-plan self-reset pair.

    Currently Marker + KNX (IO has no server-side state to self-reset). Callers iterate
    this instead of hard-coding the (2, 11) ref_type pair so a further trigger-capable
    module type is picked up by flipping one registry flag.
    """
    return [cat for cat in SOURCE_CATEGORIES.values() if cat.supports_trigger_pairs]


def ignore_list_categories() -> list[SourceCategory]:
    """Source categories that support an ignore-list (schema + import + ignored-ids options).

    Currently Marker + KNX — the same two categories trigger_pair_categories() returns today,
    but for an unrelated reason (ignored_conf_key vs. supports_trigger_pairs), so options_flow's
    per-category options schema loop uses this rather than trigger_pair_categories() as a
    stand-in. A future category could support one flag without the other.
    """
    return [cat for cat in SOURCE_CATEGORIES.values() if cat.ignored_conf_key is not None]


def webio_class_name(webio_name: str, webio_class: str) -> str:
    """Comexio Web-IO class name for one of the HA-managed classes (marker/io/knx)."""
    return f"{webio_name}{source_category(webio_class).webio_name_suffix}"


def webio_class_label(webio_class: str) -> str:
    """Short display label ('Marker'/'IO'/'KNX') for a Web-IO class, used in sync/audit messages."""
    return source_category(webio_class).label


def io_audit_key(ext_name: str, identifier: str) -> str:
    """Build the internal audit-map key identifying an IO (composite ext_name+identifier key —
    IO's own special case; range_clustered categories use source_audit_key() instead)."""
    return f"{_IO_AUDIT_KEY_PREFIX}{ext_name}_{identifier}"


def source_audit_key(category: SourceCategory, item_id: object) -> str:
    """Build the internal audit-map key identifying a range_clustered source item (Marker/KNX).

    Registry-driven counterpart to io_audit_key(): every range_clustered category keys its
    audit-map entries by a plain "<prefix><id>" string (e.g. "M5"/"K5"), so one function
    covers all of them via category.audit_key_prefix — classify_audit_key() is the reverse
    lookup (audit key -> WebioClass) for callers that need to go the other way.
    """
    return f"{category.audit_key_prefix}{item_id}"


# Sync-progress percentage span shared by all active Web-IO classes, subdivided evenly per
# class in button.py's `_class_pct_ranges` so the UI progress bar advances smoothly regardless
# of how many classes are active (not hard-coded to a specific count).
SYNC_PROGRESS_START_PCT = 5
SYNC_PROGRESS_END_PCT = 95


# UI icon literals — centralized so emoji usage stays consistent and easy to audit
ICON_WARNING = "⚠️"
ICON_FLAG = "🏁"
ICON_SUCCESS = "✅"
ICON_DURATION = "⏱"
ICON_CHECK = "✓"
ICON_ERROR = "❌"
ICON_ROCKET = "🚀"
ICON_TOOLS = "🛠️"
ICON_DELETE = "🗑️"
ICON_UPLOAD = "📤"
ICON_NETWORK = "🌐"
ICON_RENAME = "✏️"
ICON_ADD = "➕"
ICON_FIX = "🔧"
ICON_CLOCK = "🕒"
ICON_LINK = "🔗"
ICON_PUZZLE = "🧩"
ICON_MUTE = "🔇"
ICON_CLEANUP = "🧹"
ICON_INFO = "💡"
ICON_SYNC = "🔄"
ICON_INACTIVE = "➖"

DEFAULT_NAME = "Comexio"

SCAN_INTERVAL_DEFAULT = 15
SCAN_INTERVAL_OPTIONS = ["1", "5", "10", "15", "30", "45", "60", "120", "300", "600", "1440"]

# Per-request HTTP timeout for all Comexio API calls (seconds). Without this, a stalled
# response hangs the calling coroutine indefinitely instead of failing fast.
COMEXIO_HTTP_TIMEOUT_SEC = 30
# Outer safety net around the whole Function Plan backup cycle (bulk load + auto-backup +
# paper backfill + purge): guarantees the cycle's lock is released even if some step hangs
# in a way COMEXIO_HTTP_TIMEOUT_SEC doesn't cover, so a stuck cycle can't permanently block
# every future backup attempt.
FUNCTION_PLAN_BACKUP_CYCLE_TIMEOUT_SEC = 300

# Operation durations for progress calculation (seconds)
SYNC_DURATION_DELETE = 4
SYNC_DURATION_WRITE = 35
SYNC_DURATION_RECREATE = 79
SYNC_DURATION_FUNCTION_PLAN_PLAN = 90  # stop + element deletion per affected function plan
SYNC_DURATION_FUNCTION_PLAN_PAIR = 3  # fallback per-pair estimate for stale issues without an eta in their data
SYNC_DURATION_FUNCTION_PLAN_FINALIZE = (
    14  # PER-PLAN base: change backup, config reload, stop round-trips (measured live)
)
SYNC_DURATION_FUNCTION_PLAN_ELEMENT = (
    0.45  # per element in the plan: reposition + activation compile time (measured live)
)

MARKER_TYPE_INTERVAL = 3
MARKER_INTERVAL_MAX_VALUE = 86400

# A marker whose Comexio-side title ends with this suffix is exposed to HA as a
# read-only sensor/binary_sensor instead of a writable number/switch (e.g. "Boiler Temp [RO]").
MARKER_READ_ONLY_SUFFIX = "[RO]"

# "Virtueller Taster" markers: exposed to HA as a button instead of a switch. "[TP]" (Time
# Pulse) is the legacy alias from the original 2026-06-27 discovery; "[TRIG]" is the current,
# preferred name — both are recognized so existing [TP] markers don't need renaming.
MARKER_TRIGGER_SUFFIXES = ("[TRIG]", "[TP]")

# Safety cap for the marker_delete service's marker_id field (supports comma lists AND
# inclusive ranges, e.g. "306-355") — a single typo'd range boundary (e.g. "306-3555")
# would otherwise fire thousands of sequential, irreversible delete requests against the
# live controller before the user notices. Confirm alone doesn't protect against this,
# since it's checked once per call, not once per resolved id.
MARKER_DELETE_MAX_COUNT = 200


class MarkerKind(StrEnum):
    """How a marker is exposed to HA, derived from its Comexio-side title suffix."""

    NORMAL = "normal"
    READ_ONLY = "read_only"
    TRIGGER = "trigger"


# Analog markers have no configurable value range on the Comexio side, so their Web-IO
# datapoints must not clamp. Comexio's Web-IO push mechanism has two independently verified,
# server-side bugs (systematically reproduced 2026-08-30, unrelated to the HA integration
# code): (1) json_stringify() rounds numeric values to 6 significant decimal digits, and
# (2) whenever a Web-IO command's own Min/Max bounds sit at/near the signed-16-bit boundary
# (~±32767/32768), EVERY pushed value is silently clamped to the configured Max regardless of
# the actual input. ±500,000 was empirically confirmed to dodge both: it clears the int16
# danger zone by a wide margin, and its own magnitude never exceeds 6 significant digits.
# Physical IOs keep the authentic min/max from their Comexio type definition instead, but
# see WEBIO_INT16_DANGER_ZONE for the same guard applied there.
WEBIO_MARKER_ANALOG_MIN = -500_000
WEBIO_MARKER_ANALOG_MAX = 500_000

# Physical IO ranges scraped from Comexio's own type definitions (percent, temperature,
# voltage, ...) normally sit far outside the int16 danger zone described above, but a raw or
# counter-style IO type could plausibly land its Min/Max right on that boundary. Any IO whose
# authentic Min or Max falls in this band gets widened to the same verified-safe
# WEBIO_MARKER_ANALOG_MIN/MAX range before being sent as a Web-IO command.
WEBIO_INT16_DANGER_ZONE = (30_000, 40_000)

DEFAULT_HOST = "192.168.1.100"

# Extension firmware check: Comexio warns this call can briefly interrupt extension outputs
# while it runs, so it must not be polled like the other data. Gated instead on a change of
# api.comexio_version (already tracked for the catalog cache) — a base firmware update makes
# a matching extension update likely, so the check is deferred to the next nightly window
# and skipped entirely when the version hasn't moved since the last check.
FIRMWARE_CHECK_HOUR = 4
FIRMWARE_CHECK_MINUTE = 0


def fw_update_signal(server_id: str) -> str:
    """Dispatcher signal fired when a fresh extension firmware check result arrives."""
    return f"{DOMAIN}_{server_id}_fw_update"


# Web-IO analog range check: the bulk config scrape ($FubModules["10"], see api.py
# _add_webhook_command) never returns a Web-IO command's actual Min/Max for HA's own
# commands (confirmed live 2026-08-30) — Comexio only exposes those via each command's
# individual edit form. Reading that form costs one HTTP GET per analog command
# (currently ~150-400), so — like the firmware check above — this runs on its own nightly
# schedule rather than on every poll or every manual sync. Offset by 20 minutes from
# FIRMWARE_CHECK_HOUR/MINUTE so the two nightly jobs don't burst requests at Comexio
# simultaneously.
WEBIO_RANGE_CHECK_HOUR = 4
WEBIO_RANGE_CHECK_MINUTE = 20


def webio_range_check_entity_id(server_id: str) -> str:
    """Entity ID of the Web-IO range-check button — slugified so hyphens etc. in server_id stay valid.

    Used both at platform setup (button.py) and by the one-time migration that repoints
    any pre-existing registration built from the old, unslugified formula (__init__.py).
    """
    return f"button.comexio_{slugify(server_id)}_webio_range_check"


# Result dict keys shared between coordinator._async_webio_range_check_tick and the
# range-check button's notification — extracted so both sides can't drift apart on a typo.
RANGE_CHECK_CHECKED = "checked"
RANGE_CHECK_FIXED = "fixed"
RANGE_CHECK_FAILED = "failed"
RANGE_CHECK_CORRECTION_FAILED = "correction_failed"
RANGE_CHECK_EXCLUDED = "excluded"
RANGE_CHECK_SKIPPED = "skipped"


# Bus workload monitoring: independent fast poll of the Comexio internal bus/CPU load,
# separate from the main config-audit coordinator (which runs every few minutes). The raw
# reading is exposed as a sensor; sustained-rise/overload detection is handled by the
# Bus-Load-Watchdog below, which needs the sample history the poll builds up.
BUS_LOAD_POLL_INTERVAL_SEC = 10

# Consecutive failed ticks before the diagnostics fall back to "unknown" instead of
# silently keeping the last successful reading forever.
BUS_LOAD_FAIL_STREAK_THRESHOLD = 3


def bus_load_signal(server_id: str) -> str:
    """Dispatcher signal fired when a fresh Comexio bus workload reading arrives."""
    return f"{DOMAIN}_{server_id}_bus_load_update"


# Bus-Load-Watchdog: self-healing reaction to a sustained bus-load rise (observed root cause:
# a Comexio-side WebIO command stack that gets stuck over many hours; restarting the HA-managed
# cluster function plans relieves it — discovered manually 2026-07-30/31). Kept as plain module
# constants (not options-flow fields) so they can be tuned without picking through the logic.
#
# Rise-detection window/threshold are a first estimate from the real incident (~20 percentage
# points over ~40h, i.e. ~0.5pp/h): a 6h window with a 10pp threshold would fire around the
# halfway mark of a comparable slow-burn rise, well before it reaches critical levels. Tune
# after living with real data rather than reworking the algorithm.
BUS_LOAD_STARTUP_GRACE_SEC = 900  # no rise-evaluation for 15 min after coordinator start
BUS_LOAD_RISE_WINDOW_SEC = 21600  # 6h comparison window
BUS_LOAD_RISE_THRESHOLD_PCT = 10  # rise over the window that counts as "sustained"
BUS_LOAD_SMOOTHING_SAMPLES = 5  # last N 10s ticks averaged into "current" (ignore single spikes)

CASCADE_POST_STOP_WAIT_SEC = 30  # wait after stopping each managed plan, before restarting it
CASCADE_POST_RESTART_SETTLE_SEC = 30  # wait after restarting, before checking for recovery
CASCADE_RECOVERY_DROP_PCT = 10  # drop from the pre-cascade baseline that counts as "recovered"
CASCADE_COOLDOWN_SEC = 1800  # don't re-trigger a cascade for this long after one finishes

EMERGENCY_REBOOT_WINDOW_SEC = 300  # 5 min sustained-average window
EMERGENCY_REBOOT_THRESHOLD_PCT = 90  # average bus load over the window that triggers a reboot
EMERGENCY_REBOOT_COOLDOWN_SEC = 3600  # don't re-trigger for 1h after a reboot (system is rebooting)

WATCHDOG_HISTORY_MAX_ENTRIES = 20  # trim limit for the persisted watchdog event log

CONF_BUS_WATCHDOG_ENABLED = "bus_watchdog_enabled"
DEFAULT_BUS_WATCHDOG_ENABLED = True  # cascade-restarts HA-managed plans only, non-destructive
CONF_BUS_WATCHDOG_AUTO_REBOOT = "bus_watchdog_auto_reboot"
DEFAULT_BUS_WATCHDOG_AUTO_REBOOT = False  # gates the immediate, unconfirmed system reboot call


# Function Plan backup/restore: rotating snapshot slots per plan (see function_plan_backup.py).
FUNCTION_PLAN_AUTO_BACKUP_SLOTS = 3
FUNCTION_PLAN_CHANGE_BACKUP_SLOTS = 10
CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS = "function_plan_backup_retention_months"
DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS = 6
TIMESTAMP_DISPLAY_FORMAT = "%d.%m.%Y %H:%M"

# The six live-plan-state services with a services.yaml `fub_id` dropdown that needs to stay in
# sync with the managed plan set (services/_yaml_sync.py) — single source of truth also used by
# services/__init__.py's registration, so the two lists can't silently diverge. Named individually
# (rather than relying on positional unpacking of FUNCTION_PLAN_SERVICE_NAMES) so a reorder can't
# silently swap which service a consumer module thinks it's registering.
FUNCTION_PLAN_SERVICE_CONNECT = "function_plan_connect"
FUNCTION_PLAN_SERVICE_SORT = "function_plan_sort"
FUNCTION_PLAN_SERVICE_STOP = "function_plan_stop"
FUNCTION_PLAN_SERVICE_ACTIVATE = "function_plan_activate"
FUNCTION_PLAN_SERVICE_VISUALIZE = "function_plan_visualize"
FUNCTION_PLAN_SERVICE_ANALYZE = "function_plan_analyze"
FUNCTION_PLAN_SERVICE_FLOW_DIAGRAM = "function_plan_flow_diagram"
FUNCTION_PLAN_SERVICE_NAMES = (
    FUNCTION_PLAN_SERVICE_CONNECT,
    FUNCTION_PLAN_SERVICE_SORT,
    FUNCTION_PLAN_SERVICE_STOP,
    FUNCTION_PLAN_SERVICE_ACTIVATE,
    FUNCTION_PLAN_SERVICE_VISUALIZE,
    FUNCTION_PLAN_SERVICE_ANALYZE,
    FUNCTION_PLAN_SERVICE_FLOW_DIAGRAM,
)

# HA-bus event fired for every webhook value push that belongs to the currently rendered
# live plan preview — consumed by the comexio-plan-card debug box (frontend/).
EVENT_PLAN_VALUE = "comexio_plan_event"

# DEBUG-log line emitted once per inbound webhook value push (marker or IO). Lets a burst of
# "Manually updated comexio data" coordinator lines be traced back to the exact marker/IO and
# value that caused it. Args: kind ("marker"/"io"), resolved label, new value, previous value.
WEBHOOK_VALUE_LOG_MSG = "Webhook %s push: %s = %r (prev %r)"

# WARNING emitted when a webhook pushes a value for an IO key that is not in the parsed config
# index — the value cannot be tracked and is dropped. Args: ext name, identifier, new value.
WEBHOOK_UNKNOWN_IO_LOG_MSG = "Webhook push for unknown IO %s/%s (value %r) — not tracked, dropped"

# Marker for a comment element the restore-as-new path adds to a rebuilt plan, so a user
# opening it in Comexio Studio immediately understands why it exists and shouldn't hand-edit it.
FUNCTION_PLAN_MANAGED_PLAN_COMMENT = "! Administrated by HomeAssistant, dont delete or rename !"


# Function Plan canvas grid layout — shared by the function plan services (sorting) and the
# direct grid placement in api.function_plan_add_marker_pairs / _add_io_pairs.
FUNCTION_PLAN_LAYOUT_X_MARKER = 15.0  # left margin of the source (marker/IO) column
FUNCTION_PLAN_LAYOUT_X_WEBIO = 210.0  # marker + 195 gap
FUNCTION_PLAN_LAYOUT_Y_START = 30.0  # first data row — leaves room for the managed-plan comment
FUNCTION_PLAN_LAYOUT_COMMENT_Y = 7.5  # the managed-plan comment sits above the first data row
FUNCTION_PLAN_LAYOUT_Y_STEP = 22.5
FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH = 450.0  # x-distance between column groups
FUNCTION_PLAN_LAYOUT_GRID_SNAP = 7.5  # Studio snaps element positions to half the 15-unit raster

# Freshly uploaded Web-IO commands don't always show up in the very next config reload —
# Comexio's admin page appears to regenerate on its own cycle rather than per write.
# Retry the reload with exponential backoff before giving up on a pair.
FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS = 5
FUNCTION_PLAN_PAIR_RELOAD_INITIAL_DELAY = 1.0  # seconds, doubles every attempt

# Trigger markers ([TRIG]/[TP]): HA maintains one dedicated managed plan containing only
# Marker+Flanke self-reset pairs — never a Web-IO element. A trigger marker's Web-IO command
# stays exactly where every other marker's already lives (the normal marker cluster plan, a
# completely separate fub_id), because the marker's write path is the same direct
# api.set_value() call used by any writable marker; only the auto-reset-to-0 needs Comexio-side
# logic. Verified live 2026-08-29 (TestPlan fub_id 33, M6): with only Marker+Flanke wired in
# one plan (Marker output -> Flanke "In"; Flanke "+" output -> Marker input, no Web-IO element
# involved at all) and the Web-IO wiring left in the standard "HA - Marker" plan, toggling the
# HA switch on made M6 fall back to off immediately, exactly as intended — the marker's plan
# input behaves as a toggle, not a level-set, so each incoming edge (from any plan) flips it.
FUNCTION_PLAN_TRIGGER_PLAN_NAME = "HA - TRIGGER"
FUB_BASE_REF_ID_FLANKE = "113"  # $FubModules["5"]["113"], internal catalog name "flankenerkenner"
FLANKE_PORT_IN = 0  # "In"
FLANKE_PORT_OUT_RISING = 1  # "+", fires for one cycle on a rising edge only

# Compact per-pair layout in the trigger plan (Marker + Flanke only, no Web-IO element).
FUNCTION_PLAN_TRIGGER_LAYOUT_X_MARKER = FUNCTION_PLAN_LAYOUT_X_MARKER
FUNCTION_PLAN_TRIGGER_LAYOUT_X_FLANKE = FUNCTION_PLAN_LAYOUT_X_WEBIO
# The Flanke block has 3 in-/3 out-ports -> renders 1+3 = 4 row-heights tall (see the
# renderer's own geo["h"] formula, function_plan_render_geometry.py), unlike the single-row-tall
# marker/WebIO pills the generic FUNCTION_PLAN_LAYOUT_Y_STEP (22.5) was sized for. Reusing that
# step let consecutive trigger rows' Flanke blocks visually overlap. 90.0 = 4 rows (60) + one
# blank row (15) of breathing room between pairs, rounded up to the 7.5 grid snap.
FUNCTION_PLAN_TRIGGER_LAYOUT_Y_STEP = 90.0

# Fixed top-to-bottom rhythm of one extension column in a managed IO cluster plan. Groups the
# extension lacks are skipped; one blank row separates consecutive groups (visual scanning
# aid). "?" is the bucket for unknown prefixes — they sort after the known groups but before
# the onboard TL/UL tail, which always closes the column (TL + UL with no blank row between).
FUNCTION_PLAN_IO_GROUP_ORDER = ("I", "AI", "AO", "EI", "Q", "QI", "?", "TL", "UL")
_IO_GROUP_UNKNOWN = FUNCTION_PLAN_IO_GROUP_ORDER.index("?")
_IO_GROUP_TAIL = FUNCTION_PLAN_IO_GROUP_ORDER.index("TL")
# English header text per group, for the auto-placed comment block above each group.
FUNCTION_PLAN_IO_GROUP_LABELS: dict[str, str] = {
    "I": "Inputs",
    "AI": "Analog Inputs",
    "AO": "Analog Outputs",
    "EI": "EnOcean Input",
    "Q": "Outputs",
    "QI": "Power Inputs",
    "?": "not Listed",
    "TL": "Onboard",
    "UL": "Onboard",
}

_IO_IDENTIFIER_RE = re.compile(r"^([A-Za-z]+)(\d+)(?:_.*)?$")


def snap_to_grid(value: float) -> float:
    """Snap a canvas coordinate to the Studio placement grid (avoids off-raster drift)."""
    return round(value / FUNCTION_PLAN_LAYOUT_GRID_SNAP) * FUNCTION_PLAN_LAYOUT_GRID_SNAP


def io_sort_key(identifier: str) -> tuple[int, int, str]:
    """Sort key of an IO within its extension column: (group index, number, identifier).

    The group is the letter prefix matched exactly against FUNCTION_PLAN_IO_GROUP_ORDER
    (exact match keeps Q and QI distinct); a suffix after the number is allowed
    (EnOcean profile inputs like "EI1_WIN"). Unknown prefixes land in the "?" bucket
    between the known groups and the TL/UL tail.
    """
    if m := _IO_IDENTIFIER_RE.match(identifier):
        prefix, num = m.group(1).upper(), int(m.group(2))
        if prefix in FUNCTION_PLAN_IO_GROUP_ORDER:
            return (FUNCTION_PLAN_IO_GROUP_ORDER.index(prefix), num, identifier)
        return (_IO_GROUP_UNKNOWN, num, identifier)
    return (_IO_GROUP_UNKNOWN, 0, identifier)


def _io_group_prefix(identifier: str) -> str:
    """Letter prefix of an IO identifier ('I1' -> 'I', 'EI1_WIN' -> 'EI'), '?' if unmatched."""
    m = _IO_IDENTIFIER_RE.match(identifier)
    return m.group(1).upper() if m else "?"


def io_group_header_text(prefix: str) -> str:
    """Comment text for an IO type-group's header row (see io_group_headers)."""
    return f"---  {FUNCTION_PLAN_IO_GROUP_LABELS.get(prefix, prefix)}  ---"


def _io_row_blocks(identifiers: list[str]) -> tuple[dict[str, int], dict[int, str]]:
    """Row slot per IO identifier + header comment text per reserved header row.

    One row is reserved for a header comment before the very first type group and before
    every later group boundary (skipped inside the TL/UL onboard tail, which reads as one
    block); every later boundary additionally gets one blank separator row between the
    prior group's last IO and its header, so the header never reads as touching the
    previous block — io_column_rows and io_group_headers both derive from this single
    pass so their row numbering can never drift apart.
    """
    rows: dict[str, int] = {}
    headers: dict[int, str] = {}
    row = 0
    prev_group: int | None = None
    for ident in sorted(identifiers, key=io_sort_key):
        group = io_sort_key(ident)[0]
        if prev_group is None or (group != prev_group and min(group, prev_group) < _IO_GROUP_TAIL):
            if prev_group is not None:
                row += 1  # blank row between the previous group's last IO and this header
            headers[row] = io_group_header_text(_io_group_prefix(ident))
            row += 1
        rows[ident] = row
        row += 1
        prev_group = group
    return rows, headers


def io_column_rows(identifiers: list[str]) -> dict[str, int]:
    """Deterministic row slot of every IO inside its extension column.

    IOs are sorted by io_sort_key; one row is reserved for a header comment above each
    type group's first IO (see io_group_headers), except inside the TL/UL onboard tail
    (temperature + voltage read as one block, no separate header). Because the slots
    derive from the FULL identifier list of the extension, an IO retrofitted later lands
    in exactly the slot the initial layout reserved for it.
    """
    return _io_row_blocks(identifiers)[0]


def io_group_headers(identifiers: list[str]) -> dict[int, str]:
    """Row -> header comment text ('---  Inputs  ---') for every IO type group's block.

    The row is always the one directly above the group's first IO row from
    io_column_rows — both are computed together in _io_row_blocks so they can't diverge.
    """
    return _io_row_blocks(identifiers)[1]


# need for ha ip dns validation, to avoid false positives
KNOWN_DOMAINS = [
    "fritz.box",
    "local",
    "lan",
    "home",
    "speedport.ip",
    "tplinkwifi.net",
    "home.arpa",
    "mshome.net",
    "internal",
]


def parse_ignored_marker_tokens(
    raw: str, prefix_chars: str = "Mm"
) -> Iterator[tuple[str, int | tuple[int, int] | None]]:
    """Split and parse an ignored-ids string into (token, parsed) pairs.

    Handles comma/semicolon/space/dot separators, an optional leading letter prefix
    (`prefix_chars` — "Mm" for markers, "Kk" for KNX objects), and ranges like '8-12'.
    Empty tokens are skipped. `parsed` is an int for a single ID, a (start, end)
    tuple for an inclusive range, or None if the token could not be parsed.

    Shared low-level parser: `expand_ignored_marker_ids` below uses it for lenient runtime
    expansion, `options_flow._normalize_ignored_ids` uses it for strict UI validation.
    """
    for token in raw.replace(";", ",").replace(" ", ",").replace(".", ",").split(","):
        display_token = token.strip()
        if not display_token:
            continue
        parse_token = display_token.lstrip(prefix_chars)
        if not parse_token:
            yield display_token, None
            continue
        if "-" in parse_token:
            parts = parse_token.split("-", 1)
            try:
                start, end = int(parts[0]), int(parts[1].lstrip(prefix_chars))
            except ValueError:
                yield display_token, None
                continue
            yield display_token, (min(start, end), max(start, end))
        else:
            try:
                yield display_token, int(parse_token)
            except ValueError:
                yield display_token, None


def expand_ignored_marker_ids(raw: str, prefix_chars: str = "Mm") -> set[int]:
    """Expand an ignored-ids config string (markers or KNX) to a set of integer IDs.

    Invalid tokens are silently ignored (runtime use; options_flow validates separately).
    """
    result: set[int] = set()
    for _token, parsed in parse_ignored_marker_tokens(raw, prefix_chars):
        if parsed is None:
            continue
        if isinstance(parsed, tuple):
            result.update(range(parsed[0], parsed[1] + 1))
        else:
            result.add(parsed)
    return result
