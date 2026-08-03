from enum import StrEnum
import re

# Version: 0.8.0
DOMAIN = "comexio"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # nosec B105
CONF_SERVER_ID = "server_id"

CONF_SCHEMA_MARKER = "schema_marker"
CONF_SCHEMA_IO = "schema_io"
DEFAULT_SCHEMA_MARKER = "M{MarkerId} {MarkerTitle}"
DEFAULT_SCHEMA_IO = "{ExtName} {IoId} {IoTitle}"

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


# Web-IO class split: HA maintains two separate Web-IO device classes on the Comexio
# server — one for Markers, one for physical IOs — derived from the single webio_name
# field in the config/options flow by appending a suffix. The webIoId (the per-command
# key inside $FubModules["10"]) is a global counter across ALL Web-IO devices on a
# Comexio server, never reused per-device (verified live 2026-07-27), so commands from
# both classes can share one flat webio_commands lookup without ambiguity.
class WebioClass(StrEnum):
    """The two Web-IO device classes HA manages on the Comexio server.

    A StrEnum so every existing string-based usage (dict keys, equality checks,
    f-string interpolation, JSON payloads sent to/scraped back from Comexio)
    keeps working unchanged, while call sites gain typo-safety via the members.
    """

    MARKER = "marker"
    IO = "io"


WEBIO_CLASS_MARKER = WebioClass.MARKER
WEBIO_CLASS_IO = WebioClass.IO
WEBIO_CLASSES = tuple(WebioClass)
_WEBIO_CLASS_SUFFIXES = {WebioClass.MARKER: " [M]", WebioClass.IO: " [IO]"}
_WEBIO_CLASS_LABELS = {WebioClass.MARKER: "Marker", WebioClass.IO: "IO"}


def webio_class_name(webio_name: str, webio_class: str) -> str:
    """Comexio Web-IO class name for one of the two HA-managed classes (marker/io)."""
    if webio_class not in _WEBIO_CLASS_SUFFIXES:
        raise ValueError(f"Unknown Web-IO class {webio_class!r}, expected one of {WEBIO_CLASSES}")
    return f"{webio_name}{_WEBIO_CLASS_SUFFIXES[webio_class]}"


def webio_class_label(webio_class: str) -> str:
    """Short display label ('Marker'/'IO') for a Web-IO class, used in sync/audit messages."""
    if webio_class not in _WEBIO_CLASS_LABELS:
        raise ValueError(f"Unknown Web-IO class {webio_class!r}, expected one of {WEBIO_CLASSES}")
    return _WEBIO_CLASS_LABELS[webio_class]


# Internal audit-map key convention (coordinator._async_update_data): IO entries are keyed
# as "IO_{ext_name}_{identifier}", markers as bare "M{id}" — centralized here so the build
# and classify sides can't drift apart.
_IO_AUDIT_KEY_PREFIX = "IO_"


def io_audit_key(ext_name: str, identifier: str) -> str:
    """Build the internal audit-map key identifying an IO (as opposed to a Marker)."""
    return f"{_IO_AUDIT_KEY_PREFIX}{ext_name}_{identifier}"


def is_io_audit_key(key: str) -> bool:
    """Whether an internal audit-map key identifies an IO (vs. a Marker)."""
    return key.startswith(_IO_AUDIT_KEY_PREFIX)


# Sync-progress percentage span shared by all Web-IO classes, subdivided evenly per class
# in button.py's `_class_pct_ranges` so the UI progress bar advances smoothly regardless
# of how many classes exist (currently 2, but not hard-coded to that number).
SYNC_PROGRESS_START_PCT = 5
SYNC_PROGRESS_END_PCT = 95


DEFAULT_NAME = "Comexio"

SCAN_INTERVAL_DEFAULT = 15
SCAN_INTERVAL_OPTIONS = ["1", "5", "10", "15", "30", "45", "60", "120", "300", "600", "1440"]

# Operation durations for progress calculation (seconds)
SYNC_DURATION_DELETE = 4
SYNC_DURATION_WRITE = 35
SYNC_DURATION_RECREATE = 79

MARKER_TYPE_INTERVAL = 3
MARKER_INTERVAL_MAX_VALUE = 86400

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


# Bus workload monitoring: independent fast poll of the Comexio internal bus/CPU load,
# separate from the main config-audit coordinator (which runs every few minutes). Only
# the raw reading is exposed here — sustained-overload detection (e.g. "80% for 60s") is
# left to a native HA automation (numeric_state trigger with a `for:` duration), which
# already covers debounce/hysteresis correctly instead of reimplementing it in Python.
BUS_LOAD_POLL_INTERVAL_SEC = 10

# Consecutive failed ticks before the diagnostics fall back to "unknown" instead of
# silently keeping the last successful reading forever.
BUS_LOAD_FAIL_STREAK_THRESHOLD = 3


def bus_load_signal(server_id: str) -> str:
    """Dispatcher signal fired when a fresh Comexio bus workload reading arrives."""
    return f"{DOMAIN}_{server_id}_bus_load_update"


# Function Plan backup/restore: rotating snapshot slots per plan (see function_plan_backup.py).
FUNCTION_PLAN_AUTO_BACKUP_SLOTS = 3
FUNCTION_PLAN_CHANGE_BACKUP_SLOTS = 10
CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS = "function_plan_backup_retention_months"
DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS = 6
TIMESTAMP_DISPLAY_FORMAT = "%d.%m.%Y %H:%M"

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
