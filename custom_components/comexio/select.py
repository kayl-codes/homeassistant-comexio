# Version: 0.1.0
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FUNCTION_PLAN_FUB_ID, DOMAIN
from .coordinator import ComexioCoordinator
from .function_plan_backup import format_backup_label
from .services import format_plan_label

_LOGGER = logging.getLogger(__name__)

# "<name> (ID <n>)" suffix written by format_plan_label — the ID is the only unambiguous
# handle, since plan names aren't unique in Comexio.
_PLAN_LABEL_ID_RE = re.compile(r"\(ID (\d+)\)\s*$")

# Prefixed onto an inactive plan's name (not the "(ID n)" suffix _PLAN_LABEL_ID_RE anchors
# on) — a native HA select entity offers no per-option styling, so a text marker is the only
# way to flag "not running" directly in the dropdown.
_INACTIVE_PLAN_PREFIX = "⏸ "


def _plan_option_label(fid, fub: dict) -> str:
    """Select-option label for one fub_data entry, prefixed when the plan isn't active."""
    name = fub.get("Name") or f"Plan {fid}"
    prefix = "" if fub.get("Active", True) else _INACTIVE_PLAN_PREFIX
    return format_plan_label(f"{prefix}{name}", fid)


# Explicit choice in the backup selector for "show the live plan, not a stored snapshot".
# Cannot collide with format_backup_label()'s "<kind>[<slot>] — <timestamp>" shape.
LIVE_BACKUP_OPTION = "Live"


def _fub_id_from_label(option: str) -> int | None:
    """Parse the fub_id back out of a plan select-option label."""
    match = _PLAN_LABEL_ID_RE.search(option)
    return int(match.group(1)) if match else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanSelectEntity(coordinator), ComexioPlanBackupSelectEntity(coordinator)])


class ComexioPlanSelectEntity(CoordinatorEntity, SelectEntity):
    """Select entity listing available function plans for use in service calls."""

    _attr_has_entity_name = True
    _attr_name = "Function Plans"
    _attr_icon = "mdi:file-tree-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ComexioCoordinator) -> None:
        super().__init__(coordinator)
        # unique_id keeps the legacy "logikplan" spelling — it is persisted in the entity
        # registry and referenced by coordinator.get_active_function_plan_fub_id().
        self._attr_unique_id = f"comexio_{coordinator.server_id}_logikplan_plan_selector"
        # None = no explicit choice yet in this HA run; current_option then falls back to the
        # choice persisted in entry.options (see current_option).
        self._selected: str | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def options(self) -> list[str]:
        # Plan names aren't unique in Comexio (only fub_id is) — the ID suffix keeps
        # options unambiguous and lets _resolve_fub_id() parse it back out directly.
        fub_data = self.coordinator.api.fub_data
        return sorted((_plan_option_label(fid, fub) for fid, fub in fub_data.items()), key=str.lower)

    @property
    def current_option(self) -> str | None:
        # Restore the last choice from entry.options rather than keeping it purely in memory:
        # the coordinator's first refresh runs before this platform is set up, so an in-memory
        # selection would leave every startup audit blind to which plan is actively managed.
        # Resolved lazily (not once in __init__) because fub_data can still be empty at setup
        # time after a failed plan fetch — the coordinator would then act on the persisted plan
        # while this selector showed nothing.
        # No implicit fallback to the first plan: with the choice persisted, "nothing selected"
        # is a real, restorable state, and silently acting on an arbitrary plan is worse.
        options = self.options
        if self._selected in options:
            return self._selected
        label = self._label_for_persisted_fub_id()
        return label if label in options else None

    async def async_select_option(self, option: str) -> None:
        self._selected = option
        self.async_write_ha_state()
        fub_id = _fub_id_from_label(option)
        if fub_id is None:
            _LOGGER.warning("Could not resolve a fub_id from plan option %r — selection not persisted", option)
            return
        new_options = dict(self.coordinator.config_entry.options)
        new_options[CONF_FUNCTION_PLAN_FUB_ID] = fub_id
        self.coordinator.request_options_update_without_reload(new_options)

    def _label_for_persisted_fub_id(self) -> str | None:
        """Select-option label matching the fub_id persisted in entry.options, or None."""
        fub_id = self.coordinator.persisted_function_plan_fub_id()
        if fub_id is None:
            return None
        fub = self.coordinator.api.fub_data.get(str(fub_id))
        if fub is None:
            return None
        return _plan_option_label(fub_id, fub)


class ComexioPlanBackupSelectEntity(CoordinatorEntity, SelectEntity):
    """Select entity listing stored backup snapshots for the plan chosen in the 'Function Plans' selector.

    Lets the Plan Preview button (button.py) render a historical snapshot instead of the
    live plan, so a backup can be visually sighted before deciding whether to restore it.
    Purely an in-memory preview/targeting control — no entry.options persistence, since it is
    an ephemeral viewing choice, not a lasting configuration value. Without an explicit user
    choice the selector defaults to LIVE_BACKUP_OPTION; a 'Function Plans' plan change discards
    the explicit choice and falls back to that same default, so a stale backup choice can never
    be silently applied to a newly-selected, unrelated plan.
    Picking a stored snapshot freezes the preview's wiring/elements at that snapshot while its
    per-connection values keep following the live plan (see button.py's _active_backup_choice
    and coordinator.prime_snapshot_preview_cache) — it does NOT silently drift back to the live
    plan's wiring on the next webhook push. LIVE_BACKUP_OPTION is the only way back to the fully
    live view.
    """

    _attr_has_entity_name = True
    _attr_name = "Function Plan Backup"
    _attr_icon = "mdi:backup-restore"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ComexioCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{coordinator.server_id}_plan_backup_selector"
        # None = no explicit user choice yet -> current_option falls back to the default
        # (newest auto backup, if present). Computed lazily because the backup cache is
        # empty until the manager's first async_load after startup.
        self._selected: str | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_added_to_hass(self) -> None:
        """Track the 'Function Plans' selector: a plan change resets this selector to its default.

        Also eagerly loads the backup manager's caches so options() is populated from
        persisted storage right after a restart, instead of staying empty until some
        other entity (e.g. the backup sensor) or the next backup cycle loads them.
        """
        await super().async_added_to_hass()
        await self.coordinator.function_plan_backup.async_load()
        if select_eid := er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"comexio_{self.coordinator.server_id}_logikplan_plan_selector"
        ):
            self.async_on_remove(async_track_state_change_event(self.hass, [select_eid], self._handle_plan_change))

    @callback
    def _handle_plan_change(self, event: Event[EventStateChangedData]) -> None:
        self._selected = None
        self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        fub_id = self.coordinator.get_active_function_plan_fub_id()
        fub_data = self.coordinator.api.fub_data
        plan_name = fub_data.get(str(fub_id), {}).get("Name") if fub_id is not None else None
        if plan_name is None:
            return []
        entries = self.coordinator.function_plan_backup.plan_backups_for_identity_sync(fub_id, plan_name)
        return [LIVE_BACKUP_OPTION, *(format_backup_label(e) for e in entries)]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Read by comexio-plan-card.js to grey out its Restore trigger the moment ANY
        # restore starts — a card only disables the button in the instance that was
        # clicked, but this select is shared by every card instance on the dashboard, so
        # it's the one place that can reach all of them at once.
        return {"restore_in_progress": self.coordinator._restore_lock.locked()}

    @property
    def current_option(self) -> str | None:
        options = self.options
        if self._selected in options:
            return self._selected
        # Default: the live plan, not a stored snapshot — a snapshot is only ever shown after
        # an explicit user choice.
        return LIVE_BACKUP_OPTION if LIVE_BACKUP_OPTION in options else None

    async def async_select_option(self, option: str) -> None:
        self._selected = option
        self.async_write_ha_state()
