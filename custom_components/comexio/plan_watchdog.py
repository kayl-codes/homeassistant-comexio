"""Watchdog for the function plans HA manages (plan_map, the trigger plan included).

HA's markers, IOs, KNX objects and triggers only work while their cluster plans run in
Comexio. After every run-state fetch the coordinator checks them here: a stopped plan raises
a fixable repair ("start now"), turns the problem sensor on, sends a push with a "Start plan"
action to the configured notify services and, with the auto-start switch on, is started
again right away.
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
import voluptuous as vol

from .const import DOMAIN, FUNCTION_PLAN_WATCHDOG_RESTART_RETRY_SEC

_LOGGER = logging.getLogger(__name__)

ISSUE_FUNCTION_PLAN_STOPPED = "function_plan_stopped"
START_ACTION_PREFIX = "COMEXIO_START_PLAN_"
NOTIFY_DOMAIN = "notify"

# Push texts by HA language (English fallback); {name} and {fub_id} are filled in.
PUSH_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "stopped_title": "Comexio: function plan stopped",
        "stopped": "The managed function plan {name} (ID {fub_id}) is not running.",
        "start": "Start plan",
        "started_title": "Comexio: function plan started",
        "started": "The function plan {name} (ID {fub_id}) runs again.",
        "auto_started": (
            "The function plan {name} (ID {fub_id}) was not running and has been started by Plan Auto-Start."
        ),
        "failed_title": "Comexio: start failed",
        "failed": "Comexio did not confirm the start of {name} (ID {fub_id}).",
    },
    "de": {
        "stopped_title": "Comexio: Logikplan gestoppt",
        "stopped": "Der verwaltete Logikplan {name} (ID {fub_id}) läuft nicht.",
        "start": "Plan starten",
        "started_title": "Comexio: Logikplan gestartet",
        "started": "Der Logikplan {name} (ID {fub_id}) läuft wieder.",
        "auto_started": "Der Logikplan {name} (ID {fub_id}) lief nicht und wurde per Plan Auto-Start gestartet.",
        "failed_title": "Comexio: Start fehlgeschlagen",
        "failed": "Comexio hat den Start von {name} (ID {fub_id}) nicht bestätigt.",
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
        start_plan: Callable[[int], Awaitable[bool]],
        notify_targets: Callable[[], list[str]],
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._server_id = server_id
        self._start_plan = start_plan
        self._notify_targets = notify_targets
        # Set by the auto-start switch (restored from its last state).
        self.auto_restart = False
        # fub_id → name of the managed plans not running; None until the first check.
        self.stopped: dict[int, str] | None = None
        # fub_id → time.monotonic() of the last refused auto-start (retry back-off).
        self._restart_refused_at: dict[int, float] = {}
        self._checking = False

    async def async_check(self, managed: Mapping[int, str], run_state: Callable[[int], bool | None]) -> bool:
        """Judge the managed plans; True if the set of stopped plans changed."""
        if self._checking:
            return False
        self._checking = True
        try:
            previous = self.stopped or {}
            stopped = next_stopped_plans(managed, run_state, previous)
            self._log_transitions(previous, stopped)
            auto_started: dict[int, str] = {}
            if self.auto_restart and stopped:
                stopped, auto_started = await self._async_auto_start(stopped)
            self._sync_issues(stopped)
            changed = self.stopped != stopped
            self.stopped = stopped
            await self._async_push_transitions(previous, stopped, auto_started)
            return changed
        finally:
            self._checking = False

    async def async_plan_started(self, fub_id: int, *, confirm: bool = False) -> None:
        """A stopped plan was started from its repair or push: clear it without waiting for the next poll.

        confirm replaces the push with a "runs again" one (started from the phone), else the push is removed.
        """
        self._restart_refused_at.pop(fub_id, None)
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

    def _log_transitions(self, previous: Mapping[int, str], stopped: Mapping[int, str]) -> None:
        for fub_id in stopped.keys() - previous.keys():
            _LOGGER.warning(
                "[%s] Managed function plan '%s' (ID %s) is not running in Comexio",
                self._server_id,
                stopped[fub_id],
                fub_id,
            )
        for fub_id in previous.keys() - stopped.keys():
            _LOGGER.info(
                "[%s] Managed function plan '%s' (ID %s) runs again", self._server_id, previous[fub_id], fub_id
            )

    async def _async_auto_start(self, stopped: dict[int, str]) -> tuple[dict[int, str], dict[int, str]]:
        """Start the stopped plans; returns (still stopped, started)."""
        still_stopped: dict[int, str] = {}
        started: dict[int, str] = {}
        now = time.monotonic()
        for fub_id, name in stopped.items():
            refused_at = self._restart_refused_at.get(fub_id)
            if refused_at is not None and now - refused_at < FUNCTION_PLAN_WATCHDOG_RESTART_RETRY_SEC:
                still_stopped[fub_id] = name
                continue
            if await self._start_plan(fub_id):
                self._restart_refused_at.pop(fub_id, None)
                self._notify_auto_started(fub_id, name)
                started[fub_id] = name
                continue
            if refused_at is None:
                _LOGGER.warning(
                    "[%s] Auto-start of managed function plan '%s' (ID %s) failed; retrying every %s s",
                    self._server_id,
                    name,
                    fub_id,
                    FUNCTION_PLAN_WATCHDOG_RESTART_RETRY_SEC,
                )
            self._restart_refused_at[fub_id] = now
            still_stopped[fub_id] = name
        return still_stopped, started

    def _notify_auto_started(self, fub_id: int, name: str) -> None:
        _LOGGER.warning("[%s] Auto-started managed function plan '%s' (ID %s)", self._server_id, name, fub_id)
        persistent_notification.async_create(
            self._hass,
            f"The managed function plan **{name}** (ID {fub_id}) was not running in Comexio and has been "
            "started again by the **Plan Auto-Start** switch.",
            title=f"Comexio {self._server_id}: function plan started",
            notification_id=f"{DOMAIN}_{self._server_id}_plan_auto_start_{fub_id}",
        )

    def _sync_issues(self, stopped: Mapping[int, str]) -> None:
        """Raise a repair per stopped plan and close the ones of plans that run again."""
        wanted: set[str] = set()
        for fub_id, name in stopped.items():
            issue_id = stopped_plan_issue_id(self._server_id, fub_id)
            wanted.add(issue_id)
            details = {"plan_name": name, "fub_id": str(fub_id)}
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_FUNCTION_PLAN_STOPPED,
                translation_placeholders=details,
                # The fix flow gets only this data, not the placeholders.
                data={"entry_id": self._entry_id, **details},
            )
        for issue_id in _own_issue_ids(self._hass, self._server_id):
            if issue_id not in wanted:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    async def _async_push_transitions(
        self, previous: Mapping[int, str], stopped: Mapping[int, str], auto_started: Mapping[int, str]
    ) -> None:
        """Alarm push for a newly stopped plan; the push of a plan running again is replaced or removed."""
        for fub_id in sorted(stopped.keys() - previous.keys()):
            await self._async_push(fub_id, stopped[fub_id], "stopped_title", "stopped", with_action=True, alarm=True)
        for fub_id, name in sorted(auto_started.items()):
            await self._async_push(fub_id, name, "started_title", "auto_started")
        for fub_id in sorted(previous.keys() - stopped.keys() - auto_started.keys()):
            await self._async_clear_push(fub_id)

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
        texts = PUSH_TEXTS.get(self._hass.config.language, PUSH_TEXTS["en"])
        data: dict[str, Any] = {"tag": self._push_tag(fub_id), "group": f"{DOMAIN}_{self._server_id}"}
        if with_action:
            data["actions"] = [{"action": start_action_id(self._server_id, fub_id), "title": texts["start"]}]
        if alarm:
            # Delivered at once and through Do Not Disturb focus (Android: ttl/priority, iOS: interruption-level).
            data.update({"ttl": 0, "priority": "high", "push": {"interruption-level": "time-sensitive"}})
        await self._async_send(
            {
                "title": texts[title_key],
                "message": texts[message_key].format(name=name, fub_id=fub_id),
                "data": data,
            }
        )

    async def _async_clear_push(self, fub_id: int) -> None:
        await self._async_send({"message": "clear_notification", "data": {"tag": self._push_tag(fub_id)}})

    async def _async_send(self, service_data: dict[str, Any]) -> None:
        """Send to every configured notify service; a failing one is logged and does not stop the others."""
        for target in self._notify_targets():
            service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
            try:
                await self._hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
            except (HomeAssistantError, vol.Invalid) as err:
                _LOGGER.warning(
                    "[%s] Function plan watchdog push to notify.%s failed: %s", self._server_id, service, err
                )
