"""coordinator.async_plan_transition: HA's own plan start/stop shows as in progress, then the run states are read."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio.const import PLAN_TRANSITION_STARTING, PLAN_TRANSITION_STOPPING
from custom_components.comexio.coordinator import ComexioCoordinator


def _coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator._plan_transitions = {}
    coordinator.async_update_listeners = MagicMock()
    coordinator._async_refresh_plan_run_states = AsyncMock()
    return coordinator


def test_the_plan_is_in_transition_only_during_the_action() -> None:
    coordinator = _coordinator()
    seen: list[str | None] = []

    async def run() -> None:
        async with coordinator.async_plan_transition(43, PLAN_TRANSITION_STOPPING):
            seen.append(coordinator.plan_transition(43))
            coordinator._async_refresh_plan_run_states.assert_not_called()

    asyncio.run(run())
    assert seen == [PLAN_TRANSITION_STOPPING]
    assert coordinator.plan_transition(43) is None
    coordinator._async_refresh_plan_run_states.assert_awaited_once()
    # Once to show the transition, once to clear it.
    assert coordinator.async_update_listeners.call_count == 2


def test_a_failed_action_still_clears_the_transition_and_reads_the_states() -> None:
    coordinator = _coordinator()

    async def run() -> None:
        async with coordinator.async_plan_transition(43, PLAN_TRANSITION_STARTING):
            raise RuntimeError("comexio down")

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert coordinator.plan_transition(43) is None
    coordinator._async_refresh_plan_run_states.assert_awaited_once()


def test_without_refresh_the_run_states_are_not_read() -> None:
    """The watchdog's auto-start runs inside a run-state fetch and must not start another one."""
    coordinator = _coordinator()

    async def run() -> None:
        async with coordinator.async_plan_transition(43, PLAN_TRANSITION_STARTING, refresh=False):
            pass

    asyncio.run(run())
    coordinator._async_refresh_plan_run_states.assert_not_called()
    assert coordinator.plan_transition(43) is None
