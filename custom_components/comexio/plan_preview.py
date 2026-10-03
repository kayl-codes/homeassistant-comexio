"""Render the plan the 'Plan' selector shows into the preview — live, or the snapshot the 'Backup' selector names.

Called when the plan card opens (function_plan_preview_start) and, while a preview is armed,
whenever the 'Plan' or 'Backup' selection changes — so the poll only runs while a card shows it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .function_plan_backup import format_backup_label

if TYPE_CHECKING:
    from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)


def preview_selection_available(coordinator: ComexioCoordinator) -> bool:
    """Whether the 'Plan' selector points at one concrete plan or the orphaned-plans view."""
    return coordinator.get_active_function_plan_fub_id() is not None or coordinator.orphaned_plans_view_active()


def _backup_selector_state(coordinator: ComexioCoordinator) -> str | None:
    """The backup selector's current option, or None while it has none."""
    select_eid = er.async_get(coordinator.hass).async_get_entity_id(
        "select", DOMAIN, f"comexio_{coordinator.server_id}_plan_backup_selector"
    )
    state = coordinator.hass.states.get(select_eid) if select_eid else None
    if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state.state


def _active_backup_choice(coordinator: ComexioCoordinator, fub_id: int, plan_name: str) -> tuple[str, int] | None:
    """(kind, slot) if the backup selector points at a stored snapshot of this plan, else None (live)."""
    if (label := _backup_selector_state(coordinator)) is None:
        return None
    entries = coordinator.function_plan_backup.plan_backups_for_identity_sync(fub_id, plan_name)
    return next(((e["kind"], e["slot"]) for e in entries if format_backup_label(e) == label), None)


async def _async_render_orphaned(coordinator: ComexioCoordinator) -> None:
    """Render the backup of a deleted plan chosen in the backup selector's orphaned-plans view."""
    label = _backup_selector_state(coordinator)
    choice = coordinator.orphaned_backup_choice(label) if label is not None else None
    if choice is None:
        raise HomeAssistantError("No backup of a deleted plan is selected in the function plan 'Backup' selector.")
    url = await coordinator.async_generate_orphaned_plan_preview(
        choice["fub_id"], choice["plan_name"], choice["kind"], choice["slot"]
    )
    if url is None:
        raise HomeAssistantError(
            f"The backup {choice['kind']}[{choice['slot']}] of deleted plan '{choice['plan_name']}' "
            "could not be shown — it was deleted meanwhile or another preview took over."
        )


async def async_render_selected_preview(coordinator: ComexioCoordinator) -> None:
    """Render the selected plan (or chosen backup snapshot) and arm the live preview."""
    if coordinator.orphaned_plans_view_active():
        await _async_render_orphaned(coordinator)
        return
    api = coordinator.api
    fub_id = coordinator.get_active_function_plan_fub_id()
    if fub_id is None:
        _LOGGER.warning("[%s] Plan preview requested but no plan is selected", coordinator.server_id)
        return
    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))

    backup_choice = _active_backup_choice(coordinator, fub_id, plan_name)
    if backup_choice is not None:
        kind, slot = backup_choice
        snapshot = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
        if snapshot is None:
            _LOGGER.warning(
                "[%s] Plan preview: backup %s[%d] not found for '%s'", coordinator.server_id, kind, slot, plan_name
            )
            return
        await coordinator.async_generate_plan_preview(
            fub_id,
            plan_name,
            snapshot.get("elements", {}),
            snapshot.get("connections", {}),
            f"snapshot:{kind}:{slot}",
            snapshot.get("labels"),
        )
        return

    plan_data = await api.function_plan_load_elements(fub_id)
    if not plan_data:
        _LOGGER.warning("[%s] Plan preview: could not load plan %s", coordinator.server_id, fub_id)
        return
    await coordinator.async_generate_plan_preview(
        fub_id, plan_name, plan_data.get("elements", {}), plan_data.get("connections", {}), "live"
    )


async def async_follow_selection(coordinator: ComexioCoordinator) -> None:
    """A 'Plan' or 'Backup' selection changed: show it in an armed preview, else leave the preview idle.

    Without an open plan card nothing is rendered, so picking a plan on the device page starts no poll.
    """
    if not coordinator.preview_armed or not preview_selection_available(coordinator):
        return
    try:
        await async_render_selected_preview(coordinator)
    except HomeAssistantError as err:
        _LOGGER.warning("[%s] Plan preview not updated to the new selection: %s", coordinator.server_id, err)
