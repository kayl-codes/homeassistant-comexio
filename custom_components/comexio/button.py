# Version: 0.7.6
import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import datetime
from functools import partial
import json
import logging
import time
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform, entity_registry as er, issue_registry as ir
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
    FUNCTION_PLAN_SERVICE_ACTIVATE,
    FUNCTION_PLAN_SERVICE_STOP,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    ICON_ADD,
    ICON_CHECK,
    ICON_CLOCK,
    ICON_DELETE,
    ICON_DURATION,
    ICON_ERROR,
    ICON_FIX,
    ICON_FLAG,
    ICON_NETWORK,
    ICON_RENAME,
    ICON_ROCKET,
    ICON_SUCCESS,
    ICON_TOOLS,
    ICON_UPLOAD,
    ICON_WARNING,
    ISSUE_UNINSTALL_CLEANUP,
    RANGE_CHECK_CHECKED,
    RANGE_CHECK_CORRECTION_FAILED,
    RANGE_CHECK_EXCLUDED,
    RANGE_CHECK_FAILED,
    RANGE_CHECK_FIXED,
    RANGE_CHECK_SKIPPED,
    SOURCE_CATEGORIES,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    SYNC_PROGRESS_END_PCT,
    SYNC_PROGRESS_START_PCT,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASSES,
    MarkerKind,
    SourceCategory,
    WebioClass,
    category_by_fub_module_type,
    source_category,
    trigger_pair_categories,
    uninstall_cleanup_pending_notification_id,
    webio_class_label,
    webio_class_name,
    webio_range_check_entity_id,
)
from .coordinator import ComexioCoordinator, webio_still_present
from .entity import ComexioKnxEntity, ComexioMarkerEntity
from .function_plan_backup import format_backup_label
from .repairs import count_referencing_automations_and_scripts
from .services import async_resync_io_group_headers, async_sort_function_plan

_LOGGER = logging.getLogger(__name__)

# Progress percentages of the function plan wiring pass, which runs after the Web-IO sync
# has already consumed the SYNC_PROGRESS_START_PCT..SYNC_PROGRESS_END_PCT span.
_PCT_PLAN_PAIRS = 60
_PCT_PLAN_FINALIZE = 92

_SYNC_PROGRESS_NOTIFY_EVERY = 5  # notify every Nth pair; the plan's final pair always notifies too

_NOTE_ACTIVATED = ", plan activated"
_NOTE_NOT_ACTIVATED = f", {ICON_WARNING} plan NOT activated"
_ERR_RENAMED_MID_SYNC = "fub {fub_id} renamed/repurposed mid-sync"
_STEP_ANALYZING_CONFIG = "Analyzing configuration"
_STEP_CHECKING_PLAN = "Checking function plan wiring"
_DELTA_RESULT_KEYS = {"rename": "renamed", "delete": "removed", "type": "updated", "create": "added"}
_FAILED_WRITES_SHOWN = 10  # failed Web-IO writes named in the result message; the rest are counted


def _format_counts(added: int, updated: int, renamed: int, removed: int) -> str:
    """Sync result counts in words — "+0 … -0" read like signed numbers."""
    return f"{added} added, {updated} updated, {renamed} renamed, {removed} removed"


def _failed_writes_note(failed_writes: list[str]) -> str:
    """Result-message block naming the sync steps that failed, or "" when none did."""
    if not failed_writes:
        return ""
    shown = ", ".join(failed_writes[:_FAILED_WRITES_SHOWN])
    hidden = len(failed_writes) - _FAILED_WRITES_SHOWN
    more = f" (+{hidden} more)" if hidden > 0 else ""
    return (
        f"{ICON_WARNING} {len(failed_writes)} sync step(s) failed: {shown}{more}. "
        "See the log for the reason, then run the sync again.\n\n"
    )


def _plan_failure(plan_name: str, detail: str) -> str:
    """failed_writes entry for a function plan step that did not complete."""
    return f"function plan '{plan_name}': {detail}"


def _activation_note(ctx: "_SyncContext", plan_name: str, activated: bool) -> str:
    """Summary-line note for a plan (re)activation; a plan left stopped counts as failed."""
    if activated:
        return _NOTE_ACTIVATED
    ctx.failed_writes.append(_plan_failure(plan_name, "not activated"))
    return _NOTE_NOT_ACTIVATED


def _mark_cancelled(ctx: "_SyncContext", msg: str) -> str:
    """Flag a user-cancelled run as partial and put the cancel banner above its result.

    cancel_sync only stops further work (delta tasks / cluster-plan wiring loop) — whatever
    already ran before the flag was set is real and reported below it as-is. The banner and
    the "partial" sensor state tell the user the requested action did not run to its end.
    """
    ctx.failed_writes.append("cancelled by user")
    return f"{ICON_WARNING} **Sync cancelled by user — results below are partial.**\n\n{msg}"


def _items_of_class(seq: list[dict], cls: str) -> list[dict]:
    """Filter an audit-result list (missing/renamed/type/orphan) down to one Web-IO class."""
    return [i for i in seq if i.get("webio_class") == cls]


def _mmss(seconds: float) -> str:
    """Format a duration as m:ss."""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _post_result_notification(hass: HomeAssistant, notif_id: str, msg: str, title: str) -> None:
    """Post a run's final/result notification so it reliably surfaces as a new alert.

    `persistent_notification.async_create` silently degrades to an in-place update (HA core
    fires `UpdateType.UPDATED` instead of `UpdateType.ADDED`) whenever `notification_id` already
    exists — which the frontend does not reliably re-surface as something new to look at. Two
    ids can already exist here: `notif_id` itself (the live-progress notification this very run
    has been updating in place) and `{notif_id}_result` (an unread summary left over from a
    *previous* run, if the user never dismissed it) — both are dismissed before creating, so the
    result always lands as a genuinely new notification regardless of run history. Reported
    18.09.2026: a first version of this fix only dismissed `notif_id`, so it reliably helped the
    very first run after a manual dismissal but silently regressed to the original bug from the
    second run onward.
    """
    result_id = f"{notif_id}_result"
    persistent_notification.async_dismiss(hass, notif_id)
    persistent_notification.async_dismiss(hass, result_id)
    persistent_notification.async_create(hass, msg, title=title, notification_id=result_id)


def _sync_notification_title(server_id: str, *, is_error: bool, partial: bool) -> str:
    """Title of a sync notification: an aborted run, a partial one, or a normal/progress one."""
    if is_error:
        return "Comexio Sync Failed"
    if partial:
        return f"Comexio Sync Incomplete ({server_id})"
    return f"Comexio Sync ({server_id})"


def _skipped_check_line(check: str) -> str:
    """Function Plan block line for a mid-sync re-check that could not run.

    Without it a skipped check reads exactly like "nothing to wire"; the gap itself is not lost,
    the next poll re-audits it and raises the usual repair issue.
    """
    return (
        f"{ICON_WARNING} {check}: check skipped, Comexio's current state could not be read"
        " — re-checked on the next poll, see log"
    )


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
    err_note = f", {ICON_WARNING} {len(errors)} errors" if errors else ""
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
    if done != total and overall_done % _SYNC_PROGRESS_NOTIFY_EVERY:
        return  # throttle notification updates to every Nth pair
    now = time.monotonic()
    # ETA from the rate since the last update, not the run-wide average — a fixed per-plan
    # setup cost (backup, stop_fup, reload-wait) would otherwise skew early estimates high.
    # "last_t"/"last_overall" default to state["t0"]/0, so the very first update already
    # degenerates to the run-wide average on its own — no separate first-update branch needed.
    recent_elapsed = now - state.get("last_t", state["t0"])
    recent_count = overall_done - state.get("last_overall", 0)
    rate = recent_elapsed / recent_count if recent_count else 0.0
    remaining = rate * (overall_total - overall_done)
    state["last_t"] = now
    state["last_overall"] = overall_done
    ctx.update_status(
        f"{_plan_step_header(plan_name, state.get('action'))}"
        f"**Progress:** {state.get('unit', 'pair')} {overall_done} of {overall_total}{state.get('total_note', '')}"
        "\n\n---\n"
        f"{ICON_FLAG} **Remaining:** ~{_mmss(remaining)} min",
        pct=_PCT_PLAN_PAIRS + int((_PCT_PLAN_FINALIZE - _PCT_PLAN_PAIRS) * (overall_done / overall_total)),
        step_info=f"Function Plan '{plan_name}': pair {done}/{total}",
    )


def _plan_step_header(plan_name: str, action: str | None) -> str:
    """Notification header naming the plan and the sub-step currently running on it."""
    step = f"**Step:** {action}\n" if action else ""
    return f"**Function Plan:** `{plan_name}`\n{step}"


def _start_plan_step(ctx: "_SyncContext", state: dict, plan_name: str, action: str, step_info: str) -> None:
    """Record the sub-step a plan leg is starting and show it in the progress notification.

    Stored in the run-wide progress state so every throttled _plan_pair_progress update that
    follows keeps naming it — the per-pair message used to overwrite the leg's own start
    message, leaving the user unable to tell which part of the sync was running.
    """
    state["action"] = action
    ctx.update_status(_plan_step_header(plan_name, action), pct=_PCT_PLAN_PAIRS, step_info=step_info)


def _loopback_command_progress(
    ctx: "_SyncContext", plan_name: str, n: int, total: int, k_id: int, marker_id: int
) -> None:
    """Show which API-Loopback Web-IO command is being saved — each blocks ~SYNC_DURATION_WRITE s."""
    remaining = (total - n + 1) * SYNC_DURATION_WRITE
    ctx.update_status(
        f"{_plan_step_header(plan_name, 'creating API-Loopback Web-IO commands')}"
        f"**Progress:** command {n} of {total} (K{k_id} → M{marker_id})\n\n---\n"
        f"{ICON_CLOCK} Comexio needs about {SYNC_DURATION_WRITE} s per command\n"
        f"{ICON_FLAG} **Remaining:** ~{_mmss(remaining)} min",
        step_info=f"Function Plan '{plan_name}': loopback Web-IO command {n}/{total}",
    )


@dataclass
class _KnxPrestage:
    """Result of _prestage_knx_webio: work done once, before any KNX cluster plan is touched.

    allocated: {k_id: bridge marker_id} for every open bridge, so leg 2 only wires them.
    preembedded: loopback command names bulk-uploaded with a freshly created class, so
    leg 3 only confirms them instead of saving each one (~35-40 s per single save).
    """

    allocated: dict[int, int] = field(default_factory=dict)
    preembedded: set[str] = field(default_factory=set)


async def _delete_old_webio_class(api: Any, base_id: str, class_name: str, label: str) -> None:
    """Delete the class a recreate replaces; raises unless a lookup confirms it is gone.

    Uploading on top of a class that is still there would leave two classes of the same name
    behind — the sync aborts instead (reported by async_handle_press). Comexio's answer to the
    delete carries no verdict, so only the lookup shows the class is gone.
    """
    if not await api.delete_webio_base(base_id):
        raise RuntimeError(f"Deleting old Web-IO class failed ({label})")
    await asyncio.sleep(0.5)
    if await api.get_webio_base_info(class_name):
        raise RuntimeError(f"Old Web-IO class still present after deleting it ({label})")


async def _confirm_webio_device_created(api: Any, class_name: str, old_device_id: str | None, label: str) -> None:
    """Raise unless a lookup finds the device a recreate just created.

    create_webio_device's True only means Comexio accepted the request (a refusal answered as
    HTTP 200 reads the same). The old device of the same name must not pass for the new one.
    """
    found = await api.get_webio_device_info(class_name)
    if found is None or (old_device_id and str(found) == str(old_device_id)):
        raise RuntimeError(f"Device creation not confirmed ({label}, class created, device instance not found)")


class _SyncLoginError(HomeAssistantError):
    """The sync could not log in to Comexio — reported without a traceback."""


async def _ensure_sync_login(api: Any) -> None:
    """Log in again if the session lapsed since the last poll (e.g. after a Comexio reboot).

    Without this every request of the sync would hit the login form and fail.
    """
    if not await api.login():
        raise _sync_login_error(api.last_login_error)


def _sync_login_error(reason: str | None) -> _SyncLoginError:
    """Why a sync could not start, from ComexioAPI.last_login_error."""
    if reason == "rejected":
        return _SyncLoginError(
            "Comexio rejected the admin login — please check the credentials (reconfigure the integration)."
        )
    return _SyncLoginError(
        "Comexio admin login failed — the server is not reachable or not answering. Please try again later."
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
    # Marker list fetched fresh mid-sync (async_fresh_trigger_audit) — coordinator.data stays
    # frozen while in_sync, so it lacks the bridge Markers this same sync created.
    fresh_markers: list[dict[str, Any]] | None = None
    # Parsed config of the KNX step's fresh audit, kept only when that step wrote nothing to
    # Comexio — the trigger step then reuses it instead of fetching config + plans again.
    unchanged_config_snapshot: dict[str, Any] | None = None
    # Steps that did not complete: Web-IO writes Comexio rejected or never answered (the API
    # adapter logged why), function plan wiring errors, cleanup deletions and a user cancel.
    # Named in the result message and turn the status sensor to "partial".
    failed_writes: list[str] = field(default_factory=list)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio sync button."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sync_button = ComexioSyncButton(coordinator, coordinator.server_id)
    cancel_button = ComexioCancelSyncButton(coordinator, coordinator.server_id)
    migration_button = ComexioEntityIdMigrationButton(coordinator, coordinator.server_id)
    stats_cleanup_button = ComexioStatisticsCleanupButton(coordinator, coordinator.server_id)
    fw_check_button = ComexioFirmwareCheckButton(coordinator, coordinator.server_id)
    webio_range_check_button = ComexioWebioRangeCheckButton(coordinator, coordinator.server_id)
    preview_button = ComexioPlanPreviewButton(coordinator, coordinator.server_id)
    uninstall_cleanup_button = ComexioCleanupButton(coordinator, coordinator.server_id)
    plan_toggle_button = ComexioPlanToggleButton(coordinator, coordinator.server_id)
    entities: list[Any] = [
        sync_button,
        cancel_button,
        migration_button,
        stats_cleanup_button,
        fw_check_button,
        webio_range_check_button,
        preview_button,
        uninstall_cleanup_button,
        plan_toggle_button,
    ]

    conf = {**entry.data, **entry.options}
    # Trigger ("virtueller Taster") buttons for every trigger-capable source category
    # (Marker + KNX — registry-driven, see const.trigger_pair_categories).
    for category in trigger_pair_categories():
        if not conf.get(category.import_conf_key, category.import_default):
            continue
        ignored_ids = coordinator.ignored_ids_for(category.key)
        button_cls = _TRIGGER_BUTTON_CLASSES[category.key]
        entities.extend(
            button_cls(coordinator, coordinator.server_id, src)
            for src in coordinator.data.get(category.data_key, [])
            if src.get("kind") == MarkerKind.TRIGGER and int(src["id"]) not in ignored_ids
        )

    async_add_entities(entities)

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
                    "cleanup_entities",
                    "knx_bridge_add_missing",
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
        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        notify_enabled = conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS)
        notif_id = f"comexio_sync_{self.server_id}"
        if self.coordinator._sync_lock.locked():
            _LOGGER.warning("[%s] Sync already in progress, ignoring concurrent request", self.server_id)
            if notify_enabled:
                # Own notification_id — notif_id is the *in-progress* sync's live-progress
                # notification, and reusing it here would clobber that progress display.
                persistent_notification.async_create(
                    self.hass,
                    f"{ICON_WARNING} Sync request ignored — a sync or Web-IO range check is already "
                    "running. Please try again shortly.",
                    title=f"Comexio Sync ({self.server_id})",
                    notification_id=f"comexio_sync_blocked_{self.server_id}",
                )
            return
        await self.coordinator._sync_lock.acquire()
        if self.hass.data[DOMAIN].get(self.coordinator.config_entry.entry_id) is not self.coordinator:
            # A reload (coordinator.async_reload_entry holds this lock through it) finished
            # while this press waited: this coordinator and its API session are torn down.
            self.coordinator._sync_lock.release()
            _LOGGER.warning("[%s] Sync request dropped — the integration was reloaded meanwhile", self.server_id)
            if notify_enabled:
                persistent_notification.async_create(
                    self.hass,
                    f"{ICON_WARNING} Sync request ignored — the integration was reloaded while it waited. "
                    "Please start the sync again.",
                    title=f"Comexio Sync ({self.server_id})",
                    notification_id=f"comexio_sync_blocked_{self.server_id}",
                )
            return
        self.coordinator.in_sync = True
        self.coordinator.sync_error = False
        self.coordinator.sync_failed_writes = []

        update_status = partial(self._update_sync_status, notify_enabled, notif_id)

        update_status("Initializing sync process...", pct=0, step_info="Initializing")
        start_time = datetime.datetime.now()
        api = self.coordinator.api
        webio_name = self.coordinator.config_entry.data.get("webio_name", "HomeAssistant")
        class_names = {cls: webio_class_name(webio_name, cls) for cls in WEBIO_CLASSES}
        ctx: _SyncContext | None = None

        try:
            update_status("Analyzing Comexio configuration...", pct=5, step_info=_STEP_ANALYZING_CONFIG)

            await _ensure_sync_login(api)

            # Check if the Web-IO device instances are already present (one per class). Reported
            # per class (not just once before the loop) because this duration varies a lot with
            # Comexio server responsiveness, and the notification otherwise sits on the same
            # static text for however long that turns out to take.
            dev_ids: dict[str, str | None] = {}
            for cls in WEBIO_CLASSES:
                update_status(
                    f"Analyzing Comexio configuration — checking Web-IO device '{class_names[cls]}'...",
                    pct=5,
                    step_info=_STEP_ANALYZING_CONFIG,
                )
                dev_ids[cls] = await api.get_webio_device_info(class_names[cls])

            # Get the current network address of this Home Assistant instance — normally cached by
            # the coordinator's audit; only a cold resolver searches the KNOWN_DOMAINS names (in parallel).
            update_status(
                "Analyzing Comexio configuration — resolving Home Assistant network address...",
                pct=5,
                step_info=_STEP_ANALYZING_CONFIG,
            )
            ha_address = await self.coordinator.async_ha_address()

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
            cleanup_entity_ids: list[tuple[str, int]] = audit_data.get("cleanup_entities", [])
            lp_fub_id = self.coordinator.get_active_function_plan_fub_id()

            if action == "cleanup_entities":
                # Standalone action: remove HA entities + Function Plan wiring + WebIO commands
                # for ignored markers/KNX objects that still have legacy remnants.
                await self._handle_cleanup_entities(
                    ctx, cleanup_entity_ids, dev_ids, notif_id, notify_enabled, lp_fub_id
                )
                return

            if action == "function_plan_add_missing":
                # Standalone action: wire pre-existing but unwired pairs only, no Web-IO sync.
                plan_summary = await self._wire_created_pairs(ctx, [], gap_items)
                plan_summary += await self._wire_trigger_pairs(ctx)
                duration = datetime.datetime.now() - start_time
                msg = self._build_function_plan_add_missing_message(plan_summary, duration, ctx.failed_writes)
            elif action == "knx_bridge_add_missing":
                # Standalone action: complete every open KNX write-path leg for every KNX
                # object that still lacks one — write-path bridge Marker, API-Loopback fan-out,
                # AND (decided 18.09.2026: a bridge with an incomplete K-Element left behind
                # isn't "fixed") the K -> Web-IO read path — in one combined stop/write/finalize
                # cycle per cluster (_wire_knx_full). refresh_audit=True re-audits bridge/
                # loopback against Comexio's *current* config instead of trusting
                # last_audit_results; the read-path leg reuses the already-fetched
                # function_plan_missing gap_items, same as function_plan_add_missing above.
                plan_summary = await self._wire_knx_full(ctx, [], gap_items, refresh_audit=True)
                duration = datetime.datetime.now() - start_time
                msg = self._build_function_plan_add_missing_message(plan_summary, duration, ctx.failed_writes)
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
                    debris_removed,
                ) = await self._sync_all_classes(ctx, audit_data, dev_ids)

                # KNX's read-path candidates are carved out of both inputs here and handed to
                # _wire_knx_full instead, which wires them together with the bridge/loopback
                # legs in one combined cycle per cluster — leaving them in what's passed to
                # _wire_created_pairs would wire the same K -> Web-IO pairs a second time
                # (_wire_created_pairs' own _classify_created_names buckets KNX-named Web-IO
                # commands from created_names exactly like _wire_knx_full does, and its gap-item
                # merge does the same for gap_items — both need the KNX slice removed).
                knx_prefix = SOURCE_CATEGORIES[WebioClass.KNX].audit_key_prefix
                knx_gap_key = f"{SOURCE_CATEGORIES[WebioClass.KNX].key.value}_id"
                created_names_no_knx = [
                    name for name in created_names if _parse_source_id_from_webio_name(name, knx_prefix) is None
                ]
                gap_items_no_knx = [item for item in gap_items if knx_gap_key not in item]
                # _wire_knx_full must run BEFORE _wire_trigger_pairs: a KNX [TRIG]/[TP] source's
                # self-reset pair is now wired via its write-path bridge Marker (see
                # _resolve_knx_trigger_bridge_markers), which _wire_knx_full's leg 2 is what
                # creates in the first place. Running trigger-pairs first left that bridge
                # missing and the trigger pair unwired every time (live 2026-09-21: "K2: no
                # write-path bridge marker yet, cannot wire trigger pair").
                # Each step re-reads Comexio (the KNX step a fresh config + plan snapshot, several
                # seconds; the trigger step reuses it when the KNX step wrote nothing, otherwise
                # fetches its own) and reports nothing when there is nothing to wire — without
                # these lines the status sat on the last analysis step for the whole wiring pass.
                update_status(
                    "Checking function plan wiring of the Web-IO commands...",
                    pct=_PCT_PLAN_PAIRS,
                    step_info=_STEP_CHECKING_PLAN,
                )
                plan_summary = await self._wire_created_pairs(ctx, created_names_no_knx, gap_items_no_knx)
                update_status(
                    "Checking KNX write path and wiring...",
                    pct=_PCT_PLAN_PAIRS,
                    step_info=_STEP_CHECKING_PLAN,
                )
                plan_summary += await self._wire_knx_full(ctx, created_names, gap_items, refresh_audit=True)
                update_status(
                    "Checking trigger pairs...",
                    pct=_PCT_PLAN_PAIRS,
                    step_info=_STEP_CHECKING_PLAN,
                )
                plan_summary += await self._wire_trigger_pairs(ctx, refresh_audit=True)

                duration = datetime.datetime.now() - start_time
                duration_str = f"{_mmss(duration.total_seconds())} min"
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
                    debris_removed,
                    ctx.failed_writes,
                )
                if plan_summary:
                    msg += "\n\n**Function Plan:**\n" + "\n".join(plan_summary)

            if self.coordinator.cancel_sync:
                msg = _mark_cancelled(ctx, msg)

            self.coordinator.last_audit_failed = False
            self.coordinator.sync_failed_writes = list(ctx.failed_writes)
            update_status(msg, pct=100, step_info="Done", final=True)

        except Exception as e:
            self._report_sync_failure(e, ctx, update_status)

        finally:
            await self._finalize_sync()

    def _report_sync_failure(
        self, error: Exception, ctx: _SyncContext | None, update_status: Callable[..., None]
    ) -> None:
        """Log and report the error that aborted the sync, with the writes that failed before it.

        Called from the except block of async_handle_press, so the traceback is still available.
        """
        if isinstance(error, _SyncLoginError):
            _LOGGER.error("[%s] Sync not started: %s", self.server_id, error)
        else:
            _LOGGER.exception("[%s] Sync failed", self.server_id)
        self.coordinator.in_sync = False
        self.coordinator.sync_error = True
        # Writes that already failed before the abort would otherwise vanish behind "Error: ...".
        if ctx is not None:
            self.coordinator.sync_failed_writes = list(ctx.failed_writes)
        failed_note = _failed_writes_note(self.coordinator.sync_failed_writes)
        update_status(f"Error: {error}\n\n{failed_note}".rstrip(), is_error=True, final=True)

    def _update_sync_status(
        self,
        notify_enabled: bool,
        notif_id: str,
        msg: str,
        is_error: bool = False,
        pct: int | None = None,
        step_info: str | None = None,
        final: bool = False,
    ) -> None:
        """Update coordinator sync-progress state and optionally show a UI notification.

        `final=True` marks the last call of a run (the completed summary or a fatal error).
        Every other call in a run reuses the same `notif_id` to update one live-progress
        notification in place rather than spamming a new one per step — but that also means a
        plain reuse for the summary only rewrites a notification the user may already have
        read/dismissed mid-run, and HA's frontend does not re-surface an in-place update as a
        new alert. See `_post_result_notification` for how the final call avoids that (reported
        by user 18.09.2026 — sync finished but no summary notification was seen).
        """
        self.coordinator.sync_progress_text = msg
        if pct is not None:
            self.coordinator.sync_progress_pct = pct
        if step_info is not None:
            self.coordinator.sync_current_step = step_info
        self.coordinator.async_set_updated_data(self.coordinator.data)
        if notify_enabled:
            partial = final and bool(self.coordinator.sync_failed_writes)
            title = _sync_notification_title(self.server_id, is_error=is_error, partial=partial)
            if final:
                _post_result_notification(self.hass, notif_id, msg, title)
            else:
                persistent_notification.async_create(self.hass, msg, title=title, notification_id=notif_id)

    async def _sync_all_classes(
        self, ctx: _SyncContext, audit_data: dict[str, Any], dev_ids: dict[str, str | None]
    ) -> tuple[int, int, int, int, bool, list[str], int, dict[str, dict[str, int]], list[str], int]:
        """Run `_sync_class` for every Web-IO class and aggregate the results.

        The trailing element is the flat list of Web-IO command names created in this run
        (across all classes) — the input of the managed cluster plan wiring pass. The final
        element is the total count of Function Plan debris elements removed (dangling plan
        elements + orphan-unwiring), a separate concern from the Web-IO command counters.
        """
        missing_items = audit_data.get("missing", [])
        renamed_items = audit_data.get("rename", [])
        type_mismatches = audit_data.get("type", [])
        orphans = audit_data.get("orphan", [])
        dangling_items = audit_data.get("function_plan_dangling", [])

        added, removed, updated, renamed = 0, 0, 0, 0
        updated_ip = False
        recreated_classes: list[str] = []
        skipped_creates = 0
        debris_removed = 0
        per_class: dict[str, dict[str, int]] = {}
        created_names: list[str] = []
        # Sync a class if the user opted into it, OR if its Web-IO device still exists on the
        # server (a class deselected after a prior sync — its orphaned commands must still be
        # delta-synced away; a full sync would otherwise never touch it). An opted-out class
        # with no server device is skipped entirely — _decide_effective_action force-"recreate"s
        # any class with no device, so including it would create it on the live server.
        opted_in = set(self.coordinator.active_webio_classes)
        active_classes = tuple(c for c in WEBIO_CLASSES if c in opted_in or dev_ids.get(c))
        if not active_classes:
            return 0, 0, 0, 0, False, [], 0, {}, [], 0
        # Split the shared progress span evenly across however many classes are active,
        # so the UI bar advances smoothly regardless of len(active_classes).
        pct_span = (SYNC_PROGRESS_END_PCT - SYNC_PROGRESS_START_PCT) / len(active_classes)
        class_pct_ranges = {
            cls: (
                round(SYNC_PROGRESS_START_PCT + idx * pct_span),
                round(SYNC_PROGRESS_START_PCT + (idx + 1) * pct_span),
            )
            for idx, cls in enumerate(active_classes)
        }

        for cls in active_classes:
            pct_start, pct_end = class_pct_ranges[cls]
            cls_result = await self._sync_class(
                ctx,
                cls,
                dev_ids[cls],
                _items_of_class(missing_items, cls),
                _items_of_class(renamed_items, cls),
                _items_of_class(type_mismatches, cls),
                _items_of_class(orphans, cls),
                _items_of_class(dangling_items, cls),
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
            debris_removed += cls_result["debris_removed"]
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
            debris_removed,
        )

    @staticmethod
    def _build_function_plan_add_missing_message(
        plan_summary: list[str], duration: datetime.timedelta, failed_writes: list[str] | None = None
    ) -> str:
        """Compose the result notification for the standalone Function Plan wiring action."""
        body = "\n".join(plan_summary) if plan_summary else "Nothing to do — all pairs were already wired."
        duration_str = f"{_mmss(duration.total_seconds())} min"
        failed_note = _failed_writes_note(failed_writes or [])
        headline = (
            f"{ICON_WARNING} **Function Plan update finished with errors**"
            if failed_note
            else f"{ICON_SUCCESS} **Function Plan update finished**"
        )
        return f"{headline}\n\n{failed_note}{body}\n\n{ICON_DURATION} Total duration: {duration_str}"

    async def _handle_cleanup_entities(
        self,
        ctx: _SyncContext,
        entity_ids: list[tuple[str, int] | int],
        dev_ids: dict[str, str | None],
        notif_id: str,
        notify_enabled: bool,
        lp_fub_id: int | None = None,
    ) -> None:
        """Remove HA entities, Function Plan elements and WebIO commands for ignored markers/KNX objects.

        Deletions that failed land in ctx.failed_writes; the caller turns them into a partial sync.
        """

        def _notify(msg: str) -> None:
            # Every call here is this action's terminal result (no separate progress phase of
            # its own) — reusing notif_id in place has the same not-surfaced-as-new problem
            # _update_sync_status's final=True path fixes, see _post_result_notification.
            if notify_enabled:
                title = f"Comexio Cleanup{' Incomplete' if ctx.failed_writes else ''} ({self.server_id})"
                _post_result_notification(self.hass, notif_id, msg, title)

        if not entity_ids:
            # Reachable via a direct press_action service call with no pending audit gap — the
            # repair-flow UI only ever offers this action when entity_ids is non-empty. Give
            # explicit feedback instead of silently no-op-ing, consistent with the sibling
            # function_plan_add_missing action's "Nothing to do." fallback.
            _notify(
                "Nothing to clean up — no ignored markers or KNX objects currently have leftover entities to remove."
            )
            _LOGGER.info("[%s] cleanup_entities: nothing to clean up (no entity_ids)", self.server_id)
            return

        # Legacy audit snapshots (pre-KNX) stored bare marker ids; tolerate that shape too.
        grouped: dict[WebioClass, list[int]] = {}
        for entry in entity_ids:
            cls, eid = (WEBIO_CLASS_MARKER, entry) if isinstance(entry, int) else entry
            grouped.setdefault(WebioClass(cls), []).append(eid)

        # entity_ids come from a possibly-stale audit snapshot; a marker/KNX object could have
        # been un-ignored in the meantime (between the audit poll and this button press).
        # Restrict the destructive operations below to ids that are still actually ignored.
        all_lines: list[str] = []
        for cls, ids in grouped.items():
            category = source_category(cls)
            ignored_ids = self.coordinator.ignored_ids_for(cls)
            still_ignored = [eid for eid in ids if eid in ignored_ids]
            if not still_ignored:
                continue
            all_lines.extend(
                await self._cleanup_entities_for_category(ctx, category, still_ignored, dev_ids.get(cls), lp_fub_id)
            )

        if not all_lines:
            _notify("Nothing to clean up — the markers/KNX objects flagged by the last audit are no longer ignored.")
            _LOGGER.info("[%s] cleanup_entities: no entity_ids still ignored, skipping", self.server_id)
            return

        # The sensor turns "partial" and its progress_details keep the report (see _finalize_sync).
        self.coordinator.sync_failed_writes = list(ctx.failed_writes)
        if ctx.failed_writes:
            self.coordinator.sync_progress_text = "\n".join(all_lines)
        _notify("\n".join(all_lines))
        _LOGGER.info("[%s] cleanup_entities done: %s", self.server_id, ", ".join(all_lines))

    async def _cleanup_entities_for_category(
        self, ctx: _SyncContext, category: SourceCategory, ids: list[int], dev_id: str | None, lp_fub_id: int | None
    ) -> list[str]:
        """Run the entity/Function-Plan/WebIO cleanup for one source category; return summary lines."""
        api = ctx.api
        deleted_entities = self._delete_source_entities(ids, category)
        lp_count, webio_cmd_ids, stopped_plans, stop_failures = await self._cleanup_function_plan_plans(
            api, ids, lp_fub_id, category
        )
        webio_removed, webio_failed = await self._delete_webio_commands(api, dev_id, webio_cmd_ids, category)

        lines = self._build_cleanup_summary_lines(
            ids,
            deleted_entities,
            lp_count,
            webio_removed,
            webio_failed,
            category,
            has_stop_failures=bool(stop_failures),
        )
        lines.extend(self._notify_stopped_plans(stopped_plans))
        lines.extend(self._build_stop_failure_lines(stop_failures))
        if webio_failed:
            ctx.failed_writes.append(f"cleanup {category.label}: {webio_failed} Web-IO command deletion(s)")
        ctx.failed_writes.extend(_plan_failure(name, "left stopped after cleanup") for name, _ in stopped_plans)
        ctx.failed_writes.extend(_plan_failure(name, "not cleaned up (not stopped)") for name, _ in stop_failures)
        return lines

    def _delete_source_entities(self, ids: list[int], category: SourceCategory) -> int:
        """Remove HA entities for the given marker/KNX ids; return the deleted-entity count."""
        registry = er.async_get(self.hass)
        source_entities = self.coordinator.marker_entities_by_id(ids, category.unique_id_infix)
        deleted_entities = 0
        # source_entities is our own dict, not the live registry.entities mapping, so
        # registry.async_remove() below mutating the registry doesn't affect this iteration.
        for source_id, entity in source_entities.items():
            registry.async_remove(entity.entity_id)
            deleted_entities += 1
            _LOGGER.info(
                "[%s] Removed entity %s for ignored %s %s%d",
                self.server_id,
                entity.entity_id,
                category.label,
                category.audit_key_prefix,
                source_id,
            )
        return deleted_entities

    async def _delete_webio_commands(
        self, api: Any, dev_id: str | None, webio_cmd_ids: list[Any], category: SourceCategory
    ) -> tuple[int, int]:
        """Delete the given WebIO commands from dev_id; return (removed, failed) counts.

        A missing dev_id with pending webio_cmd_ids counts as failed (not a silent (0, 0)) —
        the commands genuinely could not be deleted, and _build_cleanup_summary_lines only
        surfaces a warning line when webio_failed is nonzero.
        """
        if not dev_id:
            if webio_cmd_ids:
                _LOGGER.warning(
                    "[%s] Could not delete %d WebIO command(s) — no %s Web-IO device instance",
                    self.server_id,
                    len(webio_cmd_ids),
                    category.label,
                )
            return 0, len(webio_cmd_ids)
        webio_removed, webio_failed = 0, 0
        for cmd_id in webio_cmd_ids:
            ok = await api.delete_single_command(cmd_id, dev_id)
            if ok:
                webio_removed += 1
            else:
                webio_failed += 1
                _LOGGER.warning(
                    "[%s] Could not delete WebIO command id=%s after Function Plan cleanup",
                    self.server_id,
                    cmd_id,
                )
        return webio_removed, webio_failed

    @staticmethod
    def _build_cleanup_summary_lines(
        ids: list[int],
        deleted_entities: int,
        lp_count: int,
        webio_removed: int,
        webio_failed: int,
        category: SourceCategory,
        has_stop_failures: bool = False,
    ) -> list[str]:
        """Build the base result lines for the cleanup-entities summary notification."""
        ids_str = ", ".join(f"{category.audit_key_prefix}{eid}" for eid in ids)
        if (
            deleted_entities == 0
            and lp_count == 0
            and webio_removed == 0
            and webio_failed == 0
            and not has_stop_failures
        ):
            return [f"Nothing to clean up for {ids_str} — no entities, Function Plan elements or WebIO commands found."]
        lines = [
            f"{ICON_CHECK} {deleted_entities} entities removed ({ids_str})",
            f"{ICON_CHECK} {lp_count} Function Plan elements removed",
            f"{ICON_CHECK} {webio_removed} WebIO commands deleted",
        ]
        if webio_failed:
            lines.append(f"{ICON_WARNING} {webio_failed} WebIO deletions failed (may still be in use)")
        return lines

    def _notify_stopped_plans(self, stopped_plans: list[tuple[str, int]]) -> list[str]:
        """Send a dedicated notification per stopped plan; return summary lines for them."""
        extra_lines = []
        for plan_name_s, fub_id_s in stopped_plans:
            extra_lines.append(
                f"{ICON_WARNING} Function Plan '{plan_name_s}' (ID {fub_id_s}) was stopped — "
                "please restart it in Comexio"
            )
            persistent_notification.async_create(
                self.hass,
                f"Function Plan **'{plan_name_s}'** (ID {fub_id_s}) was stopped during entity "
                "cleanup and was **not** automatically restarted.\n\n"
                "Please restart it in Comexio so that live updates "
                "(webhooks) are sent to Home Assistant again.",
                title=f"Comexio: Function Plan stopped ({self.server_id})",
                notification_id=f"comexio_function_plan_stopped_{self.server_id}_{fub_id_s}",
            )
        return extra_lines

    @staticmethod
    def _build_stop_failure_lines(stop_failures: list[tuple[str, int]]) -> list[str]:
        """Build summary lines for plans that couldn't be stopped, so the failure isn't
        silently indistinguishable from "nothing to clean up" (deleted_elem_count also 0)."""
        return [
            f"{ICON_ERROR} Function Plan '{plan_name_s}' (ID {fub_id_s}) could not be stopped — "
            "cleanup skipped for this plan, no changes were made there"
            for plan_name_s, fub_id_s in stop_failures
        ]

    @staticmethod
    def _resolve_plan_webio_and_unwired(
        api: Any, plan_data: dict, cleanup_ids: list[int], ref_type: int = 2
    ) -> tuple[list[int], list[int]]:
        """For each marker in cleanup_ids, collect its wired WebIO ids in this plan — or,
        if it has no wiring at all, its element id for direct deletion (see
        _delete_unwired_marker_elements). A marker wired to something OTHER than a WebIO
        (a timer, logic block, etc.) is left untouched entirely: it still participates in
        plan logic unrelated to this ignored-marker Web-IO cleanup, so it's neither queued
        for unwiring (nothing WebIO-related to unwire) nor for deletion (would break that
        other wiring).
        """
        webio_ids: list[int] = []
        unwired_marker_elem_ids: list[int] = []
        for marker_id in cleanup_ids:
            marker_elem_id = api._find_source_element_id(plan_data.get("elements", {}), marker_id, ref_type)
            if not marker_elem_id:
                continue
            marker_webio_ids = api._find_wired_webio_ids_for_marker(marker_id, marker_elem_id, plan_data)
            if marker_webio_ids:
                webio_ids.extend(marker_webio_ids)
            elif not api._element_has_any_wiring(marker_elem_id, plan_data):
                unwired_marker_elem_ids.append(int(marker_elem_id))
        return webio_ids, unwired_marker_elem_ids

    @staticmethod
    async def _delete_unwired_marker_elements(
        api: Any, fub_id: int, unwired_marker_elem_ids: list[int]
    ) -> tuple[int, tuple[str, int] | None, tuple[str, int] | None]:
        """Delete marker elements with no WebIO counterpart directly — unwire_webio_commands
        only ever touches elements it finds wired to one of the given webio_ids, so a marker
        with no wiring at all would otherwise be left behind in the plan forever.

        Returns (deleted element count, stopped_plan or None, stop_failure or None).
        """
        result = await api._delete_plan_elements_and_restart(
            fub_id, unwired_marker_elem_ids, [], api.function_plan_name(fub_id)
        )
        stopped_plan = None
        stop_failure = None
        if result.get("stop_failed"):
            stop_failure = (result.get("plan_name", "?"), fub_id)
        elif result.get("plan_stopped") and result.get("fub_id") is not None:
            stopped_plan = (result.get("plan_name", "?"), result["fub_id"])
        return result.get("deleted_elem_count", 0), stopped_plan, stop_failure

    async def _cleanup_function_plan_plans(
        self, api: Any, source_ids: list[int], lp_fub_id: int | None, category: SourceCategory
    ) -> tuple[int, list[int], list[tuple[str, int]], list[tuple[str, int]]]:
        """Run the Function Plan cleanup for every managed plan the markers/KNX objects are wired in.

        Returns (deleted element count, WebIO command ids to delete, stopped plans, stop
        failures — the latter two as (name, fub_id) tuples).
        """
        ref_type = int(category.fub_module_type)
        plan_to_ids = await self.coordinator.resolve_source_cleanup_plans(source_ids, lp_fub_id, ref_type)
        lp_count = 0
        webio_cmd_ids: list[int] = []
        stopped_plans: list[tuple[str, int]] = []
        stop_failures: list[tuple[str, int]] = []
        for fub_id, cleanup_ids in plan_to_ids.items():
            await self.coordinator.async_function_plan_change_backup(
                fub_id, f"cleanup_ignored {[f'{category.audit_key_prefix}{m}' for m in cleanup_ids]}"
            )
            # Resolve which WebIO commands are actually wired to these markers/KNX objects in this
            # plan, then hand off to the same unwire mechanism the orphan-delete sync path uses —
            # it owns the webIoId -> real cmdId resolution, so callers never have to guess it
            # out of a plan element's ref_id (that field IS the webIoId, not the WebCommandId).
            plan_data = await api.function_plan_load_elements(fub_id)
            webio_ids: list[int] = []
            unwired_marker_elem_ids: list[int] = []
            if plan_data:
                webio_ids, unwired_marker_elem_ids = self._resolve_plan_webio_and_unwired(
                    api, plan_data, cleanup_ids, ref_type
                )
            if unwired_marker_elem_ids:
                deleted, stopped_plan, stop_failure = await self._delete_unwired_marker_elements(
                    api, fub_id, unwired_marker_elem_ids
                )
                lp_count += deleted
                if stop_failure:
                    stop_failures.append(stop_failure)
                if stopped_plan:
                    stopped_plans.append(stopped_plan)
            if not webio_ids:
                continue
            unwired = await self.coordinator.unwire_webio_commands(webio_ids, preferred_fub_id=fub_id)
            lp_count += unwired["deleted_elem_count"]
            webio_cmd_ids.extend(unwired["cmd_ids"])
            stopped_plans.extend(unwired["stopped_plans"])
            stop_failures.extend(unwired["stop_failures"])
        # A WebIO command could in principle be collected from more than one plan (e.g. a
        # marker anomalously wired into multiple managed plans); dedupe before deletion so
        # the same command isn't attempted twice.
        return lp_count, list(dict.fromkeys(webio_cmd_ids)), stopped_plans, stop_failures

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
        debris_removed: int = 0,
        failed_writes: list[str] | None = None,
    ) -> str:
        """Compose the final sync-result notification text."""
        failed_note = _failed_writes_note(failed_writes or [])
        recreate_note = ""
        if recreated_classes:
            recreated_str = ", ".join(webio_class_label(c) for c in recreated_classes)
            recreate_note = f"{ICON_ROCKET} Recreated: {recreated_str}\n\n"

        skip_note = ""
        if skipped_creates:
            skip_note = (
                f"{ICON_WARNING} Skipped {skipped_creates} creation(s) — Web-IO base class missing. "
                "Run a Full Sync to recreate it.\n\n"
            )

        debris_note = ""
        if debris_removed:
            debris_note = f"{ICON_CHECK} {debris_removed} Function Plan debris element(s) removed\n\n"

        changed = added + updated + renamed + removed
        if changed == 0 and not recreated_classes and updated_ip and not skipped_creates and not failed_note:
            return (
                f"{ICON_SUCCESS} **Comexio Server Address updated**\n\n{debris_note}"
                f"The IP address has been successfully updated in the Web-IO device(s).\n"
                f"{ICON_DURATION} Duration: {duration_str}"
            )
        if changed == 0 and recreated_classes and not updated_ip and not skipped_creates and not failed_note:
            return (
                f"{ICON_SUCCESS} **Comexio Recreation Finished**\n\n{recreate_note}{debris_note}"
                f"{ICON_DURATION} Duration: {duration_str}"
            )
        # With a failed write something WAS needed — it just did not get applied.
        nothing_done = "no changes applied" if failed_note else "no changes needed"
        headline = (
            f"{ICON_WARNING} **Comexio Sync Finished with errors**"
            if failed_note
            else f"{ICON_SUCCESS} **Comexio Sync Finished**"
        )
        return (
            f"{headline}\n\n{failed_note}{recreate_note}{skip_note}{debris_note}"
            f"Results: {_format_counts(added, updated, renamed, removed) if changed else nothing_done}"
            + (", IP-Address updated" if updated_ip else "")
            + f".\n{self._build_per_class_note(per_class)}{ICON_DURATION} Duration: {duration_str}"
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
            f"  • {webio_class_label(cls)}: {_format_counts(c['added'], c['updated'], c['renamed'], c['removed'])}"
            for cls, c in changed_classes.items()
        ]
        return "\n".join(lines) + "\n"

    async def _finalize_sync(self) -> None:
        """Reset sync flags/UI state and force a full integration reload after a press."""
        # _sync_lock stays held until the reload below has finished: releasing it first left
        # a >=0.5 s window in which a new sync (press_action, automation, UI) could start on
        # this coordinator and have its API session closed by the reload — the exact
        # "Session is closed" failure async_reload_entry guards against (review 28.09.2026).
        # The finally guarantees the release on every path; the inner guards make sure a
        # failing reset step can never skip the reload that covers every reload
        # coordinator.async_reload_entry skipped during this sync.
        try:
            try:
                # 1. Flag reset
                self.coordinator.in_sync = False
                self.coordinator.cancel_sync = False
                # An aborted or partial run keeps its report as the sensor's progress_details.
                if not (self.coordinator.sync_error or self.coordinator.sync_failed_writes):
                    self.coordinator.sync_progress_text = "Idle"
                self.coordinator.sync_progress_pct = None
                self.coordinator.sync_current_step = None
                self.coordinator.async_set_updated_data(self.coordinator.data)

                # 2. Reset audit_ignored. Set the skip flag first so the update_listener
                #    suppresses its reload — the explicit reload below is the single reload (R2).
                new_options = dict(self.coordinator.config_entry.options)
                new_options["audit_ignored"] = False
                self.coordinator.request_options_update_without_reload(new_options)
            except Exception:
                _LOGGER.exception("[%s] Resetting the sync state failed — reloading anyway", self.server_id)

            # 3. Give the Comexio server a moment to finish the write operation;
            #    the update_listener task runs here, sees the skip flag, and returns. (R2)
            await asyncio.sleep(0.5)

            # 4. Reset UI button
            try:
                self.async_write_ha_state()
            except Exception:
                _LOGGER.exception("[%s] Could not reset the sync button state — reloading anyway", self.server_id)

            # 5. Restart integration (necessary!)
            _LOGGER.info("[%s] Forcing integration reload after sync...", self.server_id)
            await self.hass.config_entries.async_reload(self.coordinator.config_entry.entry_id)
        finally:
            # 6. Release the (old) coordinator's lock — the reload built a new coordinator
            #    with its own lock; a press that waited on this one is dropped by
            #    async_handle_press' coordinator-identity check.
            self.coordinator._sync_lock.release()

    async def _decide_effective_action(
        self,
        ctx: _SyncContext,
        cls: str,
        class_dev_id: str | None,
        cls_missing: list[dict],
        cls_renamed: list[dict],
        cls_types: list[dict],
        cls_orphans: list[dict],
        dev_ip_mismatch: bool,
    ) -> str:
        """Pick recreate vs. delta-sync for one class, based on ETA vs. the Fast-Track threshold."""
        label = webio_class_label(cls)
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
            # Comexio's answer carries no verdict — only the lookup shows the device is gone. A
            # device left behind would make the recreate upload a second class next to it.
            error = await ctx.api.webio_device_delete_error(class_dev_id) or await webio_still_present(
                ctx.api.get_webio_device_info, ctx.class_names[cls]
            )
            if not error:
                _LOGGER.info("[%s] %s Fast-Track enabled: device has been deleted.", self.server_id, label)
                return "recreate"
            _LOGGER.info("[%s] %s device not deleted (%s). Falling back to Delta-Sync.", self.server_id, label, error)
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
        # Built up front (pure, no network) so the status can name the command count — the
        # upload itself gives no progress signal of its own.
        web_io_json = api.generate_webio_json(
            self.server_id,
            class_name,
            self.coordinator.data,
            webio_class=cls,
            ignored_marker_ids=self.coordinator.ignored_marker_ids,
            ignored_knx_ids=self.coordinator.ignored_knx_ids,
        )
        n_commands = len(json.loads(web_io_json)["commands"])
        status_msg = (
            f"{ICON_TOOLS} **Initial Setup ({label})**\nCreating Web-IO class with {n_commands} command(s)..."
            if ctx.action == "full_sync" and not class_dev_id
            else f"{ICON_ROCKET} **Fast-Track active ({label})**\n"
            f"High-speed recreation of {n_commands} command(s) in progress..."
        )
        ctx.update_status(status_msg, pct=pct_start, step_info=f"Creating Web-IO class ({label})")

        base_info = await api.get_webio_base_info(class_name)
        if base_info:
            base_id, base_deletable = base_info
            if base_deletable:
                ctx.update_status(
                    f"{status_msg}\n\n{ICON_DELETE} Deleting old class...",
                    pct=pct_start + 2,
                    step_info="Deleting old class",
                )
                # Uploading on top of a class that failed to delete would leave two classes
                # of the same name behind — abort instead (reported by async_handle_press).
                await _delete_old_webio_class(api, base_id, class_name, label)
            else:
                _LOGGER.warning(
                    "[%s] %s base %s still blocked by other logic. Reusing base structure.",
                    self.server_id,
                    label,
                    base_id,
                )

        ctx.update_status(
            f"{status_msg}\n\n{ICON_UPLOAD} Uploading configuration ({n_commands} command(s))...",
            pct=(pct_start + pct_end) // 2,
            step_info="Uploading configuration",
        )
        success, res_id = await api.upload_web_io(self.server_id, class_name, web_io_json)
        if not success:
            raise RuntimeError(f"Upload failed ({label}): {res_id}")
        if not await api.create_webio_device(class_name, res_id, ctx.ha_address):
            raise RuntimeError(f"Device creation failed ({label}, class created, device instance not)")
        await _confirm_webio_device_created(api, class_name, class_dev_id, label)

    async def _delta_sync_class(
        self,
        ctx: _SyncContext,
        cls: str,
        class_name: str,
        label: str,
        class_dev_id: str | None,
        cls_effective_action: str,
        cls_audit: dict[str, list[dict]],
        dev_ip_mismatch: bool,
        pct_start: int,
        pct_end: int,
    ) -> dict[str, Any]:
        """Targeted create/rename/delete/type-fix of individual Web-IO commands for one class.

        cls_audit bundles this class' slice of the coordinator's audit results — keys
        "missing"/"renamed"/"types"/"orphans"/"dangling" — into one dict purely to keep this
        method's parameter count under SonarQube's limit (python:S107); see _sync_class for
        where the individual lists still come from.
        """
        api = ctx.api
        _LOGGER.info(
            "[%s] %s: performing targeted Delta Sync for mode: %s", self.server_id, label, cls_effective_action
        )

        base_id = ctx.webio_devices_audit.get(cls, {}).get("base_id")
        if not base_id or str(base_id) in {"0", "None"}:
            try:
                b_info = await api.get_webio_base_info(class_name)
            except RuntimeError as err:
                # Only a fallback lookup: without a base_id, new-command creates are skipped
                # (and reported) below while renames/type-fixes still run — no reason to abort
                # the whole Delta Sync over it.
                _LOGGER.warning("[%s] %s: fallback Base ID lookup failed: %s", self.server_id, label, err)
                b_info = None
            if b_info:
                base_id = b_info[0]
                _LOGGER.debug("[%s] %s: resolved fallback Base ID: %s", self.server_id, label, base_id)

        tasks_to_do = self._build_delta_tasks(
            cls_effective_action,
            cls_audit["renamed"],
            cls_audit["orphans"],
            cls_audit["missing"],
            cls_audit["types"],
        )
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

        # Function Plan debris (cls_dangling) rides along with the same actions that already
        # remove Web-IO commands — no dedicated repair-dialog action for it.
        cls_dangling_to_delete = (
            cls_audit["dangling"] if cls_effective_action in {"full_sync", "delete_orphans"} else []
        )
        result = await self._execute_delta_tasks(
            ctx, cls, class_dev_id, base_id, tasks_to_do, cls_dangling_to_delete, on_progress, label, pct_start
        )
        result["updated_ip"] = False
        result["skipped_creates"] = skipped_creates

        # Final step: Update Server Address (IP) — save_single_command does not
        # update device-level settings, so this is always a separate explicit call.
        if cls_effective_action in {"full_sync", "update_ip"} and dev_ip_mismatch and class_dev_id:
            ctx.update_status(
                f"{ICON_NETWORK} **Repair in progress ({label}):** Updating HA IP address...",
                pct=pct_end - 2,
                step_info="Updating HA IP address",
            )
            result["updated_ip"] = await api.update_webio_device_ip(class_dev_id, ctx.ha_address, class_name)
            if not result["updated_ip"]:
                ctx.failed_writes.append(f"{label}: server address update")

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
        labels = {
            "rename": f"{ICON_RENAME} Rename",
            "delete": f"{ICON_DELETE} Remove",
            "create": f"{ICON_ADD} Add",
            "type": f"{ICON_FIX} Type-Fix",
        }
        t_label = labels.get(task_type, task_type)
        prog_msg = (
            f"**Class:** {label}\n**Mode:** `{cls_effective_action}`\n"
            f"**Progress:** Step {current + 1} of {total_tasks}\n"
            f"**Current:** {t_label}: `{task_name}`\n\n---\n"
            f"{ICON_CLOCK} **Start:** {ctx.start_time.strftime('%H:%M:%S')} (Runtime: {elaps_str})\n"
            f"{ICON_FLAG} **Done:** ~{eta_t} (Remaining: {rem_str})"
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

    async def _cleanup_delta_debris(
        self, cls: str, tasks_to_do: list[dict], cls_dangling: list[dict]
    ) -> tuple[set[int], int]:
        """Unwire orphaned Web-IO commands about to be deleted and remove leftover Function
        Plan debris, ahead of running the delta tasks themselves (see _execute_delta_tasks).

        Returns (fub_ids touched — for the caller to re-sort, since deleting an element opens
        a gap in the plan's grid — and the total element count removed).
        """
        resort_fub_ids: set[int] = set()
        debris_removed = 0

        # Unwire any Function-Plan element still connected to an orphan before deleting its
        # Web-IO command, unconditionally — not just when Comexio's delete call happens to
        # refuse it (it doesn't reliably refuse, which is how orphaned plan elements were
        # left dangling before). Batched once for the whole run, not per task.
        delete_webio_ids = [
            int(t["item"]["webIoId"])
            for t in tasks_to_do
            if t["type"] == "delete" and t["item"].get("webIoId") is not None
        ]
        if delete_webio_ids and not getattr(self.coordinator, "cancel_sync", False):
            unwired = await self.coordinator.unwire_webio_commands(delete_webio_ids)
            resort_fub_ids.update(unwired["touched_fub_ids"])
            debris_removed += unwired["deleted_elem_count"]

        # Function Plan debris (marker/KNX/IO elements whose WebIO counterpart was already
        # removed elsewhere, e.g. directly in Comexio Studio) has no command to unwire —
        # just delete the leftover element(s) directly.
        source_type = source_category(cls).fub_module_type
        dangling_ref_ids = [i["ref_id"] for i in cls_dangling]
        if dangling_ref_ids and not getattr(self.coordinator, "cancel_sync", False):
            cleaned = await self.coordinator.delete_dangling_plan_elements(source_type, dangling_ref_ids)
            resort_fub_ids.update(cleaned["touched_fub_ids"])
            debris_removed += cleaned["deleted_elem_count"]

        return resort_fub_ids, debris_removed

    @staticmethod
    async def _apply_delta_task(
        api: Any,
        base_id: str | None,
        class_dev_id: str | None,
        task: dict,
        result: dict[str, Any],
        failed_writes: list[str],
    ) -> None:
        """Execute one delta-sync task (rename/delete/type-fix/create), updating `result` in place.

        A write the API reports as failed is recorded in `failed_writes` instead of counted,
        and a failed create is not handed on to the function plan wiring pass."""
        item, t_type = task["item"], task["type"]
        if t_type == "delete":
            ok = await api.delete_single_command(item["id"], class_dev_id)
        else:
            # rename, type and create all save the full command; only create has no id yet.
            # A digital<->analog type change may move the entity to a different HA domain
            # (e.g. switch -> number). No registry cleanup is needed here: the forced
            # integration reload after every sync (_finalize_sync) re-runs __init__.py's
            # expected_platform check, which already removes stale-domain entities based on
            # the actual HA entity domain rather than this Comexio-side type signal alone.
            existing_cmd_id = None if t_type == "create" else item["id"]
            ok = await api.save_single_command(base_id, class_dev_id, item["payload"], existing_cmd_id=existing_cmd_id)
        if not ok:
            failed_writes.append(f"{t_type} {item['name']}")
            return
        result[_DELTA_RESULT_KEYS[t_type]] += 1
        if t_type == "create":
            result["created_names"].append(item["name"])

    async def _execute_delta_tasks(
        self,
        ctx: _SyncContext,
        cls: str,
        class_dev_id: str | None,
        base_id: str | None,
        tasks_to_do: list[dict],
        cls_dangling: list[dict],
        on_progress: Callable[[int, str, str], None],
        label: str,
        pct_start: int,
    ) -> dict[str, int]:
        """Run the collected delta-sync tasks against the Comexio API, in order."""
        api = ctx.api
        result: dict[str, Any] = {
            "added": 0,
            "removed": 0,
            "updated": 0,
            "renamed": 0,
            "created_names": [],
            "debris_removed": 0,
        }

        resort_fub_ids, debris_removed = await self._cleanup_delta_debris(cls, tasks_to_do, cls_dangling)
        result["debris_removed"] += debris_removed

        # Deleting an element opens a gap in the plan's grid; re-sort to close it. Safe to
        # call directly (no is-managed-plan guard here) because both cleanup calls above only
        # ever return fub_ids that already passed coordinator._is_managed_function_plan() —
        # resort touching a non-"{prefix} - "-named plan (e.g. a user's own hand-laid-out
        # plan) would silently rewrite its layout, so that filter must stay enforced at the
        # source rather than re-checked here. was_active=True because each cleanup's own
        # restart already brought the plan back up by this point.
        #
        # Each sort is a real multi-step Comexio round-trip (stop_fup/delete_elements/
        # save_elements_pos/run_fup) that can take several seconds per plan — without a status
        # update here the sync notification sits frozen on whatever text preceded this class'
        # delta sync for the whole loop, looking hung even though it's actively working
        # (reported by user 21.09.2026: "was macht der solange, das ist total unklar").
        total_resorts = len(resort_fub_ids)
        for idx, fub_id in enumerate(sorted(resort_fub_ids), start=1):
            if total_resorts:
                ctx.update_status(
                    f"{ICON_TOOLS} **Class:** {label}\nRe-sorting Function Plan {idx} of {total_resorts} "
                    f"after cleanup (fub {fub_id})...",
                    pct=pct_start,
                    step_info=f"{label}: re-sorting plan {idx}/{total_resorts} (fub {fub_id})",
                )
            await async_sort_function_plan(self.hass, self.coordinator, api, fub_id, notify=False, was_active=True)

        for idx, task in enumerate(tasks_to_do):
            if getattr(self.coordinator, "cancel_sync", False):
                break
            on_progress(idx, task["item"]["name"], task["type"])
            await self._apply_delta_task(
                api,
                base_id,
                class_dev_id,
                task,
                result,
                ctx.failed_writes,
            )
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
        cls_dangling: list[dict],
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
            ctx, cls, class_dev_id, cls_missing, cls_renamed, cls_types, cls_orphans, dev_ip_mismatch
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
                "debris_removed": 0,
                "created_names": [
                    cmd["Name"]
                    for cmd in ctx.api.build_webio_commands(
                        self.server_id,
                        self.coordinator.data,
                        webio_class=cls,
                        ignored_marker_ids=self.coordinator.ignored_marker_ids,
                        ignored_knx_ids=self.coordinator.ignored_knx_ids,
                    )
                ],
            }

        cls_audit = {
            "missing": cls_missing,
            "renamed": cls_renamed,
            "types": cls_types,
            "orphans": cls_orphans,
            "dangling": cls_dangling,
        }
        result = await self._delta_sync_class(
            ctx,
            cls,
            class_name,
            label,
            class_dev_id,
            cls_effective_action,
            cls_audit,
            dev_ip_mismatch,
            pct_start,
            pct_end,
        )
        result["recreated"] = False
        return result

    # --- MANAGED CLUSTER PLAN WIRING ---

    @staticmethod
    def _classify_created_names(
        created_names: list[str],
        clustered_cats: list[SourceCategory],
        known_io_names: dict[str, tuple[str, str]],
    ) -> tuple[dict[WebioClass, list[int]], list[tuple[str, str]]]:
        """Split freshly-created Web-IO command names into per-category source ids and IO refs."""
        source_ids_by_cat: dict[WebioClass, list[int]] = {}
        created_io_refs: list[tuple[str, str]] = []
        for name in created_names:
            for cat in clustered_cats:
                if (sid := _parse_source_id_from_webio_name(name, cat.audit_key_prefix)) is not None:
                    source_ids_by_cat.setdefault(cat.key, []).append(sid)
                    break
            else:
                if (io_ref := _parse_io_from_webio_name(name, known_io_names)) is not None:
                    created_io_refs.append(io_ref)
        return source_ids_by_cat, created_io_refs

    @staticmethod
    def _merge_gap_source_ids(
        source_ids_by_cat: dict[WebioClass, list[int]], gap_items: list[dict], gap_keys: dict[str, SourceCategory]
    ) -> list[dict]:
        """Fold pre-existing unwired range-clustered gap items into source_ids_by_cat in place;
        return the leftover IO gap items."""
        for item in gap_items:
            for gap_key, cat in gap_keys.items():
                if gap_key in item:
                    source_ids_by_cat.setdefault(cat.key, []).append(item[gap_key])
        return [item for item in gap_items if not any(k in item for k in gap_keys)]

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

        known_io_names = {
            f"HA IO {io['ext_name']} {io['identifier']}": (io["ext_name"], io["identifier"])
            for io in self.coordinator.data.get("io", [])
        }
        # Range-clustered categories (Marker + KNX): one pipeline, only the id prefix / plan
        # label differ. IO stays a separate branch (per-extension plans, composite key).
        clustered_cats = [cat for cat in SOURCE_CATEGORIES.values() if cat.range_clustered]
        gap_keys = {f"{cat.key.value}_id": cat for cat in clustered_cats}

        source_ids_by_cat, created_io_refs = self._classify_created_names(created_names, clustered_cats, known_io_names)

        if ctx.action in {"full_sync", "function_plan_add_missing"}:
            gap_io_items = self._merge_gap_source_ids(source_ids_by_cat, gap_items, gap_keys)
        else:
            gap_io_items = []

        source_ids_by_cat = {k: list(dict.fromkeys(v)) for k, v in source_ids_by_cat.items() if v}
        io_items = _merge_io_items(created_io_refs, gap_io_items, managed_exts)
        if not source_ids_by_cat and not io_items:
            return []

        total = sum(len(v) for v in source_ids_by_cat.values()) + len(io_items)
        progress_state = {"done": 0, "total": total, "t0": time.monotonic()}
        summary: list[str] = []
        n_added = 0
        n_errors = 0

        for cat_key, ids in source_ids_by_cat.items():
            lines, added, errors = await self._wire_source_clusters(ctx, source_category(cat_key), ids, progress_state)
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

    async def _wire_trigger_pairs(self, ctx: _SyncContext, refresh_audit: bool = False) -> list[str]:
        """Create/remove Marker+Flanke self-reset pairs for [TRIG]/[TP] markers.

        Driven by the coordinator's function_plan_trigger_missing/orphan audit maps (per-ref_type
        id lists, one bucket per trigger-capable source category), not by created_names — a
        trigger source's Web-IO command is audited/created exactly like any other source's,
        independently of this construct (see const.py's trigger-plan notes).
        refresh_audit=True re-audits against Comexio's *current* config instead of trusting
        last_audit_results — used after _sync_all_classes, since a marker renamed to add/drop
        its [TRIG]/[TP] suffix around the time this sync ran would otherwise be judged against
        the audit snapshot from the *previous* poll, leaving its self-reset pair
        uncreated/undeleted until some unrelated later poll happens to pick it up.
        coordinator.async_request_refresh() cannot help here: it goes through
        _async_update_data(), which returns the existing (stale) data as-is for as long as
        self.coordinator.in_sync is True — i.e. the whole duration of this sync. See
        async_fresh_trigger_audit()'s docstring for why it fetches around that instead.
        """
        if ctx.action not in {"full_sync", "function_plan_add_missing"}:
            return []
        if refresh_audit:
            # One-shot: the snapshot is only valid right after the KNX step that left it.
            snapshot, ctx.unchanged_config_snapshot = ctx.unchanged_config_snapshot, None
            fresh = await self.coordinator.async_fresh_trigger_audit(snapshot)
            if fresh is None:
                ctx.fresh_markers = None
                return [_skipped_check_line("Trigger pairs")]
            missing_by_ref, orphan_by_ref, ctx.fresh_markers = fresh
        else:
            audit_data = getattr(self.coordinator, "last_audit_results", {})
            missing_by_ref = audit_data.get("function_plan_trigger_missing", {})
            orphan_by_ref = audit_data.get("function_plan_trigger_orphan", {})
        if not missing_by_ref and not orphan_by_ref:
            return []

        summary: list[str] = []
        for ref_type, ids in missing_by_ref.items():
            if ids:
                summary.append(await self._add_trigger_pairs(ctx, ids, ref_type))
        for ref_type, ids in orphan_by_ref.items():
            if ids:
                summary.append(await self._remove_trigger_pairs(ctx, ids, ref_type))
        return summary

    def _resolve_knx_trigger_bridge_markers(self, ctx: _SyncContext, k_ids: list[int]) -> tuple[list[int], list[str]]:
        """Translate KNX trigger source ids to their write-path bridge marker ids.

        Comexio refuses to start a plan that wires a K element's own Flanke self-reset
        loop back into that K element's input: the K object's input is already driven by
        its write-bridge Marker (create_knx_bridge_marker's plan), so the reciprocal
        connection this self-reset trick needs collides with it ("Der Funktionsplan konnte
        aufgrund von mehrfach verwendeten Ausgängen nicht gestartet werden" / "Element K<n>
        wird bereits im Funktionsplan ... verwendet", confirmed live 2026-09-21). The bridge
        Marker itself has no such conflict (nothing else drives its plan-input), so the
        Trigger plan must always wire that Marker, never the K element directly — see
        [[project_knx_write_path_design]].

        Returns (bridge_marker_ids, errors) — a K id without a bridge marker yet (write
        bridge not created, e.g. mid-sync ordering issue) is reported as an error and
        skipped rather than silently wiring nothing. A K id whose bridge Marker exists but
        is missing from the wiring-derived map (see _knx_bridge_marker_id_by_title) still
        resolves via its title instead of being dropped.
        """
        bridge_marker_by_k_id = self.coordinator._knx_bridge_marker_by_k_id()
        if bridge_marker_by_k_id is None:
            return [], [f"KNX trigger pairs: bridge-marker map not loaded yet, skipped {len(k_ids)} id(s)"]
        marker_ids: list[int] = []
        errors: list[str] = []
        for k_id in k_ids:
            marker_id = bridge_marker_by_k_id.get(str(k_id))
            if marker_id is None:
                marker_id = self._knx_bridge_marker_id_by_title(ctx, k_id)
            if marker_id is None:
                errors.append(f"K{k_id}: no write-path bridge marker yet, cannot wire trigger pair")
            else:
                marker_ids.append(int(marker_id))
        return marker_ids, errors

    def _knx_bridge_marker_id_by_title(self, ctx: _SyncContext, k_id: int) -> int | None:
        """Fallback bridge-Marker lookup via its title suffix "[K<k_id>]", for when
        _knx_bridge_marker_by_k_id()'s plan-wiring-derived map has no entry for k_id.

        That map is built from the KNX cluster plan's *connection* wiring (see
        _plan_knx_bridge_pairs) — if that connection is removed or broken in Comexio
        (manually, or by an external tool) while the bridge Marker itself is left behind,
        still carrying its machine-set "[K<k_id>]" title (create_knx_bridge_marker,
        MARKER_KNX_BRIDGE_SUFFIX_RE), the map lookup alone would make it permanently
        invisible: a future orphan audit derives its own candidates from that very same
        map (see coordinator._audit_trigger_pairs), so a K id dropped here would never be
        reconsidered either, leaving its stale Marker+Flanke trigger-plan wiring stuck
        forever. Scanning the coordinator's cached marker list directly (kind==KNX_BRIDGE,
        the same classification MARKER_KNX_BRIDGE_SUFFIX_RE drives) sidesteps the
        connection-wiring dependency entirely. Prefers ctx.fresh_markers: the cached list is
        frozen for the whole sync and misses a bridge Marker created earlier in it.
        """
        suffix = f"[K{k_id}]"
        markers = ctx.fresh_markers if ctx.fresh_markers is not None else self.coordinator.data.get("markers", [])
        for m in markers:
            if m.get("kind") == MarkerKind.KNX_BRIDGE and (m.get("title") or "").rstrip().endswith(suffix):
                return int(m["id"])
        return None

    async def _add_trigger_pairs(self, ctx: _SyncContext, missing_ids: list[int], ref_type: int = 2) -> str:
        """Resolve/create the trigger plan and add the missing source+Flanke pairs.

        ref_type is the plan-element type of the trigger source category (marker=2,
        KNX=11 — blind guess); it selects the audit-key prefix. A KNX source is wired via
        its bridge Marker instead of the K element itself (see
        _resolve_knx_trigger_bridge_markers) — the actual element created is always type=2.
        """
        api = ctx.api
        fub_id, is_fresh = await self.coordinator.resolve_trigger_plan()
        if fub_id is None:
            ctx.failed_writes.append(_plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, "not resolved/created"))
            return (
                f"{ICON_WARNING} Trigger plan '{FUNCTION_PLAN_TRIGGER_PLAN_NAME}': could not resolve/create — see log."
            )

        prefix = category_by_fub_module_type(ref_type).audit_key_prefix
        knx_ref_type = int(SOURCE_CATEGORIES[WebioClass.KNX].fub_module_type)
        bridge_errors: list[str] = []
        wire_ref_type = ref_type
        wire_ids = missing_ids
        if ref_type == knx_ref_type:
            wire_ids, bridge_errors = self._resolve_knx_trigger_bridge_markers(ctx, missing_ids)
            wire_ref_type = int(SOURCE_CATEGORIES[WebioClass.MARKER].fub_module_type)

        plan_name = self._plan_name(fub_id)
        was_active = bool(api.fub_data.get(str(fub_id), {}).get("Active", True))
        t0 = time.monotonic()
        await self.coordinator.async_function_plan_change_backup(
            fub_id, f"add_trigger_pairs {[f'{prefix}{m}' for m in missing_ids]}"
        )
        await api.function_plan_stop_fup(fub_id)
        added, errors = await api.function_plan_add_trigger_pairs(
            fub_id, wire_ids, fresh_plan=is_fresh, ref_type=wire_ref_type
        )
        errors = bridge_errors + errors
        if errors:
            _LOGGER.warning("[%s] function_plan_add_trigger_pairs errors: %s", self.server_id, errors)
            ctx.failed_writes.append(_plan_failure(plan_name, f"{len(errors)} error(s)"))
        if added and not is_fresh:
            await async_sort_function_plan(self.hass, self.coordinator, api, fub_id, notify=False, was_active=False)
        note = ""
        if is_fresh or was_active:
            # create_fup always creates plans inactive (fub_active="0") — a fresh plan must be
            # activated unconditionally, was_active=False would otherwise leave it stopped forever.
            note = _activation_note(ctx, plan_name, await api.function_plan_run_fup(fub_id))
        return _plan_summary_line(plan_name, is_fresh, len(added), len(missing_ids), "trigger pairs", t0, note, errors)

    async def _remove_trigger_pairs(self, ctx: _SyncContext, orphan_ids: list[int], ref_type: int = 2) -> str:
        """Remove orphaned source+Flanke pairs from the trigger plan (source lost its suffix).

        ref_type selects the trigger source category (marker=2, KNX=11 — blind guess) so the
        audit-key prefix and the API element lookup target the right $FubModules bucket.

        Unlike _add_trigger_pairs (which goes through resolve_trigger_plan, verifying the
        cached fub_id's live name before trusting it), this reads CONF_FUNCTION_PLAN_PLAN_MAP
        directly — a stale entry left behind by a plan rename/deletion/ID reuse in Comexio
        would otherwise let this delete Marker+Flanke elements from whatever plan that fub_id
        now belongs to, including one the user authored by hand. The trigger plan's name is
        always the fixed FUNCTION_PLAN_TRIGGER_PLAN_NAME regardless of the configured cluster-
        plan prefix (see resolve_trigger_plan), so an exact-name check is used here instead of
        _is_managed_function_plan()'s prefix check — otherwise changing CONF_FUNCTION_PLAN_PLAN_PREFIX
        away from its default would make this guard reject the trigger plan itself.
        """
        raw_fub_id = self.coordinator.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {}).get(
            FUNCTION_PLAN_TRIGGER_PLAN_NAME
        )
        if raw_fub_id is None:
            ctx.failed_writes.append(_plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, "missing, orphans not removed"))
            return f"{ICON_WARNING} {len(orphan_ids)} orphaned trigger construct(s) found, but no trigger plan exists."

        fub_id = int(raw_fub_id)
        if self.coordinator.api.fub_data.get(str(fub_id), {}).get("Name") != FUNCTION_PLAN_TRIGGER_PLAN_NAME:
            ctx.failed_writes.append(
                _plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, "mapping stale, orphans not removed")
            )
            return (
                f"{ICON_WARNING} Trigger plan mapping (fub={fub_id}) no longer points to "
                f"'{FUNCTION_PLAN_TRIGGER_PLAN_NAME}' — skipped orphan cleanup to avoid touching a user-owned plan."
            )

        prefix = category_by_fub_module_type(ref_type).audit_key_prefix
        await self.coordinator.async_function_plan_change_backup(
            fub_id, f"remove_trigger_pairs {[f'{prefix}{m}' for m in orphan_ids]}"
        )
        # KNX orphans are reported as K ids, but the actual plan element is the bridge Marker
        # (see _resolve_knx_trigger_bridge_markers) — translate before deleting.
        knx_ref_type = int(SOURCE_CATEGORIES[WebioClass.KNX].fub_module_type)
        remove_ref_type = ref_type
        remove_ids = orphan_ids
        if ref_type == knx_ref_type:
            remove_ids, bridge_errors = self._resolve_knx_trigger_bridge_markers(ctx, orphan_ids)
            if bridge_errors:
                _LOGGER.warning("[%s] remove_trigger_pairs bridge lookup errors: %s", self.server_id, bridge_errors)
                ctx.failed_writes.append(
                    _plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, f"{len(bridge_errors)} trigger pair(s) not removed")
                )
            remove_ref_type = int(SOURCE_CATEGORIES[WebioClass.MARKER].fub_module_type)
        deleted, plan_stopped = await ctx.api.function_plan_remove_trigger_pairs(
            fub_id, remove_ids, ref_type=remove_ref_type
        )
        note = f", {ICON_WARNING} plan left stopped — please restart it in Comexio" if plan_stopped else ""
        if plan_stopped:
            ctx.failed_writes.append(_plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, "left stopped"))
        if remove_ids and not deleted:
            # Stop refused (or the plan was not running) or the delete failed — the orphans are still there.
            ctx.failed_writes.append(
                _plan_failure(FUNCTION_PLAN_TRIGGER_PLAN_NAME, "orphaned trigger pairs not removed")
            )
            note += f", {ICON_WARNING} orphans not removed — see log"
        return f"{ICON_DELETE} Removed {deleted} orphaned trigger element(s){note}"

    async def _wire_source_clusters(
        self, ctx: _SyncContext, category: SourceCategory, source_ids: list[int], progress_state: dict
    ) -> tuple[list[str], int, int]:
        """Resolve the marker/KNX cluster plans and add the pairs. Returns (lines, added, errors)."""
        plan_to_ids, created_plans, failed_plans = await self.coordinator.resolve_marker_clusters(
            source_ids, category.label
        )
        # Surface a resolve failure to the user, not just the log — a cluster can fail to
        # resolve/create (e.g. the stale-fub_id guard rejecting a contaminated plan) while
        # other clusters in the same batch succeed, so plan_to_ids alone can't signal it.
        summary: list[str] = [
            f"{ICON_WARNING} Cluster plan '{name}' could not be resolved/created — see log" for name in failed_plans
        ]
        errors = len(failed_plans)
        ctx.failed_writes.extend(_plan_failure(name, "not resolved/created") for name in failed_plans)
        if not plan_to_ids:
            if not failed_plans:
                _LOGGER.warning(
                    "[%s] Cluster plan wiring: no %s cluster plan available", self.server_id, category.label
                )
                summary.append(f"{ICON_WARNING} No {category.label} cluster plan available — see log.")
                ctx.failed_writes.append(f"{category.label} cluster plan: none available")
                errors = 1
            return summary, 0, errors

        ref_type = int(category.fub_module_type)
        added = 0
        for fub_id, cluster_ids in plan_to_ids.items():
            if getattr(self.coordinator, "cancel_sync", False):
                break
            line, lp_added, lp_errors = await self._add_pairs_to_plan(
                ctx, fub_id, sorted(cluster_ids), fub_id in created_plans, progress_state, ref_type
            )
            if lp_errors:
                ctx.failed_writes.append(_plan_failure(self._plan_name(fub_id), f"{len(lp_errors)} error(s)"))
            summary.append(line)
            added += len(lp_added)
            errors += len(lp_errors)
        return summary, added, errors

    async def _wire_knx_full(
        self, ctx: _SyncContext, created_names: list[str], gap_items: list[dict], refresh_audit: bool = False
    ) -> list[str]:
        """Wire every open KNX leg (read-path, write-path bridge, API-Loopback fan-out) for
        every KNX cluster in exactly ONE combined stop -> write -> finalize cycle per plan.

        Reported by the user 18.09.2026: running _wire_created_pairs' KNX slice, then
        _wire_knx_bridges, then _wire_knx_bridge_loopbacks in sequence on the SAME plan made
        each of them independently capture-was_active -> stop_fup -> write -> sort/reactivate,
        so a plan with all three legs open visibly sorted/reactivated three times in a row
        instead of once, and the tripled Comexio round-trips compounded into a long silent
        stretch. An intermediate design (legs 1+2 combined, leg 3 deferred to one final
        follow-up pass per affected cluster) still left every affected plan visibly
        sorting/reactivating twice — flagged by the user as still redundant (screenshots,
        21.09.2026: "erst werden die einen Elemente hinzugefügt, dann wird sortiert, dann
        werden die nächsten elemente hinzugefügt, dann wird wieder sortiert ... das ist doch
        überflüssig"). This method now wires all three legs for a cluster inside a single
        _wire_knx_cluster call — one capture-was_active -> stop_fup -> write (all legs) ->
        sort/reactivate cycle per plan, full stop.

        The three legs have a real dependency chain, not just a shared plan:
          - Leg 1 (K -> webIO-HA read path, function_plan_add_source_pairs ref_type=11) creates
            the K-Element's own connection record. Computed the same way _wire_created_pairs
            computes it (freshly created_names this run, unioned with the pre-existing
            function_plan_missing gap_items for a full sync) — there is no fresh-audit
            equivalent for this leg (see async_fresh_knx_audits' docstring for why KNX
            bridge/loopback have one and this doesn't: only those two can come into existence
            mid-run from a plan/import_knx toggle created earlier in the same run).
          - Leg 2 (Merker -> K write-path bridge) creates an entirely independent connection
            record (Marker as source, K as sink) and can run before/after/independent of leg 1
            — it creates the K-Element itself if missing.
          - Leg 3 (K -> webIO-Loopback fan-out) hard-requires leg 1's connection record to
            already exist (it only ever EXTENDS it, see wire_knx_bridge_loopback's docstring)
            and needs each K-Element's marker_id from leg 2. A bridge leg 2 creates THIS run has
            no marker_id in the pre-fetched loopback audit yet (that audit only lists a K-Element
            once its bridge marker is already visible server-side).

            Until 2026-09-21 this dependency was resolved either via an extra
            async_fresh_knx_bridge_loopback_audit() re-fetch per cluster (a full get_raw_config +
            parse_config + force-reload of every relevant fub_id — live-observed by the user as
            a ~7.5-minute silent stretch on a loaded Comexio server), or via a deferred
            second cycle per affected cluster. Both are gone now: api.py's
            function_plan_add_knx_bridge_pairs (leg 2) already creates the bridge Marker
            synchronously and knows its marker_id — it now returns that mapping directly
            instead of discarding it, so _wire_knx_cluster can wire leg 3 for a freshly-bridged
            K-object in the SAME cycle, with no re-audit and no follow-up pass at all.

        Before the first cluster is touched, _prestage_knx_webio allocates every open bridge
        Marker and bulk-creates the API-Loopback Web-IO class with ALL loopback commands
        (28.09.2026) — the per-cluster legs then only wire those markers/commands instead of
        saving loopback commands one by one for every cluster after the first.

        Runs for a Full Sync as well as the standalone knx_bridge_add_missing action (decided
        18.09.2026: that action completes every open KNX write-path leg, including the read
        path, rather than leaving a bridge with an incomplete K-Element behind) — but not for
        a scoped delta action (update_types/create_missing/...), mirroring _wire_trigger_pairs'
        gating. created_names is empty for knx_bridge_add_missing (it doesn't sync Web-IO
        commands itself) — its read-path candidates come from gap_items alone, same as
        function_plan_add_missing's own read-path-only wiring.
        """
        if ctx.action not in {"full_sync", "knx_bridge_add_missing"}:
            return []

        clustered_cats = [cat for cat in SOURCE_CATEGORIES.values() if cat.range_clustered]
        source_ids_by_cat, _created_io_refs = self._classify_created_names(created_names, clustered_cats, {})
        gap_keys = {f"{cat.key.value}_id": cat for cat in clustered_cats}
        self._merge_gap_source_ids(source_ids_by_cat, gap_items, gap_keys)
        read_path_ids = set(source_ids_by_cat.get(WebioClass.KNX, []))

        bridge_missing, loopback_missing, snapshot, skipped = await self._knx_audit_items(refresh_audit)
        bridge_by_id = {int(item["ref_id"]): item for item in bridge_missing}
        loopback_by_id = {int(item["ref_id"]): item for item in loopback_missing}

        all_k_ids = read_path_ids | set(bridge_by_id) | set(loopback_by_id)
        if not all_k_ids:
            # Nothing is written below, so Comexio still matches the snapshot.
            ctx.unchanged_config_snapshot = snapshot
            return skipped

        plan_to_ids, created_plans, failed_plans = await self.coordinator.resolve_knx_clusters(sorted(all_k_ids))
        # Surface a resolve failure to the user, not just the log — see _wire_source_clusters.
        summary: list[str] = skipped + [
            f"{ICON_WARNING} KNX cluster plan '{name}' could not be resolved/created — see log" for name in failed_plans
        ]
        ctx.failed_writes.extend(_plan_failure(name, "not resolved/created") for name in failed_plans)
        if not plan_to_ids:
            if not failed_plans:
                _LOGGER.warning("[%s] KNX combined wiring: no KNX cluster plan available", self.server_id)
                summary.append(f"{ICON_WARNING} No KNX cluster plan available — see log.")
                ctx.failed_writes.append("KNX cluster plan: none available")
            return summary

        # NOT len(all_k_ids): a K-id needing e.g. both the read-path and bridge legs
        # contributes 2 to "done" below (once per leg it actually goes through) but would
        # only contribute 1 to a union-based total, letting done run past total (>100%
        # progress, negative ETA). Sum of the three per-leg counts instead, so it lines up
        # 1:1 with the progress_state["done"] += len(...) calls in _wire_knx_cluster's three
        # leg helpers below. Every open bridge counts twice — leg 2 AND the leg 3 it pulls in
        # for the same K — so the total is fixed from the start instead of jumping mid-run
        # (118 -> 177 for 59 new K-objects, noticed live 28.09.2026); a bridge that fails in
        # leg 2 takes its leg-3 slot back out (see _wire_knx_cluster).
        progress_state = {
            "done": 0,
            "total": len(read_path_ids) + 2 * len(bridge_by_id) + len(loopback_by_id),
            "t0": time.monotonic(),
            "unit": "step",
            "total_note": f" ({len(all_k_ids)} KNX objects, up to 3 legs each)",
        }
        prestage, prestage_errors = await self._prestage_knx_webio(ctx, bridge_by_id, loopback_by_id, progress_state)
        if prestage_errors:
            _LOGGER.warning("[%s] KNX prestage errors: %s", self.server_id, prestage_errors)
            summary.append(
                f"{ICON_WARNING} KNX upfront preparation incomplete ({len(prestage.allocated)} of"
                f" {len(bridge_by_id)} bridge Marker(s) prepared) — retried per plan, see log"
            )
        for fub_id, cluster_ids in plan_to_ids.items():
            # Checked only between clusters, same as _add_pairs_to_plan's existing pattern —
            # but each cluster here now wires all three legs (including leg 3 for a bridge it
            # just created — see _wire_knx_cluster's docstring) in one combined stop/write/
            # finalize cycle instead of one leg's own.
            if getattr(self.coordinator, "cancel_sync", False):
                break
            line = await self._wire_knx_cluster(
                ctx,
                fub_id,
                sorted(cluster_ids),
                fub_id in created_plans,
                read_path_ids,
                bridge_by_id,
                loopback_by_id,
                progress_state,
                prestage,
            )
            if line:
                summary.append(line)
        return summary

    async def _knx_audit_items(
        self, refresh_audit: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None, list[str]]:
        """(bridge_missing, loopback_missing, snapshot, skipped-check lines) for _wire_knx_full.

        refresh_audit re-audits against Comexio's current config (async_fresh_knx_audits); a check
        it had to skip counts as nothing missing for this run and gets its own result line.
        """
        if not refresh_audit:
            audit_data = getattr(self.coordinator, "last_audit_results", {})
            return (
                audit_data.get("knx_bridge_missing", []),
                audit_data.get("knx_bridge_loopback_missing", []),
                None,
                [],
            )
        bridge_missing, loopback_missing, snapshot = await self.coordinator.async_fresh_knx_audits()
        skipped = [
            _skipped_check_line(check)
            for check, items in (("KNX write-path bridges", bridge_missing), ("KNX API-Loopback", loopback_missing))
            if items is None
        ]
        return bridge_missing or [], loopback_missing or [], snapshot, skipped

    async def _prestage_knx_webio(
        self,
        ctx: _SyncContext,
        bridge_by_id: dict[int, dict],
        loopback_by_id: dict[int, dict],
        progress_state: dict,
    ) -> tuple[_KnxPrestage, list[str]]:
        """Allocate every open bridge Marker and bulk-create the API-Loopback Web-IO class
        with ALL loopback commands, before any KNX cluster plan is wired.

        Live 28.09.2026: the loopback class was bulk-created with the first cluster's bridges
        only, so every bridge of the next cluster fell back to save_single_command — ~35-40 s
        each on the Comexio side (9 commands ≈ 5.5 min, looked like a hung sync). The user's
        rule: all Web-IO classes are created upfront with all their commands, then the
        function plans; single command saves are only for later top-ups of an existing class.
        The bridge marker ids are needed for the command names, hence the allocation first.
        """
        if not bridge_by_id and not loopback_by_id:
            return _KnxPrestage(), []
        _start_plan_step(
            ctx,
            progress_state,
            "KNX (all plans)",
            f"preparing {len(bridge_by_id)} KNX bridge Marker(s) + API-Loopback Web-IO class",
            "Web-IO: preparing KNX API-Loopback class",
        )
        # Only an optimization: on any failure the per-plan legs create the markers/commands
        # themselves, so it must never abort the sync (no plan is stopped yet at this point).
        api = ctx.api
        prestage = _KnxPrestage()
        errors: list[str] = []
        bridges = [(int(i["ref_id"]), int(i["marker_id"]), bool(i["binary"])) for i in loopback_by_id.values()]
        try:
            if bridge_by_id:
                marker_map, errors = await api.allocate_knx_bridge_markers(list(bridge_by_id.values()))
                prestage.allocated = {k_id: marker_id for k_id, (marker_id, _binary) in marker_map.items()}
                bridges += [(k_id, marker_id, binary) for k_id, (marker_id, binary) in marker_map.items()]
            _start_plan_step(
                ctx,
                progress_state,
                "KNX (all plans)",
                f"API-Loopback Web-IO class: {len(bridges)} command(s) (one bulk upload if the class is new)",
                "Web-IO: uploading KNX API-Loopback class",
            )
            prestage.preembedded = await api.prestage_knx_loopback_class(api.api_user, api.api_pass, bridges)
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as err:
            _LOGGER.warning(
                "[%s] KNX prestage failed — falling back to per-plan creation", self.server_id, exc_info=True
            )
            errors.append(f"KNX prestage failed: {err!r}")
        return prestage, errors

    async def _wire_knx_leg_read_path(
        self, ctx: _SyncContext, fub_id: int, plan_name: str, read_ids: list[int], progress_state: dict
    ) -> tuple[list[int], list[str], str]:
        """Leg 1: K -> webIO-HA read path. Returns (added K ref_ids, errors, summary part)."""
        _start_plan_step(
            ctx,
            progress_state,
            plan_name,
            f"adding {len(read_ids)} KNX read-path pair(s)",
            "Function Plan: adding pairs",
        )
        added, errors = await ctx.api.function_plan_add_source_pairs(
            fub_id,
            read_ids,
            # Always the off-canvas-parking + follow-up-sort branch (fresh_plan=False), even
            # for a plan created THIS run (is_fresh=True) — see _wire_knx_leg_bridge's docstring
            # for why: it and this leg would otherwise place their respective elements at
            # identical coordinates on a brand-new plan.
            fresh_plan=False,
            progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
            ref_type=11,
        )
        progress_state["done"] += len(read_ids)
        return added, errors, f"read-path +{len(added)}/{len(read_ids)}"

    async def _wire_knx_leg_bridge(
        self,
        ctx: _SyncContext,
        fub_id: int,
        plan_name: str,
        bridge_items: list[dict],
        loopback_by_id: dict[int, dict],
        progress_state: dict,
        preallocated: dict[int, int],
    ) -> tuple[list[int], list[str], str, list[dict]]:
        """Leg 2: Merker -> K write-path bridge. Returns (added, errors, summary part,
        loopback items for every K just bridged THIS call that still needs leg 3 — see
        _wire_knx_cluster, which wires these in the same stop/write/finalize cycle).
        """
        _start_plan_step(
            ctx,
            progress_state,
            plan_name,
            f"adding {len(bridge_items)} KNX bridge Marker(s)",
            "Function Plan: adding KNX bridges",
        )
        added, errors, bridged = await ctx.api.function_plan_add_knx_bridge_pairs(
            fub_id,
            bridge_items,
            # fresh_plan=False unconditionally (see _wire_knx_full's CRITICAL note): both this
            # leg's bridge-Marker and leg 1's K-Element use the IDENTICAL _pair_pos formula in
            # api.py when fresh_plan=True, each with its own n_added starting at 0 — on a plan
            # created THIS run (is_fresh=True) that places the i-th K-object's leg-1 element and
            # leg-2 element on the exact same coordinates. The old three-independent-cycles
            # design never hit this because leg 2's cycle always saw an already-existing,
            # non-fresh plan by the time it ran. Forcing the parking+sort path here is safe
            # regardless of is_fresh: shared parking coordinates are harmless (the sort pass
            # below resolves them into unique final positions), and _wire_knx_cluster forces
            # was_active=True for a genuinely fresh plan itself (create_fup's plans start
            # inactive and are cached that way — no default kicks in — see its comment there).
            fresh_plan=False,
            progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
            preallocated=preallocated,
        )
        progress_state["done"] += len(bridge_items)
        part = f"bridges +{len(added)}/{len(bridge_items)}"

        # api.py's function_plan_add_knx_bridge_pairs now hands back (marker_id, binary) for
        # every K it just bridged — created_knx_bridge_marker already knows both synchronously,
        # so no re-audit is needed to learn them (see its docstring). This lets _wire_knx_cluster
        # wire leg 3 for a brand-new bridge in the SAME cycle as legs 1+2 (single sort/reactivate
        # per plan, not the two-cycle "consolidate legs 1+2, defer leg 3" design this replaced —
        # see _wire_knx_cluster's docstring for why that design's own re-audit is now unnecessary,
        # per user feedback 21.09.2026 that even ONE extra sort/add cycle per affected cluster
        # was still visibly redundant).
        newly_bridged_loopback_items = [
            {"ref_id": k, "marker_id": bridged[k][0], "binary": bridged[k][1]} for k in added if k not in loopback_by_id
        ]
        return added, errors, part, newly_bridged_loopback_items

    async def _wire_knx_leg_loopback(
        self,
        ctx: _SyncContext,
        fub_id: int,
        plan_name: str,
        loopback_items: list[dict],
        progress_state: dict,
        preembedded: set[str],
    ) -> tuple[list[int], list[str], str]:
        """Leg 3: K -> webIO-Loopback fan-out. Returns (added K ref_ids, errors, summary part)."""
        _start_plan_step(
            ctx,
            progress_state,
            plan_name,
            f"wiring API-Loopback fan-out for {len(loopback_items)} KNX bridge(s)",
            "Function Plan: KNX loopback fan-out",
        )
        api = ctx.api
        bridges = [(int(i["ref_id"]), int(i["marker_id"]), bool(i["binary"])) for i in loopback_items]
        added, skipped, errors = await api.function_plan_add_knx_bridge_loopback_pairs(
            fub_id,
            bridges,
            api.api_user,
            api.api_pass,
            fresh_plan=False,  # see _wire_knx_leg_bridge's docstring
            progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
            command_progress_cb=partial(_loopback_command_progress, ctx, plan_name),
            preembedded=preembedded,
        )
        progress_state["done"] += len(loopback_items)
        skip_note = f" (+{len(skipped)} already wired)" if skipped else ""
        return added, errors, f"loopback +{len(added)}/{len(loopback_items)}{skip_note}"

    async def _wire_knx_cluster(
        self,
        ctx: _SyncContext,
        fub_id: int,
        cluster_ids: list[int],
        is_fresh: bool,
        read_path_ids: set[int],
        bridge_by_id: dict[int, dict],
        loopback_by_id: dict[int, dict],
        progress_state: dict,
        prestage: _KnxPrestage,
    ) -> str:
        """Wire every open KNX leg for one cluster plan in a single stop -> write -> finalize cycle.

        Legs 1+2+3 all run in this ONE cycle now, including leg 3 (API-Loopback fan-out) for a
        K just bridged by leg 2 THIS call — api.py's function_plan_add_knx_bridge_pairs hands
        back each new bridge's marker_id directly (see _wire_knx_leg_bridge's docstring), so no
        re-audit is needed to learn it before wiring leg 3. Replaces the previous design (legs
        1+2 here, leg 3 deferred to a separate end-of-run re-audit + follow-up pass across every
        affected cluster) per user feedback 21.09.2026: even that single extra sort/reactivate
        cycle per affected cluster was still visibly redundant ("mehrere sortings... das ist
        doch überflüssig") — a plan getting a brand-new bridge now sorts exactly once, like any
        other plan, instead of twice.

        is_fresh (plan created THIS run) only affects the cosmetic "(new)" summary tag and the
        finalize call below — every leg's own API call always uses fresh_plan=False regardless,
        see _wire_knx_leg_bridge's docstring for why.

        The rename-mismatch check below now runs ONCE per cluster, up front, instead of once per
        leg as in the old three-independent-cycles design — a narrower (not new) guard against a
        rename/repurpose of this exact plan happening mid-cycle, between two of the three legs
        this method now runs back-to-back without re-checking. Accepted as part of the same
        consolidation tradeoff as cancel_sync's coarser granularity (see _wire_knx_full's loop).
        """
        api = ctx.api
        plan_name = self._plan_name(fub_id)
        knx_label = SOURCE_CATEGORIES[WebioClass.KNX].label
        if (mismatch := self._check_plan_rename_mismatch(fub_id, cluster_ids, plan_name, knx_label)) is not None:
            # mismatch[1]/[2] (empty added-ids / the _ERR_RENAMED_MID_SYNC string) are dropped here
            # unlike _add_pairs_to_plan's/_add_io_pairs_to_plan's own use of this same helper, which
            # return the full triple for their callers' aggregate error count. _wire_knx_full has no
            # such aggregate (it only ever returns summary lines) and mismatch[0] already carries the
            # same information into that summary — accepted as harmless today, but keep in mind if an
            # aggregate error count is ever added for the KNX path too.
            ctx.failed_writes.append(_plan_failure(plan_name, "renamed during the sync"))
            return mismatch[0]

        cluster_set = set(cluster_ids)
        read_ids = sorted(read_path_ids & cluster_set)
        bridge_items = [bridge_by_id[k] for k in cluster_ids if k in bridge_by_id]
        loopback_items = [loopback_by_id[k] for k in cluster_ids if k in loopback_by_id]
        if not read_ids and not bridge_items and not loopback_items:
            return ""  # defensive: resolve_knx_clusters only groups ids from one of the three sets above

        # Capture the activation state BEFORE stop_fup — same reasoning as _add_pairs_to_plan.
        # is_fresh forces this True regardless of the raw flag: create_fup always creates plans
        # inactive (fub_active="0") AND immediately caches that same fub_info into fub_data (see
        # create_fup's own docstring/body) — so .get(..., True)'s default never actually fires for
        # a plan created THIS run, the real stored value (0/False) wins instead. Without this, a
        # brand-new cluster plan would end up permanently stopped: is_fresh=False further down
        # only controls whether the sort pass runs, was_active alone controls reactivation — same
        # invariant _add_trigger_pairs already documents and relies on.
        was_active = is_fresh or bool(api.fub_data.get(str(fub_id), {}).get("Active", True))
        t0 = time.monotonic()
        await self.coordinator.async_function_plan_change_backup(
            fub_id, f"wire_knx_full {[f'K{m}' for m in cluster_ids]}"
        )
        await api.function_plan_stop_fup(fub_id)

        all_added: list[int] = []
        all_errors: list[str] = []
        parts: list[str] = []

        try:
            if read_ids:
                added, errors, part = await self._wire_knx_leg_read_path(
                    ctx, fub_id, plan_name, read_ids, progress_state
                )
                all_added.extend(added)
                all_errors.extend(errors)
                parts.append(part)

            if bridge_items:
                added, errors, part, new_loopback_items = await self._wire_knx_leg_bridge(
                    ctx, fub_id, plan_name, bridge_items, loopback_by_id, progress_state, prestage.allocated
                )
                all_added.extend(added)
                all_errors.extend(errors)
                parts.append(part)
                # _wire_knx_full's upfront total already reserved a leg-3 slot for every open
                # bridge — release the slots of bridges that did not come out of leg 2.
                progress_state["total"] -= len(bridge_items) - len(new_loopback_items)
                loopback_items = loopback_items + new_loopback_items

            if loopback_items:
                added, errors, part = await self._wire_knx_leg_loopback(
                    ctx, fub_id, plan_name, loopback_items, progress_state, prestage.preembedded
                )
                all_added.extend(added)
                all_errors.extend(errors)
                parts.append(part)
        except (Exception, asyncio.CancelledError):
            # Anything unexpected escaping the three legs above (most legs already isolate their
            # own known failure modes, see _wire_knx_leg_bridge's re-audit try/except) must not
            # leave the plan stopped on the real Comexio server with no indication to the user —
            # async_handle_press' own except Exception only reports "Error: ..." and never
            # restarts a plan itself. Best-effort restart before letting the exception propagate,
            # same failure class _remove_trigger_pairs already guards for its own single write.
            # asyncio.CancelledError is listed explicitly — it subclasses BaseException, not
            # Exception, since Python 3.8, so a cancelled sync (HA shutdown, config-entry
            # reload, a cancelled service call) would otherwise skip this restart entirely and
            # leave the managed plan permanently stopped (Sourcery finding, review 2026-09-23).
            if was_active:
                await api.function_plan_run_fup(fub_id)
            raise

        if all_errors:
            _LOGGER.warning("[%s] _wire_knx_cluster errors on fub=%s: %s", self.server_id, fub_id, all_errors)
            ctx.failed_writes.append(_plan_failure(plan_name, f"{len(all_errors)} error(s)"))

        # is_fresh's only remaining effect is the "(new)" summary tag below and the was_active
        # override above — the finalize call itself always sorts (is_fresh=False) for the same
        # reason every leg above forces fresh_plan=False; reactivation is guaranteed regardless
        # via the corrected was_active, not via is_fresh here.
        note = await self._finalize_plan_after_pairs(
            ctx, fub_id, plan_name, list(dict.fromkeys(all_added)), False, was_active
        )
        err_note = f", {ICON_WARNING} {len(all_errors)} errors" if all_errors else ""
        line = (
            f"• '{plan_name}'{' (new)' if is_fresh else ''}: {', '.join(parts)}"
            f" in {_mmss(time.monotonic() - t0)} min{note}{err_note}"
        )
        return line

    async def _wire_io_clusters(
        self, ctx: _SyncContext, io_items: list[dict[str, str]], progress_state: dict
    ) -> tuple[list[str], int, int]:
        """Resolve the IO cluster plans and add the pairs. Returns (lines, added, errors)."""
        by_ext: dict[str, list[str]] = {}
        for item in io_items:
            by_ext.setdefault(item["ext_name"], []).append(item["identifier"])

        ext_plans, created_plans, failed_exts = await self.coordinator.resolve_io_clusters(sorted(by_ext))
        # Surface a resolve failure to the user, not just the log — see _wire_source_clusters.
        summary: list[str] = [
            f"{ICON_WARNING} IO cluster plan for '{ext}' could not be resolved/created — see log" for ext in failed_exts
        ]
        errors = len(failed_exts)
        ctx.failed_writes.extend(f"IO cluster plan for '{ext}': not resolved/created" for ext in failed_exts)
        if not ext_plans:
            if not failed_exts:
                _LOGGER.warning("[%s] Cluster plan wiring: no IO cluster plan available", self.server_id)
                summary.append(f"{ICON_WARNING} No IO cluster plan available — see log.")
                ctx.failed_writes.append("IO cluster plan: none available")
                errors = 1
            return summary, 0, errors

        plan_exts: dict[int, list[tuple[str, int]]] = {}
        for ext, (fub_id, column) in ext_plans.items():
            plan_exts.setdefault(fub_id, []).append((ext, column))

        added = 0
        for fub_id, ext_cols in plan_exts.items():
            if getattr(self.coordinator, "cancel_sync", False):
                break
            line, lp_added, lp_errors = await self._add_io_pairs_to_plan(
                ctx, fub_id, sorted(ext_cols, key=lambda t: t[1]), by_ext, fub_id in created_plans, progress_state
            )
            if lp_errors:
                ctx.failed_writes.append(_plan_failure(self._plan_name(fub_id), f"{len(lp_errors)} error(s)"))
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
        ref_type: int = 2,
    ) -> tuple[str, list[int], list[str]]:
        """Add marker/KNX pairs to one plan. Returns (summary_line, added_ids, errors).

        Freshly created plans get their pairs at final grid positions (no sort pass)
        and are activated afterwards; existing plans are re-sorted quietly and — if
        they were active before this run stopped them — reactivated. ref_type selects
        the source category (marker=2 / KNX=11 — blind guess).
        """
        api = ctx.api
        category = category_by_fub_module_type(ref_type)
        plan_name = self._plan_name(fub_id)
        if (mismatch := self._check_plan_rename_mismatch(fub_id, cluster_ids, plan_name, category.label)) is not None:
            return mismatch

        # Capture the activation state BEFORE stop_fup: function_plan_add_source_pairs
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
            fub_id, f"add_marker_pairs {[f'{category.audit_key_prefix}{m}' for m in cluster_ids]}"
        )
        await api.function_plan_stop_fup(fub_id)
        lp_added, lp_errors = await api.function_plan_add_source_pairs(
            fub_id,
            cluster_ids,
            fresh_plan=is_fresh,
            progress_cb=lambda done, total: _plan_pair_progress(ctx, progress_state, plan_name, done, total),
            ref_type=ref_type,
        )
        progress_state["done"] += len(cluster_ids)
        if lp_errors:
            _LOGGER.warning("[%s] function_plan_add_source_pairs errors: %s", self.server_id, lp_errors)

        note = await self._finalize_plan_after_pairs(ctx, fub_id, plan_name, lp_added, is_fresh, was_active)
        return (
            _plan_summary_line(plan_name, is_fresh, len(lp_added), len(cluster_ids), "pairs", t0, note, lp_errors),
            lp_added,
            lp_errors,
        )

    def _check_plan_rename_mismatch(
        self, fub_id: int, cluster_ids: list[int], plan_name: str, category_label: str
    ) -> tuple[str, list[int], list[str]] | None:
        """Aborted-result tuple if the plan was renamed/repurposed since resolve_marker_clusters() ran."""
        expected_name = self.coordinator.expected_source_cluster_name(cluster_ids[0], category_label)
        if plan_name == expected_name:
            return None
        _LOGGER.error(
            "[%s] Aborting %s pair write to fub=%s: expected managed plan '%s' but it is now named "
            "'%s' — it was renamed/repurposed since resolve_marker_clusters() ran",
            self.server_id,
            category_label,
            fub_id,
            expected_name,
            plan_name,
        )
        return (
            f"{ICON_WARNING} fub {fub_id}: aborted — expected '{expected_name}' but plan is now '{plan_name}'",
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
            if was_active and not await ctx.api.function_plan_run_fup(fub_id):
                return _activation_note(ctx, plan_name, False)
            return ""

        if is_fresh:
            # Fresh plan: pairs already sit at their final grid slots — skip the sort pass.
            self._plan_finalize_status(ctx, plan_name, "activating plan", "activating")
            return _activation_note(ctx, plan_name, await ctx.api.function_plan_run_fup(fub_id))

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
        note = f", sorted in {sort_res['duration']:.1f}s" if sorted_ok else f", {ICON_WARNING} sort failed"
        if was_active and not (sort_res and sort_res.get("activated")):
            # Sort was skipped or lost the reactivation (e.g. save_elements_pos failed) —
            # don't leave a previously active plan stopped.
            note += _activation_note(ctx, plan_name, await ctx.api.function_plan_run_fup(fub_id))
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
                f"{ICON_WARNING} fub {fub_id} ('{plan_name}'): aborted — no longer matches "
                f"expected IO cluster for {stale_exts}",
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
        await api.function_plan_stop_fup(fub_id)

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
            activated = await api.function_plan_run_fup(fub_id)
            if added or not activated:
                note = _activation_note(ctx, plan_name, activated)

        return _plan_summary_line(plan_name, is_fresh, len(added), n_total, "IO pairs", t0, note, errors), added, errors


def _parse_source_id_from_webio_name(name: str, prefix: str) -> int | None:
    """Extract the source ID from a Web-IO command name like 'HA M68 Title' or 'HA K41 Title'.

    `prefix` is the category's audit-key prefix ("M" for markers, "K" for KNX — blind guess).
    Returns the integer source ID, or None if the name doesn't match the given category's pattern.
    """
    parts = name.split()
    if len(parts) >= 2 and parts[0] == "HA" and parts[1].upper().startswith(prefix.upper()):
        with contextlib.suppress(ValueError):
            return int(parts[1][len(prefix) :])
    return None


def _parse_io_from_webio_name(name: str, known_names: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """Extract (ext_name, identifier) from a Web-IO command name like 'HA IO UD1 Q3'.

    Tries an exact reverse lookup against the known (ext_name, identifier) pairs first, since
    a positional `name.split()` misparses an extension name that itself contains spaces. Falls
    back to the positional heuristic only for names with no known counterpart.
    """
    if ref := known_names.get(name):
        return ref
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


class ComexioMarkerTriggerButton(ComexioMarkerEntity, ButtonEntity):
    """Representation of a "virtueller Taster" ([TRIG]/[TP]) Comexio Marker as a Button.

    Pressing it writes a single 1 via the normal marker Web-IO write path. The marker's
    own auto-reset back to 0 runs entirely inside Comexio (Marker+Flanke self-reset loop
    in the dedicated "HA - TRIGGER" plan) — no HA-side wait/reset is needed.
    """

    async def async_press(self) -> None:
        """Fire the trigger by writing 1 to the source once.

        Re-checks the source's current kind against the coordinator's last-polled data
        (not the kind captured at entity creation) so a title edit that drops [TRIG]/[TP]
        or adds [RO] after this button was created can't be bypassed by a stale,
        still-registered entity. Like every other write path in this integration, this
        reads coordinator.data as of the most recent poll — it narrows the staleness
        window to at most one poll interval, it does not eliminate it.
        """
        data_key = SOURCE_CATEGORIES[self._SOURCE].data_key
        source = next(
            (s for s in self.coordinator.data.get(data_key, []) if str(s.get("id")) == self._marker_id),
            None,
        )
        if source is None or source.get("kind") != MarkerKind.TRIGGER:
            raise HomeAssistantError(
                f"{self._source_label} {self._marker_id} is no longer a trigger "
                f"{self._source_label} — reload the integration to refresh entities."
            )
        if not await self._async_source_write(1):
            raise HomeAssistantError(f"Failed to trigger {self._source_label} {self._marker_id}")


class ComexioKnxTriggerButton(ComexioKnxEntity, ComexioMarkerTriggerButton):
    """A "virtueller Taster" ([TRIG]/[TP]) Comexio KNX object as a Button (blind, see project_knx_objects memory)."""


_TRIGGER_BUTTON_CLASSES: dict[WebioClass, type[ComexioMarkerTriggerButton]] = {
    WEBIO_CLASS_MARKER: ComexioMarkerTriggerButton,
    WEBIO_CLASS_KNX: ComexioKnxTriggerButton,
}


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
                msg = f"{ICON_SUCCESS} Firmware check finished in {duration:.1f}s.\n\nChecked modules: {names}"
            else:
                msg = f"{ICON_WARNING} Firmware check returned no data ({duration:.1f}s) — see log for details."
            persistent_notification.async_create(
                self.hass,
                msg,
                title=f"Comexio Firmware Check ({self.server_id})",
                notification_id=f"comexio_fw_check_{self.server_id}",
            )


class ComexioWebioRangeCheckButton(CoordinatorEntity, ButtonEntity):
    """Button to force-run the Web-IO analog Min/Max range check outside its nightly window.

    See coordinator.async_force_webio_range_check / async_start_webio_range_check — the
    check itself is a plain safe GET per analog command, so unlike the firmware check this
    has no risk gate to bypass; the button exists purely so drift doesn't have to wait for
    the next WEBIO_RANGE_CHECK_HOUR window.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_webio_range_check_btn"
        self._attr_translation_key = "webio_range_check"
        self._attr_icon = "mdi:ruler"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # Pin the object_id instead of leaving it to derive from the translated name —
        # that derivation only runs once, at first registration, so a translation file
        # that's momentarily out of sync with the code (deploy-order race) would freeze
        # a bad entity_id into the registry permanently.
        self.entity_id = webio_range_check_entity_id(server_id)

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    async def async_press(self) -> None:
        """Force-run the Web-IO analog range check."""
        _LOGGER.warning("[%s] Manual Web-IO range check requested by user", self.server_id)
        start = time.monotonic()
        result = await self.coordinator.async_force_webio_range_check()
        duration = time.monotonic() - start

        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        if conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS):
            if result[RANGE_CHECK_SKIPPED]:
                icon = ICON_WARNING
                msg = (
                    f"{icon} Web-IO range check skipped — a sync is in progress or no data is "
                    "loaded yet. Please try again shortly."
                )
            else:
                has_problems = (
                    result[RANGE_CHECK_FAILED] or result[RANGE_CHECK_CORRECTION_FAILED] or result[RANGE_CHECK_EXCLUDED]
                )
                icon = ICON_WARNING if has_problems else ICON_SUCCESS
                msg = (
                    f"{icon} Web-IO range check finished in {duration:.1f}s.\n\n"
                    f"Checked: {result[RANGE_CHECK_CHECKED]}, corrected: {result[RANGE_CHECK_FIXED]}, "
                    f"failed to read: {result[RANGE_CHECK_FAILED]}, "
                    f"correction failed: {result[RANGE_CHECK_CORRECTION_FAILED]}, "
                    f"excluded: {result[RANGE_CHECK_EXCLUDED]}."
                )
            persistent_notification.async_create(
                self.hass,
                msg,
                title=f"Comexio Web-IO Range Check ({self.server_id})",
                notification_id=f"comexio_webio_range_check_{self.server_id}",
            )


class ComexioEntityIdMigrationButton(CoordinatorEntity, ButtonEntity):
    """Button to migrate entity_ids to the Comexio scheme (see coordinator.detect_entity_id_mismatches)."""

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
        """Migrate entity_ids to the Comexio scheme and report the outcome.

        Unlike the repair dialog the button has no confirmation step, so the result —
        including how many automations/scripts still reference the old ids — is posted as
        a notification (English, like all persistent notifications of this integration).
        """
        old_ids = [m["current_id"] for m in self.coordinator.entity_id_mismatches]
        references = count_referencing_automations_and_scripts(self.hass, old_ids)
        migrated = self.coordinator.async_migrate_entity_ids()
        failed = len(self.coordinator.entity_id_mismatches)
        if not failed:
            ir.async_delete_issue(self.hass, DOMAIN, f"entity_id_mismatch_{self.server_id}")
        self.coordinator.async_set_updated_data(self.coordinator.data)
        persistent_notification.async_create(
            self.hass,
            f"{migrated} entity IDs renamed, {failed} failed (see log). History and statistics moved along. "
            f"At least {references} automations/scripts (? = could not be checked) referenced the old entity IDs "
            "and must be updated; "
            "please check dashboards as well.",
            title=f"Comexio {self.server_id}: entity ID migration",
            notification_id=f"comexio_entity_id_migration_{self.server_id}",
        )


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
            return

        plan_data = await api.function_plan_load_elements(fub_id)
        if not plan_data:
            _LOGGER.warning("[%s] Plan preview: could not load plan %s", self.server_id, fub_id)
            return
        await self.coordinator.async_generate_plan_preview(
            fub_id, plan_name, plan_data.get("elements", {}), plan_data.get("connections", {}), "live"
        )


class ComexioCleanupButton(CoordinatorEntity, ButtonEntity):
    """Raises the uninstall-cleanup Repair issue, whose dialog tears down what the
    integration created in Comexio — everything, or one scope (markers / IOs / KNX).
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
        deletion only runs when the user confirms it (and picks a scope) in the Repair dialog."""
        # Force a fresh audit rather than trusting a poll-interval-old snapshot — devices
        # created/removed since the last poll would otherwise be missed or acted on stale.
        await self.coordinator.async_request_refresh()
        self.coordinator.create_uninstall_cleanup_issue(ISSUE_UNINSTALL_CLEANUP)
        conf = {**self.coordinator.config_entry.data, **self.coordinator.config_entry.options}
        if conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS):
            persistent_notification.async_create(
                self.hass,
                "A repair issue **Uninstall Cleanup** was created. Open "
                "[Settings → System → Repairs](/config/repairs), choose what to remove "
                "(everything, markers, IOs or KNX) and confirm. Nothing is deleted until you do.",
                title=f"Comexio Uninstall Cleanup ({self.coordinator.server_id})",
                notification_id=uninstall_cleanup_pending_notification_id(self.coordinator.server_id),
            )


class ComexioPlanToggleButton(CoordinatorEntity, ButtonEntity):
    """Start/stop toggle for the currently selected 'Function Plans' plan.

    The icon always shows the OPPOSITE of the plan's current activation state — mdi:pause
    while it's running (press to stop it), mdi:play while it's stopped (press to start it).
    A thin UI wrapper around the existing function_plan_stop/function_plan_activate services,
    so notification/logging/duration reporting stays in one place instead of being duplicated.
    """

    _attr_has_entity_name = True
    _attr_name = "Function Plan Toggle"

    def __init__(self, coordinator: ComexioCoordinator, server_id: str) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.server_id = server_id
        self._attr_unique_id = f"comexio_{server_id}_function_plan_toggle_btn"
        # Set for the duration of async_press so the icon can show an in-flight state —
        # the stop/activate service call + raw_config refresh below take a few seconds, during
        # which the icon would otherwise still show the pre-press (now stale) state.
        self._pending = False

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    def _selected_fub_id(self) -> int | None:
        return self.coordinator.get_active_function_plan_fub_id()

    def _selected_plan_active(self) -> bool | None:
        """True/False for the selected plan's live Active flag, None if nothing is selected."""
        fub_id = self._selected_fub_id()
        if fub_id is None:
            return None
        return self.coordinator.api.get_fub_active(fub_id)

    @property
    def icon(self) -> str:
        if self._pending:
            return "mdi:progress-clock"
        return "mdi:play" if self._selected_plan_active() is False else "mdi:pause"

    @property
    def available(self) -> bool:
        """Grayed out with no plan selected, or while its live Active flag isn't known yet
        (fub_id missing from the cached fub_data — toggling would be a guess)."""
        return self._selected_fub_id() is not None and self._selected_plan_active() is not None

    async def async_press(self) -> None:
        fub_id = self._selected_fub_id()
        active = self._selected_plan_active()
        if fub_id is None or active is None:
            return
        service = FUNCTION_PLAN_SERVICE_ACTIVATE if active is False else FUNCTION_PLAN_SERVICE_STOP
        self._pending = True
        self.async_write_ha_state()
        try:
            await self.hass.services.async_call(
                DOMAIN,
                service,
                {"config_entry": self.coordinator.config_entry.entry_id, "fub_id": fub_id},
                blocking=True,
            )
            # Reflect the actual resulting state right away — the selected plan's Active flag
            # otherwise wouldn't update until the next poll cycle, leaving both this button's
            # icon and the 'Function Plans' dropdown's inactive marker stale for a whole
            # scan_interval.
            api = self.coordinator.api
            raw_config = await api.get_raw_config()
            live_fub = raw_config.get("Fubs", {}).get(str(fub_id))
            if live_fub is not None:
                api.update_fub_cache_entry(fub_id, live_fub)
        finally:
            self._pending = False
            self.coordinator.async_update_listeners()
