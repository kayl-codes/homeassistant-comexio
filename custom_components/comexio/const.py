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


def bus_load_signal(server_id: str) -> str:
    """Dispatcher signal fired when a fresh Comexio bus workload reading arrives."""
    return f"{DOMAIN}_{server_id}_bus_load_update"


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
