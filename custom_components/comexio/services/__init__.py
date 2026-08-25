# Version: 0.7.7
"""Comexio integration services package.

Registration entry point (async_setup_services) for all `comexio.*` Home Assistant
services. Split from the former monolithic services.py (Sourcery: "too large,
multi-purpose") into thematic submodules:

- `_context`  — shared instance/plan/login resolver helpers (leaf module).
- `_grid`     — sort/grid-placement math for function plan elements (leaf module).
- `_yaml_sync`— services.yaml dynamic-dropdown rewriting + HA schema cache refresh.
- `connect`   — function_plan_connect (marker↔WebIO wiring) handler.
- `plan_actions` — function_plan_visualize / function_plan_sort / function_plan_stop /
  function_plan_activate handlers (act on a single live/snapshot plan).
- `backup`    — function_plan_restore / function_plan_delete_backups /
  function_plan_purge_orphaned_backups / function_plan_list_backups handlers.
- `misc`      — generate_web_io, set_value, function_plan_debug_session,
  function_plan_preview_extend, function_plan_search — handlers that don't share enough
  with the above groups.

This module is imported as `from .services import ...` from outside the package (e.g.
`__init__.py`, `button.py`, `select.py`) exactly as it was when services.py was a single
file — a package import resolves identically to a module import in Python, so that public
surface (`async_setup_services`, `async_resync_io_group_headers`,
`async_sort_function_plan`, `format_plan_label`) is unaffected by this split.
"""

import functools
import logging
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse

from ..const import (
    DOMAIN,
    FUNCTION_PLAN_SERVICE_ACTIVATE as _SVC_ACTIVATE,
    FUNCTION_PLAN_SERVICE_ANALYZE as _SVC_ANALYZE,
    FUNCTION_PLAN_SERVICE_CONNECT as _SVC_CONNECT,
    FUNCTION_PLAN_SERVICE_FLOW_DIAGRAM as _SVC_FLOW_DIAGRAM,
    FUNCTION_PLAN_SERVICE_SORT as _SVC_SORT,
    FUNCTION_PLAN_SERVICE_STOP as _SVC_STOP,
    FUNCTION_PLAN_SERVICE_VISUALIZE as _SVC_VISUALIZE,
)
from ._context import format_plan_label
from ._grid import async_resync_io_group_headers
from ._yaml_sync import _refresh_service_descriptions, _update_services_yaml_plans
from .analyze import _handle_function_plan_analyze
from .backup import (
    _handle_function_plan_delete_backups,
    _handle_function_plan_list_backups,
    _handle_function_plan_purge_orphaned_backups,
    _handle_function_plan_restore,
)
from .connect import handle_function_plan_connect
from .flow_diagram import _handle_function_plan_flow_diagram
from .misc import (
    _handle_function_plan_debug_session,
    _handle_function_plan_preview_extend,
    _handle_function_plan_search,
    _handle_set_value,
    handle_generate_web_io,
)
from .plan_actions import (
    async_sort_function_plan,
    handle_function_plan_activate,
    handle_function_plan_sort,
    handle_function_plan_stop,
    handle_function_plan_visualize,
)

__all__ = [
    "async_resync_io_group_headers",
    "async_setup_services",
    "async_sort_function_plan",
    "format_plan_label",
]

_LOGGER = logging.getLogger(__name__)


# (service name, handler, supports_response) — data-driven so async_setup_services stays a
# flat loop instead of one "if not has_service: register(...)" block per service (that grew
# past the cognitive-complexity budget once the service count reached the mid-teens).
_SIMPLE_SERVICES: tuple[tuple[str, Any, SupportsResponse | None], ...] = (
    ("set_value", _handle_set_value, SupportsResponse.OPTIONAL),
    ("generate_web_io", handle_generate_web_io, None),
    (_SVC_CONNECT, handle_function_plan_connect, None),
    (_SVC_VISUALIZE, handle_function_plan_visualize, SupportsResponse.OPTIONAL),
    (_SVC_ANALYZE, _handle_function_plan_analyze, SupportsResponse.OPTIONAL),
    (_SVC_FLOW_DIAGRAM, _handle_function_plan_flow_diagram, SupportsResponse.OPTIONAL),
    (_SVC_SORT, handle_function_plan_sort, None),
    (_SVC_STOP, handle_function_plan_stop, None),
    (_SVC_ACTIVATE, handle_function_plan_activate, None),
    ("function_plan_restore", _handle_function_plan_restore, None),
    ("function_plan_delete_backups", _handle_function_plan_delete_backups, None),
    ("function_plan_purge_orphaned_backups", _handle_function_plan_purge_orphaned_backups, None),
    ("function_plan_list_backups", _handle_function_plan_list_backups, SupportsResponse.ONLY),
    ("function_plan_debug_session", _handle_function_plan_debug_session, None),
    ("function_plan_preview_extend", _handle_function_plan_preview_extend, SupportsResponse.OPTIONAL),
    ("function_plan_search", _handle_function_plan_search, SupportsResponse.OPTIONAL),
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register additional services for the Comexio integration."""
    for name, handler, supports_response in _SIMPLE_SERVICES:
        if not hass.services.has_service(DOMAIN, name):
            kwargs = {} if supports_response is None else {"supports_response": supports_response}
            hass.services.async_register(DOMAIN, name, functools.partial(handler, hass), **kwargs)

    await _update_services_yaml_plans(hass)

    # Exposed so coordinator.py can trigger the same refresh right after it writes a new auto-
    # or change-backup — those happen on the polling cycle and before plan-mutating actions,
    # neither of which routes through this module's own service handlers.
    hass.data[DOMAIN]["_refresh_service_descriptions"] = functools.partial(_refresh_service_descriptions, hass)
    await _refresh_service_descriptions(hass)
