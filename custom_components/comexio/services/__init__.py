# Version: 0.7.6
"""Comexio integration services package.

Registration entry point (async_setup_services) for all `comexio.*` Home Assistant
services. Split from the former monolithic services.py (Sourcery: "too large,
multi-purpose") into thematic submodules:

- `_context`  — shared instance/plan/login resolver helpers (leaf module).
- `_grid`     — sort/grid-placement math for function plan elements (leaf module).
- `_yaml_sync`— services.yaml dynamic-dropdown rewriting + HA schema cache refresh.
- `connect`   — logikplan_connect_poc (marker↔WebIO wiring) handler.
- `plan_actions` — logikplan_visualize / logikplan_sort / logikplan_stop /
  logikplan_activate handlers (act on a single live/snapshot plan).
- `backup`    — function_plan_restore / function_plan_delete_backups /
  function_plan_purge_orphaned_backups / function_plan_list_backups handlers.
- `misc`      — generate_web_io, set_value, function_plan_debug_session,
  function_plan_search — handlers that don't share enough with the above groups.

This module is imported as `from .services import ...` from outside the package (e.g.
`__init__.py`, `button.py`, `select.py`) exactly as it was when services.py was a single
file — a package import resolves identically to a module import in Python, so that public
surface (`async_setup_services`, `async_resync_io_group_headers`,
`async_sort_function_plan`, `format_plan_label`) is unaffected by this split.
"""

import functools
import logging

from homeassistant.core import HomeAssistant, SupportsResponse

from ..const import DOMAIN
from ._context import format_plan_label
from ._grid import async_resync_io_group_headers
from ._yaml_sync import _refresh_service_descriptions, _update_services_yaml_plans
from .backup import (
    _handle_function_plan_delete_backups,
    _handle_function_plan_list_backups,
    _handle_function_plan_purge_orphaned_backups,
    _handle_function_plan_restore,
)
from .connect import handle_logikplan_connect_poc
from .misc import (
    _handle_function_plan_debug_session,
    _handle_function_plan_search,
    _handle_set_value,
    handle_generate_web_io,
)
from .plan_actions import (
    async_sort_function_plan,
    handle_logikplan_activate,
    handle_logikplan_sort,
    handle_logikplan_stop,
    handle_logikplan_visualize,
)

__all__ = [
    "async_resync_io_group_headers",
    "async_setup_services",
    "async_sort_function_plan",
    "format_plan_label",
]

_LOGGER = logging.getLogger(__name__)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register additional services for the Comexio integration."""
    if not hass.services.has_service(DOMAIN, "set_value"):
        hass.services.async_register(
            DOMAIN,
            "set_value",
            functools.partial(_handle_set_value, hass),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, "generate_web_io"):
        hass.services.async_register(DOMAIN, "generate_web_io", functools.partial(handle_generate_web_io, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_connect_poc"):
        hass.services.async_register(
            DOMAIN, "logikplan_connect_poc", functools.partial(handle_logikplan_connect_poc, hass)
        )
    if not hass.services.has_service(DOMAIN, "logikplan_visualize"):
        hass.services.async_register(
            DOMAIN,
            "logikplan_visualize",
            functools.partial(handle_logikplan_visualize, hass),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, "logikplan_sort"):
        hass.services.async_register(DOMAIN, "logikplan_sort", functools.partial(handle_logikplan_sort, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_stop"):
        hass.services.async_register(DOMAIN, "logikplan_stop", functools.partial(handle_logikplan_stop, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_activate"):
        hass.services.async_register(DOMAIN, "logikplan_activate", functools.partial(handle_logikplan_activate, hass))
    if not hass.services.has_service(DOMAIN, "function_plan_restore"):
        hass.services.async_register(
            DOMAIN, "function_plan_restore", functools.partial(_handle_function_plan_restore, hass)
        )
    if not hass.services.has_service(DOMAIN, "function_plan_delete_backups"):
        hass.services.async_register(
            DOMAIN, "function_plan_delete_backups", functools.partial(_handle_function_plan_delete_backups, hass)
        )
    if not hass.services.has_service(DOMAIN, "function_plan_purge_orphaned_backups"):
        hass.services.async_register(
            DOMAIN,
            "function_plan_purge_orphaned_backups",
            functools.partial(_handle_function_plan_purge_orphaned_backups, hass),
        )
    if not hass.services.has_service(DOMAIN, "function_plan_list_backups"):
        hass.services.async_register(
            DOMAIN,
            "function_plan_list_backups",
            functools.partial(_handle_function_plan_list_backups, hass),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, "function_plan_debug_session"):
        hass.services.async_register(
            DOMAIN, "function_plan_debug_session", functools.partial(_handle_function_plan_debug_session, hass)
        )
    if not hass.services.has_service(DOMAIN, "function_plan_search"):
        hass.services.async_register(
            DOMAIN,
            "function_plan_search",
            functools.partial(_handle_function_plan_search, hass),
            supports_response=SupportsResponse.OPTIONAL,
        )

    await _update_services_yaml_plans(hass)

    # Exposed so coordinator.py can trigger the same refresh right after it writes a new auto-
    # or change-backup — those happen on the polling cycle and before plan-mutating actions,
    # neither of which routes through this module's own service handlers.
    hass.data[DOMAIN]["_refresh_service_descriptions"] = functools.partial(_refresh_service_descriptions, hass)
    await _refresh_service_descriptions(hass)
