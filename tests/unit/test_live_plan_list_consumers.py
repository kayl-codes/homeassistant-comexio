"""Deleted-plan backups follow coordinator.live_plan_list(): None = no plan list, {} = no plan left in Comexio."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import repairs
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.repairs import ComexioRepairFlow
from custom_components.comexio.services import backup

LIVE_LISTS = pytest.mark.parametrize("live_plans", [None, {}], ids=["no-plan-list", "no-plan-left"])


def _manager(**methods: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {"async_load": AsyncMock(), "plan_backups_for_identity_sync": lambda *_: [{"slot": 0}]}
    return SimpleNamespace(**(defaults | methods))


@LIVE_LISTS
def test_the_orphaned_backups_repair_needs_a_plan_list(live_plans: dict | None) -> None:
    manager = _manager(plan_backups_for_identity_sync=lambda *_: [])
    coordinator = SimpleNamespace(live_plan_list=lambda: live_plans, function_plan_backup=manager)
    flow = ComexioRepairFlow("orphaned_plan_backups_cx1_2_x", {"entry_id": "e1", "fub_id": "2", "plan_name": "Pumps"})
    flow.hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}}, config=SimpleNamespace(language="en"))  # type: ignore[assignment]

    with patch.object(repairs.ir, "async_delete_issue"):
        result = asyncio.run(flow.async_step_orphaned_backups({"action": "delete"}))

    if live_plans is None:
        assert result["reason"] == "plans_unavailable"
        manager.async_load.assert_not_called()
    else:
        # With no plan left the plan counts as deleted: the flow reaches the backups (gone meanwhile here).
        assert result["reason"] == "already_deleted"
        manager.async_load.assert_awaited_once()


def _run_service(handler: Any, coordinator: SimpleNamespace, data: dict[str, Any]) -> MagicMock:
    call = SimpleNamespace(data=data)
    with (
        patch.object(backup, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(backup, "_resolve_backup_identity", AsyncMock(return_value=("Pumps", None))),
        patch.object(backup, "_refresh_service_descriptions", AsyncMock()),
        patch.object(backup, "delete_orphaned_backup_issue"),
        patch.object(backup.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(handler(MagicMock(), call))
    return notify


def _service_coordinator(live_plans: dict | None, manager: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        server_id="cx1",
        live_plan_list=lambda: live_plans,
        function_plan_backup=manager,
        config_entry=SimpleNamespace(options={}),
        async_update_listeners=MagicMock(),
    )


@LIVE_LISTS
def test_purge_needs_a_plan_list(live_plans: dict | None) -> None:
    manager = _manager(async_purge_orphaned=AsyncMock(return_value=[]))
    notify = _run_service(
        backup._handle_function_plan_purge_orphaned_backups,
        _service_coordinator(live_plans, manager),
        {"confirm": True},
    )

    if live_plans is None:
        manager.async_purge_orphaned.assert_not_called()
        assert notify.call_args.args[1:] == (backup._PLAN_LIST_UNAVAILABLE,)
        assert notify.call_args.kwargs["title"] == backup._TITLE_PURGE_ORPHANED_BACKUPS_ERR
    else:
        assert manager.async_purge_orphaned.await_args.args[0] == {}


@LIVE_LISTS
def test_keep_needs_a_plan_list(live_plans: dict | None) -> None:
    manager = _manager(async_keep_orphaned=AsyncMock(return_value=True))
    notify = _run_service(
        backup._handle_function_plan_keep_backups, _service_coordinator(live_plans, manager), {"fub_id": "2:Pumps"}
    )

    if live_plans is None:
        manager.async_keep_orphaned.assert_not_called()
        assert notify.call_args.kwargs["title"] == backup._TITLE_KEEP_BACKUPS_ERR
    else:
        manager.async_keep_orphaned.assert_awaited_once_with(2, "Pumps")


def test_a_backup_cycle_without_plans_still_audits_the_orphaned_backups() -> None:
    """With no plan left the bulk load has nothing, but every backup is orphaned now."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator._function_plan_backup_lock = asyncio.Lock()
    coordinator.api = SimpleNamespace(function_plan_load_all_plans=AsyncMock(return_value={}))
    audit = AsyncMock()
    coordinator._async_audit_orphaned_backups = audit  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    audit.assert_awaited_once()
    # The orphaned-backups sensor and select show the audit's result right away, not at the next poll.
    coordinator.async_update_listeners.assert_called_once()


def test_a_failed_bulk_load_shows_the_reset_changed_plans_at_once() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator._function_plan_backup_lock = asyncio.Lock()
    coordinator.last_changed_plans = [{"fub_id": 2}]
    coordinator.api = SimpleNamespace(function_plan_load_all_plans=AsyncMock(side_effect=OSError("timeout")))
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.last_changed_plans == []
    coordinator.async_update_listeners.assert_called_once()
