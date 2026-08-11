# Version: 0.7.5
import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import datetime
from functools import partial
import logging
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_platform, entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import voluptuous as vol

from .const import (
    CONF_ENABLE_NOTIFICATIONS,
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
    CONF_FUNCTION_PLAN_PLAN_MAP,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DOMAIN,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    SYNC_PROGRESS_END_PCT,
    SYNC_PROGRESS_START_PCT,
    WEBIO_CLASSES,
    webio_class_label,
    webio_class_name,
)
from .coordinator import ComexioCoordinator
from .function_plan_backup import format_backup_label
from .services import async_resync_io_group_headers, async_sort_function_plan

_LOGGER = logging.getLogger(__name__)

# Progress percentages of the function plan wiring pass, which runs after the Web-IO sync
# has already consumed the SYNC_PROGRESS_START_PCT..SYNC_PROGRESS_END_PCT span.
_PCT_PLAN_PAIRS = 60
_PCT_PLAN_FINALIZE = 92

_NOTE_ACTIVATED = ", plan activated"
_NOTE_NOT_ACTIVATED = ", ⚠️ plan NOT activated"
_ERR_RENAMED_MID_SYNC = "fub {fub_id} renamed/repurposed mid-sync"


def _items_of_class(seq: list[dict], cls: str) -> list[dict]:
    """Filter an audit-result list (missing/renamed/type/orphan) down to one Web-IO class."""
    return [i for i in seq if i.get("webio_class") == cls]


def _mmss(seconds: float) -> str:
    """Format a duration as m:ss."""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _plan_summary_line(
    plan_name: str,
    is_fresh: bool,
    n_added: int,
    n_total: int,
    unit: str,
    t0: float,
    note: str,
    errors: list[str],
) -> str:
    """One '• <plan>: +n/m pairs in m:ss' line for the sync notification's Function Plan block."""
    err_note = f", ⚠️ {len(errors)} errors" if errors else ""
    return (
        f"• '{plan_name}'{' (new)' if is_fresh else ''}: +{n_added}/{n_total} {unit}"
        f" in {_mmss(time.monotonic() - t0)} min{note}{err_note}"
    )


def _plan_pair_progress(ctx: "_SyncContext", state: dict, plan_name: str, done: int, total: int) -> None:
    """Push a progress update while pairs are being wired into a managed cluster plan.

    state carries the run-wide counters ({"done", "total", "t0"}) so the percentage spans
    all plans of the run, not just the plan currently being written.
    """
    overall_done = state["done"] + done
    overall_total = max(1, state["total"])
    elapsed = time.monotonic() - state["t0"]
    remaining = (elapsed / overall_done) * (overall_total - overall_done) if overall_done else 0.0
    ctx.update_status(
        f"**Function Plan:** `{plan_name}`\n"
        f"**Progress:** pair {overall_done} of {overall_total}\n\n---\n"
        f"🏁 **Remaining:** ~{_mmss(remaining)} min",
        pct=_PCT_PLAN_PAIRS + int((_PCT_PLAN_FINALIZE - _PCT_PLAN_PAIRS) * (overall_done / overall_total)),
        step_info=f"Function Plan '{plan_name}': pair {done}/{total}",
    )


@dataclass
class _SyncContext:
    """Per-press state shared by the ComexioSyncButton class-based sync helpers.

    Built once in async_handle_press and threaded through instead of relying on
    closures, so the per-class helper methods are assessed for cognitive complexity
    independently of async_handle_press (SonarQube S3776 folds nested closures'
    complexity into their enclosing function, which is what made the previous
    single-nested-function version score 99 against the project's limit of 15).
    """

    api: Any
    action: str
    ha_address: str
    webio_devices_audit: dict[str, dict[str, Any]]
    start_time: datetime.datetime
    class_names: dict[str, str]
    update_status: Callable[..., None]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio sync button."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sync_button = ComexioSyncButton(coordinator, coordinator.server_id)
    cancel_button = ComexioCancelSyncButton(coordinator, coordinator.server_id)
    migration_button = ComexioEntityIdMigrationButton(coordinator, coordinator.server_id)
    stats_cleanup_button = ComexioStatisticsCleanupButton(coordinator, coordinator.server_id)
    fw_check_button = ComexioFirmwareCheckButton(coordinator, coordinator.server_id)
    preview_button = ComexioPlanPreviewButton(coordinator, coordinator.server_id)
    uninstall_cleanup_button = ComexioCleanupButton(coordinator, coordinator.server_id)
    async_add_entities(
        [
            sync_button,
            cancel_button,
            migration_button,
            stats_cleanup_button,
            fw_check_button,
            preview_button,
            uninstall_cleanup_button,
        ]
    )

    # Register the entity service.
    # As a custom integration, the service is registered under
    # the 'comexio' domain (e.g., comexio.press_action).
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "press_action",
        {
            vol.Required("action"): vol.In(
                [
                    "full_sync",
                    "update_types",
                    "create_missing",
                    "update_renames",
                    "delete_orphans",
                    "update_ip",
                    "function_plan_add_missing",
                ]
            )
        },
        "async_handle_press",
    )


class ComexioSyncButton(CoordinatorEntity, ButtonEntity):
    """Button for automated Web-IO lifecycle management with Deep Delta Sync."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_start_btn"
        self._attr_translation_key = "webio_sync"
        self._attr_icon = "mdi:cloud-upload"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def icon(self) -> str:
        """Show a different icon while syncing."""
        if getattr(self.coordinator, "in_sync", False):
            return "mdi:sync-circle"  # Loading icon
        return "mdi:cloud-upload"

    @property
    def available(self) -> bool:
        """Gray out the button in the UI while syncing."""
        return not getattr(self.coordinator, "in_sync", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show status text in attributes."""
        return {
            "syncing": getattr(self.coordinator, "in_sync", False),
            "last_audit": self.coordinator.last_summary_hash,
        }

    async def async_press(self) -> None:
        """Standard UI-Press -> Full Sync."""
        await self.async_handle_press(action="full_sync")

    async def async_handle_press(self, action: str = "full_sync") -> None:
        """Execute the sync logic with mode selection."""
        if self.coordinator._sync_lock.locked():
            _LOGGER.warning("[%s] Sync already in progress, ignoring concurrent request", self.server_id)
            return
        await self.coordinator._sync_lock.acquire()
        self.coordinator.in_sync = True
        self.coordinator.sync_error = False

        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        notify_enabled = conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS)
        notif_id = f"comexio_sync_{self.server_id}"
        update_status = partial(self._update_sync_status, notify_enabled, notif_id)

        update_status("Initializing sync process...", pct=0, step_info="Initializing")
        start_time = datetime.datetime.now()
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        class_names = {cls: webio_class_name(webio_name, cls) for cls in WEBIO_CLASSES}

        try:
            update_status("Analyzing Comexio configuration...", pct=5, step_info="Analyzing configuration")

            # Check if the Web-IO device instances are already present (one per class)
            dev_ids = {cls: await api.get_webio_device_info(class_names[cls]) for cls in WEBIO_CLASSES}

            # Get the current network address of this Home Assistant instance
            ha_address = await api.get_ha_address()

            # Retrieve audit results stored in the coordinator
            audit_data = getattr(self.coordinator, "last_audit_results", {})
            webio_devices_audit = audit_data.get("webio_devices", {})

            ctx = _SyncContext(
                api=api,
                action=action,
                ha_address=ha_address,
                webio_devices_audit=webio_devices_audit,
                start_time=start_time,
                class_names=class_names,
                update_status=update_status,
            )

            gap_items = audit_data.get("function_plan_missing", [])

            if action == "function_plan_add_missing":
                # Standalone action: wire pre-existing but unwired pairs only, no Web-IO sync.
                plan_summary = await self._wire_created_pairs(ctx, [], gap_items)
                duration = datetime.datetime.now() - start_time
                msg = self._build_function_plan_add_missing_message(plan_summary, duration)
            else:
                (
                    added,
                    removed,
                    updated,
                    renamed,
                    updated_ip,
                    recreated_classes,
                    skipped_creates,
                    per_class,
                    created_names,
                ) = await self._sync_all_classes(ctx, audit_data, dev_ids)

                plan_summary = await self._wire_created_pairs(ctx, created_names, gap_items)

                duration = datetime.datetime.now() - start_time
                duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"
                msg = self._build_sync_result_message(
                    added,
                    updated,
                    renamed,
                    removed,
                    recreated_classes,
                    updated_ip,
                    duration_str,
                    skipped_creates,
                    per_class,
                )
                if plan_summary:
                    msg += "\n\n**Function Plan:**\n" + "\n".join(plan_summary)

            self.coordinator.last_audit_failed = False
            update_status(msg, pct=100, step_info="Done")

        except Exception as e:
            self.coordinator.in_sync = False
            self.coordinator.sync_error = True
            _LOGGER.exception("[%s] Sync failed", self.server_id)
            update_status(f"Error: {e}", is_error=True)

        finally:
            await self._finalize_sync()

    def _update_sync_status(
        self,
        notify_enabled: bool,
        notif_id: str,
        msg: str,
        is_error: bool = False,
        pct: int | None = None,
        step_info: str | None = None,
    ) -> None:
        """Update coordinator sync-progress state and optionally show a UI notification."""
        self.coordinator.sync_progress_text = msg
        if pct is not None:
            self.coordinator.sync_progress_pct = pct
        if step_info is not None:
            self.coordinator.sync_current_step = step_info
        self.coordinator.async_set_updated_data(self.coordinator.data)
        if notify_enabled:
            title = "Comexio Sync Failed" if is_error else f"Comexio Sync ({self.server_id})"
            persistent_notification.async_create(self.hass, msg, title=title, notification_id=notif_id)

    async def _sync_all_classes(
        self, ctx: _SyncContext, audit_data: dict[str, Any], dev_ids: dict[str, str | None]
    ) -> tuple[int, int, int, int, bool, list[str], int, dict[str, dict[str, int]], list[str]]:
        """Run `_sync_class` for every Web-IO class and aggregate the results.

        The trailing element is the flat list of Web-IO command names created in this run
        (across all classes) — the input of the managed cluster plan wiring pass.
        """
        missing_items = audit_data.get("missing", [])
        renamed_items = audit_data.get("rename", [])
        type_mismatches = audit_data.get("type", [])
        orphans = audit_data.get("orphan", [])

        added, removed, updated, renamed = 0, 0, 0, 0
        updated_ip = False
        recreated_classes: list[str] = []
        skipped_creates = 0
        per_class: dict[str, dict[str, int]] = {}
        created_names: list[str] = []
        # Split the shared progress span evenly across however many classes exist,
        # so the UI bar advances smoothly regardless of len(WEBIO_CLASSES).
        pct_span = (SYNC_PROGRESS_END_PCT - SYNC_PROGRESS_START_PCT) / len(WEBIO_CLASSES)
        class_pct_ranges = {
            cls: (
                round(SYNC_PROGRESS_START_PCT + idx * pct_span),
                round(SYNC_PROGRESS_START_PCT + (idx + 1) * pct_span),
            )
            for idx, cls in enumerate(WEBIO_CLASSES)
        }

        for cls in WEBIO_CLASSES:
            pct_start, pct_end = class_pct_ranges[cls]
            cls_result = await self._sync_class(
                ctx,
                cls,
                dev_ids[cls],
                _items_of_class(missing_items, cls),
                _items_of_class(renamed_items, cls),
                _items_of_class(type_mismatches, cls),
                _items_of_class(orphans, cls),
                ctx.webio_devices_audit.get(cls, {}).get("ip_mismatch", False),
                pct_start,
                pct_end,
            )
            added += cls_result["added"]
            removed += cls_result["removed"]
            updated += cls_result["updated"]
            renamed += cls_result["renamed"]
            updated_ip = updated_ip or cls_result["updated_ip"]
            skipped_creates += cls_result["skipped_creates"]
            created_names.extend(cls_result.get("created_names", []))
            if cls_result["recreated"]:
                recreated_classes.append(cls)
            else:
                # Recreated classes are already called out separately in the result
                # message (recreate_note); only delta-synced classes need a breakdown.
                per_class[cls] = {
                    "added": cls_result["added"],
                    "removed": cls_result["removed"],
                    "updated": cls_result["updated"],
                    "renamed": cls_result["renamed"],
                }

        return (
            added,
            removed,
            updated,
            renamed,
            updated_ip,
            recreated_classes,
            skipped_creates,
            per_class,
            created_names,
        )

    @staticmethod
    def _build_function_plan_add_missing_message(plan_summary: list[str], duration: datetime.timedelta) -> str:
        """Compose the result notification for the standalone Function Plan wiring action."""
        duration_str = f"{duration.seconds // 60}:{duration.seconds % 60:02d} min"
        body = "\n".join(plan_summary) if plan_summary else "Nothing to do — all pairs were already wired."
        return f"✅ **Function Plan update finished**\n\n{body}\n\n⏱ Total duration: {duration_str}"

    def _build_sync_result_message(
        self,
        added: int,
        updated: int,
        renamed: int,
        removed: int,
        recreated_classes: list[str],
        updated_ip: bool,
        duration_str: str,
        skipped_creates: int = 0,
        per_class: dict[str, dict[str, int]] | None = None,
    ) -> str:
        """Compose the final sync-result notification text."""
        recreate_note = ""
        if recreated_classes:
            recreated_str = ", ".join(webio_class_label(c) for c in recreated_classes)
            recreate_note = f"🚀 Recreated: {recreated_str}\n\n"

        skip_note = ""
        if skipped_creates:
            skip_note = (
                f"⚠️ Skipped {skipped_creates} creation(s) — Web-IO base class missing. "
                "Run a Full Sync to recreate it.\n\n"
            )

        changed = added + updated + renamed + removed
        if changed == 0 and not recreated_classes and updated_ip and not skipped_creates:
            return (
                "✅ **Comexio Server Address updated**\n\n"
                f"The IP address has been successfully updated in the Web-IO device(s).\n"
                f"⏱ Duration: {duration_str}"
            )
        if changed == 0 and recreated_classes and not updated_ip and not skipped_creates:
            return f"✅ **Comexio Recreation Finished**\n\n{recreate_note}⏱ Duration: {duration_str}"
        return (
            f"✅ **Comexio Sync Finished**\n\n{recreate_note}{skip_note}"
            f"Results: +{added}, {updated} updated, {renamed} renamed, -{removed} removed"
            + (", IP-Address updated" if updated_ip else "")
            + f".\n{self._build_per_class_note(per_class)}⏱ Duration: {duration_str}"
        )

    @staticmethod
    def _build_per_class_note(per_class: dict[str, dict[str, int]] | None) -> str:
        """Format a per-class delta breakdown line, only when >1 class actually changed."""
        if not per_class:
            return ""
        changed_classes = {cls: counts for cls, counts in per_class.items() if any(counts.values())}
        if len(changed_classes) < 2:
            return ""
        lines = [
            f"  • {webio_class_label(cls)}: +{c['added']}, {c['updated']} updated, "
            f"{c['renamed']} renamed, -{c['removed']} removed"
            for cls, c in changed_classes.items()
        ]
        return "\n".join(lines) + "\n"

    async def _finalize_sync(self) -> None:
        """Reset sync flags/UI state and force a full integration reload after a press."""
        # 1. Flag reset
        self.coordinator.in_sync = False
        self.coordinator.cancel_sync = False
        if not getattr(self.coordinator, "sync_error", False):
            self.coordinator.sync_progress_text = "Idle"
        self.coordinator.sync_progress_pct = None
        self.coordinator.sync_current_step = None
        self.coordinator.async_set_updated_data(self.coordinator.data)

        # 2. Reset audit_ignored. Set the skip flag first so the update_listener
        #    suppresses its reload — the explicit reload below is the single reload (R2).
        new_options = dict(self.coordinator.config_entry.options)
        new_options["audit_ignored"] = False
        self.coordinator.request_options_update_without_reload(new_options)

        # 3. Release sync lock before reload (old coordinator is replaced by reload anyway)
        self.coordinator._sync_lock.release()

        # 4. Give the Comexio server a moment to finish the write operation;
        #    the update_listener task runs here, sees the skip flag, and returns. (R2)
        await asyncio.sleep(0.5)

        # 5. Reset UI button
        self.async_write_ha_state()

        # 6. Restart integration (necessary!)
        _LOGGER.info("[%s] Forcing integration reload after sync...", self.server_id)
        await self.hass.config_entries.async_reload(self.coordinator.config_entry.entry_id)

    async def _decide_effective_action(
        self,
        ctx: _SyncContext,
        label: str,
        class_dev_id: str | None,
        cls_missing: list[dict],
        cls_renamed: list[dict],
        cls_types: list[dict],
        cls_orphans: list[dict],
        dev_ip_mismatch: bool,
    ) -> str:
        """Pick recreate vs. delta-sync for one class, based on ETA vs. the Fast-Track threshold."""
        action = ctx.action
        cls_action_eta = 0
        cls_task_count = 0
        if action in {"full_sync", "update_renames"}:
            cls_action_eta += len(cls_renamed) * SYNC_DURATION_WRITE
            cls_task_count += len(cls_renamed)
        if action in {"full_sync", "delete_orphans"}:
            cls_action_eta += len(cls_orphans) * SYNC_DURATION_DELETE
            cls_task_count += len(cls_orphans)
        if action in {"full_sync", "create_missing"}:
            cls_action_eta += len(cls_missing) * SYNC_DURATION_WRITE
            cls_task_count += len(cls_missing)
        if action in {"full_sync", "update_types"}:
            cls_action_eta += len(cls_types) * SYNC_DURATION_WRITE
            cls_task_count += len(cls_types)
        if action in {"full_sync", "update_ip"} and dev_ip_mismatch:
            cls_action_eta += SYNC_DURATION_WRITE
        if cls_task_count > 1:
            cls_action_eta += int(1.5 * (cls_task_count - 1))

        if not class_dev_id:
            _LOGGER.info("[%s] %s: no device instance found. Forcing recreate.", self.server_id, label)
            return "recreate"
        if action == "update_ip":
            _LOGGER.info("[%s] %s: targeted IP update requested. Skipping Fast-Track check.", self.server_id, label)
            return "update_ip"
        if cls_action_eta > SYNC_DURATION_RECREATE:
            _LOGGER.info(
                "[%s] %s ETA (%ds) > Fast-Track (%ds). Attempting Fast-Track.",
                self.server_id,
                label,
                cls_action_eta,
                SYNC_DURATION_RECREATE,
            )
            if await ctx.api.delete_webio_device(class_dev_id):
                _LOGGER.info("[%s] %s Fast-Track enabled: device has been deleted.", self.server_id, label)
                return "recreate"
            _LOGGER.info("[%s] %s device is in use. Falling back to Delta-Sync.", self.server_id, label)
            return action
        _LOGGER.info(
            "[%s] %s ETA (%ds) is faster than Fast-Track. Proceeding exactly as requested.",
            self.server_id,
            label,
            cls_action_eta,
        )
        return action

    async def _recreate_class(
        self,
        ctx: _SyncContext,
        cls: str,
        class_name: str,
        label: str,
        class_dev_id: str | None,
        pct_start: int,
        pct_end: int,
    ) -> None:
        """Delete-and-recreate a Web-IO class from scratch (Fast-Track / Initial Setup)."""
        api = ctx.api
        status_msg = (
            f"🛠️ **Initial Setup ({label})**\nCreating Web-IO class..."
            if ctx.action == "full_sync" and not class_dev_id
            else f"🚀 **Fast-Track active ({label})**\nHigh-speed recreation in progress..."
        )
        ctx.update_status(status_msg, pct=pct_start, step_info=f"Creating Web-IO class ({label})")

        base_info = await api.get_webio_base_info(class_name)
        if base_info:
            base_id, base_deletable = base_info
            if base_deletable:
                ctx.update_status(
                    f"{status_msg}\n\n🗑️ Deleting old class...",
                    pct=pct_start + 2,
                    step_info="Deleting old class",
                )
                await api.delete_webio_base(base_id)
                await asyncio.sleep(0.5)
            else:
                _LOGGER.warning(
                    "[%s] %s base %s still blocked by other logic. Reusing base structure.",
                    self.server_id,
                    label,
                    base_id,
                )

        ctx.update_status(
            f"{status_msg}\n\n📤 Uploading configuration...",
            pct=(pct_start + pct_end) // 2,
            step_info="Uploading configuration",
        )
        web_io_json = api.generate_webio_json(
            self.server_id,
            class_name,
            self.coordinator.data,
            webio_class=cls,
            ignored_marker_ids=self.coordinator.ignored_marker_ids,
        )
        success, res_id = await api.upload_web_io(self.server_id, class_name, web_io_json)
        if not success:
            raise RuntimeError(f"Upload failed ({label}): {res_id}")
        if not await api.create_webio_device(class_name, res_id, ctx.ha_address):
            raise RuntimeError(f"Device creation failed ({label}, class created, device instance not)")

    async def _delta_sync_class(
        self,
        ctx: _SyncContext,
        cls: str,
        class_name: str,
        label: str,
        class_dev_id: str | None,
        cls_effective_action: str,
        cls_missing: list[dict],
        cls_renamed: list[dict],
        cls_types: list[dict],
        cls_orphans: list[dict],
        dev_ip_mismatch: bool,
        pct_start: int,
        pct_end: int,
    ) -> dict[str, Any]:
        """Targeted create/rename/delete/type-fix of individual Web-IO commands for one class."""
        api = ctx.api
        _LOGGER.info(
            "[%s] %s: performing targeted Delta Sync for mode: %s", self.server_id, label, cls_effective_action
        )

        base_id = ctx.webio_devices_audit.get(cls, {}).get("base_id")
        if not base_id or str(base_id) in {"0", "None"}:
            b_info = await api.get_webio_base_info(class_name)
            if b_info:
                base_id = b_info[0]
                _LOGGER.debug("[%s] %s: resolved fallback Base ID: %s", self.server_id, label, base_id)

        tasks_to_do = self._build_delta_tasks(cls_effective_action, cls_renamed, cls_orphans, cls_missing, cls_types)
        skipped_creates = 0
        if not base_id:
            skipped_creates = sum(1 for t in tasks_to_do if t["type"] == "create")
            if skipped_creates:
                # base_id is only read by save_single_command for brand-new commands
                # (deviceBaseId); rename/type-fix pass existing_cmd_id and don't need it,
                # so only the create tasks are unsafe to run here — skip those, keep going.
                _LOGGER.warning(
                    "[%s] %s: no Web-IO base class found (device=%s) — skipping %d create task(s). "
                    "Run a Full Sync to recreate the class first.",
                    self.server_id,
                    label,
                    class_dev_id,
                    skipped_creates,
                )
                tasks_to_do = [t for t in tasks_to_do if t["type"] != "create"]
        total_tasks = max(1, len(tasks_to_do))
        on_progress = partial(
            self._report_delta_progress, ctx, tasks_to_do, total_tasks, label, cls_effective_action, pct_start, pct_end
        )

        result = await self._execute_delta_tasks(ctx, class_dev_id, base_id, tasks_to_do, on_progress)
        result["updated_ip"] = False
        result["skipped_creates"] = skipped_creates

        # Final step: Update Server Address (IP) — save_single_command does not
        # update device-level settings, so this is always a separate explicit call.
        if cls_effective_action in {"full_sync", "update_ip"} and dev_ip_mismatch and class_dev_id:
            ctx.update_status(
                f"🌐 **Repair in progress ({label}):** Updating HA IP address...",
                pct=pct_end - 2,
                step_info="Updating HA IP address",
            )
            result["updated_ip"] = await api.update_webio_device_ip(class_dev_id, ctx.ha_address, class_name)

        return result

    def _report_delta_progress(
        self,
        ctx: _SyncContext,
        tasks_to_do: list[dict],
        total_tasks: int,
        label: str,
        cls_effective_action: str,
        pct_start: int,
        pct_end: int,
        current: int,
        task_name: str,
        task_type: str,
    ) -> None:
        """Push a progress update for one delta-sync task (bound as the on_progress callback)."""
        rem_tasks = tasks_to_do[current:]
        eta_s = sum(SYNC_DURATION_DELETE if t["type"] == "delete" else SYNC_DURATION_WRITE for t in rem_tasks)
        if len(rem_tasks) > 1:
            eta_s += int(1.5 * (len(rem_tasks) - 1))
        elaps = datetime.datetime.now() - ctx.start_time
        elaps_str = f"{elaps.seconds // 60:02}:{elaps.seconds % 60:02d}"
        rem_str = f"{eta_s // 60:02}:{eta_s % 60:02d}"
        eta_t = (datetime.datetime.now() + datetime.timedelta(seconds=eta_s)).strftime("%H:%M:%S")
        labels = {"rename": "✏️ Rename", "delete": "🗑️ Remove", "create": "➕ Add", "type": "🔧 Type-Fix"}
        t_label = labels.get(task_type, task_type)
        prog_msg = (
            f"**Class:** {label}\n**Mode:** `{cls_effective_action}`\n"
            f"**Progress:** Step {current + 1} of {total_tasks}\n"
            f"**Current:** {t_label}: `{task_name}`\n\n---\n"
            f"🕒 **Start:** {ctx.start_time.strftime('%H:%M:%S')} (Runtime: {elaps_str})\n"
            f"🏁 **Done:** ~{eta_t} (Remaining: {rem_str})"
        )
        pct_val = pct_start + int((pct_end - pct_start) * (current / total_tasks))
        ctx.update_status(
            prog_msg,
            pct=pct_val,
            step_info=f"{label} {current + 1}/{total_tasks} | {t_label}: '{task_name}' | Rem: {rem_str}",
        )

    def _build_delta_tasks(
        self,
        cls_effective_action: str,
        cls_renamed: list[dict],
        cls_orphans: list[dict],
        cls_missing: list[dict],
        cls_types: list[dict],
    ) -> list[dict]:
        """Collect the individual create/rename/delete/type-fix tasks for one delta-sync run."""
        tasks_to_do = []
        if cls_effective_action in {"full_sync", "update_renames"}:
            tasks_to_do.extend([{"item": i, "type": "rename"} for i in cls_renamed])
        if cls_effective_action in {"full_sync", "delete_orphans"}:
            tasks_to_do.extend([{"item": i, "type": "delete"} for i in cls_orphans])
        if cls_effective_action in {"full_sync", "create_missing"}:
            tasks_to_do.extend([{"item": i, "type": "create"} for i in cls_missing])
        if cls_effective_action in {"full_sync", "update_types"}:
            tasks_to_do.extend([{"item": i, "type": "type"} for i in cls_types])
        return tasks_to_do

    async def _execute_delta_tasks(
        self,
        ctx: _SyncContext,
        class_dev_id: str | None,
        base_id: str | None,
        tasks_to_do: list[dict],
        on_progress: Callable[[int, str, str], None],
    ) -> dict[str, int]:
        """Run the collected delta-sync tasks against the Comexio API, in order."""
        api = ctx.api
        dev_reg = dr.async_get(self.hass)
        result: dict[str, Any] = {"added": 0, "removed": 0, "updated": 0, "renamed": 0, "created_names": []}
        for idx, task in enumerate(tasks_to_do):
            if getattr(self.coordinator, "cancel_sync", False):
                break
            item, t_type = task["item"], task["type"]
            on_progress(idx, item["name"], t_type)
            if t_type == "rename":
                await api.save_single_command(base_id, class_dev_id, item["payload"], existing_cmd_id=item["id"])
                result["renamed"] += 1
            elif t_type == "delete":
                await api.delete_single_command(item["id"], class_dev_id)
                result["removed"] += 1
            elif t_type == "type":
                # Clean up entity from registry if type changed
                device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{self.server_id}_{item['id']}")})
                if device:
                    dev_reg.async_remove_device(device.id)
                await api.save_single_command(base_id, class_dev_id, item["payload"], existing_cmd_id=item["id"])
                result["updated"] += 1
            elif t_type == "create":
                await api.save_single_command(base_id, class_dev_id, item["payload"])
                result["added"] += 1
                result["created_names"].append(item["name"])
        return result

    async def _sync_class(
        self,
        ctx: _SyncContext,
        cls: str,
        class_dev_id: str | None,
        cls_missing: list[dict],
        cls_renamed: list[dict],
        cls_types: list[dict],
        cls_orphans: list[dict],
        dev_ip_mismatch: bool,
        pct_start: int,
        pct_end: int,
    ) -> dict[str, Any]:
        """Recreate-or-Delta-Sync one Web-IO class (marker/io).

        Run independently per class so the two devices' Fast-Track eligibility (and
        any resulting device deletion) never interfere with each other.
        """
        class_name = ctx.class_names[cls]
        label = webio_class_label(cls)

        cls_effective_action = await self._decide_effective_action(
            ctx, label, class_dev_id, cls_missing, cls_renamed, cls_types, cls_orphans, dev_ip_mismatch
        )

        if cls_effective_action == "recreate":
            await self._recreate_class(ctx, cls, class_name, label, class_dev_id, pct_start, pct_end)
            # Everything in this class is brand new, so every command of it needs the
            # function plan pairing pass too: its old webIoId (if any) went away with the
            # deleted class, and the pre-sync audit never flagged a wiring gap for it (it
            # only checks keys already present in com_map, which was empty here). Pairing
            # is idempotent — already-wired markers/IOs are skipped.
            return {
                "added": 0,
                "removed": 0,
                "updated": 0,
                "renamed": 0,
                "updated_ip": False,
                "recreated": True,
                "skipped_creates": 0,
                "created_names": [
                    cmd["Name"]
                    for cmd in ctx.api.build_webio_commands(
                        self.server_id,
                        self.coordinator.data,
                        webio_class=cls,
                        ignored_marker_ids=self.coordinator.ignored_marker_ids,
                    )
                ],
            }

        result = await self._delta_sync_class(
            ctx,
            cls,
            class_name,
            label,
            class_dev_id,
            cls_effective_action,
            cls_missing,
            cls_renamed,
            cls_types,
            cls_orphans,
            dev_ip_mismatch,
            pct_start,
            pct_end,
        )
        result["recreated"] = False
        return result

    # --- MANAGED CLUSTER PLAN WIRING ---

    async def _wire_created_pairs(
        self, ctx: _SyncContext, created_names: list[str], gap_items: list[dict]
    ) -> list[str]:
        """Wire every Web-IO command created in this run into its managed cluster plan.

        Marker commands are distributed across marker cluster plans (one per ID range),
        IO commands across the IO cluster plan of their extension — but only for the
        extensions the user opted into (CONF_FUNCTION_PLAN_IO_EXTENSIONS), and never for
        an extension that is currently offline (its hardware isn't there, so wiring it is
        pointless; the next sync picks it up once it's back).

        For a full sync (or the standalone wiring-only action), pairs the audit found
        already existing but not yet wired (gap_items) are merged in too — a Fast-Track
        recreate never touches Function Plan wiring on its own, so pre-existing gaps must
        still be closed here, not just pairs freshly created in this run.
        Returns per-plan summary lines for the final sync notification.
        """
        managed_exts = set(self.coordinator.config_entry.options.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, []))
        managed_exts -= self.coordinator.offline_extensions or set()

        created_marker_ids: list[int] = []
        created_io_refs: list[tuple[str, str]] = []
        for name in created_names:
            if (mid := _parse_marker_id_from_webio_name(name)) is not None:
                created_marker_ids.append(mid)
            elif (io_ref := _parse_io_from_webio_name(name)) is not None:
                created_io_refs.append(io_ref)

        if ctx.action in {"full_sync", "function_plan_add_missing"}:
            gap_marker_ids = [item["marker_id"] for item in gap_items if "marker_id" in item]
            gap_io_items = [item for item in gap_items if "marker_id" not in item]
        else:
            gap_marker_ids = []
            gap_io_items = []

        marker_ids = list(dict.fromkeys(created_marker_ids + gap_marker_ids))
        io_items = _merge_io_items(created_io_refs, gap_io_items, managed_exts)
        if not marker_ids and not io_items:
            return []

        progress_state = {"done": 0, "total": len(marker_ids) + len(io_items), "t0": time.monotonic()}
        summary: list[str] = []
        n_added = 0
        n_errors = 0

        if marker_ids:
            lines, added, errors = await self._wire_marker_clusters(ctx, marker_ids, progress_state)
            summary.extend(lines)
            n_added += added
            n_errors += errors
        if io_items:
            lines, added, errors = await self._wire_io_clusters(ctx, io_items, progress_state)
            summary.extend(lines)
            n_added += added
            n_errors += errors

        _LOGGER.info("[%s] Cluster plan wiring done: added=%d, errors=%d", self.server_id, n_added, n_errors)
        return summary

    async def _wire_marker_clusters(
        self, ctx: _SyncContext, marker_ids: list[int], progress_state: dict
    ) -> tuple[list[str], int, int]:
        """Resolve the marker cluster plans and add the pairs. Returns (lines, added, errors)."""
        plan_to_ids, created_plans = await self.coordinator.resolve_marker_clusters(marker_ids)
        if not plan_to_ids:
            _LOGGER.warning("[%s] Cluster plan wiring: no marker cluster plan available", self.server_id)
            return ["⚠️ No marker cluster plan available — see log."], 0, 1

        summary: list[str] = []
        added = 0
        errors = 0
        for fub_id, cluster_ids in plan_to_ids.items():
            line, lp_added, lp_errors = await self._add_pairs_to_plan(
                ctx, fub_id, sorted(cluster_ids), fub_id in created_plans, progress_state
            )
            summary.append(line)
            added += len(lp_added)
            errors += len(lp_errors)
        return summary, added, errors

    async def _wire_io_clusters(
        self, ctx: _SyncContext, io_items: list[dict[str, str]], progress_state: dict
    ) -> tuple[list[str], int, int]:
        """Resolve the IO cluster plans and add the pairs. Returns (lines, added, errors)."""
        by_ext: dict[str, list[str]] = {}
        for item in io_items:
            by_ext.setdefault(item["ext_name"], []).append(item["identifier"])

        ext_plans, created_plans = await self.coordinator.resolve_io_clusters(sorted(by_ext))
        if not ext_plans:
            _LOGGER.warning("[%s] Cluster plan wiring: no IO cluster plan available", self.server_id)
            return ["⚠️ No IO cluster plan available — see log."], 0, 1

        plan_exts: dict[int, list[tuple[str, int]]] = {}
        for ext, (fub_id, column) in ext_plans.items():
            plan_exts.setdefault(fub_id, []).append((ext, column))

        summary: list[str] = []
        added = 0
        errors = 0
        for fub_id, ext_cols in plan_exts.items():
            line, lp_added, lp_errors = await self._add_io_pairs_to_plan(
                ctx, fub_id, sorted(ext_cols, key=lambda t: t[1]), by_ext, fub_id in created_plans, progress_state
            )
            summary.append(line)
            added += len(lp_added)
            errors += len(lp_errors)
        return summary, added, errors

    def _plan_name(self, fub_id: int) -> str:
        """Live name of a plan, falling back to its fub_id when it isn't in the cache."""
        return self.coordinator.api.fub_data.get(str(fub_id), {}).get("Name") or str(fub_id)

    async def _add_pairs_to_plan(
        self,
        ctx: _SyncContext,
        fub_id: int,
        cluster_ids: list[int],
        is_fresh: bool,
        progress_state: dict,
    ) -> tuple[str, list[int], list[str]]:
        """Add marker pairs to one plan. Returns (summary_line, added_ids, errors).

        Freshly created plans get their pairs at final grid positions (no sort pass)
        and are activated afterwards; existing plans are re-sorted quietly and — if
        they were active before this run stopped them — reactivated.
        """
        api = ctx.api
        plan_name = self._plan_name(fub_id)
        if (mismatch := self._check_plan_rename_mismatch(fub_id, cluster_ids, plan_name)) is not None:
            return mismatch

        # Capture the activation state BEFORE stop_fup: function_plan_add_marker_pairs
        # reloads the config (refreshing fub_data) after the stop, so any later lookup
        # would see the plan as inactive and skip reactivation.
        was_active = bool(api.fub_data.get(str(fub_id), {}).get("Active", True))
        t0 = time.monotonic()
        ctx.update_status(
            f"Adding {len(cluster_ids)} pair(s) to Function Plan '{plan_name}'...",
            pct=_PCT_PLAN_PAIRS,
            step_info="Function Plan: adding pairs",
        )
        await self.coordinator.async_function_plan_change_backup(
            fub_id, f"add_marker_pairs {[f'M{m}' for m in cluster_ids]}"
        )
        await api.logikplan_stop_fup(fub_id)
        lp_added, lp_errors = await api.function_plan_add_marker_pairs(
            fub_id,
            cluster_ids,
            fresh_plan=is_fresh,
            progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
        )
        progress_state["done"] += len(cluster_ids)
        if lp_errors:
            _LOGGER.warning("[%s] function_plan_add_marker_pairs errors: %s", self.server_id, lp_errors)

        note = await self._finalize_plan_after_pairs(ctx, fub_id, plan_name, lp_added, is_fresh, was_active)
        return (
            _plan_summary_line(plan_name, is_fresh, len(lp_added), len(cluster_ids), "pairs", t0, note, lp_errors),
            lp_added,
            lp_errors,
        )

    def _check_plan_rename_mismatch(
        self, fub_id: int, cluster_ids: list[int], plan_name: str
    ) -> tuple[str, list[int], list[str]] | None:
        """Aborted-result tuple if the plan was renamed/repurposed since resolve_marker_clusters() ran."""
        expected_name = self.coordinator.expected_marker_cluster_name(cluster_ids[0])
        if plan_name == expected_name:
            return None
        _LOGGER.error(
            "[%s] Aborting marker pair write to fub=%s: expected managed plan '%s' but it is now named "
            "'%s' — it was renamed/repurposed since resolve_marker_clusters() ran",
            self.server_id,
            fub_id,
            expected_name,
            plan_name,
        )
        return (
            f"⚠️ fub {fub_id}: aborted — expected '{expected_name}' but plan is now '{plan_name}'",
            [],
            [_ERR_RENAMED_MID_SYNC.format(fub_id=fub_id)],
        )

    async def _finalize_plan_after_pairs(
        self,
        ctx: _SyncContext,
        fub_id: int,
        plan_name: str,
        lp_added: list[int],
        is_fresh: bool,
        was_active: bool,
    ) -> str:
        """Activate/sort/restart the plan after pairs were added; returns the summary-line note."""
        if not lp_added:
            # Nothing changed (all pairs already wired) — only undo our own stop_fup.
            self._plan_finalize_status(ctx, plan_name, "restarting plan", "restarting")
            if was_active:
                await ctx.api.logikplan_run_fup(fub_id)
            return ""

        if is_fresh:
            # Fresh plan: pairs already sit at their final grid slots — skip the sort pass.
            self._plan_finalize_status(ctx, plan_name, "activating plan", "activating")
            return _NOTE_ACTIVATED if await ctx.api.logikplan_run_fup(fub_id) else _NOTE_NOT_ACTIVATED

        return await self._sort_and_reactivate(ctx, fub_id, plan_name, was_active)

    @staticmethod
    def _plan_finalize_status(ctx: _SyncContext, plan_name: str, what: str, step: str) -> None:
        """Progress line for the finalize phase of one managed cluster plan."""
        ctx.update_status(
            f"Finalizing Function Plan '{plan_name}' — {what}...",
            pct=_PCT_PLAN_FINALIZE,
            step_info=f"Function Plan: {step}",
        )

    async def _sort_and_reactivate(self, ctx: _SyncContext, fub_id: int, plan_name: str, was_active: bool) -> str:
        """Re-sort an existing plan after new pairs landed on placeholder slots, then reactivate."""
        self._plan_finalize_status(ctx, plan_name, "sorting elements", "sorting")
        sort_res = await async_sort_function_plan(
            self.hass, self.coordinator, ctx.api, fub_id, notify=False, was_active=was_active
        )
        sorted_ok = bool(sort_res and sort_res["success"])
        note = f", sorted in {sort_res['duration']:.1f}s" if sorted_ok else ", ⚠️ sort failed"
        if was_active and not (sort_res and sort_res.get("activated")):
            # Sort was skipped or lost the reactivation (e.g. save_elements_pos failed) —
            # don't leave a previously active plan stopped.
            note += _NOTE_ACTIVATED if await ctx.api.logikplan_run_fup(fub_id) else _NOTE_NOT_ACTIVATED
        return note

    async def _add_io_pairs_to_plan(
        self,
        ctx: _SyncContext,
        fub_id: int,
        ext_cols: list[tuple[str, int]],
        by_ext: dict[str, list[str]],
        is_fresh: bool,
        progress_state: dict,
    ) -> tuple[str, list[str], list[str]]:
        """Add the missing IO pairs of one or more extensions to one IO cluster plan.

        Unlike marker plans there is never a sort pass: every IO pair lands in its
        deterministic slot (see api.function_plan_add_io_pairs), so finalizing is just
        reactivating the plan (fresh plans and plans that were active before the stop).
        Returns (summary_line, added_labels, errors).
        """
        api = ctx.api
        plan_name = self._plan_name(fub_id)
        stale_exts = [ext for ext, _ in ext_cols if not self.coordinator.io_cluster_plan_contains(plan_name, ext)]
        if stale_exts:
            _LOGGER.error(
                "[%s] Aborting IO pair write to fub=%s: plan is now named '%s', no longer a managed IO "
                "cluster plan for %s — it was renamed/repurposed since resolve_io_clusters() ran",
                self.server_id,
                fub_id,
                plan_name,
                stale_exts,
            )
            return (
                f"⚠️ fub {fub_id} ('{plan_name}'): aborted — no longer matches expected IO cluster for {stale_exts}",
                [],
                [_ERR_RENAMED_MID_SYNC.format(fub_id=fub_id)],
            )

        was_active = bool(api.fub_data.get(str(fub_id), {}).get("Active", True))
        n_total = sum(len(by_ext[ext]) for ext, _ in ext_cols)
        t0 = time.monotonic()
        ctx.update_status(
            f"Adding {n_total} IO pair(s) to Function Plan '{plan_name}'...",
            pct=_PCT_PLAN_PAIRS,
            step_info="Function Plan: adding IO pairs",
        )
        await self.coordinator.async_function_plan_change_backup(
            fub_id, f"add_io_pairs {[f'{ext}:{len(by_ext[ext])}' for ext, _ in ext_cols]}"
        )
        await api.logikplan_stop_fup(fub_id)

        added: list[str] = []
        errors: list[str] = []
        for ext, column in ext_cols:
            lp_added, lp_errors = await api.function_plan_add_io_pairs(
                fub_id,
                ext,
                by_ext[ext],
                column_index=column,
                progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
            )
            progress_state["done"] += len(by_ext[ext])
            added.extend(f"{ext} {ident}" for ident in lp_added)
            errors.extend(lp_errors)
        if errors:
            _LOGGER.warning("[%s] function_plan_add_io_pairs errors: %s", self.server_id, errors)

        if added:
            n_headers = await async_resync_io_group_headers(self.coordinator, api, fub_id)
            _LOGGER.info(
                "[%s] function_plan_add_io_pairs: resynced %d group header(s) for plan '%s'",
                self.server_id,
                n_headers,
                plan_name,
            )

        note = ""
        if is_fresh or was_active:
            ctx.update_status(
                f"Finalizing Function Plan '{plan_name}' — activating plan...",
                pct=_PCT_PLAN_FINALIZE,
                step_info="Function Plan: activating",
            )
            activated = await api.logikplan_run_fup(fub_id)
            if added:
                note = _NOTE_ACTIVATED if activated else _NOTE_NOT_ACTIVATED

        return _plan_summary_line(plan_name, is_fresh, len(added), n_total, "IO pairs", t0, note, errors), added, errors


def _parse_marker_id_from_webio_name(name: str) -> int | None:
    """Extract the marker ID from a Web-IO command name like 'HA M68 Title'.

    Returns the integer marker ID, or None if the name doesn't match the marker pattern.
    """
    parts = name.split()
    if len(parts) >= 2 and parts[0] == "HA" and parts[1].upper().startswith("M"):
        with contextlib.suppress(ValueError):
            return int(parts[1][1:])
    return None


def _parse_io_from_webio_name(name: str) -> tuple[str, str] | None:
    """Extract (ext_name, identifier) from a Web-IO command name like 'HA IO UD1 Q3'.

    Extension names containing spaces are not parseable this way — the same known
    limitation as the audit's com_map key parsing.
    """
    parts = name.split()
    if len(parts) >= 4 and parts[0] == "HA" and parts[1] == "IO":
        return parts[2], parts[3]
    return None


def _merge_io_items(created_refs: list[tuple[str, str]], gap_items: list[dict], managed_exts: set[str]) -> list[dict]:
    """Merge freshly created IO commands (managed extensions only) with audit gap items,
    deduplicated by (ext_name, identifier)."""
    merged: dict[tuple[str, str], dict] = {}
    for ext, ident in created_refs:
        if ext in managed_exts:
            merged[(ext, ident)] = {"ext_name": ext, "identifier": ident}
    for item in gap_items:
        merged.setdefault((item["ext_name"], item["identifier"]), item)
    return list(merged.values())


class ComexioCancelSyncButton(CoordinatorEntity, ButtonEntity):
    """Button to interrupt an ongoing Comexio sync process."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_sync_cancel_btn"
        self._attr_translation_key = "cancel_sync"
        self._attr_icon = "mdi:stop-circle-outline"
        # 'diagnostic' ensures the button is grouped under 'Configuration'
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Only available when a sync is actually running."""
        return getattr(self.coordinator, "in_sync", False)

    async def async_press(self) -> None:
        """Trigger the cancel flag in the coordinator."""
        _LOGGER.warning("[%s] Manual cancel requested by user", self.server_id)
        self.coordinator.cancel_sync = True
        self.async_write_ha_state()


class ComexioFirmwareCheckButton(CoordinatorEntity, ButtonEntity):
    """Button to force-run the extension firmware check outside its nightly window.

    Comexio warns this call can briefly interrupt extension outputs — pressing it
    deliberately accepts that risk (e.g. to test the update.* entities without waiting
    for both the 04:00 window and a comexio_version change; see async_force_firmware_check).
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_fw_check_btn"
        self._attr_translation_key = "fw_check"
        self._attr_icon = "mdi:chip"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_press(self) -> None:
        """Force-run the firmware check, bypassing the comexio_version gate."""
        _LOGGER.warning("[%s] Manual extension firmware check requested by user", self.server_id)
        start = time.monotonic()
        ran = await self.coordinator.async_force_firmware_check()
        duration = time.monotonic() - start

        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        if conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS):
            if ran:
                names = ", ".join(sorted(self.coordinator.extension_firmware))
                msg = f"✅ Firmware check finished in {duration:.1f}s.\n\nChecked modules: {names}"
            else:
                msg = f"⚠️ Firmware check returned no data ({duration:.1f}s) — see log for details."
            persistent_notification.async_create(
                self.hass,
                msg,
                title=f"Comexio Firmware Check ({self.server_id})",
                notification_id=f"comexio_fw_check_{self.server_id}",
            )


class ComexioEntityIdMigrationButton(CoordinatorEntity, ButtonEntity):
    """Button to fix duplicate server_id in entity IDs."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_entity_id_fix_btn"
        self._attr_translation_key = "entity_id_fix"
        self._attr_icon = "mdi:identifier"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Active only when entity_id mismatches exist."""
        return len(self.coordinator.entity_id_mismatches) > 0

    async def async_press(self) -> None:
        """Migrate entity_ids by removing the duplicate server_id prefix."""
        self.coordinator.async_migrate_entity_ids()
        ir.async_delete_issue(self.hass, DOMAIN, f"entity_id_mismatch_{self.server_id}")
        self.coordinator.async_set_updated_data(self.coordinator.data)


class ComexioStatisticsCleanupButton(CoordinatorEntity, ButtonEntity):
    """Button to delete orphaned long-term statistics left behind by entity renames."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_statistics_cleanup_btn"
        self._attr_translation_key = "statistics_cleanup"
        self._attr_icon = "mdi:database-remove"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    @property
    def available(self) -> bool:
        """Active only when orphaned statistics exist."""
        return len(self.coordinator.orphaned_statistics) > 0

    async def async_press(self) -> None:
        """Delete the orphaned statistic_ids via the recorder."""
        from homeassistant.components.recorder import get_instance

        ids = list(self.coordinator.orphaned_statistics)
        if ids and "recorder" in self.hass.config.components:
            instance = get_instance(self.hass)
            instance.async_clear_statistics(ids)

        self.coordinator.orphaned_statistics = []
        ir.async_delete_issue(self.hass, DOMAIN, f"statistics_orphaned_{self.server_id}")

        _LOGGER.info("[%s] Cleared %d orphaned statistics", self.server_id, len(ids))
        self.coordinator.async_set_updated_data(self.coordinator.data)


class ComexioPlanPreviewButton(CoordinatorEntity, ButtonEntity):
    """Button to render the plan currently selected in the 'Function Plans' selector as an SVG preview."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_plan_preview_btn"
        self._attr_translation_key = "plan_preview"
        self._attr_icon = "mdi:image-outline"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_added_to_hass(self) -> None:
        """Track the 'Function Plans' selector so 'available' re-evaluates on every selection change.

        Picking a plan there only writes the select's own state (see select.py's
        async_select_option, which explicitly skips a coordinator reload) — without this
        listener, this button's published 'available' state would stay stuck at whatever it
        was at startup.
        """
        await super().async_added_to_hass()
        select_eid = er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"comexio_{self.server_id}_logikplan_plan_selector"
        )
        if select_eid:
            self.async_on_remove(async_track_state_change_event(self.hass, [select_eid], self._handle_selector_change))

    @callback
    def _handle_selector_change(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Only available while the 'Function Plans' selector points at one concrete plan."""
        return self.coordinator.get_active_function_plan_fub_id() is not None

    def _active_backup_choice(self, fub_id: int, plan_name: str) -> tuple[str, int] | None:
        """Return (kind, slot) if the backup selector points at a stored snapshot, else None.

        Lets the Preview button render a historical state instead of live — the selector
        itself only ever shows entries for the currently active plan (see select.py), so no
        extra identity check against fub_id/plan_name is needed here beyond looking them up.
        """
        select_eid = er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, f"comexio_{self.server_id}_plan_backup_selector"
        )
        state = self.hass.states.get(select_eid) if select_eid else None
        if not state or state.state in ("unavailable", "unknown"):
            return None
        entries = self.coordinator.function_plan_backup.plan_backups_for_identity_sync(fub_id, plan_name)
        return next(((e["kind"], e["slot"]) for e in entries if format_backup_label(e) == state.state), None)

    async def _async_arm_live_refresh(self, fub_id: int, plan_name: str) -> None:
        """Fetch live elements (if needed) and hand them to the coordinator to prime its live cache.

        Right after startup or a plan change no live render has filled the cache yet, so fetch
        the live elements once here; coordinator.prime_live_preview_cache() is a no-op if a cache
        is already armed.
        """
        if self.coordinator.has_live_preview_cache:
            return
        plan_data = await self.coordinator.api.logikplan_load_elements(fub_id)
        if plan_data:
            self.coordinator.prime_live_preview_cache(
                fub_id, plan_name, plan_data.get("elements", {}), plan_data.get("connections", {})
            )

    async def async_press(self) -> None:
        """Render the active plan into the Plan Preview sensor — live, or a chosen backup snapshot."""
        api = self.coordinator.api
        fub_id = self.coordinator.get_active_function_plan_fub_id()
        if fub_id is None:
            _LOGGER.warning("[%s] Plan preview requested but no plan is selected", self.server_id)
            return
        plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))

        backup_choice = self._active_backup_choice(fub_id, plan_name)
        if backup_choice is not None:
            kind, slot = backup_choice
            snapshot = await self.coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
            if snapshot is None:
                _LOGGER.warning(
                    "[%s] Plan preview: backup %s[%d] not found for '%s'", self.server_id, kind, slot, plan_name
                )
                return
            await self.coordinator.async_generate_plan_preview(
                fub_id,
                plan_name,
                snapshot.get("elements", {}),
                snapshot.get("connections", {}),
                f"snapshot:{kind}:{slot}",
                snapshot.get("labels"),
            )
            await self._async_arm_live_refresh(fub_id, plan_name)
            return

        plan_data = await api.logikplan_load_elements(fub_id)
        if not plan_data:
            _LOGGER.warning("[%s] Plan preview: could not load plan %s", self.server_id, fub_id)
            return
        await self.coordinator.async_generate_plan_preview(
            fub_id, plan_name, plan_data.get("elements", {}), plan_data.get("connections", {}), "live"
        )


class ComexioCleanupButton(CoordinatorEntity, ButtonEntity):
    """Test button: raises a Repair issue to tear down everything the integration
    created in Comexio (managed Function Plans, Web-IO devices, Web-IO classes).
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_uninstall_cleanup_btn"
        self._attr_translation_key = "uninstall_cleanup"
        self._attr_icon = "mdi:delete-sweep"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_press(self) -> None:
        """Raise a Repair issue summarizing what would be torn down. The actual
        deletion only runs when the user confirms it in the Repair dialog."""
        plan_map = self.coordinator.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
        # Force a fresh audit rather than trusting a poll-interval-old snapshot — devices
        # created/removed since the last poll would otherwise be missed or acted on stale.
        await self.coordinator.async_request_refresh()
        webio_devices = self.coordinator.last_audit_results.get("webio_devices", {})
        device_count = sum(1 for dev in webio_devices.values() if dev.get("device_id"))
        class_count = sum(1 for dev in webio_devices.values() if dev.get("base_id"))

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"uninstall_cleanup_{self.server_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="uninstall_cleanup",
            translation_placeholders={
                "server_id": self.server_id,
                "plan_count": str(len(plan_map)),
                "device_count": str(device_count),
                "class_count": str(class_count),
            },
            data={
                "entry_id": self.coordinator.config_entry.entry_id,
                "plan_count": len(plan_map),
                "device_count": device_count,
                "class_count": class_count,
            },
        )
