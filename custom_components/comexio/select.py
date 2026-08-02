# Version: 0.1.0
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator
from .function_plan_backup import format_backup_label
from .services import format_plan_label


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanSelectEntity(coordinator), ComexioPlanBackupSelectEntity(coordinator)])


class ComexioPlanSelectEntity(CoordinatorEntity, SelectEntity):
    """Select entity listing available Logikplan plans for use in service calls."""

    _attr_has_entity_name = True
    _attr_name = "Logikplan Plan"
    _attr_icon = "mdi:file-tree-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ComexioCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"comexio_{coordinator.server_id}_logikplan_plan_selector"
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
        return sorted(
            (format_plan_label(fub.get("Name") or f"Plan {fid}", fid) for fid, fub in fub_data.items()),
            key=str.lower,
        )

    @property
    def current_option(self) -> str | None:
        opts = self.options
        if self._selected in opts:
            return self._selected
        return opts[0] if opts else None

    async def async_select_option(self, option: str) -> None:
        self._selected = option
        self.async_write_ha_state()


class ComexioPlanBackupSelectEntity(CoordinatorEntity, SelectEntity):
    """Select entity listing stored backup snapshots for the plan chosen in the 'Logikplan Plan' selector.

    Purely an in-memory preview/targeting control — no entry.options persistence, since it is
    an ephemeral viewing choice, not a lasting configuration value. Without an explicit user
    choice the selector defaults to the newest auto backup (auto[0]) of the active plan;
    a 'Logikplan Plan' plan change discards the explicit choice and falls back to that default,
    so a stale backup choice can never be silently applied to a newly-selected, unrelated plan.
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
        """Track the 'Logikplan Plan' selector: a plan change resets this selector to its default.

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
        return [format_backup_label(e) for e in entries]

    @property
    def current_option(self) -> str | None:
        options = self.options
        if self._selected in options:
            return self._selected
        # Default: newest auto backup of the active plan (slot 0 = newest per kind, see
        # plan_backups_for_identity_sync). Matched via label prefix — the visible label is
        # the lookup key. Without a match the state stays unknown.
        return next((o for o in options if o.startswith("auto[0]")), None)

    async def async_select_option(self, option: str) -> None:
        self._selected = option
        self.async_write_ha_state()
