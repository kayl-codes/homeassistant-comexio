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

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
import hashlib
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

from .const import (
    DOMAIN,
    FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS,
    FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC,
    FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC,
    FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC,
    FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
    one_line,
    one_line_values,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_FUNCTION_PLAN_STOPPED = "function_plan_stopped"
# Translation key of a stopped user plan's repair; same issue id scheme and fix flow.
ISSUE_FUNCTION_PLAN_STOPPED_USER = "function_plan_stopped_user"
# Translation key once Plan Auto-Start gave up on a plan ("check the plan"); same issue id and fix flow.
ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED = "function_plan_auto_start_suspended"
# Repair of a watched user plan Comexio no longer has (deleted); one per plan, its own id scheme.
ISSUE_FUNCTION_PLAN_VANISHED = "function_plan_vanished"
# Translation key of the same repair while another plan holds the ID (renamed, or the ID reused); same id and flow.
ISSUE_FUNCTION_PLAN_REPLACED = "function_plan_replaced"
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
        "outdated_title": "Comexio: push outdated",
        "outdated": (
            "That push was sent before an update and did not name its plan, so nothing was started. ID {fub_id} "
            "now holds {name}: tap Start plan to start it."
        ),
        "suspended_title": "Comexio: check function plan",
        "suspended": (
            "Plan Auto-Start could not keep {name} (ID {fub_id}) running after {attempts} attempts and "
            "stopped trying. Please check the plan."
        ),
        "vanished_title": "Comexio: function plan deleted",
        "vanished": "The monitored function plan {name} (ID {fub_id}) no longer exists in Comexio.",
        "replaced_title": "Comexio: function plan not found",
        "replaced": (
            "The monitored function plan {name} (ID {fub_id}) was not found: ID {fub_id} now holds {current}. "
            "If it was renamed, take the rename over in the Home Assistant repair; the other choices are there too."
        ),
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
        "outdated_title": "Comexio: Push veraltet",
        "outdated": (
            "Diese Push-Nachricht stammt von vor einem Update und nannte ihren Plan nicht, daher wurde nichts "
            "gestartet. ID {fub_id} gehört jetzt zu {name}: Plan starten startet ihn."
        ),
        "suspended_title": "Comexio: Logikplan prüfen",
        "suspended": (
            "Plan Auto-Start konnte {name} (ID {fub_id}) nach {attempts} Versuchen nicht am Laufen halten "
            "und versucht es nicht weiter. Bitte den Plan prüfen."
        ),
        "vanished_title": "Comexio: Logikplan gelöscht",
        "vanished": "Der überwachte Logikplan {name} (ID {fub_id}) existiert in Comexio nicht mehr.",
        "replaced_title": "Comexio: Logikplan nicht gefunden",
        "replaced": (
            "Der überwachte Logikplan {name} (ID {fub_id}) wurde nicht gefunden: ID {fub_id} trägt jetzt {current}. "
            "Wurde er umbenannt, übernimm die Umbenennung in der Reparatur von Home Assistant; dort stehen auch "
            "die anderen Möglichkeiten."
        ),
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
    """A plan's identity in a deleted plan's repair id, push tag and push actions and in the "Start plan" action.

    Its ID and a digest of its name. The ID alone is not one: an ID can be reused, so two deleted
    watched plans may share it (picks "47:X" and "47:Y"), and a stale "Start plan" could start the
    plan holding the ID now. The name itself cannot go into an action id (any character), its digest can:
    64 bits of SHA-256, so two names of one ID do not collide (CRC32's 32 bits could).
    """
    return f"{fub_id}_{hashlib.sha256(name.encode()).hexdigest()[:16]}"


# vanished_plan_token: the plan id (group) and the name digest.
_VANISHED_ID_PATTERN = r"(\d+)_[0-9a-f]{16}"

# Push tag → lock held while a push under it is sent, with the number of pushes holding or waiting for it
# (see ManagedPlanWatchdog._push_lock). Module-wide, not per watchdog: a reloaded entry's new watchdog sends
# under the old one's tags (they name the server) while the old one's sends may still be on their way.
_PUSH_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}
# Plan push tags whose last push delivered carries buttons (an alarm, a start reply). Module-wide like _PUSH_LOCKS:
# a reloaded entry's watchdog has no verdict of its own that would take such a push off (one delivered before the
# reload, or still on its way), so its checks clear the ones of plans they see running or no longer watch.
_ACTIONABLE_TAGS: set[str] = set()


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
    own = re.compile(re.escape(prefix) + (_VANISHED_ID_PATTERN if vanished else r"\d+"))
    return [issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN and own.fullmatch(issue_id)]


def start_action_id(server_id: str, fub_id: int, name: str) -> str:
    """Action id of the push's "Start plan" button."""
    return plan_action_id(ACTION_START, server_id, vanished_plan_token(fub_id, name))


def plan_action_id(kind: str, server_id: str, token: str) -> str:
    """Action id of a push button, by the plan's vanished_plan_token: the started plan's or the deleted plan's.

    "Start plan" carries the name digest too: a push from before Comexio gave the plan's ID to
    another watched plan must not start that one.
    """
    return f"{_ACTION_PREFIXES[kind]}{server_id}_{token}"


def parse_plan_action(server_id: str, action: Any) -> tuple[str, int, str | None] | None:
    """(kind, plan id, vanished_plan_token) of one of this instance's push actions, None for any other action.

    The token is None only for a "Start plan" sent before the action carried the name digest (the
    plan id alone). fullmatch, so server "home" does not pick up the actions of server "home_2".
    """
    if not isinstance(action, str):
        return None
    for kind, prefix in _ACTION_PREFIXES.items():
        own = re.escape(prefix + server_id)
        if match := re.fullmatch(rf"{own}_(?P<token>{_VANISHED_ID_PATTERN})", action):
            return kind, int(match[2]), match["token"]
        if kind == ACTION_START and (match := re.fullmatch(rf"{own}_(\d+)", action)):
            return kind, int(match[1]), None
    return None


def parse_start_action(server_id: str, action: Any) -> int | None:
    """The plan id of this instance's "Start plan" action, None for any other action."""
    parsed = parse_plan_action(server_id, action)
    return parsed[1] if parsed is not None and parsed[0] == ACTION_START else None


def _retries_due[K](unsent: Mapping[K, tuple[float, frozenset[str]]]) -> dict[K, frozenset[str] | None]:
    """The undelivered alarms (key → (time it failed, targets that failed)) due again, with their targets."""
    now = time.monotonic()
    return {
        key: targets
        for key, (failed_at, targets) in unsent.items()
        if now - failed_at >= FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    }


def _is_mobile_app(target: str) -> bool:
    """A companion-app notify target: the only kind with push actions, and the only one a clear reaches."""
    return target.removeprefix(f"{NOTIFY_DOMAIN}.").startswith(MOBILE_APP_SERVICE_PREFIX)


def _drop_delivered[K](unsent: dict[K, tuple[float, frozenset[str]]], key: K, done: frozenset[str]) -> None:
    """A push delivered to some targets replaced (same tag) an alarm waiting there to be sent again."""
    if key not in unsent:
        return
    failed_at, targets = unsent[key]
    if targets - done:
        unsent[key] = (failed_at, targets - done)
    else:
        del unsent[key]


def next_stopped_plans(
    managed: Mapping[int, str], run_state: Callable[[int], bool | None], previous: Mapping[int, str]
) -> dict[int, str]:
    """fub_id → name of the managed plans that do not run.

    A plan whose run state is unknown right now (None: unreadable, or stopped by HA itself a
    moment ago) keeps its previous verdict, so a poll outage neither raises nor clears a repair.
    The verdict is the plan's, not the ID's: an ID holding another plan now (name changed) keeps none.
    """
    stopped: dict[int, str] = {}
    for fub_id, name in managed.items():
        running = run_state(fub_id)
        if running is False or (running is None and previous.get(fub_id) == name):
            stopped[fub_id] = name
    return stopped


def newly_stopped_plans(previous: Mapping[int, str], stopped: Mapping[int, str]) -> list[int]:
    """IDs of the plans that stopped since the last check, sorted.

    An ID stopped then under another name holds another plan now (Comexio reused the ID, or the
    plan was renamed): the old push names the plan that held it, so the one holding it gets its own.
    Only a bare-ID pick keeps an ID watched across a name change (see _watched_user_plan_names).
    """
    return sorted(fub_id for fub_id, name in stopped.items() if previous.get(fub_id) != name)


class _VanishedGuard(StrEnum):
    """What a deleted plan's push (token) is judged by again once its tag's lock is held (see _no_longer_due)."""

    NONE = "none"
    # Not once the plan's "restored" push went out: it would replace that confirmation.
    UNLESS_RESTORED = "unless_restored"
    # As UNLESS_RESTORED, and only while the plan still is vanished (a reply offering its Restore).
    WHILE_VANISHED = "while_vanished"
    # Not once a check ran (a reply before the first check): a listed plan got that check's alarm, same buttons;
    # one not listed is back or unwatched, its buttons would be turned down.
    WHILE_PENDING = "while_pending"
    # Not once a check dropped the plan from vanished and listed it again (a "restored" push: a new episode's
    # alarm holds the tag).
    UNLESS_VANISHED_AGAIN = "unless_vanished_again"


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
        # fub_id → name of the plan holding a vanished plan's ID now (renamed, or the ID reused), as of the last check.
        self.vanished_holders: dict[int, str] = {}
        # Tokens (vanished_plan_token) of vanished plans whose push was replaced by "restored": the plan
        # dropping out of vanished keeps it.
        self._restored_pushes: set[str] = set()
        # (fub_id, name) of the plans a running async_sync_vanished dropped from vanished but has not cleared yet:
        # a "restored" push meanwhile is marked as for a vanished plan.
        self._vanished_clearing: set[tuple[int, str]] = set()
        # token → (time.monotonic(), notify targets) of a vanished alarm push those targets failed to take:
        # sent again to them with a check at least FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC later; a target
        # any push for the plan reached since is dropped.
        self._vanished_push_unsent: dict[str, tuple[float, frozenset[str]]] = {}
        # fub_id → the same for a stopped alarm push.
        self._stopped_push_unsent: dict[int, tuple[float, frozenset[str]]] = {}
        # Push tag → lock held while a push under it is sent: the phone shows the one sent last, so a stale
        # push's clear and a stop's alarm (same tag) must not overlap, else the clear may arrive after the alarm.
        # With the number of pushes holding or waiting for it: removed once none does (deleted-plan tags are many).
        # Shared with the watchdog of a reload (see _PUSH_LOCKS).
        self._push_locks = _PUSH_LOCKS
        self._actionable_tags = _ACTIONABLE_TAGS
        # fub_id → name of the managed plans as of the last check: an ID under another name holds another plan.
        self._names: dict[int, str] = {}
        # Local time of the last check; shown in the repair dialogs and on the problem sensor.
        self.last_check: datetime | None = None
        self._checking = False
        # Plans started from their repair or push while a check runs: its stopped plans were judged before.
        self._started_in_check: set[int] = set()
        # Set once the entry unloads (close): no push or clear is sent from then on.
        self._closed = False
        # Plans the running check auto-started (see async_plan_started).
        self._auto_started_in_check: set[int] = set()

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
        self._started_in_check.clear()
        try:
            self.last_check = dt_util.now()
            self._user_plans = frozenset(user_plans)
            # Read once: a state changing during the auto-start await must not judge a plan twice differently.
            states = {fub_id: run_state(fub_id) for fub_id in managed}
            run_state = states.__getitem__
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
            if self._closed:
                # Unloaded while Auto-Start awaited Comexio: the reloaded watchdog owns the shared repairs now,
                # this check's snapshot would delete the ones it raised meanwhile.
                return False
            # A plan started meanwhile (repair or push, while Auto-Start awaited Comexio) is left out: its
            # repair and push were already taken care of, writing it back would raise them again.
            started = self._started_in_check
            stopped = {
                fub_id: name for fub_id, name in stopped.items() if fub_id not in auto_started and fub_id not in started
            }
            previous = {fub_id: name for fub_id, name in previous.items() if fub_id not in started}
            # Also one whose Auto-Start answered after it was confirmed running: its "auto-started" push would
            # replace that confirmation.
            auto_started = {fub_id: name for fub_id, name in auto_started.items() if fub_id not in started}
            undecided = {fub_id for fub_id, state in states.items() if fub_id not in stopped and state is None}
            self._sync_issues(stopped, undecided)
            changed = self.stopped != stopped or self.suspended != suspended_before
            self.stopped = stopped
            await self._async_push_transitions(
                previous, stopped, auto_started, self.suspended - suspended_before, self._maybe_actionable(states)
            )
            return changed
        finally:
            self._checking = False
            self._auto_started_in_check.clear()

    def close(self) -> None:
        """The entry unloads: send nothing more, judged under the tag's lock.

        A check or push action of this watchdog may still run; what it would send now is stale. A reloaded
        entry's watchdog sends under the same tags (shared lock: a send already on its way arrives first), so
        a clear of this one queued behind its alarm would take that alarm off the phone.
        One on its way arrives still: if it has buttons, the reloaded watchdog takes it off for a plan it sees
        running or no longer watches (see _maybe_actionable).
        """
        self._closed = True

    def _maybe_actionable(self, states: Mapping[int, bool | None]) -> set[int]:
        """The plans running now whose tag may still show a push with buttons (see _ACTIONABLE_TAGS), and the plans
        no longer watched whose tag may (e.g. unpicked, then reloaded: no verdict of this watchdog clears it).

        Its last push delivered has them, or a push under it is on its way (one of an unloaded entry's watchdog
        is judged only once it arrived). Judged again under the tag's lock (see _async_clear_push): the clear then
        waits for that push.
        """
        running = {
            fub_id
            for fub_id, state in states.items()
            if state and ((tag := self._push_tag(fub_id)) in self._actionable_tags or tag in self._push_locks)
        }
        prefix = self._push_tag(0).removesuffix("0")
        shown = {
            tag.removeprefix(prefix) for tag in {*self._actionable_tags, *self._push_locks} if tag.startswith(prefix)
        }
        return running | ({int(fub_id) for fub_id in shown if fub_id.isdecimal() and fub_id.isascii()} - states.keys())

    def last_check_text(self) -> str:
        """The last check as local time in the HA language's format, "—" before the first one."""
        return "—" if self.last_check is None else self.last_check.strftime(self._texts()["time_format"])

    def is_user_plan(self, fub_id: int) -> bool:
        return fub_id in self._user_plans

    def _ignored_issue(self, fub_id: int) -> ir.IssueEntry | None:
        """The plan's stopped repair if the user ignored it (HA's own ignore, kept across restarts)."""
        issue = ir.async_get(self._hass).async_get_issue(DOMAIN, stopped_plan_issue_id(self._server_id, fub_id))
        return issue if issue is not None and issue.dismissed_version is not None else None

    async def async_ignore_stopped(self, fub_id: int) -> bool:
        """Ignore a stopped plan's repair from its dialog: repair and alarm push go, the plan stays watched.

        The checks keep the ignored repair hidden and send no alarm for it. The plan running again
        deletes the repair and with it the ignore, so its next stop raises a repair and an alarm again;
        Auto-Start giving up on it lifts the ignore too. False if the repair was cleared meanwhile.
        """
        issue_id = stopped_plan_issue_id(self._server_id, fub_id)
        if ir.async_get(self._hass).async_get_issue(DOMAIN, issue_id) is None:
            return False  # Cleared meanwhile (the plan ran again): nothing left to ignore.
        ir.async_ignore_issue(self._hass, DOMAIN, issue_id, True)
        self._stopped_push_unsent.pop(fub_id, None)
        _LOGGER.info(
            "[%s] Stopped function plan '%s' (ID %s): repair ignored, no alarm until it ran again",
            self._server_id,
            (self.stopped or {}).get(fub_id, str(fub_id)),
            fub_id,
        )
        await self._async_clear_push(fub_id)
        return True

    def _auto_start_enabled(self, fub_id: int) -> bool:
        return self.auto_restart_user if self.is_user_plan(fub_id) else self.auto_restart

    async def async_plan_started(self, fub_id: int, *, confirm: bool = False) -> None:
        """A stopped plan was started from its repair or push: clear it without waiting for the next poll.

        confirm replaces the push with a "runs again" one (started from the phone), else the push is removed.
        """
        if self._closed:
            # Unloaded while the start awaited Comexio: the reloaded watchdog owns the shared repair now (it may
            # have raised it again meanwhile); its next check sees the plan running and clears it.
            return
        # The suspension is not touched here: the run-state fetch right after the start (or the next
        # poll) lifts it once it sees the plan running.
        if fub_id not in self._auto_started_in_check:
            # The user's start: a stop after it is no failed Auto-Start. A tap while the check that auto-started
            # the plan still runs found it started already, so that start keeps its stopped-again window.
            self._auto_started_at.pop(fub_id, None)
        if self._checking:
            self._started_in_check.add(fub_id)
        ir.async_delete_issue(self._hass, DOMAIN, stopped_plan_issue_id(self._server_id, fub_id))
        name = (self.stopped or {}).get(fub_id, str(fub_id))
        if self.stopped is not None:
            self.stopped = {key: value for key, value in self.stopped.items() if key != fub_id}
        if confirm:
            await self._async_push(fub_id, name, "started_title", "started")
        else:
            # Kept if a check judged the plan stopped again before the tag's lock was free: its alarm is due.
            await self._async_clear_push(fub_id, keep_stopped=True)

    async def async_push_start_failed(self, fub_id: int, name: str) -> None:
        """Tell the phone a start from its push failed, with the "Start plan" action again.

        Only while the plan is the ID's stopped one: started meanwhile (another phone), the button would start
        a running plan; not judged stopped (yet), the next check that does pushes its alarm with the button.
        """
        await self._async_push(fub_id, name, "failed_title", "failed", with_action=True, while_stopped=True)

    async def async_push_start_busy(self, fub_id: int, name: str) -> None:
        """Tell the phone a start from its push was not sent (something else runs), with the action again.

        Only while the plan is the ID's stopped one, as async_push_start_failed.
        """
        await self._async_push(fub_id, name, "busy_title", "busy", with_action=True, while_stopped=True)

    async def async_push_start_outdated(self, fub_id: int, name: str) -> None:
        """Replace a "Start plan" push that names no plan (sent before the update) with one naming the ID's plan.

        The alarm is pushed on the stop only, so the old push would otherwise keep a button that starts nothing.
        A target that failed to take it gets the plan's alarm again (it names the plan too) while it stays stopped.
        """
        failed = await self._async_push(
            fub_id, name, "outdated_title", "outdated", with_action=True, while_stopped=True
        )
        if failed:
            self._stopped_push_unsent[fub_id] = (time.monotonic(), failed)

    def _log_transitions(
        self, previous: Mapping[int, str], stopped: Mapping[int, str], managed: Mapping[int, str]
    ) -> None:
        for fub_id in newly_stopped_plans(previous, stopped):
            _LOGGER.warning(
                "[%s] Monitored function plan '%s' (ID %s) is not running in Comexio",
                self._server_id,
                stopped[fub_id],
                fub_id,
            )
        for fub_id in previous.keys() - stopped.keys():
            # A stopped plan deleted, renamed or unpicked (or its ID reused) drops out of managed: no recovery.
            if fub_id not in managed:
                verdict = "is no longer watched (deleted, renamed or unselected)"
            elif managed[fub_id] != previous[fub_id]:
                verdict = f"is no longer watched: its ID holds '{managed[fub_id]}' now"
            else:
                verdict = "runs again"
            _LOGGER.info(
                "[%s] Monitored function plan '%s' (ID %s) %s", self._server_id, previous[fub_id], fub_id, verdict
            )

    def _track_running(self, managed: Mapping[int, str], run_state: Callable[[int], bool | None]) -> None:
        """Lift the Auto-Start suspension of a plan seen running; forget plans no longer watched.

        A plan seen running starts with a clean slate — unless it is still inside the stopped-again
        window of its own auto-start, where it has yet to prove it keeps running.
        """
        self._forget_reassigned(managed)
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

    def _forget_reassigned(self, managed: Mapping[int, str]) -> None:
        """An ID under another name than at the last check holds another plan (Comexio reused it, or the
        plan was renamed): the Auto-Start state of the plan that held it is not the new one's."""
        reassigned = {fub_id for fub_id, name in managed.items() if self._names.get(fub_id, name) != name}
        self._names = dict(managed)
        self.suspended -= reassigned
        for fub_id in reassigned:
            for state in (
                self._failed_starts,
                self._auto_started_at,
                self._gave_up_at,
                self._refused_at,
                self._deferred_at,
            ):
                state.pop(fub_id, None)

    def _count_failed_start(self, fub_id: int, name: str, reason: str) -> None:
        """One more failed auto-start; at FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS Auto-Start gives up on the plan."""
        failed = self._failed_starts.get(fub_id, 0) + 1
        self._failed_starts[fub_id] = failed
        _LOGGER.warning(
            "[%s] Auto-start of monitored function plan '%s' (ID %s) failed (%s), attempt %s of %s",
            self._server_id,
            one_line(name),
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
                one_line(name),
                fub_id,
            )

    async def _async_auto_start(self, stopped: Mapping[int, str]) -> dict[int, str]:
        """Start the stopped plans Auto-Start has not given up on; returns the ones started."""
        started: dict[int, str] = {}
        for fub_id, name in stopped.items():
            if self._closed:
                break  # unloaded while an earlier start awaited Comexio: the reloaded watchdog starts its plans
            # Read per plan: each start awaits Comexio, which serializes requests, so a time taken
            # once before the loop would date the later plans' starts and refusals too early.
            now = time.monotonic()
            if not self._auto_start_due(fub_id, name, now):
                continue
            result = await self._start_plan(fub_id)
            now = time.monotonic()  # the answer's time: the stopped-again window and the retry gap start here
            deferred_before = self._deferred_at.pop(fub_id, None) is not None
            if fub_id in self._started_in_check:
                # Started from its repair or push while this start awaited Comexio: not Auto-Start's start (no
                # notification, no stopped-again window), nor its refusal (no failed attempt).
                continue
            if result:
                self._auto_started_at[fub_id] = now
                self._auto_started_in_check.add(fub_id)
                self._notify_auto_started(fub_id, name)
                started[fub_id] = name
            elif result is False:
                self._refused_at[fub_id] = now
                self._count_failed_start(fub_id, name, "start refused by Comexio")
            else:
                self._defer_start(fub_id, name, now, logged_before=deferred_before)
        return started

    def _auto_start_due(self, fub_id: int, name: str, now: float) -> bool:
        """Whether Auto-Start starts this stopped plan now; counts a stop shortly after its own start as a failure."""
        started_at = self._auto_started_at.pop(fub_id, None)
        if started_at is not None and now - started_at < FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC:
            self._count_failed_start(fub_id, name, "stopped again shortly after its start")
        # Re-checked per plan: the switch can be turned off, or the plan started from its repair or push,
        # while an earlier start awaits Comexio.
        if fub_id in self.suspended or fub_id in self._started_in_check or not self._auto_start_enabled(fub_id):
            return False
        if self._retry_too_soon(fub_id, now):
            _LOGGER.debug(
                "[%s] Auto-start of monitored function plan '%s' (ID %s) waits until %s s after its last attempt",
                self._server_id,
                one_line(name),
                fub_id,
                FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
            )
            return False
        return True

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
            one_line(name),
            fub_id,
            FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
        )

    def _retry_too_soon(self, fub_id: int, now: float) -> bool:
        return any(
            (last := attempts.get(fub_id)) is not None and now - last < FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC
            for attempts in (self._refused_at, self._deferred_at)
        )

    def _notify_auto_started(self, fub_id: int, name: str) -> None:
        shown = one_line(name)
        _LOGGER.warning("[%s] Auto-started monitored function plan '%s' (ID %s)", self._server_id, shown, fub_id)
        switch = "Plan Auto-Start (user plans)" if self.is_user_plan(fub_id) else "Plan Auto-Start (HA plans)"
        persistent_notification.async_create(
            self._hass,
            f"The monitored function plan **{shown}** (ID {fub_id}) was not running in Comexio and has been "
            f"started again by the **{switch}** switch.",
            title=f"Comexio {self._server_id}: function plan started",
            notification_id=f"{DOMAIN}_{self._server_id}_plan_auto_start_{fub_id}",
        )

    def _sync_issues(self, stopped: Mapping[int, str], undecided: Collection[int] = ()) -> None:
        """Raise a repair per stopped plan and close the ones of plans that run again.

        An ignored repair of a plan whose run state is unknown stays: after a restart no previous verdict
        holds it, and closing it would drop the ignore before the plan was seen running again.
        """
        wanted = {
            stopped_plan_issue_id(self._server_id, fub_id)
            for fub_id in undecided
            if self._ignored_issue(fub_id) is not None
        }
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
            if self._ignore_lapsed(fub_id, name):
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=self._issue_translation_key(fub_id),
                translation_placeholders=one_line_values(details),
                # The fix flow gets only this data, not the placeholders.
                data={"entry_id": self._entry_id, **details},
            )
        for issue_id in _own_issue_ids(self._hass, self._server_id):
            if issue_id not in wanted:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    def _ignore_lapsed(self, fub_id: int, name: str) -> bool:
        """Whether the ignore of the ID's repair no longer holds for this stop: it is then raised afresh.

        Ignored for the plan that held the ID before (renamed, or the ID reused), or Auto-Start gave up
        on the plan since (a more severe report than the stop the user ignored). Placeholders and key are
        not kept across a restart: an ignore read back from storage names neither, so it holds for the name
        (the next check writes both back) but not for a give-up.
        """
        ignored = self._ignored_issue(fub_id)
        if ignored is None:
            return False
        if (ignored.translation_placeholders or {}).get("plan_name", name) != name:
            return True
        return fub_id in self.suspended and ignored.translation_key != ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED

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
        maybe_actionable: Collection[int] = (),
    ) -> None:
        """Alarm push for a newly stopped plan or one Auto-Start gave up on; the push of a plan running
        again (or deleted: its vanished push has a tag of its own) is replaced or removed, so is one with buttons
        still shown for a plan running now, or no longer watched, that no verdict of this watchdog clears
        (maybe_actionable, e.g. after a reload)."""
        alarms = dict.fromkeys(
            {*newly_stopped_plans(previous, stopped), *(set(newly_suspended) & stopped.keys())}, None
        )
        for fub_id, targets in sorted((self._stopped_push_retries_due(stopped) | alarms).items()):
            if self._ignored_issue(fub_id) is None:  # An ignored repair alarms no more until the plan ran again.
                await self._async_push_stopped(fub_id, stopped[fub_id], targets)
        # Re-checked per plan: one started from its push while an earlier push was sent has its push already.
        for fub_id, name in sorted(auto_started.items()):
            if fub_id not in self._started_in_check:
                await self._async_push(fub_id, name, "started_title", "auto_started")
        for fub_id in sorted(previous.keys() - stopped.keys() - auto_started.keys()):
            if fub_id not in self._started_in_check:
                await self._async_clear_push(fub_id)
        # A plan cleared just above is skipped under the lock (its tag is no longer actionable).
        for fub_id in sorted(set(maybe_actionable) - auto_started.keys()):
            await self._async_clear_push(fub_id, only_actionable=True)

    async def _async_push_stopped(self, fub_id: int, name: str, targets: frozenset[str] | None) -> None:
        """The alarm push of a stopped plan: "suspended" once Auto-Start gave up on it.

        targets: a retry, to the notify targets that failed to take it; None sends it to every one.
        """
        kind = "suspended" if fub_id in self.suspended else "stopped"
        failed = await self._async_push(
            fub_id,
            name,
            f"{kind}_title",
            kind,
            with_action=True,
            alarm=True,
            targets=targets,
            while_stopped=True,
            # Ignored while it waited for the tag's lock: the ignore's clear went out first, the alarm would undo it.
            unless_ignored=True,
        )
        if failed:
            # Else the plan's last push stays on that phone, maybe one naming the plan that held the ID before.
            self._stopped_push_unsent[fub_id] = (time.monotonic(), failed)

    def _stopped_push_retries_due(self, stopped: Mapping[int, str]) -> dict[int, frozenset[str] | None]:
        """Undelivered stopped alarms due again, with their targets; one of a plan running again (or unwatched)
        is dropped."""
        self._stopped_push_unsent = {
            fub_id: unsent for fub_id, unsent in self._stopped_push_unsent.items() if fub_id in stopped
        }
        return _retries_due(self._stopped_push_unsent)

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
        targets: frozenset[str] | None = None,
        while_stopped: bool = False,
        vanished_guard: _VanishedGuard = _VanishedGuard.NONE,
        unless_ignored: bool = False,
        current_name: str = "",
    ) -> frozenset[str]:
        """One push for a plan; with_action adds the "Start plan" button, actions further buttons.

        token: a deleted plan's push (vanished_plan_token), with its own tag and actions. targets: only
        to these notify targets (an alarm's retry, its failures logged quietly). while_stopped: only if the
        ID still holds this stopped plan (same name) once the tag's lock is held (a push asking to start it):
        started since, its clear went out first; another plan under the ID has its own alarm. vanished_guard: a
        deleted plan's push judged by its vanished state (see _VanishedGuard), unless_ignored: not once the plan's
        stopped repair was ignored (all judged under the lock too). Returns the targets
        that failed to take it (see _async_send). Where it was delivered it replaces (same tag) the
        plan's stopped or vanished alarm still waiting to be sent again, so those targets are dropped.
        """
        texts = self._texts()
        data: dict[str, Any] = {"tag": self._push_tag(fub_id, token), "group": f"{DOMAIN}_{self._server_id}"}
        buttons = [*([ACTION_START] if with_action else []), *actions]
        if buttons:
            data["actions"] = [
                {
                    "action": plan_action_id(kind, self._server_id, token or vanished_plan_token(fub_id, name)),
                    "title": texts[kind],
                }
                for kind in buttons
            ]
        if alarm:
            # Delivered at once and through Do Not Disturb focus (Android: ttl/priority, iOS: interruption-level).
            data |= {"ttl": 0, "priority": "high", "push": {"interruption-level": "time-sensitive"}}
        async with self._push_lock(data["tag"]):
            if self._closed:
                _LOGGER.debug("[%s] Watchdog push '%s' not sent: the entry unloads", self._server_id, message_key)
                return frozenset()
            reason = self._no_longer_due(
                fub_id,
                name,
                token,
                while_stopped=while_stopped,
                vanished_guard=vanished_guard,
                unless_ignored=unless_ignored,
            )
            if reason is not None:
                _LOGGER.info(
                    "[%s] Watchdog push '%s' for function plan %s (%s) not sent: %s",
                    self._server_id,
                    message_key,
                    fub_id,
                    one_line(name),
                    reason,
                )
                return frozenset()
            delivered, failed = await self._async_send(
                {
                    "title": texts[title_key],
                    "message": texts[message_key].format(
                        name=one_line(name),
                        fub_id=fub_id,
                        attempts=FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS,
                        current=one_line(current_name),
                    ),
                    "data": data,
                },
                targets=targets,
            )
            # A deleted plan's push is not tracked: no check judges its token running. One delivered to any target
            # counts for all (accepted: a phone it failed on keeps the old push until its action is pressed).
            if delivered and token is None:
                if buttons:
                    self._actionable_tags.add(data["tag"])
                else:
                    self._actionable_tags.discard(data["tag"])  # replaced
        # No await from the lock's release to the callers' bookkeeping of the failed targets: a push waiting
        # for the tag runs only after it, so its delivery drops these failures, never the other way round.
        # Delivered replaces (same tag) the plan's alarm still waiting to be sent again there; a retry's
        # target no longer configured is dropped too, else it would be "retried" forever, reaching no one.
        done = delivered if targets is None else targets - failed
        if token is None:
            _drop_delivered(self._stopped_push_unsent, fub_id, done)
        else:
            _drop_delivered(self._vanished_push_unsent, token, done)
        return failed

    def _no_longer_due(
        self,
        fub_id: int,
        name: str,
        token: str | None,
        *,
        while_stopped: bool,
        vanished_guard: _VanishedGuard,
        unless_ignored: bool,
    ) -> str | None:
        """Why a push judged due before its tag's lock was free is no longer due (see _async_push); None if it is."""
        if while_stopped and (self.stopped or {}).get(fub_id) != name:
            return "the plan no longer is the ID's stopped one (started meanwhile, or the next check pushes its alarm)"
        if unless_ignored and self._ignored_issue(fub_id) is not None:
            return "its repair was ignored meanwhile"
        return None if token is None else self._vanished_push_not_due(token, vanished_guard)

    def _vanished_push_not_due(self, token: str, guard: _VanishedGuard) -> str | None:
        restored = token in self._restored_pushes
        if restored and guard in (_VanishedGuard.UNLESS_RESTORED, _VanishedGuard.WHILE_VANISHED):
            return "it was restored meanwhile"
        if guard is _VanishedGuard.WHILE_PENDING and self.vanished is not None:
            return "a check ran meanwhile (its alarm, or the plan is no longer vanished)"
        listed = self.vanished_name(token) is not None
        if guard is _VanishedGuard.WHILE_VANISHED and not listed:
            return "it is no longer vanished (back again, or unwatched)"
        if guard is _VanishedGuard.UNLESS_VANISHED_AGAIN and listed and not restored:
            # Marked while vanished (see async_push_restored); the mark gone, a check dropped the plan since.
            return "it vanished again meanwhile"
        return None

    async def async_sync_vanished(
        self, vanished: Collection[tuple[int, str]], holders: Mapping[int, str] | None = None
    ) -> None:
        """Repair per watched user plan Comexio no longer has, by (fub_id, name); alarm push for a newly vanished one.

        holders: fub_id → name of the plan holding a vanished plan's ID now (renamed, or the ID reused); its
        repair and push name that plan. The first call after setup pushes every vanished plan, as the
        stopped-plan check does: a plan deleted while HA was down is reported. A plan back again (restored
        under a new ID) or unpicked loses its repair and push, except a "restored" confirmation, which stays
        on the phone.
        """
        if self._closed:
            return  # unloaded: the reloaded watchdog owns the shared repairs, this snapshot may predate them
        previous = self.vanished or set()
        self.vanished = set(vanished)
        self._log_holder_changes(previous & self.vanished, dict(holders or {}))
        self.vanished_holders = dict(holders or {})
        self._sync_vanished_issues(self.vanished)
        newly_vanished = self.vanished - previous
        retries = self._vanished_push_retries_due(self.vanished) | dict.fromkeys(newly_vanished, None)
        self._vanished_clearing = previous - self.vanished
        try:
            for (fub_id, name), targets in sorted(retries.items()):
                await self._async_push_vanished(fub_id, name, targets)
            for fub_id, name in sorted(previous - self.vanished):
                self._vanished_clearing.discard((fub_id, name))
                token = vanished_plan_token(fub_id, name)
                if token in self._restored_pushes:
                    self._restored_pushes.discard(token)
                    continue
                await self._async_clear_push(fub_id, token)
        finally:
            # Not reached (the sync raised): a mark set meanwhile would outlive its plan and hold back its next alarm.
            self._restored_pushes -= {vanished_plan_token(fub_id, name) for fub_id, name in self._vanished_clearing}
            self._vanished_clearing = set()

    def _log_holder_changes(self, still_vanished: Collection[tuple[int, str]], holders: Mapping[int, str]) -> None:
        """Log a still-vanished plan whose ID got another holder; its repair follows, its push is not resent."""
        for fub_id, name in sorted(still_vanished):
            if (holder := holders.get(fub_id)) != self.vanished_holders.get(fub_id):
                _LOGGER.info(
                    "[%s] Monitored function plan '%s' (ID %s), still not found: the ID now holds %s",
                    self._server_id,
                    one_line(name),
                    fub_id,
                    "no plan" if holder is None else f"'{one_line(holder)}'",
                )

    def _sync_vanished_issues(self, vanished: Collection[tuple[int, str]]) -> None:
        for fub_id, name in vanished:
            details = {"plan_name": name, "fub_id": str(fub_id)}
            if (holder := self.vanished_holders.get(fub_id)) is not None:
                details["current_name"] = holder
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                vanished_plan_issue_id(self._server_id, fub_id, name),
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_FUNCTION_PLAN_VANISHED if holder is None else ISSUE_FUNCTION_PLAN_REPLACED,
                translation_placeholders=one_line_values(details),
                data={"entry_id": self._entry_id, **details},
            )
        wanted = {vanished_plan_issue_id(self._server_id, fub_id, name) for fub_id, name in vanished}
        for issue_id in _own_issue_ids(self._hass, self._server_id, vanished=True):
            if issue_id not in wanted:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    async def _async_push_vanished(self, fub_id: int, name: str, targets: frozenset[str] | None) -> None:
        """The alarm push of a deleted plan; "Restore" only for a plan its backups can be found by.

        targets: a retry, to the notify targets that failed to take it; None sends it to every one.
        """
        holder = self.vanished_holders.get(fub_id)
        if targets is None and holder is None:
            _LOGGER.warning(
                "[%s] Monitored function plan '%s' (ID %s) no longer exists in Comexio",
                self._server_id,
                one_line(name),
                fub_id,
            )
        elif targets is None:
            _LOGGER.warning(
                "[%s] Monitored function plan '%s' (ID %s) not found: the ID now holds '%s' (renamed, or the ID "
                "reused for another plan); see its repair",
                self._server_id,
                one_line(name),
                fub_id,
                one_line(holder),
            )
        token = vanished_plan_token(fub_id, name)
        if token in self._restored_pushes:
            # Its Restore brought it back meanwhile: the alarm would replace the confirmation on its way.
            _LOGGER.debug(
                "[%s] No alarm push for function plan '%s' (ID %s): restored", self._server_id, one_line(name), fub_id
            )
            self._vanished_push_unsent.pop(token, None)
            return
        failed = await self._async_push(
            fub_id,
            name,
            "vanished_title" if holder is None else "replaced_title",
            "vanished" if holder is None else "replaced",
            current_name=holder or "",
            token=token,
            # With another plan on the ID the choice (restore, take a rename over, watch it) is the repair's:
            # a one-tap Restore of a renamed plan would start a copy beside the original.
            actions=_vanished_actions(fub_id, name) if holder is None else (ACTION_UNWATCH,),
            alarm=True,
            targets=targets,
            # Restored while an earlier plan's push was sent: the alarm would replace that confirmation, and the
            # next sync keeps the "restored" push (see _restored_pushes), so the stale Restore button would stay.
            vanished_guard=_VanishedGuard.UNLESS_RESTORED,
        )
        if failed:
            self._vanished_push_unsent[token] = (time.monotonic(), failed)  # the answer's time: the gap starts here

    def _vanished_push_retries_due(
        self, vanished: Collection[tuple[int, str]]
    ) -> dict[tuple[int, str], frozenset[str] | None]:
        """Undelivered vanished alarms due again, with their targets; one of a plan back again (or unpicked)
        is dropped."""
        by_token = {vanished_plan_token(fub_id, name): (fub_id, name) for fub_id, name in vanished}
        self._vanished_push_unsent = {
            token: unsent for token, unsent in self._vanished_push_unsent.items() if token in by_token
        }
        return {by_token[token]: targets for token, targets in _retries_due(self._vanished_push_unsent).items()}

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
            # Its buttons would replace the "restored" push sent meanwhile, or name a plan no longer vanished.
            vanished_guard=_VanishedGuard.WHILE_VANISHED,
        )

    async def async_push_restored(self, fub_id: int, name: str, *, complete: bool = True) -> None:
        """Replace the vanished push with "restored" once its Restore action brought the plan back.

        complete: False says the plan is back but missing parts (a PARTIAL restore).
        """
        token = vanished_plan_token(fub_id, name)
        # Marked before the send: a check dropping the plan from vanished meanwhile must not clear the confirmation
        # on its way, nor the one dropping it right now (its clear still pending). Not marked once a check cleared it
        # already: the entry would stay stale.
        marked = (fub_id, name) in (self.vanished or ()) or (fub_id, name) in self._vanished_clearing
        if marked:
            self._restored_pushes.add(token)
        key = "restored" if complete else "restored_partial"
        try:
            # Vanished again while it waited for the tag: it would replace that episode's alarm with its Restore.
            failed = await self._async_push(
                fub_id, name, "restored_title", key, token=token, vanished_guard=_VanishedGuard.UNLESS_VANISHED_AGAIN
            )
        except BaseException:
            # Cancelled on its way: the alarm is cleared after all, as for a failed send.
            if marked:
                self._unmark_cancelled_restored(fub_id, name, token)
            raise
        if marked and any(_is_mobile_app(target) for target in failed):
            # A phone it failed on keeps the vanished alarm with its Restore button: cleared after all (also
            # where the confirmation arrived; a stale button weighs more than a lost confirmation). Another
            # notify service failing has no button to keep, nor would a clear reach it.
            if token in self._restored_pushes:
                self._restored_pushes.discard(token)  # the check dropping the plan from vanished clears it
                # Nor is the alarm retried to a phone that missed it: the plan is back. Only while still marked:
                # else a check dropped the plan meanwhile, and a retry now is a new episode's alarm.
                self._vanished_push_unsent.pop(token, None)
            elif (fub_id, name) not in (self.vanished or ()):
                # A check dropped it meanwhile and kept the push. Vanished again since: the same tag holds that
                # episode's new alarm, which stays.
                await self._async_clear_push(fub_id, token)

    def _unmark_cancelled_restored(self, fub_id: int, name: str, token: str) -> None:
        if token in self._restored_pushes:
            self._restored_pushes.discard(token)  # the check dropping the plan from vanished clears it
            self._vanished_push_unsent.pop(token, None)  # as for a failed send: the plan is back
        elif (fub_id, name) not in (self.vanished or ()):
            # A check dropped it meanwhile and kept the alarm for the confirmation: no await in a cancelled task.
            # Queued once the task runs: a check listing the plan again meanwhile sent that episode's alarm first.
            self._hass.async_create_background_task(
                self._async_clear_push(fub_id, token, unless_vanished=True),
                name=f"{DOMAIN}_{self._server_id}_restored_push_cancelled",
            )

    async def async_push_restore_failed(self, fub_id: int, name: str) -> None:
        """Tell the phone a Restore from its push failed or was refused, with both actions again."""
        await self._async_push(
            fub_id,
            name,
            "restore_failed_title",
            "restore_failed",
            token=vanished_plan_token(fub_id, name),
            actions=_vanished_actions(fub_id, name),
            # Its buttons would replace the "restored" push sent meanwhile, or name a plan no longer vanished.
            vanished_guard=_VanishedGuard.WHILE_VANISHED,
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
            # The first check ran while it waited: it would replace that check's alarm, or offer buttons the
            # action handler turns down (see _VanishedGuard.WHILE_PENDING).
            vanished_guard=_VanishedGuard.WHILE_PENDING,
        )
        # Not listed by a check: the tapped push is stale (nothing else clears it, the first check had no previous).
        if self.vanished is not None and self.vanished_name(token) is None and token not in self._restored_pushes:
            await self._async_clear_push(fub_id, token, unless_vanished=True)

    async def async_dismiss_push(self, fub_id: int, name: str | None = None) -> None:
        """Remove a plan's push from the phones: its "stopped" one, or with name its deleted-plan one.

        E.g. one whose "Start plan" action is no longer honoured, or the alarm of a plan restored meanwhile.
        The "stopped" one is kept while the ID's plan is stopped, judged when the clear is sent: a stop judged
        since the clear was asked for pushed that plan's alarm under the same tag, which the clear would take off.
        A stop judged while the clear is on its way sends its alarm after it (the tag's lock, see _push_locks).
        """
        if name is None:
            await self._async_clear_push(fub_id, keep_stopped=True)
        else:
            await self._async_clear_push(fub_id, vanished_plan_token(fub_id, name))

    async def async_dismiss_vanished_push(self, fub_id: int, token: str) -> None:
        """Remove a deleted plan's push by the token its action carried (the name may be unknown by now).

        Not once a check listed the plan as vanished again before the clear was sent: that episode's alarm holds
        the tag (this clear is queued from a background task, after the check found the push stale).
        """
        await self._async_clear_push(fub_id, token, unless_vanished=True)

    async def _async_clear_push(
        self,
        fub_id: int,
        token: str | None = None,
        *,
        keep_stopped: bool = False,
        only_actionable: bool = False,
        unless_vanished: bool = False,
    ) -> None:
        """keep_stopped: not while the ID's plan is stopped, judged under the tag's lock, so a stop judged meanwhile
        sends its alarm after the clear arrived. only_actionable: only while the tag's last push delivered has
        buttons, judged under the lock too: a push sent since replaced it. unless_vanished: not while the deleted
        plan (token) is vanished, judged under the lock too: a check listed it again, its new alarm holds the tag."""
        tag = self._push_tag(fub_id, token)
        async with self._push_lock(tag):
            if (
                self._closed
                or (keep_stopped and fub_id in (self.stopped or {}))
                or (only_actionable and tag not in self._actionable_tags)
                or (unless_vanished and token is not None and self.vanished_name(token) is not None)
            ):
                return
            # A companion-app command: any other notify service would deliver it as the text "clear_notification".
            await self._async_send({"message": "clear_notification", "data": {"tag": tag}}, mobile_app_only=True)
            self._actionable_tags.discard(tag)  # a failed clear is logged, not retried (as every clear)

    @asynccontextmanager
    async def _push_lock(self, tag: str) -> AsyncIterator[None]:
        lock, users = self._push_locks.get(tag, (asyncio.Lock(), 0))
        self._push_locks[tag] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._push_locks[tag]
            if users > 1:
                self._push_locks[tag] = (lock, users - 1)
            else:
                del self._push_locks[tag]

    async def _async_send(
        self,
        service_data: dict[str, Any],
        *,
        mobile_app_only: bool = False,
        targets: frozenset[str] | None = None,
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Send to every configured notify service; a failing one is logged and does not stop the others.

        targets: only to those of them (a retry; a target no longer configured is skipped), whose
        failures were logged already, so they are logged at debug level: a dead notify service must not
        flood the log. Returns the targets it was delivered to and those that failed.
        """
        log_failure = _LOGGER.warning if targets is None else _LOGGER.debug
        delivered: set[str] = set()
        failed: set[str] = set()
        for target in self._notify_targets():
            service = target.removeprefix(f"{NOTIFY_DOMAIN}.")
            if (mobile_app_only and not _is_mobile_app(target)) or (targets is not None and target not in targets):
                continue
            try:
                # Bounded: a hanging notify service would hold the push tag's lock, and so its clears, too.
                async with asyncio.timeout(FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC):
                    await self._hass.services.async_call(NOTIFY_DOMAIN, service, service_data, blocking=True)
                delivered.add(target)
            except (HomeAssistantError, vol.Invalid) as err:
                failed.add(target)
                log_failure("[%s] Function plan watchdog push to notify.%s failed: %s", self._server_id, service, err)
            except Exception:
                # Third-party notify platforms raise their own errors (aiohttp, timeouts, ...); none of
                # them may skip the remaining targets or the watchdog check that sends the push.
                failed.add(target)
                log_failure(
                    "[%s] Function plan watchdog push to notify.%s failed", self._server_id, service, exc_info=True
                )
        return frozenset(delivered), frozenset(failed)
