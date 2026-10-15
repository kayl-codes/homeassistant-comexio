"""Watchdog for the function plans HA manages (plan_map, the trigger plan included).

HA's markers, IOs, KNX objects and triggers only work while their cluster plans run in
Comexio. After every run-state fetch the coordinator checks them here, together with the user
plans picked in the options: a stopped plan raises a fixable repair ("start now"), turns the
problem sensor on, sends a push with a "Start plan" action to the configured notify services
and, with the auto-start switch of its kind (HA plans / user plans) on, is started again
right away. After FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS failed auto-starts (refused, or
stopped again shortly after) Auto-Start gives up on the plan with a "check the plan" repair and
alarm push, until a run-state poll sees the plan running again.

A watched user plan Comexio no longer has at all (deleted in Comexio Studio) is not reported as
stopped: it gets a "plan no longer exists" repair and alarm push, with the actions "Restore"
(its newest backup, as a new plan) and "Stop watching". Comexio answers a deleted plan's run
state like a stopped one's, so the coordinator re-reads the plan list before it reports a
watched plan as newly stopped.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
import logging
import re
import time
from typing import Any
import zlib

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    DOMAIN,
    FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS,
    FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC,
    FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC,
    FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_FUNCTION_PLAN_STOPPED = "function_plan_stopped"
# Translation key of a stopped user plan's repair; same issue id scheme and fix flow.
ISSUE_FUNCTION_PLAN_STOPPED_USER = "function_plan_stopped_user"
# Translation key once Plan Auto-Start gave up on a plan ("check the plan"); same issue id and fix flow.
ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED = "function_plan_auto_start_suspended"
# Repair of a watched user plan Comexio no longer has (deleted); one per plan, its own id scheme.
ISSUE_FUNCTION_PLAN_VANISHED = "function_plan_vanished"
START_ACTION_PREFIX = "COMEXIO_START_PLAN_"
RESTORE_ACTION_PREFIX = "COMEXIO_RESTORE_PLAN_"
UNWATCH_ACTION_PREFIX = "COMEXIO_UNWATCH_PLAN_"
ACTION_START = "start"
ACTION_RESTORE = "restore"
ACTION_UNWATCH = "unwatch"
_ACTION_PREFIXES = {
    ACTION_START: START_ACTION_PREFIX,
    ACTION_RESTORE: RESTORE_ACTION_PREFIX,
    ACTION_UNWATCH: UNWATCH_ACTION_PREFIX,
}
NOTIFY_DOMAIN = "notify"
MOBILE_APP_SERVICE_PREFIX = "mobile_app_"

# Push texts by HA language (English fallback); {name} and {fub_id} are filled in.
PUSH_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "stopped_title": "Comexio: function plan stopped",
        "stopped": "The monitored function plan {name} (ID {fub_id}) is not running.",
        "start": "Start plan",
        "started_title": "Comexio: function plan started",
        "started": "The function plan {name} (ID {fub_id}) runs again.",
        "auto_started": (
            "The function plan {name} (ID {fub_id}) was not running and has been started by Plan Auto-Start."
        ),
        "failed_title": "Comexio: start failed",
        "failed": "Comexio did not confirm the start of {name} (ID {fub_id}).",
        "busy_title": "Comexio: start not sent",
        "busy": (
            "{name} (ID {fub_id}) was not started: a sync, restore, poll, function plan edit or bus-load "
            "watchdog run is in progress. Try again in a moment."
        ),
        "suspended_title": "Comexio: check function plan",
        "suspended": (
            "Plan Auto-Start could not keep {name} (ID {fub_id}) running after {attempts} attempts and "
            "stopped trying. Please check the plan."
        ),
        "vanished_title": "Comexio: function plan deleted",
        "vanished": "The monitored function plan {name} (ID {fub_id}) no longer exists in Comexio.",
        "restore": "Restore",
        "unwatch": "Stop watching",
        "no_backup_title": "Comexio: restore not possible",
        "no_backup": "There is no backup of {name} (ID {fub_id}) to restore it from.",
        "restored_title": "Comexio: function plan restored",
        "restored": "{name} was restored from its newest backup as a new plan and stays monitored.",
        "restored_partial": "{name} was restored as a new plan, but not completely; see the Home Assistant "
        "notifications.",
        "restore_failed_title": "Comexio: restore failed",
        "restore_failed": "{name} (ID {fub_id}) was not restored; see the Home Assistant notifications.",
        "check_pending_title": "Comexio: plan list not read yet",
        "check_pending": "The plan list was not read since the start yet. Please try again shortly.",
        "time_format": "%Y-%m-%d %H:%M:%S",
    },
    "de": {
        "stopped_title": "Comexio: Logikplan gestoppt",
        "stopped": "Der überwachte Logikplan {name} (ID {fub_id}) läuft nicht.",
        "start": "Plan starten",
        "started_title": "Comexio: Logikplan gestartet",
        "started": "Der Logikplan {name} (ID {fub_id}) läuft wieder.",
        "auto_started": "Der Logikplan {name} (ID {fub_id}) lief nicht und wurde per Plan Auto-Start gestartet.",
        "failed_title": "Comexio: Start fehlgeschlagen",
        "failed": "Comexio hat den Start von {name} (ID {fub_id}) nicht bestätigt.",
        "busy_title": "Comexio: Start nicht gesendet",
        "busy": (
            "{name} (ID {fub_id}) wurde nicht gestartet: Gerade läuft ein Sync, eine Wiederherstellung, ein "
            "Abruf, eine Logikplan-Änderung oder der Buslast-Watchdog. Versuche es gleich noch einmal."
        ),
        "suspended_title": "Comexio: Logikplan prüfen",
        "suspended": (
            "Plan Auto-Start konnte {name} (ID {fub_id}) nach {attempts} Versuchen nicht am Laufen halten "
            "und versucht es nicht weiter. Bitte den Plan prüfen."
        ),
        "vanished_title": "Comexio: Logikplan gelöscht",
        "vanished": "Der überwachte Logikplan {name} (ID {fub_id}) existiert in Comexio nicht mehr.",
        "restore": "Wiederherstellen",
        "unwatch": "Nicht mehr überwachen",
        "no_backup_title": "Comexio: Wiederherstellen nicht möglich",
        "no_backup": "Es gibt kein Backup von {name} (ID {fub_id}), aus dem er wiederhergestellt werden kann.",
        "restored_title": "Comexio: Logikplan wiederhergestellt",
        "restored": "{name} wurde aus seinem neuesten Backup als neuer Plan wiederhergestellt und bleibt überwacht.",
        "restored_partial": "{name} wurde als neuer Plan wiederhergestellt, aber nicht vollständig, "
        "siehe HA-Benachrichtigungen.",
        "restore_failed_title": "Comexio: Wiederherstellen fehlgeschlagen",
        "restore_failed": "{name} (ID {fub_id}) wurde nicht wiederhergestellt, siehe HA-Benachrichtigungen.",
        "check_pending_title": "Comexio: Planliste noch nicht gelesen",
        "check_pending": "Die Planliste wurde seit dem Start noch nicht gelesen. Bitte gleich erneut versuchen.",
        "time_format": "%d.%m.%Y %H:%M:%S",
    },
}


def _issue_prefix(server_id: str) -> str:
    return f"{ISSUE_FUNCTION_PLAN_STOPPED}_{server_id}_"


def _vanished_issue_prefix(server_id: str) -> str:
    return f"{ISSUE_FUNCTION_PLAN_VANISHED}_{server_id}_"


def stopped_plan_issue_id(server_id: str, fub_id: int) -> str:
    """Repair issue id for one stopped managed plan."""
    return f"{_issue_prefix(server_id)}{fub_id}"


def vanished_plan_token(fub_id: int, name: str) -> str:
    """A deleted plan's identity in its repair id, push tag and push actions: its ID and a checksum of its name.

    The ID alone is not one: an ID can be reused, so two deleted watched plans may share it (picks
    "47:X" and "47:Y"). The name itself cannot go into an action id (any character), its checksum can.
    """
    return f"{fub_id}_{zlib.crc32(name.encode()):08x}"


# vanished_plan_token: the plan id (group) and the name checksum.
_TOKEN_PATTERN = r"(\d+)_[0-9a-f]{8}"


def unnamed_plan_label(fub_id: int) -> str:
    """Name of a bare-ID pick whose backups carry no single plan name: no backup identity to restore."""
    return f"ID {fub_id}"


def vanished_plan_restorable(fub_id: int, name: str) -> bool:
    """Whether a deleted plan has a backup identity to restore by; not a bare-ID pick its backups name ambiguously."""
    return name != unnamed_plan_label(fub_id)


def _vanished_actions(fub_id: int, name: str) -> tuple[str, ...]:
    """A deleted plan's push buttons: "Restore" only where vanished_plan_restorable."""
    return (ACTION_RESTORE, ACTION_UNWATCH) if vanished_plan_restorable(fub_id, name) else (ACTION_UNWATCH,)


def vanished_plan_issue_id(server_id: str, fub_id: int, name: str) -> str:
    """Repair issue id for one watched user plan Comexio no longer has."""
    return f"{_vanished_issue_prefix(server_id)}{vanished_plan_token(fub_id, name)}"


def _own_issue_ids(hass: HomeAssistant, server_id: str, *, vanished: bool = False) -> list[str]:
    """Ids of this instance's stopped-plan (or vanished-plan) repairs.

    The prefix alone is not enough: for server "home" it also matches the repairs of a
    server "home_2", so the rest of the id must be exactly the plan id (or vanished-plan token).
    """
    prefix = _vanished_issue_prefix(server_id) if vanished else _issue_prefix(server_id)
    own = re.compile(re.escape(prefix) + (_TOKEN_PATTERN if vanished else r"\d+"))
    return [issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN and own.fullmatch(issue_id)]


def start_action_id(server_id: str, fub_id: int) -> str:
    """Action id of the push's "Start plan" button."""
    return f"{START_ACTION_PREFIX}{server_id}_{fub_id}"


def plan_action_id(kind: str, server_id: str, fub_id: int, token: str | None = None) -> str:
    """Action id of a push button: ACTION_START (by plan id), ACTION_RESTORE or ACTION_UNWATCH (by vanished token)."""
    return f"{_ACTION_PREFIXES[kind]}{server_id}_{fub_id if kind == ACTION_START else token}"


def parse_plan_action(server_id: str, action: Any) -> tuple[str, int, str | None] | None:
    """(kind, plan id, vanished token) of one of this instance's push actions, None for any other action.

    The token is None for "Start plan". fullmatch, so server "home" does not pick up the actions
    of server "home_2".
    """
    if not isinstance(action, str):
        return None
    for kind, prefix in _ACTION_PREFIXES.items():
        pattern = r"(\d+)" if kind == ACTION_START else f"(?P<token>{_TOKEN_PATTERN})"
        if match := re.fullmatch(rf"{re.escape(prefix + server_id)}_{pattern}", action):
            if kind == ACTION_START:
                return kind, int(match[1]), None
            return kind, int(match[2]), match["token"]
    return None


def parse_start_action(server_id: str, action: Any) -> int | None:
    """The plan id of this instance's "Start plan" action, None for any other action."""
    parsed = parse_plan_action(server_id, action)
    return parsed[1] if parsed is not None and parsed[0] == ACTION_START else None


def next_stopped_plans(
    managed: Mapping[int, str], run_state: Callable[[int], bool | None], previous: Collection[int]
) -> dict[int, str]:
    """fub_id → name of the managed plans that do not run.

    A plan whose run state is unknown right now (None: unreadable, or stopped by HA itself a
    moment ago) keeps its previous verdict, so a poll outage neither raises nor clears a repair.
    """
    stopped: dict[int, str] = {}
    for fub_id, name in managed.items():
        running = run_state(fub_id)
        if running is False or (running is None and fub_id in previous):
            stopped[fub_id] = name
    return stopped


class ManagedPlanWatchdog:
    """Keeps the repairs, the pushes, the problem sensor and the optional auto-start of the managed plans."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        server_id: str,
        start_plan: Callable[[int], Awaitable[bool | None]],
        notify_targets: Callable[[], list[str]],
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._server_id = server_id
        self._start_plan = start_plan
        self._notify_targets = notify_targets
        # Set by the two auto-start switches, which restore the user's last choice over these
        # defaults: HA's own plans should run (on), user plans only start when asked to (off).
        self.auto_restart = True
        self.auto_restart_user = False
        # fub ids of the watched user plans, as of the last check.
        self._user_plans: frozenset[int] = frozenset()
        # fub_id → name of the managed plans not running; None until the first check.
        self.stopped: dict[int, str] | None = None
        # fub_id → failed auto-starts in a row (refused, or stopped again within the window).
        self._failed_starts: dict[int, int] = {}
        # fub_id → time.monotonic() of the last successful auto-start (stopped-again detection).
        self._auto_started_at: dict[int, float] = {}
        # Plans Auto-Start gave up on (FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS); lifted once seen running.
        self.suspended: set[int] = set()
        # fub_id → local time Auto-Start gave up on it (the last failed attempt), shown in its repair.
        self._gave_up_at: dict[int, str] = {}
        # fub_id → time.monotonic() of its last refused auto-start (FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC).
        self._refused_at: dict[int, float] = {}
        # fub_id → time.monotonic() of its last auto-start not sent or not answered (no failed attempt, but
        # retried no sooner than FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC either).
        self._deferred_at: dict[int, float] = {}
        # (fub_id, name) of the watched user plans Comexio no longer has (deleted); None until the first check.
        # By ID and name: a reused ID can belong to two deleted watched plans.
        self.vanished: set[tuple[int, str]] | None = None
        # Tokens (vanished_plan_token) of vanished plans whose push was replaced by "restored": the plan
        # dropping out of vanished keeps it.
        self._restored_pushes: set[str] = set()
        # token → time.monotonic() of a vanished alarm push no notify service took: sent again with a
        # check at least FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC later; any push delivered for the plan drops it.
        self._vanished_push_unsent: dict[str, float] = {}
        # Local time of the last check; shown in the repair dialogs and on the problem sensor.
        self.last_check: datetime | None = None
        self._checking = False

    async def async_check(
        self,
        managed: Mapping[int, str],
        run_state: Callable[[int], bool | None],
        user_plans: Collection[int] = (),
        *,
        auto_start: bool = True,
    ) -> bool:
        """Judge the watched plans; True if the set of stopped plans changed.

        managed holds every watched plan; user_plans names the ones that are user plans, which
        follow the user-plan auto-start switch instead of the HA-plan one. auto_start=False
        starts nothing, whatever the switches say.
        """
        if self._checking:
            return False
        self._checking = True
        try:
            self.last_check = dt_util.now()
            self._user_plans = frozenset(user_plans)
            previous = self.stopped or {}
            suspended_before = frozenset(self.suspended)
            self._track_running(managed, run_state)
            stopped = next_stopped_plans(managed, run_state, previous)
            self._log_transitions(previous, stopped, managed)
            to_start = {
                fub_id: name for fub_id, name in stopped.items() if auto_start and self._auto_start_enabled(fub_id)
            }
            auto_started: dict[int, str] = {}
            if to_start:
                auto_started = await self._async_auto_start(to_start)
                stopped = {fub_id: name for fub_id, name in stopped.items() if fub_id not in auto_started}
            self._sync_issues(stopped)
            changed = self.stopped != stopped or self.suspended != suspended_before
            self.stopped = stopped
            await self._async_push_transitions(previous, stopped, auto_started, self.suspended - suspended_before)
            return changed
        finally:
            self._checking = False

    def last_check_text(self) -> str:
        """The last check as local time in the HA language's format, "—" before the first one."""
        return "—" if self.last_check is None else self.last_check.strftime(self._texts()["time_format"])

    def is_user_plan(self, fub_id: int) -> bool:
        return fub_id in self._user_plans

    def _auto_start_enabled(self, fub_id: int) -> bool:
        return self.auto_restart_user if self.is_user_plan(fub_id) else self.auto_restart

    async def async_plan_started(self, fub_id: int, *, confirm: bool = False) -> None:
        """A stopped plan was started from its repair or push: clear it without waiting for the next poll.

        confirm replaces the push with a "runs again" one (started from the phone), else the push is removed.
        """
        # The suspension is not touched here: the run-state fetch right after the start (or the next
        # poll) lifts it once it sees the plan running.
        self._auto_started_at.pop(fub_id, None)
        ir.async_delete_issue(self._hass, DOMAIN, stopped_plan_issue_id(self._server_id, fub_id))
        name = (self.stopped or {}).get(fub_id, str(fub_id))
        if self.stopped is not None:
            self.stopped = {key: value for key, value in self.stopped.items() if key != fub_id}
        if confirm:
            await self._async_push(fub_id, name, "started_title", "started")
        else:
            await self._async_clear_push(fub_id)

    async def async_push_start_failed(self, fub_id: int, name: str) -> None:
        """Tell the phone a start from its push failed, with the "Start plan" action again."""
        await self._async_push(fub_id, name, "failed_title", "failed", with_action=True)

    async def async_push_start_busy(self, fub_id: int, name: str) -> None:
        """Tell the phone a start from its push was not sent (something else runs), with the action again."""
        await self._async_push(fub_id, name, "busy_title", "busy", with_action=True)

    def _log_transitions(
        self, previous: Mapping[int, str], stopped: Mapping[int, str], managed: Mapping[int, str]
    ) -> None:
        for fub_id in stopped.keys() - previous.keys():
            _LOGGER.warning(
                "[%s] Monitored function plan '%s' (ID %s) is not running in Comexio",
                self._server_id,
                stopped[fub_id],
                fub_id,
            )
        for fub_id in previous.keys() - stopped.keys():
            # A stopped plan deleted, renamed or unpicked (or its ID reused) drops out of managed: no recovery.
            verdict = "runs again" if fub_id in managed else "is no longer watched (deleted, renamed or unselected)"
            _LOGGER.info(
                "[%s] Monitored function plan '%s' (ID %s) %s", self._server_id, previous[fub_id], fub_id, verdict
            )

    def _track_running(self, managed: Mapping[int, str], run_state: Callable[[int], bool | None]) -> None:
        """Lift the Auto-Start suspension of a plan seen running; forget plans no longer watched.

        A plan seen running starts with a clean slate — unless it is still inside the stopped-again
        window of its own auto-start, where it has yet to prove it keeps running.
        """
        now = time.monotonic()
        lifted = {fub_id for fub_id in self.suspended if fub_id not in managed or run_state(fub_id) is True}
        on_probation = {
            fub_id
            for fub_id, started_at in self._auto_started_at.items()
            if now - started_at < FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC
        }
        healthy = {
            fub_id
            for fub_id in self._auto_started_at.keys() | self._failed_starts.keys()
            if fub_id not in managed or (run_state(fub_id) is True and fub_id not in on_probation)
        }
        for fub_id in lifted & managed.keys():
            _LOGGER.info(
                "[%s] Function plan '%s' (ID %s) runs again; Plan Auto-Start resumes for it",
                self._server_id,
                managed[fub_id],
                fub_id,
            )
        self.suspended -= lifted
        self._deferred_at = {
            fub_id: at
            for fub_id, at in self._deferred_at.items()
            if fub_id in managed and run_state(fub_id) is not True
        }
        for fub_id in lifted:
            self._gave_up_at.pop(fub_id, None)
        for fub_id in lifted | healthy:
            self._auto_started_at.pop(fub_id, None)
            self._failed_starts.pop(fub_id, None)
            self._refused_at.pop(fub_id, None)

    def _count_failed_start(self, fub_id: int, name: str, reason: str) -> None:
        """One more failed auto-start; at FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS Auto-Start gives up on the plan."""
        failed = self._failed_starts.get(fub_id, 0) + 1
        self._failed_starts[fub_id] = failed
        _LOGGER.warning(
            "[%s] Auto-start of monitored function plan '%s' (ID %s) failed (%s), attempt %s of %s",
            self._server_id,
            name,
            fub_id,
            reason,
            failed,
            FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS,
        )
        if failed >= FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS:
            self.suspended.add(fub_id)
            self._gave_up_at[fub_id] = dt_util.now().strftime(self._texts()["time_format"])
            _LOGGER.warning(
                "[%s] Plan Auto-Start gave up on function plan '%s' (ID %s); check the plan in Comexio Studio. "
                "Auto-Start resumes once the plan is seen running again",
                self._server_id,
                name,
                fub_id,
            )

    async def _async_auto_start(self, stopped: Mapping[int, str]) -> dict[int, str]:
        """Start the stopped plans Auto-Start has not given up on; returns the ones started."""
        started: dict[int, str] = {}
        for fub_id, name in stopped.items():
            # Read per plan: each start awaits Comexio, which serializes requests, so a time taken
            # once before the loop would date the later plans' starts and refusals too early.
            now = time.monotonic()
            started_at = self._auto_started_at.pop(fub_id, None)
            if started_at is not None and now - started_at < FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC:
                self._count_failed_start(fub_id, name, "stopped again shortly after its start")
            # Re-checked per plan: the switch can be turned off while an earlier start awaits Comexio.
            if fub_id in self.suspended or not self._auto_start_enabled(fub_id):
                continue
            if self._retry_too_soon(fub_id, now):
                _LOGGER.debug(
                    "[%s] Auto-start of monitored function plan '%s' (ID %s) waits until %s s after its last attempt",
                    self._server_id,
                    name,
                    fub_id,
                    FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
                )
                continue
            result = await self._start_plan(fub_id)
            now = time.monotonic()  # the answer's time: the stopped-again window and the retry gap start here
            deferred_before = self._deferred_at.pop(fub_id, None) is not None
            if result:
                self._auto_started_at[fub_id] = now
                self._notify_auto_started(fub_id, name)
                started[fub_id] = name
            elif result is False:
                self._refused_at[fub_id] = now
                self._count_failed_start(fub_id, name, "start refused by Comexio")
            else:
                self._defer_start(fub_id, name, now, logged_before=deferred_before)
        return started

    def _defer_start(self, fub_id: int, name: str, now: float, *, logged_before: bool) -> None:
        """A start not sent (a sync/restore runs) or not answered: no failed attempt, but the retry gap applies.

        Without the gap an unanswered start would be sent again with every check; logged at info once
        per series, the retries at debug.
        """
        self._deferred_at[fub_id] = now
        _LOGGER.log(
            logging.DEBUG if logged_before else logging.INFO,
            "[%s] Auto-start of monitored function plan '%s' (ID %s) not sent or not answered; retried in %s s",
            self._server_id,
            name,
            fub_id,
            FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
        )

    def _retry_too_soon(self, fub_id: int, now: float) -> bool:
        return any(
            (last := attempts.get(fub_id)) is not None and now - last < FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC
            for attempts in (self._refused_at, self._deferred_at)
        )

    def _notify_auto_started(self, fub_id: int, name: str) -> None:
        _LOGGER.warning("[%s] Auto-started monitored function plan '%s' (ID %s)", self._server_id, name, fub_id)
        switch = "Plan Auto-Start (user plans)" if self.is_user_plan(fub_id) else "Plan Auto-Start (HA plans)"
        persistent_notification.async_create(
            self._hass,
            f"The monitored function plan **{name}** (ID {fub_id}) was not running in Comexio and has been "
            f"started again by the **{switch}** switch.",
            title=f"Comexio {self._server_id}: function plan started",
            notification_id=f"{DOMAIN}_{self._server_id}_plan_auto_start_{fub_id}",
        )

    def _sync_issues(self, stopped: Mapping[int, str]) -> None:
        """Raise a repair per stopped plan and close the ones of plans that run again."""
        wanted: set[str] = set()
        for fub_id, name in stopped.items():
            issue_id = stopped_plan_issue_id(self._server_id, fub_id)
            wanted.add(issue_id)
            details = {
                "plan_name": name,
                "fub_id": str(fub_id),
                "attempts": str(FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS),
            }
            if fub_id in self._gave_up_at:
                details["gave_up_at"] = self._gave_up_at[fub_id]
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=self._issue_translation_key(fub_id),
                translation_placeholders=details,
                # The fix flow gets only this data, not the placeholders.
                data={"entry_id": self._entry_id, **details},
            )
        for issue_id in _own_issue_ids(self._hass, self._server_id):
            if issue_id not in wanted:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    def _issue_translation_key(self, fub_id: int) -> str:
        if fub_id in self.suspended:
            return ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED
        return ISSUE_FUNCTION_PLAN_STOPPED_USER if self.is_user_plan(fub_id) else ISSUE_FUNCTION_PLAN_STOPPED

    async def _async_push_transitions(
        self,
        previous: Mapping[int, str],
        stopped: Mapping[int, str],
        auto_started: Mapping[int, str],
        newly_suspended: Collection[int],
    ) -> None:
        """Alarm push for a newly stopped plan or one Auto-Start gave up on; the push of a plan running
        again (or deleted: its vanished push has a tag of its own) is replaced or removed."""
        for fub_id in sorted(stopped.keys() - previous.keys() - set(newly_suspended)):
            await self._async_push(fub_id, stopped[fub_id], "stopped_title", "stopped", with_action=True, alarm=True)
        for fub_id in sorted(set(newly_suspended) & stopped.keys()):
            await self._async_push(
                fub_id, stopped[fub_id], "suspended_title", "suspended", with_action=True, alarm=True
            )
        for fub_id, name in sorted(auto_started.items()):
            await self._async_push(fub_id, name, "started_title", "auto_started")
        for fub_id in sorted(previous.keys() - stopped.keys() - auto_started.keys()):
            await self._async_clear_push(fub_id)

    def _texts(self) -> dict[str, str]:
        return PUSH_TEXTS.get(self._hass.config.language, PUSH_TEXTS["en"])

    def _push_tag(self, fub_id: int, token: str | None = None) -> str:
        """One push per plan: a newer one with the same tag replaces it on the phone.

        A deleted plan's pushes (token: vanished_plan_token) have their own tag: two deleted plans
        can share an ID, and the plan's last "stopped" push is removed, not replaced.
        """
        return f"{DOMAIN}_{self._server_id}_" + (f"plan_{fub_id}" if token is None else f"vanished_{token}")

    async def _async_push(
        self,
        fub_id: int,
        name: str,
        title_key: str,
        message_key: str,
        *,
        token: str | None = None,
        with_action: bool = False,
        actions: Collection[str] = (),
        alarm: bool = False,
        quiet: bool = False,
    ) -> bool:
        """One push for a plan; with_action adds the "Start plan" button, actions further buttons.

        token: a deleted plan's push (vanished_plan_token), with its own tag and actions. False
        when every notify service it went to failed (see _async_send). One delivered replaces (same
        tag) a vanished alarm still waiting to be sent again, so that one is dropped.
        """
        texts = self._texts()
        data: dict[str, Any] = {"tag": self._push_tag(fub_id, token), "group": f"{DOMAIN}_{self._server_id}"}
        buttons = [*([ACTION_START] if with_action else []), *actions]
        if buttons:
            data["actions"] = [
                {"action": plan_action_id(kind, self._server_id, fub_id, token), "title": texts[kind]}
                for kind in buttons
            ]
        if alarm:
            # Delivered at once and through Do Not Disturb focus (Android: ttl/priority, iOS: interruption-level).
            data |= {"ttl": 0, "priority": "high", "push": {"interruption-level": "time-sensitive"}}
        delivered = await self._async_send(
            {
                "title": texts[title_key],
                "message": texts[message_key].format(
                    name=name, fub_id=fub_id, attempts=FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS
                ),
                "data": data,
            },
            quiet=quiet,
        )
        if delivered and token is not None:
            self._vanished_push_unsent.pop(token, None)
        return delivered

    async def async_sync_vanished(self, vanished: Collection[tuple[int, str]]) -> None:
        """Repair per watched user plan Comexio no longer has, by (fub_id, name); alarm push for a newly vanished one.

        The first call after setup pushes every vanished plan, as the stopped-plan check does: a
        plan deleted while HA was down is reported. A plan back again (restored under a new ID) or
        unpicked loses its repair and push, except a "restored" confirmation, which stays on the phone.
        """
        previous = self.vanished or set()
        self.vanished = set(vanished)
        self._sync_vanished_issues(self.vanished)
        newly_vanished = self.vanished - previous
        for fub_id, name in sorted(newly_vanished | self._vanished_push_retries_due(self.vanished)):
            await self._async_push_vanished(fub_id, name, retry=(fub_id, name) not in newly_vanished)
        for fub_id, name in sorted(previous - self.vanished):
            token = vanished_plan_token(fub_id, name)
            if token in self._restored_pushes:
                self._restored_pushes.discard(token)
                continue
            await self._async_clear_push(fub_id, token)

    def _sync_vanished_issues(self, vanished: Collection[tuple[int, str]]) -> None:
        for fub_id, name in vanished:
            details = {"plan_name": name, "fub_id": str(fub_id)}
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                vanished_plan_issue_id(self._server_id, fub_id, name),
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_FUNCTION_PLAN_VANISHED,
                translation_placeholders=details,
                data={"entry_id": self._entry_id, **details},
            )
        wanted = {vanished_plan_issue_id(self._server_id, fub_id, name) for fub_id, name in vanished}
        for issue_id in _own_issue_ids(self._hass, self._server_id, vanished=True):
            if issue_id not in wanted:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    async def _async_push_vanished(self, fub_id: int, name: str, *, retry: bool) -> None:
        """The alarm push of a deleted plan; "Restore" only for a plan its backups can be found by."""
        if not retry:
            _LOGGER.warning(
                "[%s] Monitored function plan '%s' (ID %s) no longer exists in Comexio", self._server_id, name, fub_id
            )
        token = vanished_plan_token(fub_id, name)
        delivered = await self._async_push(
            fub_id,
            name,
            "vanished_title",
            "vanished",
            token=token,
            actions=_vanished_actions(fub_id, name),
            alarm=True,
            quiet=retry,  # its first failure was logged; a dead notify service must not flood the log
        )
        if not delivered:
            self._vanished_push_unsent[token] = time.monotonic()  # the answer's time: the gap starts here

    def _vanished_push_retries_due(self, vanished: Collection[tuple[int, str]]) -> set[tuple[int, str]]:
        """Undelivered vanished alarms due again; one of a plan back again (or unpicked) is dropped."""
        by_token = {vanished_plan_token(fub_id, name): (fub_id, name) for fub_id, name in vanished}
        self._vanished_push_unsent = {
            token: failed_at for token, failed_at in self._vanished_push_unsent.items() if token in by_token
        }
        now = time.monotonic()
        return {
            by_token[token]
            for token, failed_at in self._vanished_push_unsent.items()
            if now - failed_at >= FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
        }

    def vanished_name(self, token: str) -> str | None:
        """The name of the vanished plan a push action's token (vanished_plan_token) names; None if none is."""
        return next(
            (name for fub_id, name in self.vanished or () if vanished_plan_token(fub_id, name) == token),
            None,
        )

    async def async_push_no_backup(self, fub_id: int, name: str) -> None:
        """Tell the phone a vanished plan has no backup to restore it from; "Stop watching" stays."""
        await self._async_push(
            fub_id,
            name,
            "no_backup_title",
            "no_backup",
            token=vanished_plan_token(fub_id, name),
            actions=(ACTION_UNWATCH,),
        )

    async def async_push_restored(self, fub_id: int, name: str, *, complete: bool = True) -> None:
        """Replace the vanished push with "restored" once its Restore action brought the plan back.

        complete: False says the plan is back but missing parts (a PARTIAL restore).
        """
        token = vanished_plan_token(fub_id, name)
        if (fub_id, name) in (self.vanished or ()):  # a check that already cleared it would leave the entry stale
            self._restored_pushes.add(token)
        key = "restored" if complete else "restored_partial"
        await self._async_push(fub_id, name, "restored_title", key, token=token)

    async def async_push_restore_failed(self, fub_id: int, name: str) -> None:
        """Tell the phone a Restore from its push failed or was refused, with both actions again."""
        await self._async_push(
            fub_id,
            name,
            "restore_failed_title",
            "restore_failed",
            token=vanished_plan_token(fub_id, name),
            actions=_vanished_actions(fub_id, name),
        )

    async def async_push_check_pending(self, fub_id: int, token: str) -> None:
        """A push action arrived before the first check after setup: nothing is known yet, try again."""
        await self._async_push(
            fub_id,
            "",
            "check_pending_title",
            "check_pending",
            token=token,
            actions=(ACTION_RESTORE, ACTION_UNWATCH),
        )

    async def async_dismiss_push(self, fub_id: int, name: str | None = None) -> None:
        """Remove a plan's push from the phones: its "stopped" one, or with name its deleted-plan one.

        E.g. one whose "Start plan" action is no longer honoured, or the alarm of a plan restored meanwhile.
        """
        await self._async_clear_push(fub_id, None if name is None else vanished_plan_token(fub_id, name))

    async def async_dismiss_vanished_push(self, fub_id: int, token: str) -> None:
        """Remove a deleted plan's push by the token its action carried (the name may be unknown by now)."""
        await self._async_clear_push(fub_id, token)

    async def _async_clear_push(self, fub_id: int, token: str | None = None) -> None:
        # A companion-app command: any other notify service would deliver it as the text "clear_notification".
        await self._async_send(
            {"message": "clear_notification", "data": {"tag": self._push_tag(fub_id, token)}}, mobile_app_only=True
        )

    async def _async_send(
        self, service_data: dict[str, Any], *, mobile_app_only: bool = False, quiet: bool = False
    ) -> bool:
        """Send to every configured notify service; a failing one is logged and does not stop the others.

        False only when it went to at least one service and every one of them failed. quiet logs the
        failures at debug level (a retry of a push whose first failure was logged).
        """
        log_failure = _LOGGER.debug if quiet else _LOGGER.warning
        attempted = delivered = False
        for target in self._notify_targets():
            service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
            if mobile_app_only and not service.startswith(MOBILE_APP_SERVICE_PREFIX):
                continue
            attempted = True
            try:
                await self._hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
                delivered = True
            except (HomeAssistantError, vol.Invalid) as err:
                log_failure("[%s] Function plan watchdog push to notify.%s failed: %s", self._server_id, service, err)
            except Exception:
                # Third-party notify platforms raise their own errors (aiohttp, timeouts, ...); none of
                # them may skip the remaining targets or the watchdog check that sends the push.
                log_failure(
                    "[%s] Function plan watchdog push to notify.%s failed", self._server_id, service, exc_info=True
                )
        return delivered or not attempted
