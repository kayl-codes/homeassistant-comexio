"""Repairs for the backups of deleted function plans (function_plan_backup.py, orphaned_backups.py)."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio import function_plan_backup as backup_module, orphaned_backups
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager, is_orphaned_identity
from custom_components.comexio.orphaned_backups import (
    BACKUPS_DOC_URL,
    async_audit_orphaned_backups,
    orphaned_backup_issue_id,
    repair_placeholders,
)

SERVER_ID = "cx1"
CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)
OLD = "2026-01-10T08:00:00+00:00"
RECENT = "2026-09-01T08:00:00+00:00"
# Live $Fubs: fub 1 "Lights" still exists, fub 2 was reused by a different plan, fub 3 is gone.
LIVE_FUBS = {"1": {"Name": "Lights"}, "2": {"Name": "Heating"}}


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store, keyed by storage key."""

    saved: dict[str, Any] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.saved.get(self.key)

    async def async_save(self, data: Any) -> None:
        FakeStore.saved[self.key] = data


def _snap(captured_at: str) -> dict[str, Any]:
    return {"captured_at": captured_at, "elements": {}, "connections": {}}


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FunctionPlanBackupManager:
    """Manager whose stores hold: a live plan, a reused ID, an old orphan and a recent orphan."""
    FakeStore.saved = {
        f"{DOMAIN}_logikplan_auto_{SERVER_ID}": {
            "1": {"Lights": [_snap(OLD)]},
            "2": {"Pumps": [_snap(OLD), _snap(OLD)]},
            "3": {"Garage": [_snap(RECENT)]},
        },
        f"{DOMAIN}_logikplan_changes_{SERVER_ID}": {"2": {"Pumps": [_snap(OLD)]}},
    }
    monkeypatch.setattr(backup_module, "Store", FakeStore)
    return FunctionPlanBackupManager(MagicMock(), SERVER_ID)


def _kept_saved() -> list[dict[str, Any]]:
    return FakeStore.saved[f"{DOMAIN}_function_plan_backup_kept_{SERVER_ID}"]["identities"]


@pytest.mark.parametrize(
    ("fub_id", "plan_name", "expected"),
    [
        (1, "Lights", False),  # live
        (2, "Pumps", True),  # ID reused by another plan
        (3, "Garage", True),  # ID gone
    ],
)
def test_is_orphaned_identity(fub_id: int, plan_name: str, expected: bool) -> None:
    assert is_orphaned_identity(LIVE_FUBS, fub_id, plan_name) is expected


def test_is_orphaned_identity_without_live_plans_is_never_orphaned() -> None:
    assert is_orphaned_identity(None, 3, "Garage") is False


def test_every_identity_is_orphaned_once_no_plan_is_left() -> None:
    """An empty plan list that was really read means every plan was deleted."""
    assert is_orphaned_identity({}, 1, "Lights") is True


def test_expired_orphans_lists_only_orphans_past_the_cutoff(manager: FunctionPlanBackupManager) -> None:
    expired = asyncio.run(manager.async_expired_orphans(LIVE_FUBS, CUTOFF))

    # "Lights" is live despite its old snapshot, "Garage" is orphaned but recent.
    assert expired == [{"fub_id": 2, "plan_name": "Pumps", "count": 3, "captured_at": OLD}]


def test_expired_orphans_without_live_plans_is_none(manager: FunctionPlanBackupManager) -> None:
    """A failed $Fubs fetch must neither raise nor clear a repair."""
    assert asyncio.run(manager.async_expired_orphans(None, CUTOFF)) is None


def test_expired_orphans_with_no_plan_left_lists_every_old_identity(manager: FunctionPlanBackupManager) -> None:
    expired = asyncio.run(manager.async_expired_orphans({}, CUTOFF))

    # "Garage" is orphaned too, but still within the retention.
    assert [(e["fub_id"], e["plan_name"]) for e in expired or []] == [(1, "Lights"), (2, "Pumps")]


def test_purge_without_live_plans_deletes_nothing(manager: FunctionPlanBackupManager) -> None:
    assert asyncio.run(manager.async_purge_orphaned(None, CUTOFF)) == []
    assert FakeStore.saved[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["2"] == {"Pumps": [_snap(OLD), _snap(OLD)]}


def test_kept_orphan_is_neither_listed_nor_purged(manager: FunctionPlanBackupManager) -> None:
    async def run() -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        await manager.async_keep_orphaned(2, "Pumps")
        return await manager.async_expired_orphans(LIVE_FUBS, CUTOFF), await manager.async_purge_orphaned(
            LIVE_FUBS, CUTOFF
        )

    expired, purged = asyncio.run(run())

    assert expired == []
    assert purged == []
    assert _kept_saved() == [{"fub_id": 2, "plan_name": "Pumps"}]
    assert FakeStore.saved[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["2"]["Pumps"]


def test_kept_identity_is_forgotten_once_the_plan_is_live_again(manager: FunctionPlanBackupManager) -> None:
    """A plan that comes back and is deleted again must be asked about again."""
    live_again = {**LIVE_FUBS, "2": {"Name": "Pumps"}}

    async def run() -> list[dict[str, Any]] | None:
        await manager.async_keep_orphaned(2, "Pumps")
        await manager.async_expired_orphans(live_again, CUTOFF)
        return await manager.async_expired_orphans(LIVE_FUBS, CUTOFF)

    assert asyncio.run(run()) == [{"fub_id": 2, "plan_name": "Pumps", "count": 3, "captured_at": OLD}]
    assert _kept_saved() == []


def test_kept_identity_is_forgotten_once_its_snapshots_are_deleted(manager: FunctionPlanBackupManager) -> None:
    """Forgotten at deletion, not only by the next audit: a plan recreated under the same
    identity and deleted again before that audit must still be asked about."""

    async def run() -> None:
        await manager.async_keep_orphaned(2, "Pumps")
        await manager.async_delete_plan_backups(2, "Pumps")

    asyncio.run(run())

    assert _kept_saved() == []


def test_kept_identity_is_forgotten_when_its_last_snapshot_is_deleted(manager: FunctionPlanBackupManager) -> None:
    async def run() -> list[list[dict[str, Any]]]:
        await manager.async_keep_orphaned(2, "Pumps")
        await manager.async_delete_snapshot("auto", 2, "Pumps", 0)
        after_first = list(_kept_saved())
        await manager.async_delete_snapshot("auto", 2, "Pumps", 0)
        await manager.async_delete_snapshot("change", 2, "Pumps", 0)
        return [after_first, _kept_saved()]

    after_first, after_last = asyncio.run(run())

    assert after_first == [{"fub_id": 2, "plan_name": "Pumps"}]  # snapshots left: still kept
    assert after_last == []


@pytest.mark.parametrize("remove", ["rekey", "purge_identity"])
def test_kept_identity_is_forgotten_when_restore_as_new_moves_or_purges_it(
    manager: FunctionPlanBackupManager, remove: str
) -> None:
    async def run() -> None:
        await manager.async_keep_orphaned(2, "Pumps")
        if remove == "rekey":
            await manager.async_rekey_fub_id(2, 7, "Pumps")
        else:
            await manager.async_purge_identity(2, "Pumps")

    asyncio.run(run())

    assert _kept_saved() == []


def test_delete_all_backups_forgets_every_kept_identity(manager: FunctionPlanBackupManager) -> None:
    async def run() -> None:
        await manager.async_keep_orphaned(2, "Pumps")
        await manager.async_delete_all_backups()

    asyncio.run(run())

    assert _kept_saved() == []


def test_purge_deletes_only_expired_orphans(manager: FunctionPlanBackupManager) -> None:
    purged = asyncio.run(manager.async_purge_orphaned(LIVE_FUBS, CUTOFF))

    assert purged == [{"fub_id": 2, "plan_name": "Pumps", "removed": 3, "captured_at": OLD}]
    assert FakeStore.saved[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] == {
        "1": {"Lights": [_snap(OLD)]},
        "3": {"Garage": [_snap(RECENT)]},
    }
    assert FakeStore.saved[f"{DOMAIN}_logikplan_changes_{SERVER_ID}"] == {}


def test_issue_id_is_stable_and_separates_plan_names() -> None:
    issue_id = orphaned_backup_issue_id(SERVER_ID, 2, "Pumps")

    assert issue_id == orphaned_backup_issue_id(SERVER_ID, 2, "Pumps")
    assert issue_id.startswith(f"orphaned_plan_backups_{SERVER_ID}_2_")
    assert issue_id != orphaned_backup_issue_id(SERVER_ID, 2, "Pumps 2")


def test_repair_placeholders_fall_back_for_missing_data() -> None:
    placeholders = repair_placeholders({"plan_name": "Pumps", "fub_id": "2", "count": "3"})

    assert placeholders == {
        "plan_name": "Pumps",
        "fub_id": "2",
        "count": "3",
        "newest": "?",
        "months": "?",
        "current_plan": "?",
        "docs_url": BACKUPS_DOC_URL,
    }


def test_invalid_kept_entry_does_not_block_backups(manager: FunctionPlanBackupManager) -> None:
    FakeStore.saved[f"{DOMAIN}_function_plan_backup_kept_{SERVER_ID}"] = {
        "identities": [{"fub_id": "x"}, {"plan_name": "Lights"}, {"fub_id": 2, "plan_name": "Pumps"}]
    }

    # The valid entry still counts: "Pumps" stays kept, the broken ones are skipped.
    assert asyncio.run(manager.async_expired_orphans(LIVE_FUBS, CUTOFF)) == []


@pytest.mark.parametrize("stored", [["not", "a", "dict"], {"identities": 5}, {"identities": "Pumps"}])
def test_invalid_kept_store_shape_does_not_block_backups(manager: FunctionPlanBackupManager, stored: Any) -> None:
    FakeStore.saved[f"{DOMAIN}_function_plan_backup_kept_{SERVER_ID}"] = stored

    # Nothing counts as kept, so the orphan is asked about again instead of the load raising.
    assert asyncio.run(manager.async_expired_orphans(LIVE_FUBS, CUTOFF)) == [
        {"fub_id": 2, "plan_name": "Pumps", "count": 3, "captured_at": OLD}
    ]


def _registry(monkeypatch: pytest.MonkeyPatch, issue_ids: list[tuple[str, str]]) -> MagicMock:
    ir = MagicMock()
    ir.async_get.return_value.issues = dict.fromkeys(issue_ids)
    monkeypatch.setattr(orphaned_backups, "ir", ir)
    return ir


def _deleted_ids(ir: MagicMock) -> list[tuple[str, str]]:
    return [call.args[1:] for call in ir.async_delete_issue.call_args_list]


def test_delete_all_issues_spares_a_server_whose_id_extends_this_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server "cx1" must not touch the repairs of server "cx1_2"."""
    own = orphaned_backup_issue_id(SERVER_ID, 2, "Pumps")
    ir = _registry(
        monkeypatch,
        [
            (DOMAIN, own),
            (DOMAIN, orphaned_backup_issue_id(f"{SERVER_ID}_2", 5, "Pumps")),
            (DOMAIN, "some_other_issue"),
            ("other_domain", orphaned_backup_issue_id(SERVER_ID, 8, "Foreign")),
        ],
    )

    orphaned_backups.delete_all_orphaned_backup_issues(MagicMock(), SERVER_ID)

    assert _deleted_ids(ir) == [(DOMAIN, own)]


def test_audit_raises_one_repair_per_expired_orphan_and_clears_stale_ones(
    manager: FunctionPlanBackupManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    wanted = orphaned_backup_issue_id(SERVER_ID, 2, "Pumps")
    stale = orphaned_backup_issue_id(SERVER_ID, 9, "Gone")
    ir = _registry(
        monkeypatch,
        [
            (DOMAIN, wanted),
            (DOMAIN, stale),
            (DOMAIN, orphaned_backup_issue_id(f"{SERVER_ID}_2", 9, "Gone")),
            (DOMAIN, "some_other_issue"),
            ("other_domain", orphaned_backup_issue_id(SERVER_ID, 8, "Foreign")),
        ],
    )

    asyncio.run(
        async_audit_orphaned_backups(
            MagicMock(),
            entry_id="entry1",
            server_id=SERVER_ID,
            manager=manager,
            fub_data=LIVE_FUBS,
            cutoff=CUTOFF,
            retention_months=6,
        )
    )

    ir.async_create_issue.assert_called_once()
    args, kwargs = ir.async_create_issue.call_args
    assert args[1:] == (DOMAIN, wanted)
    assert kwargs["translation_placeholders"] == {
        "plan_name": "Pumps",
        "fub_id": "2",
        "count": "3",
        "newest": "10.01.2026 08:00",
        "months": "6",
        "current_plan": "Heating",  # fub 2 now holds another plan
    }
    assert kwargs["data"] == {"entry_id": "entry1", **kwargs["translation_placeholders"]}
    assert _deleted_ids(ir) == [(DOMAIN, stale)]


def test_audit_without_live_plans_leaves_repairs_alone(
    manager: FunctionPlanBackupManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ir = MagicMock()
    monkeypatch.setattr(orphaned_backups, "ir", ir)

    asyncio.run(
        async_audit_orphaned_backups(
            MagicMock(),
            entry_id="entry1",
            server_id=SERVER_ID,
            manager=manager,
            fub_data=None,
            cutoff=CUTOFF,
            retention_months=6,
        )
    )

    ir.async_create_issue.assert_not_called()
    ir.async_delete_issue.assert_not_called()


def test_orphaned_plans_lists_orphans_with_backups_sorted_by_name(manager: FunctionPlanBackupManager) -> None:
    async def run() -> list[dict[str, Any]]:
        await manager.async_load()
        await manager.async_keep_orphaned(3, "Garage")
        return manager.orphaned_plans_sync(LIVE_FUBS)

    orphans = asyncio.run(run())

    # "Lights" is live; the reused ID 2 and the gone ID 3 are orphaned, kept or not.
    assert [(o["fub_id"], o["plan_name"], o["kept"], len(o["backups"])) for o in orphans] == [
        (3, "Garage", True, 1),
        (2, "Pumps", False, 3),
    ]


def test_orphaned_plans_without_live_plans_is_empty(manager: FunctionPlanBackupManager) -> None:
    """A failed $Fubs fetch must not show every plan as deleted."""
    asyncio.run(manager.async_load())

    assert manager.orphaned_plans_sync(None) == []


def test_orphaned_plans_with_no_plan_left_lists_every_plan(manager: FunctionPlanBackupManager) -> None:
    asyncio.run(manager.async_load())

    assert [(o["fub_id"], o["plan_name"]) for o in manager.orphaned_plans_sync({})] == [
        (3, "Garage"),
        (1, "Lights"),
        (2, "Pumps"),
    ]


def test_keep_reports_whether_a_decision_was_stored(manager: FunctionPlanBackupManager) -> None:
    """The keep service tells "kept" from "was kept already" by this result."""

    async def run() -> tuple[bool, bool]:
        return await manager.async_keep_orphaned(2, "Pumps"), await manager.async_keep_orphaned(2, "Pumps")

    assert asyncio.run(run()) == (True, False)
    assert _kept_saved() == [{"fub_id": 2, "plan_name": "Pumps"}]


def test_unkeep_takes_back_a_keep_decision(manager: FunctionPlanBackupManager) -> None:
    async def run() -> tuple[bool, bool, list[dict[str, Any]] | None]:
        await manager.async_keep_orphaned(2, "Pumps")
        first = await manager.async_unkeep_orphaned(2, "Pumps")
        second = await manager.async_unkeep_orphaned(2, "Pumps")
        return first, second, await manager.async_expired_orphans(LIVE_FUBS, CUTOFF)

    first, second, expired = asyncio.run(run())

    assert (first, second) == (True, False)
    assert _kept_saved() == []
    assert expired == [{"fub_id": 2, "plan_name": "Pumps", "count": 3, "captured_at": OLD}]


def _orphan(fub_id: int, plan_name: str, backups: list[dict[str, Any]], kept: bool = False) -> dict[str, Any]:
    return {"fub_id": fub_id, "plan_name": plan_name, "kept": kept, "backups": backups}


def test_orphaned_backup_options_plan_row_then_indented_snapshots() -> None:
    backups = [
        {"kind": "auto", "slot": 0, "captured_at": OLD},
        {"kind": "change", "slot": 0, "captured_at": RECENT, "operation": "sort"},
    ]

    rows = backup_module.build_orphaned_backup_options([_orphan(3, "Garage", backups, kept=True)])

    indent = "\u00a0" * 4
    assert [label for label, _choice in rows] == [
        "Garage (ID 3) — 2 backups",
        indent + backup_module.format_backup_label(backups[0]),
        indent + backup_module.format_backup_label(backups[1]),
    ]
    identity = {"fub_id": 3, "plan_name": "Garage", "kept": True}
    # The plan row shows the newest snapshot, whatever its kind.
    assert [choice for _label, choice in rows] == [
        {**identity, "kind": "change", "slot": 0, "plan_row": True},
        {**identity, "kind": "auto", "slot": 0, "plan_row": False},
        {**identity, "kind": "change", "slot": 0, "plan_row": False},
    ]


def test_orphaned_backup_options_keep_labels_unique_across_plans() -> None:
    """Two plans backed up in the same minute share a snapshot label — the select needs unique options."""
    same = [{"kind": "auto", "slot": 0, "captured_at": OLD}]

    rows = backup_module.build_orphaned_backup_options(
        [_orphan(3, "Garage", same), _orphan(2, "Pumps", same), _orphan(5, "Pumps", same)]
    )

    labels = [label for label, _choice in rows]
    assert len(set(labels)) == len(labels)
    snapshot = "\u00a0" * 4 + backup_module.format_backup_label(same[0])
    # Each repeat gets its own plan appended; the identity is unique per plan, so a third
    # plan's row cannot collide with the second's.
    assert labels == [
        "Garage (ID 3) — 1 backup",
        snapshot,
        "Pumps (ID 2) — 1 backup",
        snapshot + " · Pumps (ID 2)",
        "Pumps (ID 5) — 1 backup",
        snapshot + " · Pumps (ID 5)",
    ]
    assert [rows[3][1]["fub_id"], rows[5][1]["fub_id"]] == [2, 5]


def test_summarize_orphaned_backups_counts_every_snapshot_per_plan() -> None:
    orphans = [_orphan(3, "Garage", [{}], kept=True), _orphan(2, "Pumps", [{}, {}, {}])]

    count, plans = backup_module.summarize_orphaned_backups(orphans)

    assert count == 4
    assert plans == [
        {"fub_id": 3, "plan_name": "Garage", "kept": True, "backups": 1},
        {"fub_id": 2, "plan_name": "Pumps", "kept": False, "backups": 3},
    ]


def _orphaned_sensor(manager: FunctionPlanBackupManager, fub_data: dict[str, Any] | None) -> Any:
    from types import SimpleNamespace

    from custom_components.comexio.sensor import ComexioOrphanedBackupsSensor

    asyncio.run(manager.async_load())
    sensor = ComexioOrphanedBackupsSensor.__new__(ComexioOrphanedBackupsSensor)
    sensor.coordinator = SimpleNamespace(live_plan_list=lambda: fub_data, function_plan_backup=manager)
    return sensor


def test_orphaned_backups_sensor_counts_what_the_orphaned_plans_view_lists(
    manager: FunctionPlanBackupManager,
) -> None:
    sensor = _orphaned_sensor(manager, LIVE_FUBS)

    # The reused ID 2 ("Pumps": 2 auto + 1 change) and the gone ID 3 ("Garage": 1); "Lights" is live.
    assert sensor.native_value == 4
    assert [(p["plan_name"], p["backups"]) for p in sensor.extra_state_attributes["plans"]] == [
        ("Garage", 1),
        ("Pumps", 3),
    ]


def test_orphaned_backups_sensor_is_zero_when_every_plan_is_live(manager: FunctionPlanBackupManager) -> None:
    sensor = _orphaned_sensor(manager, {"1": {"Name": "Lights"}, "2": {"Name": "Pumps"}, "3": {"Name": "Garage"}})

    assert sensor.native_value == 0
    assert sensor.extra_state_attributes == {"plans": []}


def test_orphaned_backups_sensor_is_unknown_without_live_plans(manager: FunctionPlanBackupManager) -> None:
    """A failed $Fubs fetch is no reason to report 0 — nor every plan as deleted."""
    sensor = _orphaned_sensor(manager, None)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"plans": []}


def test_orphaned_backups_sensor_counts_every_backup_once_no_plan_is_left(manager: FunctionPlanBackupManager) -> None:
    sensor = _orphaned_sensor(manager, {})

    assert sensor.native_value == 5  # Lights 1, Pumps 3, Garage 1


def test_audit_with_no_plan_left_raises_a_repair_per_old_plan(
    manager: FunctionPlanBackupManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ir = _registry(monkeypatch, [])

    asyncio.run(
        async_audit_orphaned_backups(
            MagicMock(),
            entry_id="entry1",
            server_id=SERVER_ID,
            manager=manager,
            fub_data={},
            cutoff=CUTOFF,
            retention_months=6,
        )
    )

    # "Garage" is orphaned too, but still within the retention.
    created = [call.args[2] for call in ir.async_create_issue.call_args_list]
    assert created == [
        orphaned_backup_issue_id(SERVER_ID, 1, "Lights"),
        orphaned_backup_issue_id(SERVER_ID, 2, "Pumps"),
    ]
    assert {
        call.kwargs["translation_placeholders"]["current_plan"] for call in ir.async_create_issue.call_args_list
    } == {orphaned_backups.NO_CURRENT_PLAN}


def test_purge_with_no_plan_left_deletes_every_expired_plan(manager: FunctionPlanBackupManager) -> None:
    purged = asyncio.run(manager.async_purge_orphaned({}, CUTOFF))

    assert [(p["fub_id"], p["plan_name"]) for p in purged] == [(1, "Lights"), (2, "Pumps")]
    assert FakeStore.saved[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"] == {"3": {"Garage": [_snap(RECENT)]}}
