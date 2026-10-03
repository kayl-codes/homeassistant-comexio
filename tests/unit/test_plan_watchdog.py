"""Watchdog of the HA-managed function plans (plan_watchdog.py)."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio import api as api_module, plan_watchdog
from custom_components.comexio.const import DOMAIN, FUNCTION_PLAN_WATCHDOG_RESTOP_WINDOW_SEC
from custom_components.comexio.plan_watchdog import (
    ISSUE_FUNCTION_PLAN_AUTO_START_SUSPENDED,
    ISSUE_FUNCTION_PLAN_STOPPED,
    ISSUE_FUNCTION_PLAN_STOPPED_USER,
    ManagedPlanWatchdog,
    next_stopped_plans,
    parse_start_action,
    start_action_id,
    stopped_plan_issue_id,
)

SERVER_ID = "cx1"
MANAGED = {34: "HA - Marker 1", 42: "HA - TRIGGER"}


def _states(**by_id: bool | None) -> Any:
    """run_state callable from keyword args like p34=True."""
    return lambda fub_id: by_id.get(f"p{fub_id}")


@pytest.mark.parametrize(
    ("states", "previous", "expected"),
    [
        ({"p34": True, "p42": True}, set(), {}),
        ({"p34": False, "p42": True}, set(), {34: "HA - Marker 1"}),
        # Unknown (unreadable or just stopped by HA) keeps the previous verdict either way.
        ({"p34": None, "p42": True}, {34}, {34: "HA - Marker 1"}),
        ({"p34": None, "p42": True}, set(), {}),
        ({"p34": True, "p42": False}, {34}, {42: "HA - TRIGGER"}),
    ],
)
def test_next_stopped_plans(states: dict[str, bool | None], previous: set[int], expected: dict[int, str]) -> None:
    assert next_stopped_plans(MANAGED, _states(**states), previous) == expected


@pytest.fixture
def ir(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Issue registry stand-in that remembers created issues, so the clean-up sees them."""
    registry = MagicMock()
    issues: dict[tuple[str, str], Any] = {}
    registry.async_get.return_value.issues = issues
    registry.async_create_issue.side_effect = lambda _hass, domain, issue_id, **_kw: issues.update(
        {(domain, issue_id): True}
    )
    registry.async_delete_issue.side_effect = lambda _hass, domain, issue_id: issues.pop((domain, issue_id), None)
    monkeypatch.setattr(plan_watchdog, "ir", registry)
    monkeypatch.setattr(plan_watchdog, "persistent_notification", MagicMock())
    return registry


def _watchdog(
    start_results: list[bool | None] | None = None, targets: list[str] | None = None
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
        hass, entry_id="e1", server_id=SERVER_ID, start_plan=start, notify_targets=lambda: list(targets or [])
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
    assert started == []  # auto-start is off by default


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


def test_user_plan_auto_start_is_off_by_default() -> None:
    watchdog = ManagedPlanWatchdog(
        MagicMock(), entry_id="e1", server_id=SERVER_ID, start_plan=AsyncMock(), notify_targets=list
    )
    assert watchdog.auto_restart_user is False


def test_refused_auto_start_gives_up_after_two_attempts(ir: MagicMock) -> None:
    watchdog, started = _watchdog([False, False], targets=["mobile_app_phone"])
    watchdog.auto_restart = True
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert watchdog.suspended == set()
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED

    assert "gave_up_at" not in ir.async_create_issue.call_args.kwargs["data"]

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
    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))  # a fresh failure 1
    assert started == [34, 34]
    assert watchdog.suspended == set()


def test_blocked_or_unanswered_start_is_not_a_failed_attempt(ir: MagicMock) -> None:
    """None (sync/restore running, connection lost) says nothing about the plan: retried, never counted."""
    watchdog, started = _watchdog([None, None, None])
    watchdog.auto_restart = True
    for _ in range(3):
        asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert started == [34, 34, 34]
    assert watchdog.suspended == set()
    assert ir.async_create_issue.call_args.kwargs["translation_key"] == ISSUE_FUNCTION_PLAN_STOPPED


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
    assert alarm["data"]["actions"] == [{"action": start_action_id(SERVER_ID, 34), "title": "Plan starten"}]
    tag = alarm["data"]["tag"]

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=False, p42=True)))
    assert len(_pushes(watchdog)) == 2  # still stopped: no repeated alarm

    asyncio.run(watchdog.async_check(MANAGED, _states(p34=True, p42=True)))
    assert _pushes(watchdog)[2][2] == {"message": "clear_notification", "data": {"tag": tag}}


def test_failing_notify_service_does_not_stop_the_watchdog(ir: MagicMock) -> None:
    from homeassistant.exceptions import HomeAssistantError

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


def test_plan_running_past_the_window_resets_the_failure_count(ir: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(plan_watchdog, "time", SimpleNamespace(monotonic=lambda: now[0]))
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
        (start_action_id(SERVER_ID, 34), 34),
        (start_action_id(f"{SERVER_ID}_2", 34), None),  # another server's action
        ("SOME_OTHER_ACTION", None),
        (None, None),
    ],
)
def test_parse_start_action(action: Any, expected: int | None) -> None:
    assert parse_start_action(SERVER_ID, action) == expected
