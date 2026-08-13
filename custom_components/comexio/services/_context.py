# Version: 0.7.6
"""Shared service-context/resolver helpers used across the services package.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
this module holds the plan/instance/login resolution helpers every handler module
(connect, plan_actions, backup, misc) depends on, so it deliberately has no dependency
on any of its sibling modules (leaf of the package's import graph).
"""

import logging
import re
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall

from ..const import (
    DOMAIN,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH as _LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_X_MARKER as _LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_Y_START as _LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP as _LAYOUT_Y_STEP,
)
from ..coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)

_MULTI_INSTANCE_MSG = "Multiple Comexio instances — please specify `config_entry`."
_LOGIN_FAILED_MSG = "Comexio admin login failed."
_INSTANCE_NOT_FOUND_LOG = "Comexio instance %s not found in hass.data"

_PLAN_LABEL_ID_RE = re.compile(r"\(ID (\d+)\)\s*$")


def format_plan_label(name: str, fub_id) -> str:
    """Format a plan's select-option label as "<name> (ID <fub_id>)", parseable by _resolve_fub_id()."""
    return f"{name} (ID {fub_id})"


def _resolve_fub_id(fub_id_input: str, fub_data: dict, hass=None) -> int | None:
    """Resolve fub_id from a numeric string, plan name, "<name> (ID <n>)" select label, or select entity_id."""
    stripped = str(fub_id_input).strip()
    # If it looks like an entity_id, read the select entity's current state
    if hass and "." in stripped:
        state = hass.states.get(stripped)
        if state and state.state not in ("unknown", "unavailable", ""):
            stripped = state.state
    # Select options carry "(ID <n>)" — parse it directly so duplicate plan names
    # (Comexio only guarantees fub_id is unique) can't resolve to the wrong plan.
    if match := _PLAN_LABEL_ID_RE.search(stripped):
        return int(match.group(1))
    try:
        return int(stripped)
    except ValueError:
        name_lower = stripped.lower()
        for fid, fub in fub_data.items():
            if fub.get("Name", "").lower() == name_lower:
                return int(fid)
    return None


def _available_plans_str(fub_data: dict) -> str:
    """Return human-readable list of available plans from fub_data."""
    if not fub_data:
        return "no plans loaded — reload the integration"
    return ", ".join(
        f"'{fub.get('Name', '?')}' (ID {fid})" for fid, fub in sorted(fub_data.items(), key=lambda x: int(x[0]))
    )


async def _async_get_service_context(
    hass: HomeAssistant,
    call: ServiceCall,
    error_title: str,
    *,
    resolve_plan: bool = True,
    do_login: bool = True,
) -> tuple[ComexioCoordinator, Any, int | None] | None:
    """Resolve coordinator, api and (optionally) target fub_id for a function plan backup
    service call. Posts an English error notification and returns None on any failure.

    Sibling of _resolve_function_plan (kept separate rather than extended in place) since the
    backup services need to resolve WITHOUT requiring a live plan/admin login — a deleted
    plan's stored backups are still listable/restorable, and pure local-storage operations
    (delete/purge) never need a Comexio session at all.
    """
    domain_data = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("config_entry")
    if not entry_id:
        entries = [k for k, v in domain_data.items() if isinstance(v, ComexioCoordinator)]
        if len(entries) != 1:
            persistent_notification.async_create(hass, _MULTI_INSTANCE_MSG, title=error_title)
            return None
        entry_id = entries[0]
    coordinator = domain_data.get(entry_id)
    if not isinstance(coordinator, ComexioCoordinator):
        _LOGGER.error(_INSTANCE_NOT_FOUND_LOG, entry_id)
        return None
    api = coordinator.api

    fub_id: int | None = None
    if resolve_plan:
        explicit_fub_id = call.data.get("fub_id")
        fub_id_raw = explicit_fub_id or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api.fub_data, hass)
        if fub_id is None:
            reason = (
                f"Plan '{fub_id_raw}' not found."
                if explicit_fub_id
                else "No plan selected — the 'Function Plans' selector is empty. "
                "Please specify the plan (fub_id) explicitly."
            )
            persistent_notification.async_create(
                hass,
                f"{reason}\nAvailable: {_available_plans_str(api.fub_data)}",
                title=error_title,
            )
            return None

    if do_login and not await api.login():
        persistent_notification.async_create(hass, _LOGIN_FAILED_MSG, title=error_title)
        return None

    return coordinator, api, fub_id


async def _resolve_backup_identity(
    coordinator: ComexioCoordinator, fub_id: int, plan_name_hint: str | None
) -> tuple[str | None, str | None]:
    """Resolve the (fub_id, plan_name) identity for a backup action. Returns (plan_name, error).

    fub_id alone is not a stable identity — Comexio reuses IDs after deletion, so two
    different plans can share one fub_id in the backup store. plan_name_hint short-circuits
    the lookup when already known precisely (composite dropdown value, or a currently-live
    plan's own name). Otherwise looks up stored identities for that fub_id: exactly one
    resolves automatically; none is an error; two or more (a reused ID with more than one
    identity still on record) is ambiguous and asks the caller to reselect from the dropdown.
    """
    if plan_name_hint is not None:
        return plan_name_hint, None
    identities = [
        (name, count)
        for fid, name, count in await coordinator.function_plan_backup.async_backed_up_plans()
        if fid == fub_id
    ]
    if len(identities) == 1:
        return identities[0][0], None
    if not identities:
        return None, f"No stored backups for plan {fub_id}."
    names = ", ".join(f"'{name}'" for name, _ in identities)
    return (
        None,
        (
            f"fub_id {fub_id} is ambiguous — {len(identities)} different plan identities share this reused ID in "  # nosec B608
            f"the backup store ({names}). Please reselect from the Plan/Snapshot dropdown."
        ),
    )


def _parse_snapshot_field(raw: str) -> tuple[int, str, int, str | None] | None:
    """Parse the combined snapshot picker field value, or None if malformed.

    Accepts the current 'fub_id:kind:slot:plan_name' value as well as the legacy 3-part
    'fub_id:kind:slot' (plan_name unknown — caller must disambiguate via
    _resolve_backup_identity).
    """
    parts = raw.split(":", 3)
    if len(parts) not in (3, 4):
        return None
    fub_id_str, kind, slot_str = parts[0], parts[1], parts[2]
    plan_name = parts[3] if len(parts) == 4 else None
    try:
        return int(fub_id_str), kind, int(slot_str), plan_name
    except ValueError:
        return None


def _resolve_function_plan(hass: HomeAssistant, call: ServiceCall, error_title: str) -> tuple[Any, Any, int] | None:
    """Resolve (coordinator, api, fub_id) for a function plan service call — without logging in.

    Handles config_entry auto-resolution (when only one Comexio instance exists) and fub_id
    resolution via `_resolve_fub_id`, notifying the user and returning None on any failure.
    """
    domain_data = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("config_entry")
    if not entry_id:
        entries = [k for k, v in domain_data.items() if isinstance(v, ComexioCoordinator)]
        if len(entries) != 1:
            _LOGGER.error("config_entry required when multiple Comexio instances exist: %s", entries)
            persistent_notification.async_create(
                hass, "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.", title=error_title
            )
            return None
        entry_id = entries[0]
    coordinator = domain_data.get(entry_id)
    if not isinstance(coordinator, ComexioCoordinator):
        _LOGGER.error(_INSTANCE_NOT_FOUND_LOG, entry_id)
        return None

    api = coordinator.api
    fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
    fub_id = _resolve_fub_id(str(fub_id_raw), api.fub_data, hass)
    if fub_id is None:
        persistent_notification.async_create(
            hass,
            f"Plan '{fub_id_raw}' not found.\nAvailable: {_available_plans_str(api.fub_data)}",
            title=error_title,
        )
        return None

    return coordinator, api, fub_id


async def _ensure_comexio_login(hass: HomeAssistant, api, error_title: str) -> bool:
    """Log in to the Comexio admin session, notifying the user on failure."""
    if await api.login():
        return True
    persistent_notification.async_create(hass, "Comexio admin login failed.", title=error_title)
    return False


async def _resolve_function_plan_context(
    hass: HomeAssistant, call: ServiceCall, error_title: str
) -> tuple[Any, Any, int] | None:
    """Resolve (coordinator, api, fub_id) for a function plan service call and ensure the admin session is logged in."""
    ctx = _resolve_function_plan(hass, call, error_title)
    if ctx is None:
        return None
    _coordinator, api, _fub_id = ctx
    if not await _ensure_comexio_login(hass, api, error_title):
        return None
    return ctx


def _plan_activation_note(was_active: bool, activated: bool, has_changes: bool, fub_id: int) -> str:
    """Build the status note describing what happened to plan activation after a sync/sort action."""
    if not was_active:
        if has_changes:
            return "Plan was inactive — changes saved, plan remains inactive."
        return f"Plan fub_id={fub_id} — no new connections, plan unchanged."
    if activated:
        return "Plan saved and activated." if has_changes else "Plan unchanged, still active."
    return "Plan activation failed — please save manually in the Comexio UI."


def _get_canvas_grid_dims(api, fub_id: int, canvas_format_raw: str) -> tuple[str, float, float, int, int]:
    """Resolve canvas paper format + pixel bounds, then derive the (rows_per_col, max_cols) layout grid."""
    if canvas_format_raw and canvas_format_raw != "AUTO":
        canvas_label = canvas_format_raw
        x_max, y_max = api.get_fub_canvas_bounds(fub_id, paper_name=canvas_format_raw)
    else:
        canvas_label = api.get_fub_paper_format(fub_id).upper()
        x_max, y_max = api.get_fub_canvas_bounds(fub_id)

    rows_per_col = max(1, int((y_max - _LAYOUT_Y_START) / _LAYOUT_Y_STEP))
    max_cols = max(1, round((x_max - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH))
    return canvas_label, x_max, y_max, rows_per_col, max_cols
