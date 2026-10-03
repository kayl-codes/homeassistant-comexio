"""Watchdog for the function plans HA manages (plan_map, the trigger plan included).

HA's markers, IOs, KNX objects and triggers only work while their cluster plans run in
Comexio. After every run-state fetch the coordinator checks them here, together with the user
plans picked in the options: a stopped plan raises a fixable repair ("start now"), turns the
problem sensor on, sends a push with a "Start plan" action to the configured notify services
and, with the auto-start switch of its kind (HA plans / user plans) on, is started again
right away. After FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS failed auto-starts (refused, or
stopped again shortly after) Auto-Start gives up on the plan with a "check the plan" repair and
alarm push, until a run-state poll sees the plan running again.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping
import logging
import re
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN, FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS, FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC

_LOGGER = logging.getLogger(__name__)

ISSUE_FUNCTION_PLAN_STOPPED = "function_plan_stopped"
# Translation key of a stopped user plan's repair; same issue id scheme and fix flow.
ISSUE_FUNCTION_PLAN_STOPPED_USER = "function_plan_stopped_user"
# Translation key once Plan Auto-Start gave up on a plan ("check the plan"); same issue id and fix flow.
ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED = "function_plan_auto_start_suspended"
START_ACTION_PREFIX = "COMEXIO_START_PLAN_"
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
        "suspended_title": "Comexio: check function plan",
        "suspended": (
            "Plan Auto-Start could not keep {name} (ID {fub_id}) running after {attempts} attempts and "
            "stopped trying. Please check the plan."
        ),
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
        "suspended_title": "Comexio: Logikplan prüfen",
        "suspended": (
            "Plan Auto-Start konnte {name} (ID {fub_id}) nach {attempts} Versuchen nicht am Laufen halten "
            "und versucht es nicht weiter. Bitte den Plan prüfen."
        ),
        "time_format": "%d.%m.%Y %H:%M:%S",
    },
}


def _issue_prefix(server_id: str) -> str:
    return f"{ISSUE_FUNCTION_PLAN_STOPPED}_{server_id}_"


def stopped_plan_issue_id(server_id: str, fub_id: int) -> str:
    """Repair issue id for one stopped managed plan."""
    return f"{_issue_prefix(server_id)}{fub_id}"


def _own_issue_ids(hass: HomeAssistant, server_id: str) -> list[str]:
    """Ids of this instance's stopped-plan repairs.

    The prefix alone is not enough: for server "home" it also matches the repairs of a
    server "home_2", so the rest of the id must be exactly the plan id.
    """
    own = re.compile(rf"{re.escape(_issue_prefix(server_id))}\d+")
    return [issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN and own.fullmatch(issue_id)]


def start_action_id(server_id: str, fub_id: int) -> str:
    """Action id of the push's "Start plan" button."""
    return f"{START_ACTION_PREFIX}{server_id}_{fub_id}"


def parse_start_action(server_id: str, action: Any) -> int | None:
    """The plan id of this instance's "Start plan" action, None for any other action.

    fullmatch on digits, so server "home" does not pick up the actions of server "home_2".
    """
    if not isinstance(action, str):
        return None
    match = re.fullmatch(rf"{re.escape(START_ACTION_PREFIX + server_id)}_(\d+)", action)
    return int(match[1]) if match else None


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
        self._checking = False

    async def async_check(
        self,
        managed: Mapping[int, str],
        run_state: Callable[[int], bool | None],
        user_plans: Collection[int] = (),
    ) -> bool:
        """Judge the watched plans; True if the set of stopped plans changed.

        managed holds every watched plan; user_plans names the ones that are user plans, which
        follow the user-plan auto-start switch instead of the HA-plan one.
        """
        if self._checking:
            return False
        self._checking = True
        try:
            self._user_plans = frozenset(user_plans)
            previous = self.stopped or {}
            suspended_before = frozenset(self.suspended)
            self._track_running(managed, run_state)
            stopped = next_stopped_plans(managed, run_state, previous)
            self._log_transitions(previous, stopped, managed)
            to_start = {fub_id: name for fub_id, name in stopped.items() if self._auto_start_enabled(fub_id)}
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
            # A stopped plan deleted in Comexio (or no longer picked) drops out of managed: no recovery.
            verdict = "runs again" if fub_id in managed else "is no longer watched (deleted or unselected)"
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
        for fub_id in lifted:
            self._gave_up_at.pop(fub_id, None)
        for fub_id in lifted | healthy:
            self._auto_started_at.pop(fub_id, None)
            self._failed_starts.pop(fub_id, None)

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
        now = time.monotonic()
        for fub_id, name in stopped.items():
            started_at = self._auto_started_at.pop(fub_id, None)
            if started_at is not None and now - started_at < FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC:
                self._count_failed_start(fub_id, name, "stopped again shortly after its start")
            if fub_id in self.suspended:
                continue
            result = await self._start_plan(fub_id)
            if result:
                self._auto_started_at[fub_id] = now
                self._notify_auto_started(fub_id, name)
                started[fub_id] = name
            elif result is False:
                self._count_failed_start(fub_id, name, "start refused by Comexio")
            else:
                # Blocked by a running sync/restore, or no answer: says nothing about the plan, retried next poll.
                _LOGGER.info(
                    "[%s] Auto-start of monitored function plan '%s' (ID %s) deferred to the next check",
                    self._server_id,
                    name,
                    fub_id,
                )
        return started

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
        again is replaced or removed."""
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

    def _push_tag(self, fub_id: int) -> str:
        """One push per plan: a newer one with the same tag replaces it on the phone."""
        return f"{DOMAIN}_{self._server_id}_plan_{fub_id}"

    async def _async_push(
        self,
        fub_id: int,
        name: str,
        title_key: str,
        message_key: str,
        *,
        with_action: bool = False,
        alarm: bool = False,
    ) -> None:
        texts = self._texts()
        data: dict[str, Any] = {"tag": self._push_tag(fub_id), "group": f"{DOMAIN}_{self._server_id}"}
        if with_action:
            data["actions"] = [{"action": start_action_id(self._server_id, fub_id), "title": texts["start"]}]
        if alarm:
            # Delivered at once and through Do Not Disturb focus (Android: ttl/priority, iOS: interruption-level).
            data.update({"ttl": 0, "priority": "high", "push": {"interruption-level": "time-sensitive"}})
        await self._async_send(
            {
                "title": texts[title_key],
                "message": texts[message_key].format(
                    name=name, fub_id=fub_id, attempts=FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS
                ),
                "data": data,
            }
        )

    async def _async_clear_push(self, fub_id: int) -> None:
        # A companion-app command: any other notify service would deliver it as the text "clear_notification".
        await self._async_send(
            {"message": "clear_notification", "data": {"tag": self._push_tag(fub_id)}}, mobile_app_only=True
        )

    async def _async_send(self, service_data: dict[str, Any], *, mobile_app_only: bool = False) -> None:
        """Send to every configured notify service; a failing one is logged and does not stop the others."""
        for target in self._notify_targets():
            service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
            if mobile_app_only and not service.startswith(MOBILE_APP_SERVICE_PREFIX):
                continue
            try:
                await self._hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
            except (HomeAssistantError, vol.Invalid) as err:
                _LOGGER.warning(
                    "[%s] Function plan watchdog push to notify.%s failed: %s", self._server_id, service, err
                )
            except Exception:
                # Third-party notify platforms raise their own errors (aiohttp, timeouts, ...); none of
                # them may skip the remaining targets or the watchdog check that sends the push.
                _LOGGER.exception("[%s] Function plan watchdog push to notify.%s failed", self._server_id, service)
