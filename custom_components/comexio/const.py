# Version: 0.6.0
DOMAIN = "comexio"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SERVER_ID = "server_id"

# keys for API access
CONF_API_USERNAME = "api_username"
CONF_API_PASSWORD = "api_password"

# webHook
#WEBHOOK_ID = "comexio_webhook"

DEFAULT_NAME = "Comexio"

SCAN_INTERVAL_MIN = 1
SCAN_INTERVAL_MAX = 1440
SCAN_INTERVAL_DEFAULT = 15

# Operation durations for progress calculation (seconds)
SYNC_DURATION_DELETE = 4
SYNC_DURATION_WRITE = 35
SYNC_DURATION_RECREATE = 79

DEFAULT_HOST = "192.168.1.100"

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
    "internal"
]
