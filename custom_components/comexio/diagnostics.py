"""Diagnostics download (#196): what Comexio delivers, and which entity HA made of each IO.

"comexio" holds the raw data of the last readable poll — every IO row of $FubModules["1"] (also
those HA does not import), the extensions, when they were read and why the last check failed —
plus the IO type tables, so an unknown IO type can be supported from it. "home_assistant" holds
HA's reading of each imported IO.

Built from an allow-list, not by redaction: no credentials, host, certificate, webhook, serial
numbers, locations, IO descriptions or values — the file is meant to be attached to a public
GitHub issue. Extension names stay: they are part of every IO's id and join IOs to extensions.
The type tables go in whole: aiocomexio already reduces each row to its fixed fields (binary,
min, max, unit / input), Comexio's type catalogue without any text of the installation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiocomexio import __version__ as AIOCOMEXIO_VERSION
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import CONF_INCLUDE_OFFLINE_EXTENSIONS, DOMAIN
from .coordinator import ComexioCoordinator
from .reference_monitor import async_integration_version
from .unsupported_ios import io_platform, unsupported_io_reason

# Options that decide which entities exist; everything else (names, schemas, plans) stays out.
DIAGNOSTICS_OPTION_KEYS = (
    "import_markers",
    "import_ios",
    "import_quickbus",
    "import_knx",
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    "scan_interval",
)
DIAGNOSTICS_IO_KEYS = (
    "ext_name",
    "identifier",
    "type_id_raw",
    "unit",
    "min",
    "max",
    "is_binary",
    "is_input",
    "offline",
    "inactive",
)


def _io_diagnostics(io: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **{key: io.get(key) for key in DIAGNOSTICS_IO_KEYS},
        "platform": io_platform(io),
        "skipped": unsupported_io_reason(io),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Diagnostics of one Comexio entry; without a loaded coordinator only versions and options."""
    conf = {**entry.data, **entry.options}
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(coordinator, ComexioCoordinator):
        coordinator = None
    diagnostics: dict[str, Any] = {
        "versions": {
            "integration": await async_integration_version(hass),
            "aiocomexio": AIOCOMEXIO_VERSION,
            "home_assistant": HA_VERSION,
            "comexio": coordinator.api.comexio_version if coordinator else None,
        },
        "options": {key: conf[key] for key in DIAGNOSTICS_OPTION_KEYS if key in conf},
        "loaded": bool(coordinator and coordinator.data),
    }
    if coordinator is None or not coordinator.data:
        return diagnostics
    return {
        **diagnostics,
        "comexio": {
            "read_at": coordinator.unsupported_ios.raw_read_at,
            "check_error": coordinator.unsupported_ios.last_error_type,
            "extensions": coordinator.unsupported_ios.extensions,
            "ios": coordinator.unsupported_ios.raw_ios,
            "io_types": coordinator.api.io_types,
            "io_input_types": coordinator.api.io_input_types,
        },
        "home_assistant": {"ios": [_io_diagnostics(io) for io in coordinator.data.get("io", [])]},
    }
