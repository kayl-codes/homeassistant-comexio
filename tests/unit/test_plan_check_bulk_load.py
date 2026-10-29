"""A link check missing several plans in the cache loads them in one bulk request (startup took 23 s one by one)."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.comexio.const import FUNCTION_PLAN_CHECK_BULK_LOAD_MIN_MISSES
from custom_components.comexio.coordinator import _SEEDED_EMPTY_PLAN_MARKER, ComexioCoordinator

MANAGED = [34, 35, 36, 37]


def _plan(element_id: int) -> dict[str, Any]:
    return {"elements": {str(element_id): {"reference": {"type": 2, "ref_id": element_id}}}, "connections": {}}


def _coordinator(
    *, cached: dict[int, Any] | None = None, bulk: Any = None, single: dict[int, Any] | None = None
) -> ComexioCoordinator:
    """bulk: the bulk answer (an exception is raised); single: the per-plan answers (missing: None)."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "iosrv1"
    coordinator.function_plan_plans = cached or {}
    coordinator._function_plan_check_fub_ids = lambda: set(MANAGED)  # type: ignore[method-assign]
    load_all = AsyncMock(side_effect=bulk) if isinstance(bulk, Exception) else AsyncMock(return_value=bulk or {})
    coordinator.api = SimpleNamespace(  # type: ignore[assignment]
        fub_data={},
        function_plan_load_all_plans=load_all,
        function_plan_load_elements=AsyncMock(side_effect=_answers(single or {})),
    )
    return coordinator


def _answers(answers: dict[int, Any]) -> Any:
    """A function_plan_load_elements stand-in answering from answers (missing: None)."""

    def _load(fub_id: int, *, strict: bool = False) -> Any:
        return answers.get(fub_id)

    return _load


def _check(coordinator: ComexioCoordinator, memo: dict[int, dict] | None = None) -> tuple[dict[int, dict], list[int]]:
    load_failures: list[int] = []
    plans = asyncio.run(coordinator._load_function_plan_check_data(load_failures, memo=memo))
    return plans, load_failures


def _single_loads(coordinator: ComexioCoordinator) -> list[int]:
    """The plans loaded on their own; each load is strict (a payload without collections fails)."""
    calls = coordinator.api.function_plan_load_elements.await_args_list
    assert all(call.kwargs == {"strict": True} for call in calls)
    return sorted(call.args[0] for call in calls)


def test_startup_loads_every_managed_plan_in_one_bulk_request() -> None:
    bulk = {fub_id: _plan(fub_id) for fub_id in [*MANAGED, 50]}  # the bulk answer holds every plan
    coordinator = _coordinator(bulk=bulk)

    plans, load_failures = _check(coordinator)

    assert plans == {fub_id: _plan(fub_id) for fub_id in MANAGED}
    assert load_failures == []
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == []


@pytest.mark.parametrize("from_bulk", [True, False], ids=["bulk", "single"])
def test_an_empty_plan_is_used_as_it_is(from_bulk: bool) -> None:
    """strict keeps a plan with real but empty collections: a plan without wiring, no load failure."""
    empty = {"elements": {}, "connections": {}}
    bulk = {fub_id: _plan(fub_id) for fub_id in MANAGED[:-1]}
    coordinator = _coordinator(bulk={**bulk, 37: empty} if from_bulk else bulk, single={37: empty})

    plans, load_failures = _check(coordinator)

    assert plans[37] == empty
    assert load_failures == []
    assert _single_loads(coordinator) == ([] if from_bulk else [37])


# "empty": a falsy entry, which the strict bulk answer never holds; kept as a guard of the fallback.
@pytest.mark.parametrize("bulk_extra", [{}, {36: {}, 37: {}}], ids=["missing", "empty"])
def test_a_plan_the_bulk_answer_lacks_is_loaded_on_its_own(bulk_extra: dict[int, Any]) -> None:
    coordinator = _coordinator(bulk={34: _plan(34), 35: _plan(35), **bulk_extra}, single={36: _plan(36)})

    plans, load_failures = _check(coordinator)

    assert plans == {34: _plan(34), 35: _plan(35), 36: _plan(36)}
    assert load_failures == [37]  # neither answer has it
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == [36, 37]


def test_a_single_load_that_raises_fails_only_its_own_plan() -> None:
    def _load(fub_id: int, *, strict: bool = False) -> dict[str, Any]:
        if fub_id == 36:
            raise RuntimeError("boom")
        return _plan(fub_id)

    coordinator = _coordinator(bulk={34: _plan(34), 35: _plan(35)})
    coordinator.api.function_plan_load_elements.side_effect = _load

    plans, load_failures = _check(coordinator)

    assert plans == {34: _plan(34), 35: _plan(35), 37: _plan(37)}
    assert load_failures == [36]
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == [36, 37]


def test_a_caller_without_load_failures_gets_the_loaded_plans() -> None:
    """The cleanup lookups pass no load_failures: a failed plan is left out there, nothing raises."""
    coordinator = _coordinator(bulk={34: _plan(34), 35: _plan(35), 36: _plan(36)})

    plans = asyncio.run(coordinator._load_function_plan_check_data())

    assert plans == {34: _plan(34), 35: _plan(35), 36: _plan(36)}
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == [37]


@pytest.mark.parametrize("bulk", [{}, RuntimeError("boom")], ids=["failed", "raised"])
def test_a_failed_bulk_load_falls_back_to_loading_each_plan(bulk: Any) -> None:
    coordinator = _coordinator(bulk=bulk, single={fub_id: _plan(fub_id) for fub_id in MANAGED})

    plans, load_failures = _check(coordinator)

    assert plans == {fub_id: _plan(fub_id) for fub_id in MANAGED}
    assert load_failures == []
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == MANAGED


def test_few_cache_misses_are_loaded_one_by_one() -> None:
    misses = MANAGED[: FUNCTION_PLAN_CHECK_BULK_LOAD_MIN_MISSES - 1]
    cached = {fub_id: _plan(fub_id) for fub_id in MANAGED if fub_id not in misses}
    coordinator = _coordinator(cached=cached, single={fub_id: _plan(fub_id) for fub_id in misses})

    plans, _ = _check(coordinator)

    assert plans == {fub_id: _plan(fub_id) for fub_id in MANAGED}
    coordinator.api.function_plan_load_all_plans.assert_not_awaited()
    assert _single_loads(coordinator) == misses


def test_the_second_audit_category_reuses_the_plans_the_first_one_loaded() -> None:
    """Marker and KNX audit run one after the other in a poll: one bulk request serves both."""
    coordinator = _coordinator(bulk={fub_id: _plan(fub_id) for fub_id in MANAGED})
    memo: dict[int, dict] = {}

    first, _ = _check(coordinator, memo)
    second, load_failures = _check(coordinator, memo)

    assert second == first == {fub_id: _plan(fub_id) for fub_id in MANAGED}
    assert load_failures == []
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == []


def test_a_plan_the_first_audit_category_failed_is_tried_again() -> None:
    coordinator = _coordinator(bulk={34: _plan(34), 35: _plan(35), 36: _plan(36)})
    memo: dict[int, dict] = {}
    _, first_failures = _check(coordinator, memo)
    assert 37 not in memo  # a failure is not remembered
    coordinator.api.function_plan_load_elements.side_effect = _answers({37: _plan(37)})

    second, second_failures = _check(coordinator, memo)

    assert first_failures == [37]
    assert second == {fub_id: _plan(fub_id) for fub_id in MANAGED}
    assert second_failures == []
    # 37 failed in the first run and is loaded on its own again in the second, no second bulk request
    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(strict=True)
    assert _single_loads(coordinator) == [37, 37]


def test_a_check_outside_the_audit_loads_live_every_time() -> None:
    """A cleanup or unwire passes no memo, even while the audit holds one: it must see the plans as they are now."""
    coordinator = _coordinator(bulk={fub_id: _plan(fub_id) for fub_id in MANAGED})
    coordinator._audit_plan_memo = {fub_id: _plan(fub_id) for fub_id in MANAGED}

    _check(coordinator)
    _check(coordinator)

    assert coordinator.api.function_plan_load_all_plans.await_count == 2


def _audit_coordinator(marker_audit: AsyncMock) -> tuple[ComexioCoordinator, list[Any]]:
    """Coordinator whose two audit categories record the memo they ran with."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    seen: list[Any] = []

    async def _knx(*_: Any) -> None:
        seen.append(coordinator._audit_plan_memo)

    async def _marker(*args: Any) -> None:
        seen.append(coordinator._audit_plan_memo)
        await marker_audit(*args)

    coordinator.async_check_ignored_markers = _marker  # type: ignore[method-assign]
    coordinator.async_check_ignored_knx = _knx  # type: ignore[method-assign]
    return coordinator, seen


def test_both_audit_categories_share_one_memo_that_ends_with_the_audit() -> None:
    coordinator, seen = _audit_coordinator(AsyncMock())

    asyncio.run(coordinator._async_audit_ignored_categories({}, {}, {}))

    assert len(seen) == 2
    assert seen[0] == {}
    assert seen[0] is seen[1]
    assert coordinator._audit_plan_memo is None


def test_a_failing_audit_still_drops_the_memo() -> None:
    coordinator, seen = _audit_coordinator(AsyncMock(side_effect=RuntimeError("boom")))
    audit = coordinator._async_audit_ignored_categories({}, {}, {})

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(audit)

    assert len(seen) == 1  # the KNX audit did not run
    assert coordinator._audit_plan_memo is None


def test_a_seeded_empty_plan_counts_as_a_miss_and_a_cached_plan_is_kept() -> None:
    """The seed may be stale (wired since), the cached snapshot is what the backup cycle loaded."""
    seeded = {"elements": {}, "connections": {}, _SEEDED_EMPTY_PLAN_MARKER: True}
    cached = {34: _plan(1), 35: seeded, 36: seeded}
    coordinator = _coordinator(cached=cached, bulk={fub_id: _plan(fub_id) for fub_id in MANAGED})

    plans, _ = _check(coordinator)

    assert plans == {34: _plan(1), 35: _plan(35), 36: _plan(36), 37: _plan(37)}
    assert _single_loads(coordinator) == []
