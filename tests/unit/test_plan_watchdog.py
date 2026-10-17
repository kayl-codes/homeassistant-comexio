"""Watchdog of the HA-managed function plans (plan_watchdog.py)."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import api as api_module, plan_watchdog
from custom_components.comexio.const import (
    DOMAIN,
    FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS,
    FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC,
    FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC,
    FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC,
)
from custom_components.comexio.plan_watchdog import (
    ACTION_RESTORE,
    ACTION_START,
    ACTION_UNWATCH,
    ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED,
    ISSUE_FUNCTION_PLAN_STOPPED,
    ISSUE_FUNCTION_PLAN_STOPPED_USER,
    ISSUE_FUNCTION_PLAN_VANISHED,
    ManagedPlanWatchdog,
    next_stopped_plans,
    parse_start_action,
    plan_action_id,
    start_action_id,
    stopped_plan_issue_id,
    vanished_plan_token,
)

SERVER_ID = "cx1"
MANAGED = {34: "HA - Marker 1", 42: "HA - TRIGGER"}


def _states(**by_id: bool | None) -> Any:
    """run_state callable from keyword args like p34=True."""
    return lambda fub_id: by_id.get(f"p{fub_id}")


@pytest.mark.parametrize(
    ("states", "previous", "expected"),
    [
        ({"p34": True, "p42": True}, {}, {}),
        ({"p34": False, "p42": True}, {}, {34: "HA - Marker 1"}),
        # Unknown (unreadable or just stopped by HA) keeps the previous verdict either way.
        ({"p34": None, "p42": True}, {34: "HA - Marker 1"}, {34: "HA - Marker 1"}),
        ({"p34": None, "p42": True}, {}, {}),
        # (#185) ... of the plan, not of the ID: one holding another plan now keeps none.
        ({"p34": None, "p42": True}, {34: "Old"}, {}),
        ({"p34": True, "p42": False}, {34: "HA - Marker 1"}, {42: "HA - TRIGGER"}),
    ],
)
def test_next_stopped_plans(states: dict[str, bool | None], previous: dict[int, str], expected: dict[int, str]) -> None:
    assert next_stopped_plans(MANAGED, _states(**states), previous) == expected


@pytest.fixture
def ir(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Issue registry stand-in that remembers created issues, so the clean-up sees them.

    Like HA's: creating an existing issue again keeps its ignore (dismissed_version); deleting it drops it.
    """
    registry = MagicMock()
    issues: dict[tuple[str, str], Any] = {}
    registry.async_get.return_value.issues = issues
    registry.async_get.return_value.async_get_issue.side_effect = lambda domain, issue_id: issues.get(
        (domain, issue_id)
    )

    def create(_hass: Any, domain: str, issue_id: str, **kwargs: Any) -> None:
        old = issues.get((domain, issue_id))
        issues[(domain, issue_id)] = SimpleNamespace(
            dismissed_version=getattr(old, "dismissed_version", None),
            translation_placeholders=kwargs.get("translation_placeholders"),
            translation_key=kwargs.get("translation_key"),
        )

    def ignore(_hass: Any, domain: str, issue_id: str, ignored: bool) -> None:
        issues[(domain, issue_id)].dismissed_version = "2026.10.0" if ignored else None

    registry.async_create_issue.side_effect = create
    registry.async_ignore_issue.side_effect = ignore
    registry.async_delete_issue.side_effect = lambda _hass, domain, issue_id: issues.pop((domain, issue_id), None)
    monkeypatch.setattr(plan_watchdog, "ir", registry)
    monkeypatch.setattr(plan_watchdog, "persistent_notification", MagicMock())
    return registry


def _watchdog(
    start_results: list[bool | None] | None = None, targets: list[str] | None = None, server_id: str = SERVER_ID
) -> tuple[ManagedPlanWatchdog, list[int]]:
    started: list[int] = []
    results = iter(start_results or [])

    async def start(fub_id: int) -> bool | None:
        started.append(fub_id)
        return next(results)

    hass = MagicMock()
    hass.config.language = "de"
    hass.services.async_call = AsyncMock()
    watchdog = ManagedPlanWatchdog(
        hass, entry_id="e1", server_id=server_id, start_plan=start, notify_targets=lambda: list(targets or [])
    )
    # The alarm/repair tests run with Plan Auto-Start off; the auto-start tests switch it on themselves.
    watchdog.auto_restart = False
    return watchdog, started


def test_plan_auto_start_is_on_by_default() -> None:
    watchdog = ManagedPlanWatchdog(
        MagicMock(), entry_id="e1", server_id=SERVER_ID, start_plan=AsyncMock(), notify_targets=list
    )
    assert watchdog.auto_restart is True


def test_stopped_plan_raises_and_clears_its_repair(ir: MagicMock) -> None:
    watchdog, started = _watchdog()
    assert watchdog.stopped is None

    assert asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True))) is True
    assert watchdog.stopped == {34: "HA - Marker 1"}
    _, kwargs = ir.async_create_issue.call_args
    assert ir.async_create_issue.call_args.args[1:] == (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    assert kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED
    assert kwargs["data"] == {"entry_id": "e1", "plan_name": "HA - Marker 1", "fub_id": "34", "attempts": "2"}

    assert asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True))) is True
    assert watchdog.stopped == {}
    assert ir.async_get.return_value.issues == {}
    assert started == []  # _watchdog() switches Plan Auto-Start off (its default is on)


def test_other_servers_repairs_are_left_alone(ir: MagicMock) -> None:
    """Server "cx1" must not close the repairs of "cx1_2" (same prefix)."""
    foreign = (DOMAIN, f"{ISSUE_FUNCTION_PLAN_STOPPED}_{SERVER_ID}_2_34")
    ir.async_get.return_value.issues[foreign] = True
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert foreign in ir.async_get.return_value.issues


def test_auto_start_starts_the_plan_without_a_repair(ir: MagicMock) -> None:
    watchdog, started = _watchdog([True])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34]
    assert watchdog.stopped == {}
    ir.async_create_issue.assert_not_called()
    plan_watchdog.persistent_notification.async_create.assert_called_once()


def test_user_plans_follow_their_own_auto_start_switch(ir: MagicMock) -> None:
    """HA-plan auto-start on, user-plan auto-start off: only the HA plan is started."""
    watchdog, started = _watchdog([True])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=False), user_plans={42}))
    assert started == [34]
    assert watchdog.stopped == {42: "HA - TRIGGER"}
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED_USER


def test_user_plan_auto_start_starts_only_the_user_plan(ir: MagicMock) -> None:
    watchdog, started = _watchdog([True])
    watchdog.auto_restart_user = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=False), user_plans={42}))
    assert started == [42]
    assert watchdog.stopped == {34: "HA - Marker 1"}
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED


def test_auto_start_turned_off_during_a_start_starts_no_further_plan(ir: MagicMock) -> None:
    """Review (Copilot, #132): the switch turned off while the first start awaits Comexio stops the rest."""
    watchdog, started = _watchdog()
    watchdog.auto_restart = True

    async def start(fub_id: int) -> bool:
        started.append(fub_id)
        watchdog.auto_restart = False  # the user turns Auto-Start off meanwhile
        return True

    watchdog._start_plan = start
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=False)))
    assert started == [34]
    assert watchdog.stopped == {42: "HA - TRIGGER"}
    assert ir.async_create_issue.call_args.args[2] == stopped_plan_issue_id(SERVER_ID, 42)


def test_user_plan_auto_start_is_off_by_default() -> None:
    watchdog = ManagedPlanWatchdog(
        MagicMock(), entry_id="e1", server_id=SERVER_ID, start_plan=AsyncMock(), notify_targets=list
    )
    assert watchdog.auto_restart_user is False


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Controllable time.monotonic() of the watchdog."""
    now = [1000.0]
    monkeypatch.setattr(plan_watchdog, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.parametrize(
    ("result", "check_after", "window"),
    [
        (False, FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC, "retry gap"),
        (True, FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC, "stopped-again window"),
    ],
    ids=["refused", "started"],
)
def test_auto_start_times_count_from_comexios_answer(
    ir: MagicMock, clock: list[float], result: bool, check_after: float, window: str
) -> None:
    """A slow start answer must not shorten the retry gap or the stopped-again window."""
    answer_delay = 30.0
    watchdog, started = _watchdog([result])

    async def slow_start(fub_id: int) -> bool:
        started.append(fub_id)
        clock[0] += answer_delay
        return result

    watchdog._start_plan = slow_start
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))

    clock[0] += check_after - 1  # still inside the window counted from the answer
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    if result:
        assert watchdog._failed_starts == {34: 1}, f"stopped again inside the {window}"
    else:
        assert started == [34], f"no second attempt inside the {window}"


def test_refused_auto_start_gives_up_after_two_attempts(ir: MagicMock, clock: list[float]) -> None:
    watchdog, started = _watchdog([False, False], targets=["mobile_app_phone"])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert watchdog.suspended == set()
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED
    assert "gave_up_at" not in ir.async_create_issue.call_args.kwargs["data"]

    clock[0] += FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC - 1
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34]  # too soon after the refusal: no second attempt yet

    clock[0] += 1
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert watchdog.suspended == {34}
    kwargs = ir.async_create_issue.call_args.kwargs
    assert kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED
    assert kwargs["translation_placeholders"]["gave_up_at"] == kwargs["data"]["gave_up_at"] != ""

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34]  # suspended: no third attempt
    assert watchdog.stopped == {34: "HA - Marker 1"}
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34)) in ir.async_get.return_value.issues
    titles = [data["title"] for _, _, data in _pushes(watchdog)]
    assert titles == ["Comexio: Logikplan gestoppt", "Comexio: Logikplan prüfen"]


def test_plan_running_again_without_auto_start_clears_the_failure_count(ir: MagicMock) -> None:
    """A refusal long forgotten must not let a single later refusal give up on the plan."""
    watchdog, started = _watchdog([False, False])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # failure 1
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))  # started by hand
    # A fresh failure 1, at once: running also cleared the retry gap of the old refusal.
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34]
    assert watchdog.suspended == set()


def test_last_check_is_kept_for_the_repair_dialog(ir: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    watchdog, _ = _watchdog()
    assert watchdog.last_check_text() == "—"
    checked = plan_watchdog.dt_util.parse_datetime("2026-10-03T08:14:34+02:00")
    monkeypatch.setattr(plan_watchdog.dt_util, "now", lambda: checked)
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert watchdog.last_check == checked
    assert watchdog.last_check_text() == "03.10.2026 08:14:34"  # HA language "de"


def test_blocked_or_unanswered_start_is_not_a_failed_attempt(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """None (sync/restore running, no answer) says nothing about the plan: never counted, but retried only
    after the retry gap — an unanswered start must not be sent again with every check (#181)."""
    watchdog, started = _watchdog([None, None, None])
    watchdog.auto_restart = True
    caplog.set_level(logging.DEBUG, logger=plan_watchdog.__name__)
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    clock[0] += FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC - 1
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34]  # inside the gap: not sent again
    for _ in range(2):
        clock[0] += FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC
        asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34, 34]
    assert watchdog.suspended == set()
    assert watchdog._failed_starts == {}
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED
    deferred = [r.levelno for r in caplog.records if "not sent or not answered" in r.getMessage()]
    assert deferred == [logging.INFO, logging.DEBUG, logging.DEBUG]  # info once per series


def test_an_answered_start_ends_the_deferral_series(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal between two unanswered starts ends the series: the next deferral is logged at info again."""
    watchdog, started = _watchdog([None, False, None])
    watchdog.auto_restart = True
    caplog.set_level(logging.DEBUG, logger=plan_watchdog.__name__)
    deferral_after: list[dict[int, float]] = []
    for _ in range(3):
        asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
        deferral_after.append(dict(watchdog._deferred_at))
        clock[0] += FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC
    assert started == [34, 34, 34]
    assert deferral_after[1] == {}  # the refused (answered) start cleared the deferral
    deferred = [r.levelno for r in caplog.records if "not sent or not answered" in r.getMessage()]
    assert deferred == [logging.INFO, logging.INFO]


def test_a_deferred_start_of_a_plan_seen_running_is_forgotten(ir: MagicMock, clock: list[float]) -> None:
    """A plan running again starts its next stop with a fresh attempt, not inside an old retry gap."""
    watchdog, started = _watchdog([None, True])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert watchdog._deferred_at == {}
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34]


def test_plan_started_by_the_repair_clears_it_at_once(ir: MagicMock) -> None:
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=False)))
    asyncio.run(watchdog.async_plan_started(34))
    assert watchdog.stopped == {42: "HA - TRIGGER"}
    assert list(ir.async_get.return_value.issues) == [(DOMAIN, stopped_plan_issue_id(SERVER_ID, 42))]


def test_ha_stop_grace_ends_with_a_start_or_the_time(comexio_api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only HA's own stop opens the grace window; HA's start or the elapsed time closes it."""
    now = [1000.0]
    monkeypatch.setattr(api_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert comexio_api.ha_stopped_within(34, 300) is False
    comexio_api.set_fub_active(34, False)
    now[0] += 299
    assert comexio_api.ha_stopped_within(34, 300) is True
    now[0] += 1
    assert comexio_api.ha_stopped_within(34, 300) is False
    comexio_api.set_fub_active(34, False)
    comexio_api.set_fub_active(34, True)
    assert comexio_api.ha_stopped_within(34, 300) is False


def _pushes(watchdog: ManagedPlanWatchdog) -> list[tuple[str, str, dict[str, Any]]]:
    return [call.args[:3] for call in watchdog._hass.services.async_call.call_args_list]


@pytest.mark.parametrize("push", ["async_push_start_failed", "async_push_start_busy", "async_push_start_outdated"])
def test_a_repeated_start_action_names_the_plan_it_was_pushed_for(ir: MagicMock, push: str) -> None:
    """(#184) the "Start plan" of the failed/busy push carries the plan name it is sent with, as the alarm's."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {34: "HA - Marker 1"}  # the outdated replacement is sent only while it is
    asyncio.run(getattr(watchdog, push)(34, "HA - Marker 1"))
    actions = _pushes(watchdog)[0][2]["data"]["actions"]
    assert [action["action"] for action in actions] == [start_action_id(SERVER_ID, 34, "HA - Marker 1")]


def test_the_start_action_carries_the_plan_name_digest() -> None:
    """(#185) Pinned literally: the tests comparing pushes against start_action_id rely on it naming the plan."""
    assert start_action_id(SERVER_ID, 34, "HA - Marker 1") == "COMEXIO_START_PLAN_cx1_34_cbb7f0cecb5509a8"
    assert start_action_id(SERVER_ID, 34, "Other") != start_action_id(SERVER_ID, 34, "HA - Marker 1")


def test_stop_pushes_an_alarm_with_a_start_action_and_recovery_clears_it(ir: MagicMock) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "mobile_app_tablet"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    pushes = _pushes(watchdog)
    assert [(domain, service) for domain, service, _ in pushes] == [
        ("notify", "mobile_app_phone"),
        ("notify", "mobile_app_tablet"),
    ]
    alarm = pushes[0][2]
    assert "HA - Marker 1" in alarm["message"]
    assert alarm["data"]["actions"] == [
        {"action": start_action_id(SERVER_ID, 34, "HA - Marker 1"), "title": "Plan starten"}
    ]
    tag = alarm["data"]["tag"]

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert len(_pushes(watchdog)) == 2  # still stopped: no repeated alarm

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert _pushes(watchdog)[2][2] == {"message": "clear_notification", "data": {"tag": tag}}


def test_failing_notify_service_does_not_stop_the_watchdog(ir: MagicMock) -> None:
    watchdog, _ = _watchdog(targets=["mobile_app_gone", "mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = [HomeAssistantError("service not found"), None]
    assert asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True))) is True
    assert [service for _, service, _ in _pushes(watchdog)] == ["mobile_app_gone", "mobile_app_phone"]


def test_any_notify_error_spares_the_other_targets_and_the_check(ir: MagicMock) -> None:
    """Third-party notify platforms raise their own errors; the check must still finish and report."""
    watchdog, _ = _watchdog(targets=["mobile_app_flaky", "mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = [TimeoutError("push timed out"), None]
    assert asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True))) is True
    assert [service for _, service, _ in _pushes(watchdog)] == ["mobile_app_flaky", "mobile_app_phone"]
    assert watchdog.stopped == {34: "HA - Marker 1"}


def test_clear_notification_goes_to_companion_apps_only(ir: MagicMock) -> None:
    """Any other notify service would deliver the command as the text "clear_notification"."""
    watchdog, _ = _watchdog(targets=["mobile_app_phone", "notify.telegram"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    clears = [service for _, service, data in _pushes(watchdog) if data["message"] == "clear_notification"]
    assert clears == ["mobile_app_phone"]


def test_flapping_plan_suspends_auto_start_until_seen_running(ir: MagicMock) -> None:
    """A plan stopping again right after each auto-start is given up on after two failures, with a
    "check the plan" repair and alarm; only a poll seeing it running lifts that."""
    watchdog, started = _watchdog([True, True, True], targets=["mobile_app_phone"])
    watchdog.auto_restart = True
    for _ in range(2):  # start, stopped again (failure 1) → second start
        asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
        assert watchdog.stopped == {}
    assert started == [34, 34]

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # stopped again: failure 2
    assert started == [34, 34]
    assert watchdog.suspended == {34}
    assert watchdog.stopped == {34: "HA - Marker 1"}
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED
    alarm = _pushes(watchdog)[-1][2]
    assert alarm["title"] == "Comexio: Logikplan prüfen"
    assert "2 Versuchen" in alarm["message"]
    assert "actions" in alarm["data"]

    # Started by hand from the repair: the suspension holds until a poll sees the plan running.
    asyncio.run(watchdog.async_plan_started(34))
    assert watchdog.suspended == {34}
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert watchdog.suspended == set()

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34, 34]  # Auto-Start works again, with a fresh count
    assert watchdog.suspended == set()


def test_plan_running_past_the_window_resets_the_failure_count(ir: MagicMock, clock: list[float]) -> None:
    now = clock
    watchdog, started = _watchdog([True, True, True])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # failure 1, started again
    now[0] += FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))  # ran past the window
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # a fresh stop, not failure 2
    assert started == [34, 34, 34]
    assert watchdog.suspended == set()


def test_deleted_watched_plan_is_not_logged_as_running_again(ir: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    with caplog.at_level("INFO"):
        asyncio.run(watchdog.async_check({42: "HA - TRIGGER"}, _states(p42=True)))
    assert "is no longer watched" in caplog.text
    assert "runs again" not in caplog.text


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (start_action_id(SERVER_ID, 34, "HA - Marker 1"), 34),
        ("COMEXIO_START_PLAN_cx1_34", 34),  # sent before the action carried the name digest
        (start_action_id(f"{SERVER_ID}_2", 34, "HA - Marker 1"), None),  # another server's action
        ("COMEXIO_START_PLAN_cx1_2_34", None),
        ("SOME_OTHER_ACTION", None),
        (None, None),
    ],
)
def test_parse_start_action(action: Any, expected: int | None) -> None:
    assert parse_start_action(SERVER_ID, action) == expected


def test_without_auto_start_a_stopped_plan_only_keeps_its_repair(ir: MagicMock) -> None:
    """The coordinator's boot grace: nothing is started, whatever the switches say."""
    watchdog, started = _watchdog([True])
    watchdog.auto_restart = True
    watchdog.stopped = {34: "HA - Marker 1"}
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=None, p42=None), auto_start=False))
    assert started == []
    assert watchdog.stopped == {34: "HA - Marker 1"}  # unknown keeps the verdict, no new one for 42
    assert set(ir.async_get.return_value.issues) == {(DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))}


# (cl) A watched user plan deleted in Comexio: "no longer exists" with Restore / Stop watching.

TEST4 = {(47, "Test4")}
TOKEN = plan_watchdog.vanished_plan_token(47, "Test4")


def test_a_vanished_plan_gets_its_repair_and_one_alarm_push(ir: MagicMock) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(set()))  # first check after setup
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    issue_id = plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")
    assert (DOMAIN, issue_id) in ir.async_get.return_value.issues
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_VANISHED
    ((_domain, service, data),) = _pushes(watchdog)
    assert service == "mobile_app_phone"
    assert data["message"] == "Der überwachte Logikplan Test4 (ID 47) existiert in Comexio nicht mehr."
    assert [action["action"] for action in data["data"]["actions"]] == [
        plan_action_id(ACTION_RESTORE, SERVER_ID, TOKEN),
        plan_action_id(ACTION_UNWATCH, SERVER_ID, TOKEN),
    ]
    assert data["data"]["priority"] == "high"  # an alarm

    asyncio.run(watchdog.async_sync_vanished(TEST4))  # still missing: no second push
    assert len(_pushes(watchdog)) == 1
    asyncio.run(watchdog.async_sync_vanished(set()))  # restored under a new ID, or unpicked
    assert (DOMAIN, issue_id) not in ir.async_get.return_value.issues
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


def test_a_vanished_alarm_no_service_delivered_is_sent_again(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery) The plan counted as reported although the push failed: the phone never heard of it.

    (local review) Not with every check (every few seconds with a plan card open), and only the
    first failure warns: a removed phone must not flood the log.
    """
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "notify.mobile_app_tablet"])
    watchdog._hass.services.async_call.side_effect = [HomeAssistantError("down"), TimeoutError("down")]
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert caplog.text.count("Function plan watchdog push to notify") == 2
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("still down")
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC - 1
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert len(_pushes(watchdog)) == 2  # too soon: no retry
    clock[0] += 1
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert len(_pushes(watchdog)) == 4  # retried, failed again ...
    assert caplog.text.count("Function plan watchdog push to notify") == 2  # ... quietly
    watchdog._hass.services.async_call.side_effect = None
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert [data["message"] for *_, data in _pushes(watchdog)][-2:] == [
        "Der überwachte Logikplan Test4 (ID 47) existiert in Comexio nicht mehr."
    ] * 2
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished(TEST4))  # delivered now: no further push
    assert len(_pushes(watchdog)) == 6


def test_a_vanished_alarm_is_sent_again_only_to_the_service_that_failed(ir: MagicMock, clock: list[float]) -> None:
    """(Sourcery, #185) Another service taking it is no delivery for the failed one's phone."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "notify.mobile_app_tablet"])
    watchdog._hass.services.async_call.side_effect = [HomeAssistantError("down"), None, None]
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    services = [service for _d, service, _s in _pushes(watchdog)]
    assert services == ["mobile_app_phone", "mobile_app_tablet", "mobile_app_phone"]
    assert watchdog._vanished_push_unsent == {}


def test_a_later_push_delivered_for_the_plan_replaces_its_undelivered_alarm(ir: MagicMock, clock: list[float]) -> None:
    """(local review) The retry would overwrite (same tag) e.g. the "restore failed" push the phone shows now."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_push_restore_failed(47, "Test4"))
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert _pushes(watchdog)[-1][2]["title"] == watchdog._texts()["restore_failed_title"]


def test_an_undelivered_alarm_of_a_plan_back_again_is_not_sent(ir: MagicMock, clock: list[float]) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    watchdog._hass.services.async_call.side_effect = None
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished(set()))
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"
    assert watchdog._vanished_push_unsent == {}


def test_a_vanished_alarm_without_notify_service_is_not_retried(ir: MagicMock) -> None:
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert watchdog._vanished_push_unsent == {}


def test_a_plan_vanished_before_setup_gets_its_repair_and_push(ir: MagicMock) -> None:
    """(Sourcery) Deleted while HA was down: reported and pushed (Restore / Stop watching)."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")) in ir.async_get.return_value.issues
    ((_domain, _service, data),) = _pushes(watchdog)
    assert data["data"]["tag"] == watchdog._push_tag(47, TOKEN)
    assert [action["action"] for action in data["data"]["actions"]] == [
        plan_action_id(ACTION_RESTORE, SERVER_ID, TOKEN),
        plan_action_id(ACTION_UNWATCH, SERVER_ID, TOKEN),
    ]


def test_the_restored_confirmation_stays_once_the_plan_is_back(ir: MagicMock) -> None:
    """(Sourcery) The plan dropping out of vanished must not clear the "restored" push (same tag)."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    asyncio.run(watchdog.async_sync_vanished(set()))
    messages = [data["message"] for *_, data in _pushes(watchdog)]
    assert "clear_notification" not in messages
    assert len(messages) == 2
    # Vanished again later: a new alarm, and leaving vanished then clears it as usual.
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_sync_vanished(set()))
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


def test_a_plan_restored_while_an_earlier_plans_alarm_is_sent_keeps_its_restored_push(ir: MagicMock) -> None:
    """(Sourcery, #187) The sync's alarm for it would replace "restored" (same tag), and the next sync keeps that
    push, so the stale Restore button would stay."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    test5 = plan_watchdog.vanished_plan_token(48, "Test5")

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def call(*_args: Any, **_kwargs: Any) -> None:
            if not in_flight.is_set():
                in_flight.set()
                await release.wait()

        watchdog._hass.services.async_call.side_effect = call
        sync = asyncio.create_task(watchdog.async_sync_vanished({*TEST4, (48, "Test5")}))
        await in_flight.wait()
        await watchdog.async_push_restored(48, "Test5")
        release.set()
        await sync

    asyncio.run(run())
    texts = plan_watchdog.PUSH_TEXTS["de"]
    assert [(data["data"]["tag"], data["title"]) for _, _, data in _pushes(watchdog)] == [
        (watchdog._push_tag(47, TOKEN), texts["vanished_title"]),
        (watchdog._push_tag(48, test5), texts["restored_title"]),
    ]
    assert test5 not in watchdog._vanished_push_unsent
    # Back again: the next sync keeps the "restored" push (no clear), and sends no alarm for it.
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert len(_pushes(watchdog)) == 2
    assert watchdog._restored_pushes == set()


def test_a_partial_restore_says_so_on_the_phone(ir: MagicMock) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_push_restored(47, "Test4", complete=False))
    assert "nicht vollständig" in _pushes(watchdog)[-1][2]["message"]
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    assert "bleibt überwacht" in _pushes(watchdog)[-1][2]["message"]


def test_a_restored_push_after_the_check_cleared_the_plan_leaves_no_stale_entry(ir: MagicMock) -> None:
    """(Review) A check clearing the plan before the "restored" push: a later loss still clears as usual."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_sync_vanished(set()))
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_sync_vanished(set()))
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


def test_vanished_and_stopped_repairs_do_not_clean_up_each_other(ir: MagicMock) -> None:
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_check({34: "HA - Marker 1"}, _states(p34=False)))
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_check({34: "HA - Marker 1"}, _states(p34=False)))
    issues = ir.async_get.return_value.issues
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34)) in issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")) in issues


@pytest.mark.parametrize(
    ("action", "parsed"),
    [
        ("COMEXIO_START_PLAN_cx1_47_0123456789abcdef", (ACTION_START, 47, "47_0123456789abcdef")),
        ("COMEXIO_START_PLAN_cx1_47", (ACTION_START, 47, None)),  # sent before it carried the name digest
        ("COMEXIO_RESTORE_PLAN_cx1_47_0123456789abcdef", (ACTION_RESTORE, 47, "47_0123456789abcdef")),
        ("COMEXIO_UNWATCH_PLAN_cx1_47_0123456789abcdef", (ACTION_UNWATCH, 47, "47_0123456789abcdef")),
        ("COMEXIO_RESTORE_PLAN_cx1_47", None),  # a deleted plan's action carries its name digest
        ("COMEXIO_RESTORE_PLAN_cx1_2_47_0123456789abcdef", None),  # another server's ("cx1_2")
        ("COMEXIO_START_PLAN_cx1_2_47_0123456789abcdef", None),
        ("COMEXIO_RESTORE_PLAN_cx1_x", None),
        (None, None),
    ],
)
def test_parse_plan_action(action: object, parsed: tuple[str, int, str | None] | None) -> None:
    assert plan_watchdog.parse_plan_action(SERVER_ID, action) == parsed
    assert parse_start_action(SERVER_ID, action) == (parsed[1] if parsed and parsed[0] == ACTION_START else None)


def test_a_reused_id_stopped_under_another_name_gets_its_own_alarm(ir: MagicMock) -> None:
    """(Sourcery, #185) the old plan's push names it, so its Start action starts nothing for the new one."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert len(_pushes(watchdog)) == 1  # still stopped: no second alarm
    asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=False)))
    (*_, (_d, _s, alarm)) = _pushes(watchdog)
    assert len(_pushes(watchdog)) == 2
    assert alarm["data"]["tag"] == watchdog._push_tag(47)
    assert "Other" in alarm["message"]
    assert alarm["data"]["actions"][0]["action"] == start_action_id(SERVER_ID, 47, "Other")


def test_an_alarm_one_notify_target_failed_to_take_is_sent_again_to_it_only(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery, #185) Another target taking it is no delivery for the failed one's phone."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "mobile_app_tablet"])

    async def tablet_down(_domain: str, service: str, *_args: Any, **_kwargs: Any) -> None:
        if service == "mobile_app_tablet":
            raise HomeAssistantError("down")

    watchdog._hass.services.async_call.side_effect = tablet_down
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert watchdog._stopped_push_unsent[47][1] == {"mobile_app_tablet"}
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))  # retried in vain
    assert caplog.text == ""  # its failure was logged already
    assert [service for _d, service, _s in _pushes(watchdog)] == ["mobile_app_phone", "mobile_app_tablet"] + [
        "mobile_app_tablet"
    ]
    watchdog._hass.services.async_call.side_effect = None
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    (*_, (_d, service, alarm)) = _pushes(watchdog)
    assert service == "mobile_app_tablet"
    assert alarm["data"]["actions"][0]["action"] == start_action_id(SERVER_ID, 47, "Test4")
    assert watchdog._stopped_push_unsent == {}
    # A push the target took since replaces the alarm waiting there: no retry left for it.
    watchdog._hass.services.async_call.side_effect = tablet_down
    asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=False)))
    assert set(watchdog._stopped_push_unsent) == {47}
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_push_start_failed(47, "Other"))
    assert watchdog._stopped_push_unsent == {}


def test_a_fresh_alarm_goes_to_every_target_while_a_retry_waits(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """(local review) The retry's narrowed targets must not win over a new alarm for the ID; its failure warns."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "mobile_app_tablet"])

    async def tablet_down(_domain: str, service: str, *_args: Any, **_kwargs: Any) -> None:
        if service == "mobile_app_tablet":
            raise HomeAssistantError("down")

    watchdog._hass.services.async_call.side_effect = tablet_down
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC  # the retry is due
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=False)))  # another plan holds the ID now
    (_d1, phone, alarm), (_d2, tablet, _a) = _pushes(watchdog)[2:]
    assert (phone, tablet) == ("mobile_app_phone", "mobile_app_tablet")  # sent once, to both
    assert "Other" in alarm["message"]
    assert "notify.mobile_app_tablet failed" in caplog.text
    assert watchdog._stopped_push_unsent[47] == (clock[0], {"mobile_app_tablet"})


def test_a_retry_to_a_target_no_longer_configured_is_dropped(ir: MagicMock, clock: list[float]) -> None:
    """(local review) Else the entry stays and is "retried" at every check, reaching no one."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._stopped_push_unsent[47] = (clock[0], frozenset({"mobile_app_tablet"}))  # removed from the options
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))  # the alarm, to the phone
    assert set(watchdog._stopped_push_unsent) == {47}
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert len(_pushes(watchdog)) == 1
    assert watchdog._stopped_push_unsent == {}


def _alarms(watchdog: ManagedPlanWatchdog) -> list[dict[str, Any]]:
    return [data for _d, _s, data in _pushes(watchdog) if data["message"] != "clear_notification"]


def test_an_ignored_stopped_repair_stays_hidden_without_alarm_until_the_plan_ran_again(
    ir: MagicMock, clock: list[float]
) -> None:
    """(cs) Ignore: repair and push go, the plan stays watched; its next stop after a run alarms again."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert len(_alarms(watchdog)) == 1
    asyncio.run(watchdog.async_ignore_stopped(34))
    assert ir.async_get.return_value.issues[issue].dismissed_version is not None
    tag = f"{DOMAIN}_{SERVER_ID}_plan_34"
    assert _pushes(watchdog)[-1][2] == {"message": "clear_notification", "data": {"tag": tag}}
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert len(_alarms(watchdog)) == 1  # still stopped: no new alarm
    assert ir.async_get.return_value.issues[issue].dismissed_version is not None  # and the repair stays ignored
    assert watchdog.stopped == {34: "HA - Marker 1"}  # the problem sensor still sees it
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert issue not in ir.async_get.return_value.issues  # ran again: repair and ignore gone
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert ir.async_get.return_value.issues[issue].dismissed_version is None
    assert len(_alarms(watchdog)) == 2


def test_an_ignored_repair_sends_no_alarm_retry(ir: MagicMock, clock: list[float]) -> None:
    """An alarm a target failed to take is not sent again once its repair is ignored."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert 34 in watchdog._stopped_push_unsent
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_ignore_stopped(34))
    assert watchdog._stopped_push_unsent == {}
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    sent = len(_alarms(watchdog))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert len(_alarms(watchdog)) == sent


def test_an_ignore_read_back_after_a_restart_sends_no_alarm(ir: MagicMock) -> None:
    """HA keeps the ignore of a non-persistent issue across a restart, without its placeholders."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version="2026.10.0", translation_placeholders=None
    )
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert _alarms(watchdog) == []
    assert ir.async_get.return_value.issues[issue].dismissed_version is not None


def test_an_ignore_read_back_after_a_restart_does_not_hide_a_give_up(ir: MagicMock) -> None:
    """(Sourcery) The restored ignore names no repair kind: it cannot have been one of the give-up."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version="2026.10.0", translation_placeholders=None, translation_key=None
    )
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.suspended.add(34)
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    entry = ir.async_get.return_value.issues[issue]
    assert (entry.translation_key, entry.dismissed_version) == (ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED, None)


def test_an_ignore_read_back_after_a_restart_survives_an_unknown_run_state(ir: MagicMock) -> None:
    """(Sourcery) No previous verdict after a restart: an unreadable run state must not close the ignored repair."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version="2026.10.0", translation_placeholders=None, translation_key=None
    )
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=None, p42=True)))
    assert ir.async_get.return_value.issues[issue].dismissed_version == "2026.10.0"
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # still stopped: stays ignored
    assert ir.async_get.return_value.issues[issue].dismissed_version == "2026.10.0"
    assert _alarms(watchdog) == []
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))  # ran again: the ignore goes
    assert issue not in ir.async_get.return_value.issues


def test_a_check_reads_each_run_state_once(ir: MagicMock) -> None:
    """(Sourcery) Seen running, then unreadable within the same check: the ignore still goes as the plan ran."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version="2026.10.0", translation_placeholders=None, translation_key=None
    )
    reads: list[int] = []

    def run_state(fub_id: int) -> bool | None:
        reads.append(fub_id)
        return True if reads.count(fub_id) == 1 else None

    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, run_state))
    assert issue not in ir.async_get.return_value.issues
    assert sorted(reads) == sorted(MANAGED)


@pytest.mark.parametrize(("fub_id", "dismissed_version"), [(34, None), (99, "2026.10.0")])
def test_an_unknown_run_state_keeps_only_the_ignore_of_a_watched_plan(
    ir: MagicMock, fub_id: int, dismissed_version: str | None
) -> None:
    """A repair not ignored, or the ignored one of a plan no longer watched (99), still closes."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, fub_id))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version=dismissed_version, translation_placeholders=None, translation_key=None
    )
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=None, p42=True, p99=None)))
    assert issue not in ir.async_get.return_value.issues


def test_the_ignore_of_the_plan_that_held_the_id_before_does_not_hide_another(ir: MagicMock) -> None:
    """Renamed or the ID reused: the plan holding the ID now gets its own repair and alarm."""
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    ir.async_get.return_value.issues[issue] = SimpleNamespace(
        dismissed_version="2026.10.0", translation_placeholders={"plan_name": "Old"}
    )
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert ir.async_get.return_value.issues[issue].dismissed_version is None
    assert len(_alarms(watchdog)) == 1


def test_auto_start_giving_up_lifts_the_ignore(ir: MagicMock, clock: list[float]) -> None:
    """(cs) The ignored stop does not hide the more severe "Auto-Start gave up"; nor does it hold Auto-Start back."""
    watchdog, started = _watchdog([False, False], targets=["notify.mobile_app_phone"])
    watchdog.auto_restart = True
    issue = (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # refused: failure 1, repair
    assert started == [34]
    assert asyncio.run(watchdog.async_ignore_stopped(34)) is True
    alarms = len(_alarms(watchdog))
    clock[0] += FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # refused again: gives up
    assert started == [34, 34]
    assert watchdog.suspended == {34}
    entry = ir.async_get.return_value.issues[issue]
    assert (entry.translation_key, entry.dismissed_version) == (ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED, None)
    assert len(_alarms(watchdog)) == alarms + 1
    # Ignoring the "gave up" repair itself holds.
    asyncio.run(watchdog.async_ignore_stopped(34))
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert ir.async_get.return_value.issues[issue].dismissed_version is not None
    assert len(_alarms(watchdog)) == alarms + 1


def test_ignoring_a_repair_cleared_meanwhile_does_nothing(ir: MagicMock) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    assert asyncio.run(watchdog.async_ignore_stopped(34)) is False
    ir.async_ignore_issue.assert_not_called()
    assert _pushes(watchdog) == []


def test_a_reused_id_does_not_inherit_the_auto_start_state(ir: MagicMock) -> None:
    """(#185) Auto-Start gave up on "Test4": "Other", now holding its ID, gets its own attempts and alarm."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    watchdog.suspended.add(47)
    watchdog._failed_starts[47] = FUNCTION_PLAN_WATCHDOG_MAX_FAILED_STARTS
    watchdog._gave_up_at[47] = "12:00"
    # By name: _deferred_at is rebuilt by every check.
    states = ("_auto_started_at", "_refused_at", "_deferred_at")
    for state in states:
        getattr(watchdog, state)[47] = 0.0
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert 47 in watchdog.suspended  # same plan: kept
    assert all(47 in getattr(watchdog, state) for state in states)
    asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=False)))
    assert 47 not in watchdog.suspended
    assert 47 not in watchdog._failed_starts
    assert 47 not in watchdog._gave_up_at
    assert not any(47 in getattr(watchdog, state) for state in states)
    (*_, (_d, _s, alarm)) = _pushes(watchdog)
    assert alarm["title"] == plan_watchdog.PUSH_TEXTS["de"]["stopped_title"]


def test_an_undelivered_stopped_alarm_is_sent_again(ir: MagicMock, clock: list[float]) -> None:
    """(#185) Else the plan's last push stays, maybe one naming the plan that held the ID before."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert set(watchdog._stopped_push_unsent) == {47}
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert len(_pushes(watchdog)) == 1  # not due yet
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    (*_, (_d, _s, alarm)) = _pushes(watchdog)
    assert len(_pushes(watchdog)) == 2
    assert alarm["data"]["tag"] == watchdog._push_tag(47)
    assert watchdog._stopped_push_unsent == {}
    # One for a plan running again is dropped, not sent.
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    pushed = len(_pushes(watchdog))
    asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=False)))
    assert set(watchdog._stopped_push_unsent) == {47}
    asyncio.run(watchdog.async_check({47: "Other"}, _states(p47=True)))
    assert watchdog._stopped_push_unsent == {}
    (_d1, _s1, alarm), (_d2, _s2, cleared) = _pushes(watchdog)[pushed:]  # no retry of the alarm
    assert "Other" in alarm["message"]
    assert cleared["message"] == "clear_notification"


def test_an_undelivered_alarm_retried_in_vain_waits_again_and_keeps_its_kind(
    ir: MagicMock, clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """A suspended plan's alarm is sent again as "suspended"; a dead notify service gets one quiet try per interval."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.suspended.add(47)
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    with caplog.at_level(logging.WARNING):
        asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
        clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
        asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))  # retried in vain
    assert len(_pushes(watchdog)) == 2
    assert caplog.text.count("notify.mobile_app_phone") == 1  # the retry stays quiet
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert len(_pushes(watchdog)) == 2  # the interval starts again at the failed retry
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    (*_, (_d, _s, alarm)) = _pushes(watchdog)
    assert alarm["title"] == plan_watchdog.PUSH_TEXTS["de"]["suspended_title"]
    assert alarm["data"]["actions"][0]["action"] == start_action_id(SERVER_ID, 47, "Test4")


def test_a_stopped_id_holding_another_plan_with_an_unknown_state_is_not_reported_running(
    ir: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """(#185) The verdict was the old plan's: the log names the plan holding the ID now, not "runs again"."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    with caplog.at_level(logging.INFO):
        asyncio.run(watchdog.async_check({47: "Other"}, _states()))
    assert "its ID holds 'Other' now" in caplog.text
    assert "runs again" not in caplog.text
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


@pytest.mark.parametrize(
    ("previous", "stopped", "expected"),
    [
        ({}, {42: "B", 34: "A"}, [34, 42]),
        ({34: "A"}, {34: "A", 42: "B"}, [42]),
        ({34: "A"}, {34: "C"}, [34]),
        ({34: "A"}, {}, []),
    ],
)
def test_newly_stopped_plans(previous: dict[int, str], stopped: dict[int, str], expected: list[int]) -> None:
    assert plan_watchdog.newly_stopped_plans(previous, stopped) == expected


def test_a_stopped_plan_that_vanishes_swaps_its_push_for_the_alarm(ir: MagicMock) -> None:
    """Its "stopped" push (Start action) is cleared; the alarm has its own tag, so the clear cannot overtake it."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False), [47]))
    asyncio.run(watchdog.async_check({}, _states()))
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    (*_, (_d1, _s1, cleared), (_d2, _s2, alarm)) = _pushes(watchdog)
    assert cleared["message"] == "clear_notification"
    assert cleared["data"]["tag"] == watchdog._push_tag(47)
    assert alarm["data"]["tag"] == watchdog._push_tag(47, TOKEN) != watchdog._push_tag(47)


def test_two_deleted_plans_on_one_id_get_a_repair_and_push_each(ir: MagicMock) -> None:
    """(Sourcery) An ID can be reused: picks "47:Test4" and "47:Other" are two deleted plans, not one."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished({(47, "Test4"), (47, "Other")}))
    issues = ir.async_get.return_value.issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")) in issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Other")) in issues
    other = plan_watchdog.vanished_plan_token(47, "Other")
    assert {data["data"]["tag"] for *_, data in _pushes(watchdog)} == {
        watchdog._push_tag(47, TOKEN),
        watchdog._push_tag(47, other),
    }
    assert watchdog.vanished_name(other) == "Other"
    # One restored: only its repair goes, and its push is cleared, not the other's.
    asyncio.run(watchdog.async_sync_vanished({(47, "Other")}))
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")) not in issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Other")) in issues
    assert _pushes(watchdog)[-1][2]["data"]["tag"] == watchdog._push_tag(47, TOKEN)
    assert watchdog.vanished_name(TOKEN) is None


def test_a_server_does_not_clean_up_the_deleted_plan_repairs_of_another(ir: MagicMock) -> None:
    """Server "cx1" must not take "cx1_2"'s repairs ("..._cx1_2_47_<digest>") for its own."""
    other, _ = _watchdog(server_id="cx1_2")
    asyncio.run(other.async_sync_vanished(TEST4))
    watchdog, _ = _watchdog()
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_sync_vanished(set()))
    issues = ir.async_get.return_value.issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id("cx1_2", 47, "Test4")) in issues
    assert (DOMAIN, plan_watchdog.vanished_plan_issue_id(SERVER_ID, 47, "Test4")) not in issues


def test_dismissing_a_push_clears_the_tag_it_was_sent_with() -> None:
    """A deleted plan's push carries its own tag: clearing the plan's "stopped" tag would leave the alarm."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_dismiss_vanished_push(47, TOKEN))
    asyncio.run(watchdog.async_dismiss_push(47, "Test4"))
    asyncio.run(watchdog.async_dismiss_push(47))
    assert [(data["message"], data["data"]["tag"]) for *_, data in _pushes(watchdog)] == [
        ("clear_notification", watchdog._push_tag(47, TOKEN)),
        ("clear_notification", watchdog._push_tag(47, TOKEN)),
        ("clear_notification", watchdog._push_tag(47)),
    ]


def test_a_stale_push_is_not_cleared_once_the_ids_plan_stopped(ir: MagicMock) -> None:
    """(Sourcery, #185) A stop judged after the clear was asked for pushed the alarm under the same tag."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    pushes = len(_pushes(watchdog))
    asyncio.run(watchdog.async_dismiss_push(47))
    assert len(_pushes(watchdog)) == pushes
    asyncio.run(watchdog.async_dismiss_push(47, "Test4"))  # a deleted plan's push has a tag of its own
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


def _arrivals_while_the_first_push_is_held(
    watchdog: ManagedPlanWatchdog,
    first: Callable[[], Awaitable[Any]],
    then: Callable[[], Awaitable[Any]],
    meanwhile: Callable[[], None] = lambda: None,
) -> list[str]:
    """Runs first and holds its first push on its way; runs then, calls meanwhile, then lets the push arrive.

    Returns the pushes in the order they arrived.
    """
    arrived: list[str] = []

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def call(_domain: str, _service: str, data: dict[str, Any], **_kwargs: Any) -> None:
            if not in_flight.is_set():
                in_flight.set()
                await release.wait()
            arrived.append("clear" if data["message"] == "clear_notification" else "alarm")

        watchdog._hass.services.async_call.side_effect = call
        first_task = asyncio.create_task(first())
        await in_flight.wait()
        then_task = asyncio.create_task(then())
        for _ in range(10):  # then runs up to its own push
            await asyncio.sleep(0)
        meanwhile()
        release.set()
        await asyncio.gather(first_task, then_task)

    asyncio.run(run())
    return arrived


def test_a_stop_judged_while_a_stale_push_is_cleared_sends_its_alarm_after_the_clear(ir: MagicMock) -> None:
    """(Sourcery, #187) The clear passed the "not stopped" check; the stop's alarm (same tag) must not arrive first."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_dismiss_push(47),
        lambda: watchdog.async_check({47: "Test4"}, _states(p47=False)),
    )
    assert arrived == ["clear", "alarm"]
    _clear, (_d, service, alarm) = _pushes(watchdog)
    assert service == "mobile_app_phone"
    assert alarm["data"]["tag"] == watchdog._push_tag(47)
    assert alarm["data"]["actions"][0]["action"] == start_action_id(SERVER_ID, 47, "Test4")
    assert alarm["data"]["ttl"] == 0
    assert alarm["data"]["priority"] == "high"
    assert alarm["data"]["push"] == {"interruption-level": "time-sensitive"}


def test_a_plan_started_while_its_alarm_is_sent_has_the_alarm_cleared_after_it(ir: MagicMock) -> None:
    """(Sourcery, #187) Else the clear arrives first and the running plan's alarm stays on the phone."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_check({47: "Test4"}, _states(p47=False)),
        lambda: watchdog.async_plan_started(47),
    )
    assert arrived == ["alarm", "clear"]


def test_an_outdated_replacement_a_target_failed_to_take_is_sent_again(ir: MagicMock, clock: list[float]) -> None:
    """(Sourcery, #185) Else that phone keeps the old push, whose button starts nothing."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "mobile_app_tablet"])
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))

    async def tablet_down(_domain: str, service: str, *_args: Any, **_kwargs: Any) -> None:
        if service == "mobile_app_tablet":
            raise HomeAssistantError("down")

    watchdog._hass.services.async_call.side_effect = tablet_down
    asyncio.run(watchdog.async_push_start_outdated(47, "Test4"))
    assert watchdog._stopped_push_unsent[47][1] == {"mobile_app_tablet"}
    watchdog._hass.services.async_call.side_effect = None
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    (*_, (_d, service, alarm)) = _pushes(watchdog)
    assert service == "mobile_app_tablet"
    assert alarm["data"]["actions"][0]["action"] == start_action_id(SERVER_ID, 47, "Test4")
    assert watchdog._stopped_push_unsent == {}


def test_the_push_state_of_two_deleted_plans_on_one_id_stays_apart(ir: MagicMock, clock: list[float]) -> None:
    """The "restored" confirmation and the unsent alarm are kept per deleted plan, not per ID."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    other = plan_watchdog.vanished_plan_token(47, "Other")
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished({(47, "Test4"), (47, "Other")}))
    assert set(watchdog._vanished_push_unsent) == {TOKEN, other}
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    assert set(watchdog._vanished_push_unsent) == {other}  # the confirmation replaced Test4's alarm only
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished({(47, "Other")}))
    tags = [(data["message"], data["data"]["tag"]) for *_, data in _pushes(watchdog)]
    assert ("clear_notification", watchdog._push_tag(47, TOKEN)) not in tags  # its confirmation stays
    assert tags[-1][1] == watchdog._push_tag(47, other)  # the other's alarm is sent again
    assert watchdog._vanished_push_unsent == {}
    assert watchdog._restored_pushes == set()


def test_a_deleted_plan_without_a_backup_identity_is_not_offered_restore(ir: MagicMock) -> None:
    """(Sourcery) A bare-ID pick whose backups name several plans: Restore could not pick one."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    unnamed = plan_watchdog.unnamed_plan_label(47)
    asyncio.run(watchdog.async_sync_vanished({(47, unnamed)}))
    ((_domain, _service, data),) = _pushes(watchdog)
    token = plan_watchdog.vanished_plan_token(47, unnamed)
    assert [action["action"] for action in data["data"]["actions"]] == [
        plan_action_id(ACTION_UNWATCH, SERVER_ID, token)
    ]


@pytest.mark.parametrize(
    ("method", "args", "actions"),
    [
        ("async_push_restored", (47, "Test4"), []),
        ("async_push_restore_failed", (47, "Test4"), [ACTION_RESTORE, ACTION_UNWATCH]),
        ("async_push_check_pending", (47, TOKEN), [ACTION_RESTORE, ACTION_UNWATCH]),
        ("async_push_no_backup", (47, "Test4"), [ACTION_UNWATCH]),
    ],
)
def test_vanished_plan_follow_up_pushes(method: str, args: tuple, actions: list[str]) -> None:
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(getattr(watchdog, method)(*args))
    ((_domain, _service, data),) = _pushes(watchdog)
    assert [action["action"] for action in data["data"].get("actions", [])] == [
        plan_action_id(kind, SERVER_ID, TOKEN) for kind in actions
    ]
    assert "{" not in data["message"]
    assert "{" not in data["title"]
    assert data["data"]["tag"] == watchdog._push_tag(47, TOKEN)  # replaces the plan's alarm push


def test_push_texts_have_the_same_keys_in_every_language() -> None:
    keys = {lang: set(texts) for lang, texts in plan_watchdog.PUSH_TEXTS.items()}
    assert all(lang_keys == keys["en"] for lang_keys in keys.values())


def test_a_stale_push_clear_waiting_for_the_tag_is_not_sent_once_the_plan_stopped(ir: MagicMock) -> None:
    """The "not stopped" check is judged under the tag's lock: the plan stopped while this clear waited for it."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_dismiss_push(47),
        lambda: watchdog.async_dismiss_push(47),
        meanwhile=lambda: setattr(watchdog, "stopped", {47: "Test4"}),
    )
    assert arrived == ["clear"]


def test_an_alarm_decided_before_its_plan_was_started_is_not_sent(ir: MagicMock) -> None:
    """(#187) Test3's alarm holds the check while Test4 is started: Test4's alarm, decided before, is dropped."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_check({46: "Test3", 47: "Test4"}, _states(p46=False, p47=False)),
        lambda: watchdog.async_plan_started(47),
    )
    assert arrived == ["clear", "alarm"]
    assert [data["data"]["tag"] for *_, data in _pushes(watchdog) if data["message"] != "clear_notification"] == [
        watchdog._push_tag(46)
    ]


@pytest.mark.parametrize("push", ["async_push_start_busy", "async_push_start_failed"])
def test_a_start_reply_waiting_while_another_phone_started_the_plan_is_not_sent(ir: MagicMock, push: str) -> None:
    """(Sourcery, #187) Phone B's start pushes "runs again"; phone A's busy/failed reply would bring the button back."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {47: "Test4"}
    _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_plan_started(47, confirm=True),
        lambda: getattr(watchdog, push)(47, "Test4"),
    )
    assert [data["message"] for *_, data in _pushes(watchdog)] == [
        plan_watchdog.PUSH_TEXTS["de"]["started"].format(name="Test4", fub_id=47)
    ]
    assert watchdog._push_locks == {}


def test_an_outdated_replacement_waiting_while_the_id_holds_another_plan_is_not_sent(ir: MagicMock) -> None:
    """(Sourcery, #187) The replacement names the plan the action was judged for; Other's alarm names its own."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {47: "Test4"}

    def id_reassigned() -> None:
        watchdog.stopped = {47: "Other"}

    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog._async_clear_push(47),
        lambda: watchdog.async_push_start_outdated(47, "Test4"),
        id_reassigned,
    )
    assert arrived == ["clear"]
    assert watchdog._stopped_push_unsent == {}


def test_a_push_tag_lock_is_kept_while_used_and_dropped_after(ir: MagicMock) -> None:
    """(Sourcery, #187) Deleted plans' tags are many: their locks must not pile up for the coordinator's lifetime."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    users: list[int] = []
    _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_dismiss_push(47, "Test4"),
        lambda: watchdog.async_dismiss_push(47, "Test4"),
        lambda: users.append(watchdog._push_locks[watchdog._push_tag(47, vanished_plan_token(47, "Test4"))][1]),
    )
    assert users == [2]
    assert watchdog._push_locks == {}


@pytest.mark.parametrize("stopped", [None, {}, {47: "Other"}], ids=["before-the-first-check", "running", "id-reused"])
@pytest.mark.parametrize("push", ["async_push_start_busy", "async_push_start_failed"])
def test_a_start_reply_for_a_plan_not_judged_stopped_is_not_sent(
    ir: MagicMock, caplog: pytest.LogCaptureFixture, push: str, stopped: dict[int, str] | None
) -> None:
    """(#187) No button for a plan not (or no longer) the ID's stopped one; the check that judges it pushes one."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = stopped
    with caplog.at_level(logging.INFO):
        asyncio.run(getattr(watchdog, push)(47, "Test4"))
    watchdog._hass.services.async_call.assert_not_called()
    assert "function plan 47 (Test4) not sent" in caplog.text
    assert watchdog._push_locks == {}


def test_a_send_cancelled_while_waiting_for_its_tag_leaves_the_lock_usable(ir: MagicMock) -> None:
    """(local review, #187) The waiter's count is dropped on cancel; the holder's send and later ones go out."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    tag = watchdog._push_tag(47)

    async def run() -> list[int]:
        release = asyncio.Event()

        async def call(*_args: Any, **_kwargs: Any) -> None:
            await release.wait()

        watchdog._hass.services.async_call.side_effect = call
        holder = asyncio.create_task(watchdog.async_dismiss_push(47))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(watchdog.async_dismiss_push(47))
        await asyncio.sleep(0)
        users = [watchdog._push_locks[tag][1]]
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        users.append(watchdog._push_locks[tag][1])
        release.set()
        await holder
        users.append(len(watchdog._push_locks))
        await watchdog.async_dismiss_push(47)
        return users

    assert asyncio.run(run()) == [2, 1, 0]
    assert len(_pushes(watchdog)) == 2
    assert watchdog._push_locks == {}


@pytest.fixture(autouse=True)
def _no_push_lock_left() -> Iterator[None]:
    """The tag locks are module-wide: one left over would hold the next test's pushes (another event loop)."""
    yield
    left = dict(plan_watchdog._PUSH_LOCKS)
    plan_watchdog._PUSH_LOCKS.clear()
    assert left == {}


@pytest.mark.parametrize("confirm", [True, False], ids=["runs-again-push", "push-cleared"])
def test_a_plan_started_while_auto_start_awaits_comexio_is_not_written_back_as_stopped(
    ir: MagicMock, confirm: bool
) -> None:
    """(Sourcery, #187) The check judged it stopped before: no repair, no alarm, no second start, no second clear,
    none of the "runs again" push."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=False)))  # both stopped, Auto-Start off
    watchdog._hass.services.async_call.reset_mock()
    watchdog.auto_restart = True
    started: list[int] = []

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def start(fub_id: int) -> bool:
            started.append(fub_id)
            in_flight.set()
            await release.wait()
            return True

        watchdog._start_plan = start
        check = asyncio.create_task(watchdog.async_check(MANAGED, _states(p34=False, p42=False)))
        await in_flight.wait()
        await watchdog.async_plan_started(42, confirm=confirm)
        release.set()
        await check

    asyncio.run(run())
    assert started == [34]
    assert watchdog.stopped == {}
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 42)) not in ir.async_get.return_value.issues
    texts = plan_watchdog.PUSH_TEXTS["de"]
    started_text = texts["started"].format(name="HA - TRIGGER", fub_id=42, attempts=3)
    expected_42 = started_text if confirm else "clear_notification"
    assert [(data["data"]["tag"], data["message"]) for _, _, data in _pushes(watchdog)] == [
        (watchdog._push_tag(42), expected_42),
        (watchdog._push_tag(34), texts["auto_started"].format(name="HA - Marker 1", fub_id=34, attempts=3)),
    ]

    # The next check judges the plan anew: stopped again, it gets its repair and alarm.
    watchdog.auto_restart = False
    watchdog._hass.services.async_call.reset_mock()
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=False)))
    assert watchdog.stopped == {42: "HA - TRIGGER"}
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 42)) in ir.async_get.return_value.issues
    assert [data["data"]["tag"] for _, _, data in _pushes(watchdog)] == [watchdog._push_tag(42)]


def test_a_plan_started_while_the_check_sends_an_alarm_keeps_its_runs_again_push(ir: MagicMock) -> None:
    """(local review, #187) The check's clear of a plan seen running is not sent after its "runs again" push."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {34: "HA - Marker 1"}

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def call(*_args: Any, **_kwargs: Any) -> None:
            if not in_flight.is_set():
                in_flight.set()
                await release.wait()

        watchdog._hass.services.async_call.side_effect = call
        # 34 runs again (started in Comexio), 42 stopped: 42's alarm is held on its way.
        check = asyncio.create_task(watchdog.async_check(MANAGED, _states(p34=True, p42=False)))
        await in_flight.wait()
        await watchdog.async_plan_started(34, confirm=True)
        release.set()
        await check

    asyncio.run(run())
    texts = plan_watchdog.PUSH_TEXTS["de"]
    assert [(data["data"]["tag"], data["title"]) for _, _, data in _pushes(watchdog)] == [
        (watchdog._push_tag(42), texts["stopped_title"]),
        (watchdog._push_tag(34), texts["started_title"]),
    ]


@pytest.mark.parametrize("result", [True, False, None], ids=["started", "refused", "not-answered"])
def test_a_plan_confirmed_running_while_its_own_auto_start_awaits_comexio_keeps_its_runs_again_push(
    ir: MagicMock, result: bool | None
) -> None:
    """(Sourcery, #187) Its Auto-Start answering afterwards sends no "auto-started" push over the confirmation,
    nor is its answer taken as Auto-Start's: no notification, no stopped-again window, no failed attempt."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    watchdog._hass.services.async_call.reset_mock()
    watchdog.auto_restart = True
    watchdog._deferred_at[34] = time.monotonic() - FUNCTION_PLAN_WATCHDOG_RETRY_MIN_INTERVAL_SEC  # retry due

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def start(_fub_id: int) -> bool | None:
            in_flight.set()
            await release.wait()
            return result

        watchdog._start_plan = start
        check = asyncio.create_task(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
        await in_flight.wait()
        await watchdog.async_plan_started(34, confirm=True)
        release.set()
        await check

    asyncio.run(run())
    assert watchdog.stopped == {}
    texts = plan_watchdog.PUSH_TEXTS["de"]
    assert [data["message"] for _, _, data in _pushes(watchdog)] == [
        texts["started"].format(name="HA - Marker 1", fub_id=34, attempts=3)
    ]
    plan_watchdog.persistent_notification.async_create.assert_not_called()
    assert watchdog._auto_started_at == {}
    assert watchdog._failed_starts == {}
    # No retry gap either: the plan runs.
    assert watchdog._refused_at == {}
    assert watchdog._deferred_at == {}


def test_a_plan_confirmed_running_while_the_check_sends_an_alarm_keeps_its_runs_again_push(ir: MagicMock) -> None:
    """(Sourcery, #187) Auto-started before, confirmed from its push while another plan's alarm is on its way: the
    check's "auto-started" push is not sent over the confirmation (same tag)."""
    watchdog, _ = _watchdog([True, False], targets=["notify.mobile_app_phone"])
    watchdog.auto_restart = True

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def call(*_args: Any, **_kwargs: Any) -> None:
            if not in_flight.is_set():
                in_flight.set()
                await release.wait()

        watchdog._hass.services.async_call.side_effect = call
        # 34 auto-started, 42's start refused: 42's alarm is held on its way.
        check = asyncio.create_task(watchdog.async_check(MANAGED, _states(p34=False, p42=False)))
        await in_flight.wait()
        await watchdog.async_plan_started(34, confirm=True)
        release.set()
        await check

    asyncio.run(run())
    texts = plan_watchdog.PUSH_TEXTS["de"]
    pushes = [(data["data"]["tag"], data["message"]) for _, _, data in _pushes(watchdog)]
    assert pushes[0] == (watchdog._push_tag(42), texts["stopped"].format(name="HA - TRIGGER", fub_id=42, attempts=2))
    # The confirmation names the plan by its ID: an auto-started plan is no longer among the stopped ones.
    assert pushes[1:] == [(watchdog._push_tag(34), texts["started"].format(name="34", fub_id=34, attempts=2))]


def test_a_started_plans_clear_waiting_while_a_check_judges_it_stopped_again_is_not_sent(ir: MagicMock) -> None:
    """(Sourcery, #187) The stop's alarm (same tag) is due; the clear would take it off the phone."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {}
    clear_push = watchdog._async_clear_push
    asked: list[bool] = []

    async def clear(fub_id: int, token: str | None = None, *, keep_stopped: bool = False) -> None:
        asked.append(keep_stopped)
        await clear_push(fub_id, token, keep_stopped=keep_stopped)

    watchdog._async_clear_push = clear
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_dismiss_push(47),
        lambda: watchdog.async_plan_started(47),
        lambda: setattr(watchdog, "stopped", {47: "Test4"}),
    )
    assert asked == [True, True]  # the start asked for its clear too: the guard dropped it
    assert arrived == ["clear"]


def test_a_stopped_alarm_waiting_for_its_tag_while_the_repair_is_ignored_is_not_sent(ir: MagicMock) -> None:
    """(#187 merged with #188) The ignore's clear went out first; the alarm queued before it would undo it."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.stopped = {42: "HA - TRIGGER"}
    watchdog._sync_issues(watchdog.stopped)
    issue = stopped_plan_issue_id(SERVER_ID, 42)
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_push_start_failed(42, "HA - TRIGGER"),
        lambda: watchdog._async_push_stopped(42, "HA - TRIGGER", None),
        lambda: ir.async_ignore_issue(watchdog._hass, DOMAIN, issue, True),
    )
    assert arrived == ["alarm"]
    failed_text = plan_watchdog.PUSH_TEXTS["de"]["failed"].format(name="HA - TRIGGER", fub_id=42, attempts=3)
    assert [data["message"] for _, _, data in _pushes(watchdog)] == [failed_text]  # the start-failed push only


def test_a_reloaded_watchdog_waits_for_the_old_ones_push_under_the_same_tag(ir: MagicMock) -> None:
    """(Sourcery, #187) The old watchdog's clear still on its way must not arrive after the new one's alarm."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new._hass = old._hass
    new.stopped = {47: "Test4"}
    arrived = _arrivals_while_the_first_push_is_held(
        old, lambda: old.async_dismiss_push(47), lambda: new._async_push_stopped(47, "Test4", None)
    )
    assert arrived == ["clear", "alarm"]
    assert old._push_locks == {}


def test_a_closed_watchdogs_clear_queued_behind_the_reloaded_ones_alarm_is_not_sent(ir: MagicMock) -> None:
    """(Sourcery, #187) The old watchdog's check still runs after the unload; its send on the way arrives first,
    what it queues afterwards is dropped, so the new one's alarm stays on the phone."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new._hass = old._hass
    new.stopped = {47: "Test4"}

    async def then() -> None:
        alarm = asyncio.create_task(new._async_push_stopped(47, "Test4", None))
        await asyncio.sleep(0)  # queued first
        await asyncio.gather(alarm, old._async_clear_push(47), old.async_push_start_busy(47, "Test4"))

    def unload() -> None:
        old.stopped = {47: "Test4"}  # else its busy reply is dropped as not stopped, not as closed
        old.close()

    arrived = _arrivals_while_the_first_push_is_held(old, lambda: old.async_dismiss_push(47), then, unload)
    assert arrived == ["clear", "alarm"]


def test_a_check_resuming_after_the_unload_leaves_the_reloaded_watchdogs_repairs(ir: MagicMock) -> None:
    """(Sourcery, #187) The old check's snapshot predates the reload: it neither deletes the repairs the new
    watchdog raised meanwhile nor starts or pushes anything more."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new._hass = old._hass
    asyncio.run(old.async_check(MANAGED, _states(p34=False, p42=False)))  # both stopped, Auto-Start off
    old._hass.services.async_call.reset_mock()
    old.auto_restart = True
    started: list[int] = []

    async def run() -> None:
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def start(fub_id: int) -> bool:
            started.append(fub_id)
            in_flight.set()
            await release.wait()
            return True

        old._start_plan = start
        check = asyncio.create_task(old.async_check(MANAGED, _states(p34=False, p42=False)))
        await in_flight.wait()
        old.close()
        await new.async_check(MANAGED, _states(p34=False, p42=True))
        release.set()
        assert await check is False

    asyncio.run(run())
    assert started == [34]  # 42 is not started after the unload
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 34)) in ir.async_get.return_value.issues
    assert (DOMAIN, stopped_plan_issue_id(SERVER_ID, 42)) not in ir.async_get.return_value.issues  # seen running
    # The new one's alarm, and the clear of the old one's alarm for 42 (delivered before, 42 runs now).
    assert [(data["data"]["tag"], data["message"]) for _, _, data in _pushes(new)][1:] == [
        (new._push_tag(42), "clear_notification")
    ]
    assert [data["data"]["tag"] for _, _, data in _pushes(new)][0] == new._push_tag(34)


@pytest.mark.parametrize("still_stopped", [True, False])
def test_an_alarm_on_its_way_at_the_reloaded_watchdogs_first_check_is_replaced_or_cleared(
    ir: MagicMock, still_stopped: bool
) -> None:
    """(Sourcery, #187) The new watchdog has no verdict of its own to clear: for a plan it sees running it takes the
    old alarm off after it arrived, for one still stopped its own alarm replaces it."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new._hass = old._hass
    old.stopped = {42: "HA - TRIGGER"}
    arrived = _arrivals_while_the_first_push_is_held(
        old,
        lambda: old._async_push_stopped(42, "HA - TRIGGER", None),
        lambda: new.async_check(MANAGED, _states(p34=True, p42=not still_stopped)),
        old.close,
    )
    assert arrived == (["alarm", "alarm"] if still_stopped else ["alarm", "clear"])
    assert {data["data"]["tag"] for _, _, data in _pushes(old)} == {old._push_tag(42)}
    assert plan_watchdog._ACTIONABLE_TAGS == ({old._push_tag(42)} if still_stopped else set())  # noqa: SIM300


def test_an_alarm_arrived_after_the_unload_is_cleared_once_its_plan_is_seen_running(ir: MagicMock) -> None:
    """(Sourcery, #187) Arrived before the new watchdog judged: its checks clear it once, a later check sends nothing;
    a plan with an unknown state keeps it until it is seen running."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new._hass = old._hass
    old.stopped = {42: "HA - TRIGGER"}
    arrived = _arrivals_while_the_first_push_is_held(
        old, lambda: old._async_push_stopped(42, "HA - TRIGGER", None), lambda: asyncio.sleep(0), old.close
    )
    assert arrived == ["alarm"]
    old._hass.services.async_call.reset_mock()
    old._hass.services.async_call.side_effect = None
    asyncio.run(new.async_check(MANAGED, _states(p34=True, p42=None)))
    assert _pushes(new) == []
    asyncio.run(new.async_check(MANAGED, _states(p34=True, p42=True)))
    assert [(data["message"], data["data"]["tag"]) for _, _, data in _pushes(new)] == [
        ("clear_notification", new._push_tag(42))
    ]
    asyncio.run(new.async_check(MANAGED, _states(p34=True, p42=True)))
    assert len(_pushes(new)) == 1


def test_a_closed_watchdog_leaves_the_shared_repairs_to_the_reloaded_one(ir: MagicMock) -> None:
    """(local review, #187) A check or deleted-plan sync of the unloaded entry would apply a snapshot older than
    the new watchdog's repairs."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(new.async_check(MANAGED, _states(p34=False, p42=True)))
    asyncio.run(new.async_sync_vanished({(47, "Test4")}))
    issues = dict(ir.async_get.return_value.issues)
    old.close()
    assert asyncio.run(old.async_check(MANAGED, _states(p34=True, p42=False))) is False
    asyncio.run(old.async_sync_vanished(set()))
    assert ir.async_get.return_value.issues == issues
    assert old.stopped is None
    assert _pushes(old) == []


def test_a_confirmation_on_its_way_replaces_an_actionable_push_and_is_not_cleared(ir: MagicMock) -> None:
    """(local review, #187) The clear is judged under the tag's lock: started from the old alarm while a check
    begins, the "runs again" push sent meanwhile replaced it."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    plan_watchdog._ACTIONABLE_TAGS.add(watchdog._push_tag(42))
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog,
        lambda: watchdog.async_plan_started(42, confirm=True),
        lambda: watchdog.async_check(MANAGED, _states(p34=True, p42=True)),
    )
    assert arrived == ["alarm"]  # the "runs again" push only
    assert not plan_watchdog._ACTIONABLE_TAGS


def test_a_failed_clear_of_an_actionable_push_is_not_retried(ir: MagicMock) -> None:
    """As every clear: logged, not repeated with every check (pressing the stale action replaces it)."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    tag = watchdog._push_tag(42)
    plan_watchdog._ACTIONABLE_TAGS.add(tag)
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert [(data["message"], data["data"]["tag"]) for _, _, data in _pushes(watchdog)] == [("clear_notification", tag)]
    assert not plan_watchdog._ACTIONABLE_TAGS


def test_an_alarm_delivered_before_a_reload_is_cleared_once_the_reloaded_watchdog_sees_the_plan_run(
    ir: MagicMock,
) -> None:
    """(Sourcery, #187) Started before the old watchdog's next check: the new one has no stopped verdict to clear."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    new, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(old.async_check(MANAGED, _states(p34=True, p42=False)))  # alarm for 42
    old.close()
    asyncio.run(new.async_check(MANAGED, _states(p34=True, p42=True)))
    assert [(data["message"], data["data"]["tag"]) for _, _, data in _pushes(new)] == [
        ("clear_notification", new._push_tag(42))
    ]
    asyncio.run(new.async_check(MANAGED, _states(p34=True, p42=True)))
    assert len(_pushes(new)) == 1  # once


def test_an_undelivered_push_leaves_the_actionable_push_to_be_cleared(ir: MagicMock) -> None:
    """Delivered nowhere, it replaced nothing: the old push still shows."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    tag = watchdog._push_tag(42)
    plan_watchdog._ACTIONABLE_TAGS.add(tag)
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_plan_started(42, confirm=True))
    assert tag in plan_watchdog._ACTIONABLE_TAGS


def test_a_deleted_plans_push_arriving_after_the_unload_is_not_kept_actionable(ir: MagicMock) -> None:
    """No check judges a deleted plan's token running: its tag would stay in the set for good."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        old, lambda: old._async_push_vanished(47, "Test4", None), lambda: asyncio.sleep(0), old.close
    )
    assert arrived == ["alarm"]
    assert not plan_watchdog._ACTIONABLE_TAGS


def test_an_alarm_delivered_nowhere_after_the_unload_is_not_kept_actionable(ir: MagicMock) -> None:
    """Nothing to take off: a clear would remove the push shown before, e.g. a "runs again" confirmation."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    old.stopped = {42: "HA - TRIGGER"}

    async def call(*_args: Any, **_kwargs: Any) -> None:
        old.close()
        raise HomeAssistantError("down")

    old._hass.services.async_call.side_effect = call
    asyncio.run(old._async_push_stopped(42, "HA - TRIGGER", None))
    assert not plan_watchdog._ACTIONABLE_TAGS


def test_a_push_without_buttons_arriving_after_the_unload_is_left_on_the_phone(ir: MagicMock) -> None:
    """A confirmation offers nothing to press: it is not cleared."""
    old, _ = _watchdog(targets=["notify.mobile_app_phone"])
    arrived = _arrivals_while_the_first_push_is_held(
        old,
        lambda: old._async_push(42, "HA - TRIGGER", "started_title", "started"),
        lambda: asyncio.sleep(0),
        old.close,
    )
    assert arrived == ["alarm"]
    assert not plan_watchdog._ACTIONABLE_TAGS


@pytest.mark.parametrize(
    "reply",
    [
        lambda w: w.async_push_no_backup(47, "Test4"),
        lambda w: w.async_push_restore_failed(47, "Test4"),
        lambda w: w.async_push_check_pending(47, vanished_plan_token(47, "Test4")),
    ],
    ids=["no_backup", "restore_failed", "check_pending"],
)
def test_a_restore_reply_waiting_while_the_plan_was_restored_is_not_sent(
    ir: MagicMock, reply: Callable[[ManagedPlanWatchdog], Awaitable[None]]
) -> None:
    """(Sourcery, #187) Its Restore/Unwatch buttons would replace the "restored" push that went out first."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog.vanished = {(47, "Test4")}
    arrived = _arrivals_while_the_first_push_is_held(
        watchdog, lambda: watchdog.async_push_restored(47, "Test4"), lambda: reply(watchdog)
    )
    assert arrived == ["alarm"]  # the "restored" push only
    assert [data["title"] for _, _, data in _pushes(watchdog)] == [plan_watchdog.PUSH_TEXTS["de"]["restored_title"]]


def test_a_hanging_notify_service_counts_as_failed(ir: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Else it holds the tag's lock, and the plan's clears wait for it forever."""
    monkeypatch.setattr(plan_watchdog, "FUNCTION_PLAN_WATCHDOG_PUSH_TIMEOUT_SEC", 0.01)
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])

    async def hang(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(1)

    watchdog._hass.services.async_call.side_effect = hang
    asyncio.run(watchdog.async_check({47: "Test4"}, _states(p47=False)))
    assert watchdog._stopped_push_unsent[47][1] == {"notify.mobile_app_phone"}


def test_a_restored_push_that_failed_leaves_the_vanished_alarm_to_be_cleared(ir: MagicMock) -> None:
    """(#187 review) The phone it failed on keeps the alarm with its Restore button: the plan leaving vanished
    clears it as without a confirmation."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    watchdog._hass.services.async_call.side_effect = None
    assert _pushes(watchdog)[-1][2]["title"] == watchdog._texts()["restored_title"]  # the confirmation was tried
    assert watchdog._restored_pushes == set()
    asyncio.run(watchdog.async_sync_vanished(set()))
    (*_, (_domain, _service, clear)) = _pushes(watchdog)
    assert clear == {"message": "clear_notification", "data": {"tag": watchdog._push_tag(47, TOKEN)}}


def test_a_restored_push_failing_after_a_check_kept_it_clears_the_alarm_itself(ir: MagicMock) -> None:
    """(#187 review) A check dropping the plan from vanished while the confirmation was sent skipped the clear."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))

    async def check_meanwhile_then_fail(_domain: str, _service: str, data: dict[str, Any], **_kw: Any) -> None:
        if data["message"] != "clear_notification":
            await watchdog.async_sync_vanished(set())
            raise HomeAssistantError("down")

    watchdog._hass.services.async_call.side_effect = check_meanwhile_then_fail
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    (*_, (_domain, _service, clear)) = _pushes(watchdog)
    assert clear == {"message": "clear_notification", "data": {"tag": watchdog._push_tag(47, TOKEN)}}
    assert watchdog._restored_pushes == set()


def test_a_restore_while_a_check_retries_another_alarm_keeps_its_confirmation(
    ir: MagicMock, clock: list[float]
) -> None:
    """(#189 review) The check dropping the plan had not cleared it yet: the confirmation was not marked."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    restored: list[bool] = []

    async def other_down_then_restore(_domain: str, _service: str, data: dict[str, Any], **_kw: Any) -> None:
        if "Other" not in data["message"]:
            return
        if not clock[0] > 1000.0:
            raise HomeAssistantError("down")
        if not restored:  # the retry of the other plan's alarm is on its way
            restored.append(True)
            await watchdog.async_push_restored(47, "Test4")

    watchdog._hass.services.async_call.side_effect = other_down_then_restore
    asyncio.run(watchdog.async_sync_vanished(TEST4 | {(48, "Other")}))
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    asyncio.run(watchdog.async_sync_vanished({(48, "Other")}))
    assert restored == [True]
    (*_, (_domain, _service, last)) = _pushes(watchdog)
    assert last["title"] == watchdog._texts()["restored_title"]  # no clear after the confirmation
    assert watchdog._restored_pushes == set()
    assert watchdog._vanished_clearing == set()


def test_an_alarm_retry_after_the_restore_began_is_not_sent(ir: MagicMock, clock: list[float]) -> None:
    """(#189 review) It would replace the confirmation on the phone, its Restore button back."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert set(watchdog._vanished_push_unsent) == {TOKEN}
    clock[0] += FUNCTION_PLAN_WATCHDOG_PUSH_RETRY_SEC
    checked: list[bool] = []

    async def check_while_the_confirmation_is_sent(*_args: Any, **_kwargs: Any) -> None:
        if not checked:
            checked.append(True)
            await watchdog.async_sync_vanished(TEST4)  # Comexio does not list the restored plan yet

    watchdog._hass.services.async_call.side_effect = check_while_the_confirmation_is_sent
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    assert checked == [True]
    titles = [data["title"] for _d, _s, data in _pushes(watchdog)]
    texts = watchdog._texts()
    assert titles == [texts["vanished_title"], texts["restored_title"]]  # no alarm after the confirmation
    assert watchdog._vanished_push_unsent == {}


def test_a_sync_that_raised_drops_the_marks_of_the_plans_it_did_not_clear(ir: MagicMock) -> None:
    """(#189 local review) The mark would outlive the plan and hold back its next alarm."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4 | {(48, "Other")}))

    async def restore_then_cancel(_domain: str, _service: str, data: dict[str, Any], **_kw: Any) -> None:
        if "New" in data["message"]:
            await watchdog.async_push_restored(47, "Test4")
            raise asyncio.CancelledError

    watchdog._hass.services.async_call.side_effect = restore_then_cancel
    sync = watchdog.async_sync_vanished({(48, "Other"), (49, "New")})  # cancelled on the new alarm
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sync)
    assert watchdog._restored_pushes == set()
    assert watchdog._vanished_clearing == set()


def test_a_skipped_alarm_drops_its_retry(ir: MagicMock, clock: list[float]) -> None:
    """(#189 local review) Else it is due again at every check, never sent."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._vanished_push_unsent[TOKEN] = (clock[0], frozenset({"mobile_app_phone"}))
    watchdog._restored_pushes.add(TOKEN)
    asyncio.run(watchdog._async_push_vanished(47, "Test4", frozenset({"mobile_app_phone"})))
    assert _pushes(watchdog) == []
    assert watchdog._vanished_push_unsent == {}


def test_a_failed_confirmation_drops_the_alarm_retry_to_the_phone(ir: MagicMock, clock: list[float]) -> None:
    """(#189 local review) The phone that missed the alarm would get it after the plan is back."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    assert watchdog._vanished_push_unsent == {}


@pytest.mark.parametrize(
    ("tap_in_check", "failed_starts"),
    [(True, {34: 1}), (False, {})],
    ids=["tap_while_the_check_runs", "tap_after_the_check"],
)
def test_a_start_tap_keeps_the_stopped_again_window_of_the_auto_start_in_its_check(
    ir: MagicMock, tap_in_check: bool, failed_starts: dict[int, int]
) -> None:
    """(#187 review) A tap found the plan Auto-Start just started: that start keeps its stopped-again window.
    A tap later is the user's start: a stop after it is no failed Auto-Start."""
    watchdog, _ = _watchdog([True, True], targets=["notify.mobile_app_phone"])
    watchdog.auto_restart = True
    tapped: list[bool] = []

    async def tap_on_the_first_push(*_args: Any, **_kwargs: Any) -> None:
        if tap_in_check and not tapped:
            tapped.append(True)
            await watchdog.async_plan_started(34)

    watchdog._hass.services.async_call.side_effect = tap_on_the_first_push
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert tapped == ([True] if tap_in_check else [])
    if not tap_in_check:
        asyncio.run(watchdog.async_plan_started(34))
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert watchdog._failed_starts == failed_starts


def _failing_for(*services: str) -> Any:
    async def call(_domain: str, service: str, _data: dict[str, Any], **_kw: Any) -> None:
        if service in services:
            raise HomeAssistantError("down")

    return call


@pytest.mark.parametrize(
    ("failing", "cleared"),
    [("mobile_app_tablet", True), ("telegram", False)],
    ids=["a_phone_failed", "a_service_without_buttons_failed"],
)
def test_a_restored_push_failing_on_one_target_clears_only_for_a_phone(
    ir: MagicMock, failing: str, cleared: bool
) -> None:
    """(#187 review) Only a phone keeps the old alarm's Restore button; a clear reaches no other notify service."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone", "notify.mobile_app_tablet", "notify.telegram"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    watchdog._hass.services.async_call.side_effect = _failing_for(failing)
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    assert (TOKEN in watchdog._restored_pushes) is not cleared
    asyncio.run(watchdog.async_sync_vanished(set()))
    clears = [service for _d, service, data in _pushes(watchdog) if data["message"] == "clear_notification"]
    assert clears == (["mobile_app_phone", "mobile_app_tablet"] if cleared else [])


def test_a_failed_restored_push_after_the_check_cleared_the_plan_sends_no_clear(ir: MagicMock) -> None:
    """No longer vanished before the confirmation: the check cleared the alarm already, nothing is marked."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_push_restored(47, "Test4"))
    ((_domain, _service, confirmation),) = _pushes(watchdog)  # the confirmation's attempt only, no clear
    assert confirmation["data"]["tag"] == watchdog._push_tag(47, TOKEN)
    assert confirmation["message"] != "clear_notification"
    assert watchdog._restored_pushes == set()


def test_a_check_that_raised_forgets_the_plans_it_auto_started(ir: MagicMock) -> None:
    """A later tap is the user's start again, as after a check that returned."""
    watchdog, _ = _watchdog([True])
    watchdog.auto_restart = True

    def fail_after_the_auto_start(*_args: Any) -> None:
        assert watchdog._auto_started_in_check == {34}  # the check did auto-start it
        raise RuntimeError("registry")

    watchdog._sync_issues = MagicMock(side_effect=fail_after_the_auto_start)
    check = watchdog.async_check(MANAGED, _states(p34=False, p42=True))
    with pytest.raises(RuntimeError):
        asyncio.run(check)
    assert watchdog._auto_started_in_check == set()


async def _back_and_vanished_again(watchdog: ManagedPlanWatchdog) -> None:
    """Two checks while the "restored" push is sent: the plan is back, then vanished again."""
    await watchdog.async_sync_vanished(set())
    await watchdog.async_sync_vanished(TEST4)


async def _restored_then(watchdog: ManagedPlanWatchdog, meanwhile: list[asyncio.Task[None]]) -> None:
    """The "restored" push, then the checks it ran meanwhile (their alarm queued behind it under the tag's lock)."""
    await watchdog.async_push_restored(47, "Test4")
    await asyncio.gather(*meanwhile)


def test_a_failed_restored_push_leaves_the_alarm_of_a_plan_vanished_again_meanwhile(ir: MagicMock) -> None:
    """(Sourcery #189) Back and vanished again while the confirmation was sent: the same tag holds the new
    alarm, which keeps its Restore action."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))

    async def back_and_vanished_again_then_fail(_domain: str, _service: str, data: dict[str, Any], **_kw: Any) -> None:
        if data["title"] == watchdog._texts()["restored_title"]:
            meanwhile.append(asyncio.create_task(_back_and_vanished_again(watchdog)))
            for _ in range(5):
                await asyncio.sleep(0)  # its alarm waits for the tag's lock
            raise HomeAssistantError("down")

    meanwhile: list[asyncio.Task[None]] = []
    watchdog._hass.services.async_call.side_effect = back_and_vanished_again_then_fail
    asyncio.run(_restored_then(watchdog, meanwhile))
    assert len(meanwhile) == 1
    (*_, (_domain, _service, last)) = _pushes(watchdog)
    assert last["message"] != "clear_notification"
    assert last["data"]["actions"][0]["action"] == plan_action_id(ACTION_RESTORE, SERVER_ID, TOKEN)


def test_a_failed_confirmation_keeps_the_alarm_retry_of_a_plan_vanished_again_meanwhile(ir: MagicMock) -> None:
    """(Sourcery #189) The retry is the new episode's alarm: the phone that missed it would never get it."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    meanwhile: list[asyncio.Task[None]] = []

    async def back_and_vanished_again_all_failing(*_args: Any, **_kwargs: Any) -> None:
        if not meanwhile:
            meanwhile.append(asyncio.create_task(_back_and_vanished_again(watchdog)))  # its new alarm fails too
            for _ in range(5):
                await asyncio.sleep(0)  # its alarm waits for the tag's lock
        raise HomeAssistantError("down")

    watchdog._hass.services.async_call.side_effect = back_and_vanished_again_all_failing
    asyncio.run(_restored_then(watchdog, meanwhile))
    assert len(meanwhile) == 1
    assert set(watchdog._vanished_push_unsent) == {TOKEN}


def test_a_cancelled_confirmation_leaves_the_alarm_to_be_cleared(ir: MagicMock) -> None:
    """(Sourcery #189) The mark would make the check keep the alarm, its Restore button on the phone."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    watchdog._hass.services.async_call.side_effect = asyncio.CancelledError
    restore = watchdog.async_push_restored(47, "Test4")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restore)
    assert watchdog._restored_pushes == set()
    watchdog._hass.services.async_call.side_effect = None
    asyncio.run(watchdog.async_sync_vanished(set()))
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"


def test_a_confirmation_cancelled_after_the_check_kept_the_alarm_clears_it(ir: MagicMock) -> None:
    """(#189 local review) No later check would clear it: the plan is no longer vanished."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    asyncio.run(watchdog.async_sync_vanished(TEST4))

    async def back_then_cancelled(*_args: Any, **_kwargs: Any) -> None:
        watchdog._hass.services.async_call.side_effect = None
        await watchdog.async_sync_vanished(set())  # keeps the alarm for the confirmation
        raise asyncio.CancelledError

    watchdog._hass.services.async_call.side_effect = back_then_cancelled
    restore = watchdog.async_push_restored(47, "Test4")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restore)
    assert "clear_notification" not in [data["message"] for _d, _s, data in _pushes(watchdog)]
    (clear,), _ = watchdog._hass.async_create_background_task.call_args
    asyncio.run(clear)
    assert _pushes(watchdog)[-1][2]["message"] == "clear_notification"
    assert watchdog._restored_pushes == set()


def test_a_cancelled_confirmation_drops_the_alarm_retry_to_the_phone(ir: MagicMock) -> None:
    """(Sourcery #189) A check still listing the plan vanished would send the old alarm again."""
    watchdog, _ = _watchdog(targets=["notify.mobile_app_phone"])
    watchdog._hass.services.async_call.side_effect = HomeAssistantError("down")
    asyncio.run(watchdog.async_sync_vanished(TEST4))
    assert set(watchdog._vanished_push_unsent) == {TOKEN}
    watchdog._hass.services.async_call.side_effect = asyncio.CancelledError
    restore = watchdog.async_push_restored(47, "Test4")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(restore)
    assert watchdog._vanished_push_unsent == {}
