"""The 'Orphaned plans' view of the plan selector: a view choice, never the managed plan (coordinator.py)."""

from types import SimpleNamespace

import pytest

from custom_components.comexio.const import FUNCTION_PLAN_ORPHANED_VIEW_OPTION
from custom_components.comexio.coordinator import ComexioCoordinator

PERSISTED_FUB_ID = 7


def _coordinator(selector_state: str | None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    state = None if selector_state is None else SimpleNamespace(state=selector_state)
    coordinator._active_plan_selector_state = lambda: state  # type: ignore[method-assign]
    coordinator.persisted_function_plan_fub_id = lambda: PERSISTED_FUB_ID  # type: ignore[method-assign]
    coordinator.api = SimpleNamespace(fub_data={"4": {"Name": "Lights"}, "7": {"Name": "Pumps"}})
    return coordinator


@pytest.mark.parametrize(
    ("selector_state", "active", "managed", "orphan_view"),
    [
        ("Lights (ID 4)", 4, 4, False),
        # The view names no plan, but audit and sync keep the plan picked before it.
        (FUNCTION_PLAN_ORPHANED_VIEW_OPTION, None, PERSISTED_FUB_ID, True),
        # Before the selector is set up, both fall back to the persisted plan.
        (None, PERSISTED_FUB_ID, PERSISTED_FUB_ID, False),
        ("unavailable", PERSISTED_FUB_ID, PERSISTED_FUB_ID, False),
    ],
    ids=["plan", "orphan-view", "not-set-up", "unavailable"],
)
def test_active_and_managed_plan(
    selector_state: str | None, active: int | None, managed: int | None, orphan_view: bool
) -> None:
    coordinator = _coordinator(selector_state)

    assert coordinator.get_active_function_plan_fub_id() == active
    assert coordinator.get_managed_function_plan_fub_id() == managed
    assert coordinator.orphaned_plans_view_active() is orphan_view
