from enum import StrEnum

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
