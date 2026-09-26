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
# {ExtName} is left out on purpose: IO entities sit on one device per extension and use
# has_entity_name, so HA already prefixes the friendly name with "<server> <ext>".
DEFAULT_SCHEMA_IO = "{IoId} {IoTitle}"
# Default before minor version 3 — pinned into older entries that never saved a schema.
LEGACY_DEFAULT_SCHEMA_IO = "{ExtName} {IoId} {IoTitle}"
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

# Ids the user explicitly chose "leave as writable switch" for in the knx_dpt_ambiguous
# repair flow (see repairs.py) — distinct from CONF_IGNORED_KNX (excludes a KNX object from
# HA entirely); this only suppresses the classification nag for that one id. Same comma/
# range string format, parsed with expand_ignored_marker_ids().
CONF_KNX_DPT_SUFFIX_IGNORED = "knx_dpt_suffix_ignored"

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

# Config entry minor version 2 (v0.10.0): entries migrated from an older minor version get
# this one-shot option set, so the first setup checks for leftovers of the KNX pre-releases
# (v0.10.0-rc1..rc3 built KNX plans / Web-IO / bridge markers with an older layout) and
# offers the KNX cleanup as a repair issue. Removed again right after that check.
# Minor version 3: entries created before DEFAULT_SCHEMA_IO dropped {ExtName} get the old
# default pinned into their options (see migrate_entry_options).
CONFIG_ENTRY_MINOR_VERSION = 3
CONF_KNX_PRERELEASE_CLEANUP_PENDING = "knx_prerelease_cleanup_pending"
# Repair issue translation keys; the issue id is "{key}_{server_id}".
ISSUE_UNINSTALL_CLEANUP = "uninstall_cleanup"
ISSUE_KNX_PRERELEASE_CLEANUP = "knx_prerelease_cleanup"
# Uninstall cleanup: update the running notification every N reset bridge markers.
UNINSTALL_CLEANUP_PROGRESS_EVERY = 10


def uninstall_cleanup_notification_id(server_id: str) -> str:
    """Notification id of the running/finished uninstall cleanup (result uses "<id>_result")."""
    return f"comexio_uninstall_cleanup_{server_id}"


def uninstall_cleanup_pending_notification_id(server_id: str) -> str:
    """Notification id of the button's "repair issue waiting for confirmation" hint."""
    return f"{uninstall_cleanup_notification_id(server_id)}_pending"


CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN = "logikplan_max_pairs_per_plan"
DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN = 100
# KNX cluster plans hard-cap at this size, ignoring CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN
# entirely (user decision 2026-09-20) — a KNX bridge row needs its own K-object column plus a
# wider WebIO column (services/_grid.py _KNX_COLUMN_WIDTH) and reserves two row-slots per pair
# for the Phase 7 API-Loopback fan-out (_KNX_LOOPBACK_Y_OFFSET), so an A3-formatted canvas fits
# 2 columns × 26 two-slot pairs = 52 pairs at the KNX-only row pitch (FUNCTION_PLAN_KNX_LAYOUT_Y_STEP)
# — see _cluster_plan_name's docstring (coordinator.py) for the full math and its correction
# history. Bucketing more than the canvas can hold under the generic (marker-sized) setting
# silently overflowed it (dropped pairs, see _assign_grid_positions' overflow warnings) — the
# plan's own name/range must reflect this real ceiling, not the marker-sized default of 100.
FUNCTION_PLAN_KNX_CLUSTER_SIZE = 50
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
# Must stay comfortably above SYNC_DURATION_WRITE (the calibrated ETA for a single Web-IO
# write/recreate call) -- at 30s it was tighter than that estimate itself, so a class recreate
# (create_webio_device's saveDeviceWindow POST) reproducibly hit TimeoutError under normal
# load on the live Comexio instance (observed repeatedly 20./21.09.2026, always at the same
# call). 120s (raised from the initial 45s fix, 21.09.2026) gives headroom for larger
# installations and for Comexio's own admin UI becoming slow for minutes after a heavy
# write batch (user-observed) -- COMEXIO_PROGRESS_LOG_INTERVAL_SEC below keeps a long wait
# like that visible instead of looking hung.
COMEXIO_HTTP_TIMEOUT_SEC = 120
# How often (seconds) a still-open Comexio HTTP request logs a "still waiting" progress line
# (see api.py's request trace config). Purely a UI/log-visibility knob, independent of the
# actual timeout above.
COMEXIO_PROGRESS_LOG_INTERVAL_SEC = 10
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

# Auto-created write-path bridge Markers (Entwurf A "Merker-Brücke", see
# project_knx_write_path_design memory) are titled "<K-Titel> [K<k_id>]" by
# create_knx_bridge_marker() — purely internal wiring glue with no HA entity of its own and
# no expected Web-IO command (the KNX object it feeds already gets its own K-entity/Web-IO).
MARKER_KNX_BRIDGE_SUFFIX_RE = re.compile(r"\[K\d+\]$")

# Round-boundary size the KNX bridge marker block is aligned to (user decision 2026-09-14,
# see project_knx_write_path_design memory) — also caps how far _free_marker_ids() looks
# above an already-established block's start when hunting for a reusable blank marker, so
# an unrelated real marker created well above the block can never be swept up as "free" just
# because the block's boundary is now reused indefinitely instead of recomputed every call.
MARKER_KNX_BRIDGE_BLOCK_SIZE = 50

# Phase 7 "API-Loopback" (see project_knx_write_path_design memory, "Phase 7" section): a
# K-Element's read-path output already reaches an HA-webhook Web-IO command (the normal
# source->Web-IO pair every category gets). This adds a SECOND Web-IO command per K-Element,
# fanned out from the same K-Element output, whose Lua script GETs this same Comexio server's
# own /api/?action=set endpoint to write the bridge Marker directly — closing the "Punkt 4"
# stuck-bridge-marker loop entirely inside Comexio (no HA/AWL round trip involved), since an
# out-of-band HTTP call — unlike a function-plan wire or AWL — never enters Comexio's
# algebraic-loop plan-start dependency graph. Live-verified 17.09.2026 for both a digital
# (K1->M300) and an analog (K10->M309) test pair. One class + one device, created once per
# server (not per K-Element, unlike the per-category Marker/IO/KNX classes above) — every
# K-Element just gets one more command in this same class/device.
WEBIO_CLASS_NAME_KNX_LOOPBACK = "ComexioAPI"
WEBIO_DEVICE_NAME_KNX_LOOPBACK = "Comexio API - KNX Loopback"


def knx_loopback_command_name(k_id: int, marker_id: int) -> str:
    """Web-IO command name for one K-Element's API-Loopback command, e.g. "KNX K1 to M300"."""
    return f"KNX K{k_id} to M{marker_id}"


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
    KNX_BRIDGE = "knx_bridge"


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

# KNX DPT (Datenpunkttyp) analog value ranges, keyed by the official KNX Association
# datapoint type numbering (KnxBaseTypeId, KnxSubId) — e.g. (9, 7) = DPT9.007 Humidity.
# Comexio's own $FubModules["11"]/$IOTypesBinary catalogs carry no usable value range for
# KNX object types (min/max come back as a 0/0 placeholder), so the real range is instead
# resolved per K-element via ComexioAPI.get_knx_dpt_catalog() (the $KnxDpt/$KnxDevices/
# $KnxPoints chain scraped from /admin/knx_one_wire/knx/). This widens the HA Number entity's
# own displayed/validated range, and is also used verbatim (see ComexioAPI._knx_webio_range) as
# the Min/Max embedded in the two Web-IO commands built for a KNX object (the HA-webhook push
# command and the Phase 7 API-Loopback command) instead of the generic WEBIO_MARKER_ANALOG_MIN/MAX
# range — deliberately NOT capped to that range, even though Comexio itself still has an open
# firmware bug (confirmed live by the user 2026-09-20, fix targeted for 11.1.4) that rounds/
# corrupts analog values above ~1,000,000; see README for the documented limitation.
# Composite DPTs (DPT3.x control+step, DPT18.001 control+scene number) split into two
# Comexio Points from one Device; the binary half never reaches this table (it becomes a
# switch/binary_sensor entity, not a Number), so only each composite's analog component is
# listed here.
# Deliberately excluded: DPT1 (binary, never reaches this table), DPT10/11/19 (Time/Date/
# DateTime — Comexio itself refuses to create K-elements for these), and DPT14 (4-byte
# float — no tighter KNX-standard-defined range than the IEEE754 span, so it falls back to
# WEBIO_MARKER_ANALOG_MIN/MAX like any other unresolved KNX analog element).
# The 4th tuple element is the HA Number entity's native_step (found missing entirely in
# review 2026-09-20: ComexioKnxNumber never set a DPT-derived step, so every KNX number
# entity silently inherited ComexioMarkerNumber's hardcoded 0.1 regardless of the underlying
# encoding — wrong for every whole-number DPT below, e.g. a 2-octet counter got a 0.1 step
# that doesn't exist in the actual 1-count resolution). Two kinds of encoding occur here:
# - Raw N-octet integer values (DPT5.4/5.5/5.6/5.10, 6.x, 7.x, 8.x, 12.x, 13.x, 17.1, 18.1,
#   and the DPT3.x step code): the wire value IS an integer 1:1, so step=1.
# - KNX "scaled" 1-byte types (DPT5.1 Scaling, DPT5.3 Angle): the wire value is a raw byte
#   0-255 linearly mapped onto the listed min..max span, so the real resolution is
#   (max-min)/255 — using step=1 here would make the actual on-the-wire granularity
#   unreachable via the HA slider/stepper, and using the old flat 0.1 doesn't line up with
#   the grid either (see K7/DPT5.001 in dev-tools/knx_seed_test_matrix.py: raw byte 12 ->
#   12*100/255 = 4.70588..., not a multiple of 0.1 — exactly the "enter a valid value, next
#   are 4.7 and 4.8" glitch reported live 2026-09-20).
# - DPT9 (2-octet float) keeps the pre-existing flat 0.1 default explicitly here (no simple
#   universal resolution across its whole span) — unchanged behavior, just made explicit now
#   that every entry must carry a step.
# Format: {(base_type_id, sub_id): (min, max, unit, step)}
KNX_DPT_ANALOG_RANGES: dict[tuple[int, int], tuple[float, float, str, float]] = {
    # DPT3 - 1-Bit control + 3-Bit step code (Dimming/Blinds), analog half is the step value.
    (3, 7): (0, 7, "", 1),
    (3, 8): (0, 7, "", 1),
    # DPT5 - 8-Bit unsigned value.
    (5, 1): (0, 100, "%", 100 / 255),  # Scaling
    (5, 3): (0, 360, "°", 360 / 255),  # Angle
    (5, 4): (0, 255, "%", 1),  # Percent_U8
    (5, 5): (0, 255, "", 1),  # DecimalFactor
    (5, 6): (0, 254, "", 1),  # Tariff
    (5, 10): (0, 255, "", 1),  # Value_1_Ucount (pulse counter)
    # DPT6 - 8-Bit signed value.
    (6, 1): (-128, 127, "%", 1),  # Percent_V8
    (6, 10): (-128, 127, "", 1),  # Value_1_Count
    # DPT7 - 2-Octet unsigned value.
    (7, 1): (0, 65535, "", 1),
    (7, 2): (0, 65535, "ms", 1),
    (7, 3): (0, 65535, "10ms", 1),
    (7, 4): (0, 65535, "100ms", 1),
    (7, 5): (0, 65535, "s", 1),
    (7, 6): (0, 65535, "min", 1),
    (7, 7): (0, 65535, "h", 1),
    (7, 10): (0, 65535, "", 1),
    # DPT8 - 2-Octet signed value.
    (8, 1): (-32768, 32767, "", 1),
    (8, 2): (-32768, 32767, "ms", 1),
    (8, 3): (-32768, 32767, "10ms", 1),
    (8, 4): (-32768, 32767, "100ms", 1),
    (8, 5): (-32768, 32767, "s", 1),
    (8, 6): (-32768, 32767, "min", 1),
    (8, 7): (-32768, 32767, "h", 1),
    (8, 10): (-32768, 32767, "%", 1),
    # DPT9 - 2-Octet float value (KNX floating-point-16, format range -671088.64..670760.96).
    (9, 1): (-273, 670760, "°C", 0.1),  # Value_Temp
    (9, 2): (-670760, 670760, "K", 0.1),  # Value_Tempd (temperature difference)
    (9, 4): (0, 670760, "lx", 0.1),  # Value_Lux
    (9, 5): (0, 670760, "m/s", 0.1),  # Value_Wsp
    (9, 6): (0, 670760, "Pa", 0.1),  # Value_Pres
    (9, 7): (0, 100, "%", 0.1),  # Value_Humidity (physically bounded)
    (9, 8): (0, 670760, "ppm", 0.1),  # Value_AirQuality
    (9, 20): (-670760, 670760, "V", 0.1),  # Value_Volt
    (9, 21): (-670760, 670760, "mA", 0.1),  # Value_Curr
    (9, 24): (-670760, 670760, "kW", 0.1),  # Power
    # DPT12 - 4-Octet unsigned value.
    (12, 1): (0, 4294967295, "", 1),
    # DPT13 - 4-Octet signed value.
    (13, 1): (-2147483648, 2147483647, "", 1),
    (13, 10): (-2147483648, 2147483647, "Wh", 1),
    (13, 11): (-2147483648, 2147483647, "VAh", 1),
    (13, 12): (-2147483648, 2147483647, "VARh", 1),
    (13, 13): (-2147483648, 2147483647, "kWh", 1),
    (13, 14): (-2147483648, 2147483647, "kVAh", 1),
    (13, 15): (-2147483648, 2147483647, "kVARh", 1),
    (13, 100): (-2147483648, 2147483647, "s", 1),
    # DPT17 - Scene number.
    (17, 1): (0, 63, "", 1),
    # DPT18 - Scene control (1-Bit learn/execute + 6-Bit scene number); analog half is the
    # scene-number component.
    (18, 1): (0, 63, "", 1),
}

# Value shared by both homeassistant.components.number.NumberDeviceClass.DURATION and
# homeassistant.components.sensor.SensorDeviceClass.DURATION (verified identical, see
# KNX_DPT_DEVICE_CLASS docstring below) — extracted to avoid the S1192 duplicated-literal
# finding the raw string triggers at 8 occurrences.
_DC_DURATION = "duration"

# Device class (as its plain .value string, so this module doesn't have to import either
# entity-platform's enum) for the DPTs above whose KNX_DPT_ANALOG_RANGES unit exactly matches
# one of that device class's HA-allowed units — checked against both
# homeassistant.components.number.const.DEVICE_CLASS_UNITS and
# homeassistant.components.sensor.const.DEVICE_CLASS_UNITS (2026-09-20): every value below
# resolves to the identical allowed-unit set on both platforms, so this one table serves
# ComexioKnxNumber (NumberDeviceClass) and ComexioKnxSensor (SensorDeviceClass) alike.
# Deliberately excludes every "%"-unit DPT (5.1 Scaling, 5.4 Percent_U8, 6.1 Percent_V8,
# 8.10) — HUMIDITY is only correct for 9.7, and a shared "%" unit does not imply a shared
# meaning. Also excludes the composite control DPTs (3.7/3.8), the *Ah/*ARh energy variants
# (their unit casing/reactive vs. apparent split doesn't match any HA device class), and the
# 10ms/100ms DPT7/8 sub-types (not valid HA duration units). Missing from this table ==
# no device_class; the entity still gets its own icon regardless (ComexioKnxNumber always
# sets "mdi:knx" unconditionally — a device_class here changes unit-conversion/statistics
# behavior, not the icon).
KNX_DPT_DEVICE_CLASS: dict[tuple[int, int], str] = {
    (7, 2): _DC_DURATION,  # Value_2_Ucount, ms
    (7, 5): _DC_DURATION,  # s
    (7, 6): _DC_DURATION,  # min
    (7, 7): _DC_DURATION,  # h
    (8, 2): _DC_DURATION,  # ms
    (8, 5): _DC_DURATION,  # s
    (8, 6): _DC_DURATION,  # min
    (8, 7): _DC_DURATION,  # h
    (9, 1): "temperature",  # Value_Temp
    (9, 2): "temperature_delta",  # Value_Tempd
    (9, 4): "illuminance",  # Value_Lux
    (9, 5): "wind_speed",  # Value_Wsp
    (9, 6): "pressure",  # Value_Pres
    (9, 7): "humidity",  # Value_Humidity
    (9, 20): "voltage",  # Value_Volt
    (9, 21): "current",  # Value_Curr
    (9, 24): "power",  # Power
    (13, 10): "energy",  # Wh
    (13, 13): "energy",  # kWh
    (13, 100): _DC_DURATION,  # LongDeltaTimeSec, s
}

# DPT1.x (Binary) digital device_class mapping — consumed only by ComexioKnxBinarySensor
# (a plain HomeAssistant BinarySensorDeviceClass value string). A digital KNX K-element
# defaults to ComexioKnxSwitch (writable) unless titled "[RO]" (see the MarkerKind title-
# suffix heuristic in _process_source_items); SwitchDeviceClass has no matching values
# (only SWITCH/OUTLET), so this table only ever applies once an item is read-only and lands
# on the binary_sensor platform instead. Missing from this table == no device_class (plain
# on/off) — same convention as KNX_DPT_DEVICE_CLASS above; covers Schalter/Bool/Freigabe/
# Flanke/Binärwert (1.001-1.004/1.006), none of which carry HA-recognized semantics beyond
# generic on/off (found missing entirely in review 2026-09-20, user needs these to set
# entity types correctly — see project_knx_write_path_design memory).
# DPT1.019 ("Tür/Fenster") can't be told apart from the DPT alone — KNX itself uses one type
# for both door and window contacts — so DOOR is a best-effort default here, same limitation
# ComexioBinarySensor's plain-IO name heuristic already has (see binary_sensor.py).
KNX_DPT_DIGITAL_DEVICE_CLASS: dict[tuple[int, int], str] = {
    (1, 5): "problem",  # Alarm
    (1, 18): "occupancy",  # Anwesenheit
    (1, 19): "door",  # Tür/Fenster (best-effort, see comment above)
}

# The remaining DPT1.x subtypes (Schalter/Bool/Freigabe/Flanke/Binärwert) are physically
# ambivalent: KNX uses the identical DPT for a real toggle switch, a momentary push-button
# ("Taster" -> HA "[TRIG]"), and a pure status readback ("[RO]") alike — the DPT alone
# cannot decide which, only the installer knows the real wiring (user decision 2026-09-20,
# see project_knx_write_path_design memory). Unlike KNX_DPT_DIGITAL_DEVICE_CLASS's 3 entries
# (which api._auto_suffix_unambiguous_knx tags "[RO]" automatically, no user input needed),
# a digital item whose DPT is in this set instead raises a Repair issue
# (coordinator._audit_knx_dpt_ambiguous) letting the user classify it manually.
KNX_DPT_DIGITAL_AMBIGUOUS: set[tuple[int, int]] = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 6)}

# How many consecutive poll cycles coordinator._auto_suffix_unambiguous_knx retries a KNX
# object whose rename_knx_object() call failed, before giving up on it for the rest of this
# coordinator's runtime. Bounds a transient failure (e.g. a momentary HTTP error while Comexio
# is restarting) to a few retries instead of one permanent strike, while still capping a
# genuinely persistent failure (stale admin session, name collision) to a handful of attempts
# rather than hammering the API every ~15 min forever.
KNX_DPT_AUTOTAG_MAX_RETRIES = 3

# DPT3.x (Dimmer 3.007 / Blinds 3.008) composite objects: Comexio splits each into two
# K-elements sharing one KnxDeviceId — a digital control bit (direction) and an analog
# 3-bit step code (0=break, 1-7=move), see dev-tools/knx_seed_test_matrix.py's
# save_device()/points[0]/points[1]. api._attach_knx_dpt3_composites() tags both halves so
# cover.py/light.py can expose the pair as one composite entity instead of two disconnected
# generic switch/number entities (see project_knx_write_path_design memory, "Punkt 4,
# Hälfte (b)"). Sync/audit/wiring logic (button.py, coordinator.py) is untouched by this —
# both K-elements keep their own bridge Marker exactly as before.
KNX_DPT3_COMPOSITE_DOMAIN: dict[tuple[int, int], str] = {
    (3, 7): "light",
    (3, 8): "cover",
}

# KNX Association DPT3 control-bit encoding (public standard, not Comexio-specific).
KNX_DPT3_COVER_DIRECTION_UP = 0
KNX_DPT3_COVER_DIRECTION_DOWN = 1
KNX_DPT3_LIGHT_DIRECTION_DECREASE = 0
KNX_DPT3_LIGHT_DIRECTION_INCREASE = 1
KNX_DPT3_STEPCODE_BREAK = 0
KNX_DPT3_STEPCODE_MOVE = 1

# Best-effort Dimmer (DPT3.007) brightness tracking (see light.ComexioKnxLight): DPT3.007
# carries no absolute value at all, only relative increase/decrease telegrams — assumed
# time in seconds for one continuous move telegram to travel the full 0..255 brightness
# range, used to derive how long to hold the move telegram before sending the break
# telegram. Can only ever approximate the real actuator's own ramp time (user-accepted
# trade-off, 2026-09-20 — see project_knx_write_path_design memory). Tunable here rather
# than inline, see [[feedback_thresholds_in_const]].
KNX_DPT3_LIGHT_FULL_RANGE_SECONDS = 3.0

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


def stable_object_id(unique_id: str) -> str:
    """Object id (entity_id without domain) a Comexio IO/Marker/KNX entity requests for itself.

    Derived from the unique_id alone — the technical address, e.g. "iosrv1_iox1_ai7" or
    "iosrv1_m12" — never from the (schema-built, renamable) display name, so a renamed
    description in Comexio or a changed naming schema never drifts the entity_id. HA only
    applies it when the entity is first registered (see entity.ComexioStableEntityIdMixin).
    """
    return slugify(unique_id.removeprefix("comexio_"))


def entity_id_migration_target(
    entity_id: str, unique_id: str, suggested_object_id: str | None, server_id: str
) -> str | None:
    """entity_id a registered Comexio entity should be migrated to, or None if it is fine as is.

    Entities that request a stable entity_id (see entity.ComexioStableEntityIdMixin) have it
    stored as the registry's suggested_object_id — that is the target, so an entity_id built
    by HA from the old display name converges on the technical address. Everything else
    (diagnostic buttons/sensors) is only corrected for the legacy doubled server prefix
    "comexio_<server>_<server>_" from before v0.7.5.
    """
    domain, slug = entity_id.split(".", 1)
    if suggested_object_id and suggested_object_id == stable_object_id(unique_id):
        target = f"{domain}.{suggested_object_id}"
    else:
        server_slug = slugify(server_id)
        double_prefix = f"comexio_{server_slug}_{server_slug}_"
        if not slug.startswith(double_prefix):
            return None
        target = f"{domain}.comexio_{server_slug}_{slug[len(double_prefix) :]}"
    return None if target == entity_id else target


def migrate_entry_options(minor_version: int, data: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Options of a config entry migrated from ``minor_version`` to CONFIG_ENTRY_MINOR_VERSION."""
    new_options = dict(options)
    if minor_version < 2:
        new_options[CONF_KNX_PRERELEASE_CLEANUP_PENDING] = True
    if minor_version < 3:
        if CONF_SCHEMA_IO not in data and CONF_SCHEMA_IO not in options:
            # Never saved a schema, i.e. ran on the old default: keep its entity names as they are.
            new_options[CONF_SCHEMA_IO] = LEGACY_DEFAULT_SCHEMA_IO
        # "Ignore" was given for the old, much narrower doubled-prefix repair; the stable
        # entity_id migration is a different question and must be offered once.
        new_options.pop(CONF_ENTITY_ID_MIGRATION_IGNORED, None)
    return new_options


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
# direct grid placement in api.function_plan_add_source_pairs / _add_io_pairs.
FUNCTION_PLAN_LAYOUT_X_MARKER = 15.0  # left margin of the source (marker/IO) column
FUNCTION_PLAN_LAYOUT_X_WEBIO = 210.0  # marker + 195 gap
# KNX write bridge (Entwurf A "Merker-Brücke"): a K-object sits at X_WEBIO itself — matching
# wire_knx_bridge_pair's own fresh-plan placement (api.function_plan_add_knx_bridge_pairs),
# so a sort run reproduces exactly the row a freshly created triad already gets — and its own
# downstream Web-IO (the read-path pair every K-object also has) gets this third column, one
# more 195-unit pitch further out. Confirmed live 2026-09-16: without a dedicated column here,
# the generic marker->Web-IO sort pass treats the K-object as if IT were the Web-IO partner
# (it's the direct output of the Marker->K connection) and orphans the real Web-IO element
# into an unrelated parking row elsewhere on the plan.
FUNCTION_PLAN_LAYOUT_X_KNX_WEBIO = 405.0  # bridge K-object (at X_WEBIO) + 195 gap
# Phase 7 API-Loopback Web-IO (see WEBIO_CLASS_NAME_KNX_LOOPBACK above): used only as the
# off-canvas parking x (see api.function_plan_add_knx_bridge_loopback_pairs) before the
# mandatory follow-up sort pass places it — the sort pass (services/_grid.py) puts the
# Loopback Web-IO in the SAME column as the read-path Web-IO (X_KNX_WEBIO), one row below,
# not a further-right column of its own (changed 2026-09-19 per user feedback: wanted both
# stacked in one column, not spread across two).
FUNCTION_PLAN_LAYOUT_X_KNX_LOOPBACK = 600.0
FUNCTION_PLAN_LAYOUT_Y_START = 30.0  # first data row — leaves room for the managed-plan comment
FUNCTION_PLAN_LAYOUT_COMMENT_Y = 7.5  # the managed-plan comment sits above the first data row
FUNCTION_PLAN_LAYOUT_Y_STEP = 22.5
# Studio's own port-row pitch (function_plan_render_constants._ROW_H mirrors this same value
# for the renderer) — two elements this far apart sit directly adjacent with zero visual gap,
# unlike FUNCTION_PLAN_LAYOUT_Y_STEP above (1.5 rows: the extra half-row is deliberate
# breathing room between DIFFERENT marker/KNX pairs, not wanted WITHIN one pair's own hops —
# see services/_grid.py's _KNX_LOOPBACK_Y_OFFSET, user request 2026-09-20).
FUNCTION_PLAN_LAYOUT_ROW_HEIGHT = 15.0
# Pair-to-pair row pitch used ONLY for KNX cluster plans (see coordinator.is_knx_cluster_plan /
# services/plan_actions.py's row_step selection) — a KNX bridge pair reserves 2 row-slots
# (FUNCTION_PLAN_LAYOUT_ROW_HEIGHT each, one per WebIO hop), so using that same 15.0 value for
# the pair-to-pair pitch too made every row in the plan perfectly equidistant (15 units), with
# no visual distinction between the WITHIN-pair hop gap and the gap to the NEXT pair — user
# feedback 2026-09-20 ("alles press an press") after testing that live. 18.75 keeps the
# within-pair hop gap tight (still FUNCTION_PLAN_LAYOUT_ROW_HEIGHT, unchanged) while leaving a
# visibly larger gap after each pair (22.5 units: 2*18.75-15) than within one (15) — a
# user-chosen compromise between the 15.0 that felt cramped and the generic 22.5 that felt too
# spread out. See _cluster_plan_name's docstring for the resulting cluster-size capacity math.
FUNCTION_PLAN_KNX_LAYOUT_Y_STEP = 18.75
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
