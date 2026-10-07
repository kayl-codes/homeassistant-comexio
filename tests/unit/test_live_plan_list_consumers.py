"""Deleted-plan backups follow coordinator.live_plan_list(): None = no plan list, {} = no plan left in Comexio."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import repairs
from custom_components.comexio.const import CONF_FUNCTION_PLAN_PLAN_MAP, DOMAIN
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
    coordinator.scraped_plan_ids = None
    coordinator.api = SimpleNamespace(function_plan_load_all_plans=AsyncMock(return_value={}))
    audit = AsyncMock()
    coordinator._async_audit_orphaned_backups = audit  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    audit.assert_awaited_once()
    # The orphaned-backups sensor and select show the audit's result right away, not at the next poll.
    coordinator.async_update_listeners.assert_called_once()


DELETED_PLAN = {7: {"elements": {"1": {"reference": {"type": "2", "ref_id": "253"}}}, "connections": {}}}


def _snapshot_coordinator(live_plans: dict | None) -> ComexioCoordinator:
    """A coordinator whose last bulk snapshot still holds plan 7, and whose next bulk load finds nothing."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "cx1"
    coordinator._function_plan_backup_lock = asyncio.Lock()
    coordinator.scraped_plan_ids = None if live_plans is None else set()
    coordinator.api = SimpleNamespace(
        function_plan_load_all_plans=AsyncMock(return_value={}), fub_data=live_plans if live_plans is not None else {}
    )
    coordinator.function_plan_plans = dict(DELETED_PLAN)
    coordinator._last_bulk_snapshot_fub_ids = frozenset(DELETED_PLAN)
    coordinator._last_referenced_marker_ids = {"253"}
    coordinator._lp_missing_recheck_pending = False
    coordinator._async_audit_orphaned_backups = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coordinator.reference_monitor = MagicMock()
    return coordinator


@pytest.mark.parametrize(
    "live_plans", [None, {}, {"8": {"Name": "Live"}}], ids=["no-plan-list", "no-plan-left", "plans-left"]
)
def test_no_plan_left_clears_the_unknown_block_references(live_plans: dict | None) -> None:
    """Review: the deleted plans' unknown block references must not stay in the log and the repair report.

    Only a poll that found no plan proves them deleted: an empty bulk load while plans exist keeps the findings.
    """
    coordinator = _snapshot_coordinator(live_plans)

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    if live_plans == {}:
        coordinator.reference_monitor.check_plans.assert_called_once_with({})
    else:
        coordinator.reference_monitor.check_plans.assert_not_called()


def test_no_plan_left_empties_the_snapshot_but_keeps_it_loaded() -> None:
    """#140: the deleted plan leaves the snapshot, and {} now reads as "loaded, no plans" — not "not loaded"."""
    coordinator = _snapshot_coordinator({})

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {}
    assert coordinator._referenced_marker_ids() == set()
    # A deleted managed plan (fub 7) is judged against the empty plan list instead of waited for.
    assert coordinator._relevant_plans_loaded({7}) is True
    # M253 was only referenced in the deleted plan — the refresh rebuilds the entities without it.
    coordinator.async_request_refresh.assert_awaited_once()


def test_an_empty_bulk_load_without_a_plan_list_keeps_the_snapshot() -> None:
    """Without a poll's plan list an empty bulk load is no proof that every plan is gone."""
    coordinator = _snapshot_coordinator(None)

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == DELETED_PLAN
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "bulk_load", [AsyncMock(return_value={}), AsyncMock(side_effect=OSError("timeout"))], ids=["empty", "failed"]
)
def test_a_plan_created_after_no_plan_was_left_makes_the_empty_snapshot_unloaded_again(bulk_load: AsyncMock) -> None:
    """Review: once a poll reads a new plan, {} is "not loaded" again even while every bulk load fails."""
    coordinator = _snapshot_coordinator({})
    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())
    coordinator.api.fub_data = {"8": {"Name": "New"}}
    coordinator.api.function_plan_load_all_plans = bulk_load

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator._referenced_marker_ids() is None
    assert coordinator._relevant_plans_loaded({8}) is False


def test_repeated_cycles_with_no_plan_left_refresh_only_once() -> None:
    coordinator = _snapshot_coordinator({})
    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())
    coordinator._last_referenced_marker_ids = set()  # what the triggered refresh records

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.async_request_refresh.assert_awaited_once()


def test_no_plan_left_reruns_a_pending_wiring_check() -> None:
    """The skipped function_plan_missing check runs once the snapshot is known to be empty."""
    coordinator = _snapshot_coordinator({})
    coordinator._last_referenced_marker_ids = set()
    coordinator.function_plan_plans = {7: {"elements": {}, "connections": {}}}
    coordinator._lp_missing_recheck_pending = True

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.async_request_refresh.assert_awaited_once()
    assert coordinator._lp_missing_recheck_pending is False


def test_a_first_snapshot_confirmed_empty_reruns_a_pending_wiring_check() -> None:
    """Review: no snapshot was loaded yet, so "no plan left" is a change even though both id sets are empty."""
    coordinator = _snapshot_coordinator({})
    coordinator.function_plan_plans = {}
    coordinator._last_bulk_snapshot_fub_ids = None
    coordinator._last_referenced_marker_ids = set()
    coordinator._lp_missing_recheck_pending = True

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.async_request_refresh.assert_awaited_once()
    assert coordinator._last_bulk_snapshot_fub_ids == frozenset()
    # Still pending after that refresh (e.g. a plan the bulk load never delivers): no refresh loop.
    coordinator._lp_missing_recheck_pending = True
    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())
    coordinator.async_request_refresh.assert_awaited_once()


def _identity_coordinator(live_plans: dict | None, cached_plans: dict | None = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.scraped_plan_ids = None if live_plans is None else set()
    coordinator.api = SimpleNamespace(fub_data=live_plans if live_plans is not None else cached_plans or {})
    coordinator.config_entry = SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Marker 1-50": 7}})
    return coordinator


def test_every_plan_identity_is_unresolved_once_no_plan_is_left() -> None:
    """#140: an empty plan list read by a poll means the mapped plan is gone, not "no list"."""
    assert _identity_coordinator({})._unresolved_plan_identities() == frozenset({(7, "HA - Marker 1-50")})


def test_no_plan_identity_is_unresolved_without_a_plan_list() -> None:
    assert _identity_coordinator(None)._unresolved_plan_identities() == frozenset()


def test_no_plan_identity_is_unresolved_by_a_partial_plan_cache() -> None:
    """Review: before a poll read the full list the cache holds only plans HA created or looked up."""
    coordinator = _identity_coordinator(None, cached_plans={"9": {"Name": "HA - Trigger"}})

    assert coordinator._unresolved_plan_identities() == frozenset()


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
