"""Watchdog user-plan picks and the stopped-plan repair follow a plan's identity (ID and name), not its ID alone."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import repairs
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    DOMAIN,
    parse_watchdog_user_plan_pick,
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


def _run_stopped_repair(*, watched: bool, running: bool = False) -> tuple[dict, AsyncMock, SimpleNamespace]:
    start = AsyncMock(return_value=True)
    coordinator = SimpleNamespace(
        is_watched_plan=lambda fub_id: watched,
        plan_watchdog=SimpleNamespace(last_check_text=lambda: "—", async_plan_started=AsyncMock()),
        api=SimpleNamespace(get_fub_active=MagicMock(return_value=running)),
        managed_plan_start_blocked=False,
        async_start_managed_plan=start,
    )
    flow = ComexioRepairFlow("function_plan_stopped_iosrv1_19", {"entry_id": "e1", "fub_id": 19, "plan_name": "P"})
    flow.hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}}, config=SimpleNamespace(language="en"))  # type: ignore[assignment]
    with patch.object(repairs.ir, "async_delete_issue") as delete_issue:
        result = asyncio.run(flow.async_step_function_plan_stopped({}))
    coordinator.delete_issue = delete_issue
    return result, start, coordinator


@pytest.mark.parametrize("running", [False, True], ids=["stopped", "running-meanwhile"])
def test_the_stopped_plan_repair_starts_only_a_watched_plan(running: bool) -> None:
    """Review (Copilot, #132): unselected, deleted or ID reused while the dialog was open — no start.

    Checked before the run state: a reused ID's running plan must not be confirmed as the old one.
    """
    result, start, coordinator = _run_stopped_repair(watched=False, running=running)
    assert (result["type"], result["reason"]) == ("abort", "plan_not_watched")
    assert result["description_placeholders"]["fub_id"] == "19"
    assert result["description_placeholders"]["plan_name"] == "P"
    start.assert_not_called()
    coordinator.plan_watchdog.async_plan_started.assert_not_called()
    # Deleted right away, also while a sync keeps the watchdog's own clean-up waiting.
    assert coordinator.delete_issue.call_args.args[1:] == (DOMAIN, "function_plan_stopped_iosrv1_19")


def test_the_stopped_plan_repair_starts_a_watched_plan() -> None:
    result, start, _ = _run_stopped_repair(watched=True)
    assert result["type"] == "create_entry"
    start.assert_awaited_once_with(19)
