"""Deleted-plan backups follow coordinator.live_plan_list(): None = no plan list, {} = no plan left in Comexio."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioError
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
    coordinator.function_plan_plans = {}
    coordinator.api = SimpleNamespace(function_plan_load_all_plans=AsyncMock(return_value={}), fub_data={})
    audit = AsyncMock()
    coordinator._async_audit_orphaned_backups = audit  # type: ignore[method-assign]
    coordinator._async_recheck_after_snapshot_update = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    coordinator.reference_monitor = MagicMock()

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    audit.assert_awaited_once()
    # The orphaned-backups sensor and select show the audit's result right away, not at the next poll.
    assert coordinator.async_update_listeners.call_count == 2  # the reset at once, the audit's result after


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
    coordinator._last_referenced_markers_from_snapshot = True
    coordinator._lp_missing_recheck_pending = False
    coordinator._async_audit_orphaned_backups = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coordinator.reference_monitor = MagicMock()
    return coordinator


@pytest.mark.parametrize(
    ("live_plans", "deleted"),
    [(None, False), ({}, True), ({"7": {"Name": "Live"}}, False), ({"8": {"Name": "Other"}}, True)],
    ids=["no-plan-list", "no-plan-left", "plan-still-listed", "plan-no-longer-listed"],
)
def test_an_empty_bulk_load_clears_the_unknown_block_references_of_deleted_plans(
    live_plans: dict | None, deleted: bool
) -> None:
    """Review: the deleted plans' unknown block references must not stay in the log and the repair report.

    Only a poll that no longer lists a plan proves it deleted: a plan still listed keeps its findings.
    """
    coordinator = _snapshot_coordinator(live_plans)

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    expected = {} if deleted else DELETED_PLAN
    assert coordinator.function_plan_plans == expected
    # Also for an unchanged snapshot: carried-over findings of plans outside it follow the plan list.
    coordinator.reference_monitor.check_plans.assert_called_once_with(expected)


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


def test_a_stale_bulk_load_after_no_plan_was_left_is_discarded() -> None:
    """Review: a bulk load answered before the deletion must not bring the deleted plan back."""
    coordinator = _snapshot_coordinator({})
    coordinator.api.function_plan_load_all_plans = AsyncMock(return_value=dict(DELETED_PLAN))
    coordinator.function_plan_backup = SimpleNamespace(async_auto_backup=AsyncMock())

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {}
    coordinator.reference_monitor.check_plans.assert_called_once_with({})
    coordinator.function_plan_backup.async_auto_backup.assert_not_awaited()
    # M253 was only referenced in the stale plan — the refresh rebuilds the entities without it.
    coordinator.async_request_refresh.assert_awaited_once()


def _full_cycle_coordinator(live_plans: dict | None, bulk_result: dict) -> ComexioCoordinator:
    """A _snapshot_coordinator whose bulk load returns bulk_result, stubbed for the full backup path."""
    coordinator = _snapshot_coordinator(live_plans)
    coordinator.api.function_plan_load_all_plans = AsyncMock(return_value=bulk_result)
    coordinator.api.comexio_version = None
    coordinator.api.block_settings = {}
    coordinator.function_plan_backup = SimpleNamespace(
        async_auto_backup=AsyncMock(return_value=[]),
        async_backfill_paper_metadata=AsyncMock(),
        async_backfill_block_keys=AsyncMock(),
    )
    coordinator.function_plan_label_maps = MagicMock(return_value=({}, {}, {}))  # type: ignore[method-assign]
    coordinator._current_plan_format = MagicMock(return_value=None)  # type: ignore[method-assign]
    coordinator._async_refresh_service_descriptions = AsyncMock()  # type: ignore[method-assign]
    return coordinator


@pytest.mark.parametrize("live_plans", [None, {"7": {"Name": "Live"}}], ids=["no-plan-list", "plans-left"])
def test_a_bulk_load_is_kept_while_the_poll_still_lists_its_plans(live_plans: dict | None) -> None:
    """Only plans the latest poll no longer lists are dropped — and nothing before the first poll."""
    coordinator = _full_cycle_coordinator(live_plans, dict(DELETED_PLAN))

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == DELETED_PLAN
    coordinator.reference_monitor.check_plans.assert_called_once_with(DELETED_PLAN)
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


def test_a_plan_deleted_during_the_bulk_load_does_not_come_back() -> None:
    """Review: a poll found plan 7 deleted while the bulk load was in flight; plan 8 is still there."""
    kept_plan = {8: {"elements": {}, "connections": {}}}
    coordinator = _full_cycle_coordinator({"8": {"Name": "Kept"}}, {**DELETED_PLAN, **kept_plan})

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == kept_plan
    coordinator.reference_monitor.check_plans.assert_called_once_with(kept_plan)
    assert coordinator.function_plan_backup.async_auto_backup.await_args.args[0] == kept_plan
    # M253 was only referenced in the deleted plan — the refresh rebuilds the entities without it.
    coordinator.async_request_refresh.assert_awaited_once()


def test_a_bulk_load_whose_plans_were_all_deleted_meanwhile_is_not_kept(caplog: pytest.LogCaptureFixture) -> None:
    """Review: plan 7 was deleted and plan 9 created during the load — the snapshot drops 7 and waits for 9."""
    coordinator = _full_cycle_coordinator({"9": {"Name": "New"}}, dict(DELETED_PLAN))

    with caplog.at_level(logging.DEBUG):
        asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {}
    assert coordinator._referenced_marker_ids() is None  # "not loaded" until plan 9 arrives
    coordinator.reference_monitor.check_plans.assert_called_once_with({})
    coordinator.function_plan_backup.async_auto_backup.assert_not_awaited()
    coordinator._async_audit_orphaned_backups.assert_awaited_once()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_plan_list_key_that_is_no_number_does_not_break_the_cycle() -> None:
    """A $Fubs key that is no plain number is skipped, as everywhere else — the cycle keeps plan 7."""
    coordinator = _full_cycle_coordinator({"7": {"Name": "Live"}, "x": {}}, dict(DELETED_PLAN))

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == DELETED_PLAN
    coordinator.function_plan_backup.async_auto_backup.assert_awaited_once()


@pytest.mark.parametrize(
    ("live_plans", "expected"),
    [({}, {}), ({"8": {"Name": "Other"}}, {}), ({"7": {"Name": "Live"}}, DELETED_PLAN)],
    ids=["no-plan-left", "plan-no-longer-listed", "plan-still-listed"],
)
def test_a_failed_bulk_load_keeps_only_the_plans_still_listed(live_plans: dict, expected: dict) -> None:
    """Review: a failed load cannot keep deleted plans in the audits, but keeps the wiring of the plans still listed."""
    coordinator = _snapshot_coordinator(live_plans)
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=OSError("timeout"))

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == expected
    coordinator.reference_monitor.check_plans.assert_called_once_with(expected)
    if expected:
        coordinator.async_request_refresh.assert_not_awaited()
    else:
        coordinator.async_request_refresh.assert_awaited_once()  # M253 rebuilt away
    # The orphaned-backup audit judges by the poll's plan list, so it runs even after a failed load.
    coordinator._async_audit_orphaned_backups.assert_awaited_once()
    assert coordinator.async_update_listeners.call_count == 2  # the reset at once, the audit's result after


@pytest.mark.parametrize(
    "bulk_load", [AsyncMock(return_value={}), AsyncMock(side_effect=OSError("timeout"))], ids=["empty", "failed"]
)
def test_a_cycle_without_plans_keeps_the_wiring_and_findings_of_the_plans_still_listed(bulk_load: AsyncMock) -> None:
    """Review: only the deleted plan 7 leaves the snapshot and the findings — plan 8 the load omitted stays."""
    plan_8 = {"elements": {}, "connections": {}}
    coordinator = _snapshot_coordinator({"8": {"Name": "Kept"}})
    coordinator.function_plan_plans = {**DELETED_PLAN, 8: plan_8}
    coordinator.api.function_plan_load_all_plans = bulk_load

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {8: plan_8}
    coordinator.reference_monitor.check_plans.assert_called_once_with({8: plan_8})
    coordinator.async_request_refresh.assert_awaited_once()  # M253 was only referenced in plan 7


def test_an_empty_bulk_load_prunes_the_snapshot_before_the_orphaned_backup_audit() -> None:
    """Review: a timeout during the audit must not skip the pruning, nor may it drop a plan seeded meanwhile."""
    coordinator = _snapshot_coordinator({"8": {"Name": "Managed"}})
    seeded = {"elements": {}, "connections": {}}

    async def audit() -> None:
        assert coordinator.function_plan_plans == {}  # 7 is already dropped when the audit starts
        coordinator.reference_monitor.check_plans.assert_called_once_with({})
        coordinator.async_request_refresh.assert_awaited_once()
        coordinator.async_update_listeners.assert_called_once()  # the reset last_changed_plans shows at once
        coordinator.function_plan_plans[8] = seeded  # _create_managed_plan seeds plan 8 meanwhile

    coordinator._async_audit_orphaned_backups = AsyncMock(side_effect=audit)  # type: ignore[method-assign]

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {8: seeded}


@pytest.mark.parametrize(
    ("live_plans", "warns"),
    [({}, False), (None, True), ({"7": {"Name": "Live"}}, True)],
    ids=["no-plan-left", "no-plan-list", "plans-left"],
)
def test_an_empty_bulk_load_warns_only_when_plans_should_exist(
    live_plans: dict | None, warns: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Review: a server without plans is a normal state, not a failed load to warn about every cycle."""
    coordinator = _snapshot_coordinator(live_plans)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert any(r.levelno == logging.WARNING and "no plans loaded" in r.getMessage() for r in caplog.records) is warns


@pytest.mark.parametrize(
    ("error", "level"),
    [(OSError("timeout"), logging.ERROR), (ComexioError("server busy"), logging.WARNING)],
    ids=["unexpected", "comexio"],
)
@pytest.mark.parametrize(
    "live_plans", [{}, None, {"7": {"Name": "Live"}}], ids=["no-plan-left", "no-plan-list", "plans-left"]
)
def test_a_failed_bulk_load_is_logged_once_not_warned_about_again(
    live_plans: dict | None, error: Exception, level: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Review: the API swallowed a ComexioError into {} — the cycle asks for it, so it is no "no plans loaded"."""
    coordinator = _snapshot_coordinator(live_plans)
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=error)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.api.function_plan_load_all_plans.assert_awaited_once_with(raise_errors=True)
    assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == [level]


@pytest.mark.parametrize(
    "bulk_load", [AsyncMock(return_value={}), AsyncMock(side_effect=OSError("timeout"))], ids=["empty", "failed"]
)
def test_a_cycle_without_plans_after_the_poll_pruned_still_rebuilds_the_entities(bulk_load: AsyncMock) -> None:
    """The poll already dropped plan 7; the cycle has nothing left to prune but M253 must still go."""
    coordinator = _snapshot_coordinator({"8": {"Name": "Kept"}})
    coordinator.function_plan_plans = {**DELETED_PLAN, 8: {"elements": {}, "connections": {}}}
    coordinator._prune_deleted_plans_from_snapshot()
    coordinator.api.function_plan_load_all_plans = bulk_load

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.async_request_refresh.assert_awaited_once()  # M253 was only referenced in plan 7


def test_a_plan_seeded_during_the_bulk_load_stays_in_the_snapshot() -> None:
    """Review: a sync creates and seeds plan 8 while the load for plan 7 runs — the load's result keeps it."""
    seeded = {"elements": {}, "connections": {}, "_seeded_empty": True}
    coordinator = _full_cycle_coordinator({"7": {"Name": "Live"}}, {})

    async def bulk_load(**_kwargs: Any) -> dict:
        coordinator.api.fub_data["8"] = {"Name": "HA - Marker 1-50"}  # what api.create_fup does
        coordinator.function_plan_plans[8] = seeded  # what _create_managed_plan does
        return dict(DELETED_PLAN)

    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=bulk_load)

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == {**DELETED_PLAN, 8: seeded}
    coordinator.reference_monitor.check_plans.assert_called_once_with({**DELETED_PLAN, 8: seeded})
    # Only loaded wiring is backed up, never the placeholder.
    assert coordinator.function_plan_backup.async_auto_backup.await_args.args[0] == DELETED_PLAN


def test_a_plan_the_bulk_load_was_asked_for_but_omitted_leaves_the_snapshot() -> None:
    """Unlike a plan created meanwhile, a plan the load had in its list and did not deliver is no longer loaded."""
    coordinator = _full_cycle_coordinator({"7": {"Name": "Live"}, "8": {"Name": "Malformed"}}, dict(DELETED_PLAN))
    coordinator.function_plan_plans = {**DELETED_PLAN, 8: {"elements": {}, "connections": {}}}

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.function_plan_plans == DELETED_PLAN


@pytest.mark.parametrize(
    ("live_plans", "expected"),
    [(None, DELETED_PLAN), ({"7": {"Name": "Live"}}, DELETED_PLAN), ({"8": {"Name": "Other"}}, {}), ({}, {})],
    ids=["no-plan-list", "plan-still-listed", "plan-no-longer-listed", "no-plan-left"],
)
def test_a_poll_prunes_the_plans_it_no_longer_lists_from_the_snapshot(live_plans: dict | None, expected: dict) -> None:
    """Review: the poll's audits must not read the wiring of a deleted plan until the next backup cycle."""
    coordinator = _snapshot_coordinator(live_plans)

    changed = coordinator._prune_deleted_plans_from_snapshot()

    assert coordinator.function_plan_plans == expected
    assert changed is (expected != DELETED_PLAN)
    # Every poll, not only on a change: a carried-over finding of a plan outside the snapshot follows the plan list.
    coordinator.reference_monitor.check_plans.assert_called_once_with(expected)


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


def test_failing_bulk_loads_without_a_snapshot_do_not_refresh_on_every_cycle() -> None:
    """Review: the poll took the wired markers from the stored backup; no snapshot is no "no marker" change."""
    coordinator = _snapshot_coordinator({"8": {"Name": "Other"}})
    coordinator.function_plan_plans = {}
    coordinator._last_bulk_snapshot_fub_ids = frozenset()
    coordinator._last_referenced_markers_from_snapshot = False  # {"253"} came from the stored backup
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=ComexioError("timeout"))

    for _ in range(2):
        asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator._referenced_marker_ids() is None
    coordinator.async_request_refresh.assert_not_awaited()


def test_a_snapshot_lost_by_a_failed_bulk_load_refreshes_only_once() -> None:
    """The poll after the loss switches to the stored backup's markers; later failed cycles change nothing."""
    coordinator = _snapshot_coordinator({"8": {"Name": "Other"}})
    coordinator.api.function_plan_load_all_plans = AsyncMock(side_effect=ComexioError("timeout"))
    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())
    coordinator._last_referenced_markers_from_snapshot = False  # what the triggered refresh records

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    coordinator.async_request_refresh.assert_awaited_once()


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
    coordinator.scraped_plan_ids = None
    coordinator.function_plan_plans = {}
    coordinator.last_changed_plans = [{"fub_id": 2}]
    coordinator.reference_monitor = MagicMock()
    coordinator.api = SimpleNamespace(
        function_plan_load_all_plans=AsyncMock(side_effect=OSError("timeout")), fub_data={}
    )
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]

    async def audit() -> None:
        # Shown before anything awaited, so a cycle timeout cannot keep the old list on screen.
        coordinator.async_update_listeners.assert_called_once()

    coordinator._async_audit_orphaned_backups = AsyncMock(side_effect=audit)  # type: ignore[method-assign]
    coordinator._async_recheck_after_snapshot_update = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(coordinator._async_function_plan_backup_cycle_locked())

    assert coordinator.last_changed_plans == []
    coordinator._async_audit_orphaned_backups.assert_awaited_once()
