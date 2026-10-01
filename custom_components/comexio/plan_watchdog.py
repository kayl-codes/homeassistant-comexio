"""Watchdog for the function plans HA manages (plan_map, the trigger plan included).

HA's markers, IOs, KNX objects and triggers only work while their cluster plans run in
Comexio. After every run-state fetch the coordinator checks them here: a stopped plan raises
a fixable repair ("start now"), turns the problem sensor on and, with the auto-start switch
on, is started again right away.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping
import logging
import re
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, FUNCTION_PLAN_WATCHDOG_RESTART_RETRY_SEC

_LOGGER = logging.getLogger(__name__)

ISSUE_FUNCTION_PLAN_STOPPED = "function_plan_stopped"


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
    """Keeps the repairs, the problem sensor and the optional auto-start of the managed plans."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        server_id: str,
        start_plan: Callable[[int], Awaitable[bool]],
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._server_id = server_id
        self._start_plan = start_plan
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
            if self.auto_restart and stopped:
                stopped = await self._async_auto_start(stopped)
            self._sync_issues(stopped)
            changed = self.stopped != stopped
            self.stopped = stopped
            return changed
        finally:
            self._checking = False

    def plan_started(self, fub_id: int) -> None:
        """A stopped plan was started by the repair flow: clear it without waiting for the next poll."""
        self._restart_refused_at.pop(fub_id, None)
        ir.async_delete_issue(self._hass, DOMAIN, stopped_plan_issue_id(self._server_id, fub_id))
        if self.stopped is not None:
            self.stopped = {key: name for key, name in self.stopped.items() if key != fub_id}

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

    async def _async_auto_start(self, stopped: dict[int, str]) -> dict[int, str]:
        """Start the stopped plans; returns the ones still stopped."""
        still_stopped: dict[int, str] = {}
        now = time.monotonic()
        for fub_id, name in stopped.items():
            refused_at = self._restart_refused_at.get(fub_id)
            if refused_at is not None and now - refused_at < FUNCTION_PLAN_WATCHDOG_RESTART_RETRY_SEC:
                still_stopped[fub_id] = name
                continue
            if await self._start_plan(fub_id):
                self._restart_refused_at.pop(fub_id, None)
                self._notify_auto_started(fub_id, name)
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
        return still_stopped

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
