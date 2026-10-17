"""Watchdog user-plan picks and the stopped-plan repair follow a plan's identity (ID and name), not its ID alone."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import repairs
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    DOMAIN,
    parse_watchdog_user_plan_pick,
    repointed_watchdog_user_plan_picks,
    watchdog_user_plan_pick,
)
from custom_components.comexio.options_flow import _watchdog_user_plan_field
from custom_components.comexio.repairs import ComexioRepairFlow


@pytest.mark.parametrize("name", ["Licht: EG", ":x", "a:b:c", "19"])
def test_a_pick_round_trips_names_with_colons(name: str) -> None:
    assert parse_watchdog_user_plan_pick(watchdog_user_plan_pick(19, name)) == (19, name)


@pytest.mark.parametrize(("value", "parsed"), [("19", (19, None)), ("x", None), ("x:y", None), (None, None)])
def test_bare_and_unreadable_picks(value: object, parsed: tuple | None) -> None:
    assert parse_watchdog_user_plan_pick(value) == parsed


@pytest.mark.parametrize(
    ("picks", "expected"),
    [
        # (cm) a restore-as-new moves the pick to the plan's new ID, so the plan stays watched
        (["47:Test4", "12:Other"], ["45:Test4", "12:Other"]),
        # a bare-ID pick saved before names were can only mean this plan
        (["47"], ["45:Test4"]),
        # the new ID already picked: no duplicate
        (["47:Test4", "45:Test4"], ["45:Test4"]),
        # same ID under another name (ID reused), other plans, or no picks: nothing moves
        (["47:Stranger"], None),
        (["12:Other"], None),
        ([], None),
        (None, None),
    ],
)
def test_a_restore_as_new_repoints_the_watchdog_pick(picks: object, expected: list[str] | None) -> None:
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: picks}
    assert repointed_watchdog_user_plan_picks(options, "Test4", 47, 45) == expected


def test_a_bare_pick_of_an_id_another_plan_holds_stays_with_that_plan() -> None:
    """(Sourcery) on_conflict new_id: the bare pick watches the live plan at the old ID, not the restored one."""
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47", "47:Test4"]}
    assert repointed_watchdog_user_plan_picks(options, "Test4", 47, 45, old_id_still_live=True) == ["47", "45:Test4"]
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47"]}
    assert repointed_watchdog_user_plan_picks(options, "Test4", 47, 45, old_id_still_live=True) is None


def test_user_plan_picks_carry_the_plan_name() -> None:
    """Review (Copilot, #132): a pick of only the ID would pass to the plan Comexio gives that ID next."""
    coordinator = SimpleNamespace(watchdog_user_plan_candidates=lambda: {43: "Licht", 50: "Rollo"})
    # "43": a bare ID saved before picks carried the name; "50:Alt": plan 50 renamed or its ID reused.
    # "43:Licht" next to "43": the upgraded bare ID must not double the pick.
    conf = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["43", "43:Licht", "50:Alt", "99:Gone", "98", "junk"]}
    saved, selector = _watchdog_user_plan_field(coordinator, conf)
    assert saved == ["43:Licht", "50:Alt", "99:Gone", "98", "junk"]
    assert [(o["value"], o["label"]) for o in selector.config["options"]] == [
        ("43:Licht", "Licht (ID 43)"),
        ("50:Rollo", "Rollo (ID 50)"),
        ("50:Alt", "Alt (ID 50)"),
        ("99:Gone", "Gone (ID 99)"),
        ("98", "ID 98"),
        ("junk", "junk"),
    ]


def _run_stopped_repair(
    *,
    watched: str | None,
    running: bool = False,
    blocked: bool = False,
    started: bool | None = True,
    user_input: dict | None = None,
    user_plan: bool = True,
    unwatched: bool = True,
    ignored: bool = True,
) -> tuple[dict, AsyncMock, SimpleNamespace]:
    start = AsyncMock(return_value=started)
    coordinator = SimpleNamespace(
        watches_plan_raised_for=lambda fub_id, name_matches: watched is not None and name_matches(watched),
        plan_watchdog=SimpleNamespace(
            last_check_text=lambda: "—",
            async_plan_started=AsyncMock(),
            async_ignore_stopped=AsyncMock(return_value=ignored),
        ),
        is_watched_user_plan=lambda fub_id: user_plan,
        api=SimpleNamespace(get_fub_active=MagicMock(return_value=running)),
        managed_plan_start_blocked=blocked,
        async_start_managed_plan=start,
        async_unwatch_user_plan=AsyncMock(return_value=unwatched),
        async_update_listeners=MagicMock(),
    )
    flow = ComexioRepairFlow("function_plan_stopped_iosrv1_19", {"entry_id": "e1", "fub_id": 19, "plan_name": "P"})
    flow.hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}}, config=SimpleNamespace(language="en"))  # type: ignore[assignment]
    with patch.object(repairs.ir, "async_delete_issue") as delete_issue:
        result = asyncio.run(flow.async_step_function_plan_stopped({} if user_input is None else user_input))
    coordinator.delete_issue = delete_issue
    return result, start, coordinator


def _stopped_repair_actions(*, user_plan: bool) -> list[str]:
    coordinator = SimpleNamespace(
        plan_watchdog=SimpleNamespace(last_check_text=lambda: "—"), is_watched_user_plan=lambda fub_id: user_plan
    )
    flow = ComexioRepairFlow("function_plan_stopped_iosrv1_19", {"entry_id": "e1", "fub_id": 19, "plan_name": "P"})
    flow.hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}}, config=SimpleNamespace(language="en"))  # type: ignore[assignment]
    result = asyncio.run(flow.async_step_function_plan_stopped())
    assert (result["type"], result["step_id"]) == ("form", "function_plan_stopped")
    ((field, selector),) = result["data_schema"].schema.items()
    assert field.default() == "start"
    assert selector.config["translation_key"] == "function_plan_stopped_action"
    return selector.config["options"]


@pytest.mark.parametrize(
    ("user_plan", "actions"),
    [(True, ["start", "ignore", "unwatch"]), (False, ["start", "ignore"])],
    ids=["user-plan", "ha-plan"],
)
def test_the_stopped_plan_repair_offers_stop_watching_for_user_plans_only(user_plan: bool, actions: list[str]) -> None:
    """(cs) HA's own plans are always watched: only a user plan can be taken off the list."""
    assert _stopped_repair_actions(user_plan=user_plan) == actions


def test_ignoring_the_stopped_plan_repair_keeps_the_issue() -> None:
    """(cs) An abort, not an entry: HA deletes the issue of a finished repair flow, and with it the ignore."""
    result, start, coordinator = _run_stopped_repair(watched="P", user_input={"action": "ignore"})
    assert (result["type"], result["reason"]) == ("abort", "ignored")
    coordinator.plan_watchdog.async_ignore_stopped.assert_awaited_once_with(19)
    start.assert_not_called()
    coordinator.delete_issue.assert_not_called()


def test_ignoring_a_plan_running_meanwhile_reports_it_running() -> None:
    result, _, coordinator = _run_stopped_repair(watched="P", running=True, user_input={"action": "ignore"})
    assert (result["type"], result["reason"]) == ("abort", "already_running")
    coordinator.plan_watchdog.async_ignore_stopped.assert_not_called()
    # Its repair cleared between the run-state read and the ignore: nothing was ignored.
    result, _, coordinator = _run_stopped_repair(watched="P", ignored=False, user_input={"action": "ignore"})
    assert (result["type"], result["reason"]) == ("abort", "already_running")
    coordinator.plan_watchdog.async_ignore_stopped.assert_awaited_once()


def test_a_stop_watching_that_kept_the_pick_keeps_the_repair() -> None:
    """(Review) A bare-ID pick of an ID another plan holds stays: no "No longer watched", the repair stays."""
    result, _, coordinator = _run_stopped_repair(watched="P", unwatched=False, user_input={"action": "unwatch"})
    assert (result["type"], result["reason"]) == ("abort", "still_watched")
    coordinator.delete_issue.assert_not_called()


@pytest.mark.parametrize("file_name", ["strings.json", "en.json", "de.json", "fr.json", "es.json"])
def test_every_stopped_repair_action_is_translated(file_name: str) -> None:
    path = Path(repairs.__file__).parent / "translations" / file_name
    options = json.loads(path.read_text(encoding="utf-8"))["selector"]["function_plan_stopped_action"]["options"]
    assert set(options) == set(_stopped_repair_actions(user_plan=True))
    assert all(isinstance(label, str) and label.strip() for label in options.values())


@pytest.mark.parametrize("running", [False, True], ids=["stopped", "running-meanwhile"])
def test_the_stopped_plan_repair_stops_watching_the_plan(running: bool) -> None:
    """(cs) Unwatched by the plan's ID and name, also once it runs again: the choice was to stop watching it."""
    result, start, coordinator = _run_stopped_repair(watched="P", running=running, user_input={"action": "unwatch"})
    assert result["type"] == "create_entry"
    coordinator.async_unwatch_user_plan.assert_awaited_once_with(19, "P")
    start.assert_not_called()
    coordinator.plan_watchdog.async_ignore_stopped.assert_not_called()


@pytest.mark.parametrize("action", ["ignore", "unwatch"])
def test_the_stopped_plan_repair_acts_only_on_a_watched_plan(action: str) -> None:
    result, _, coordinator = _run_stopped_repair(watched="Other", user_input={"action": action})
    assert (result["type"], result["reason"]) == ("abort", "plan_not_watched")
    coordinator.plan_watchdog.async_ignore_stopped.assert_not_called()
    coordinator.async_unwatch_user_plan.assert_not_called()


@pytest.mark.parametrize("running", [False, True], ids=["stopped", "running-meanwhile"])
@pytest.mark.parametrize("watched", [None, "Other"], ids=["not-watched", "id-reused-by-another-watched-plan"])
def test_the_stopped_plan_repair_starts_only_a_watched_plan(running: bool, watched: str | None) -> None:
    """Review (Copilot, #132): unselected, deleted or ID reused while the dialog was open — no start.

    Checked before the run state: a reused ID's running plan must not be confirmed as the old one.
    (Sourcery, #184) also when another watched plan holds the ID now: the repair's plan name must match.
    """
    result, start, coordinator = _run_stopped_repair(watched=watched, running=running)
    assert (result["type"], result["reason"]) == ("abort", "plan_not_watched")
    assert result["description_placeholders"]["fub_id"] == "19"
    assert result["description_placeholders"]["plan_name"] == "P"
    start.assert_not_called()
    coordinator.plan_watchdog.async_plan_started.assert_not_called()
    # Deleted right away, also while a sync keeps the watchdog's own clean-up waiting.
    assert coordinator.delete_issue.call_args.args[1:] == (DOMAIN, "function_plan_stopped_iosrv1_19")


def test_the_stopped_plan_repair_starts_a_watched_plan() -> None:
    result, start, _ = _run_stopped_repair(watched="P")
    assert result["type"] == "create_entry"
    start.assert_awaited_once_with(19)


@pytest.mark.parametrize(
    ("blocked", "started", "reason"),
    [(True, True, "busy"), (False, None, "busy"), (False, False, "start_failed")],
    ids=["blocked-before", "blocked-after-login", "refused"],
)
def test_a_stopped_plan_repair_that_sent_no_start_reports_busy(
    blocked: bool, started: bool | None, reason: str
) -> None:
    """(Review) a plan edit, sync or restore running means "try again", not "Comexio refused the start"."""
    result, start, coordinator = _run_stopped_repair(watched="P", blocked=blocked, started=started)
    assert (result["type"], result["reason"]) == ("abort", reason)
    assert start.await_count == (0 if blocked else 1)
    coordinator.delete_issue.assert_not_called()
