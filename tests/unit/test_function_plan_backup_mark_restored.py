"""Promoting the restored snapshot after an in-place restore (FunctionPlanBackupManager.async_mark_restored)."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio import function_plan_backup as backup_module
from custom_components.comexio.const import DOMAIN, FUNCTION_PLAN_CHANGE_BACKUP_SLOTS
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager

SERVER_ID = "cx1"
FUB_ID = 1
PLAN = "Lights"
CHANGE_KEY = f"{DOMAIN}_logikplan_changes_{SERVER_ID}"
AUTO_KEY = f"{DOMAIN}_logikplan_auto_{SERVER_ID}"


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store, keyed by storage key."""

    saved: dict[str, Any] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.saved.get(self.key)

    async def async_save(self, data: Any) -> None:
        FakeStore.saved[self.key] = data


def _snap(n: int) -> dict[str, Any]:
    return {"captured_at": f"2026-09-{n + 1:02d}T08:00:00+00:00", "hash": f"h{n}", "elements": {}, "connections": {}}


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FunctionPlanBackupManager:
    """Manager with full change history (newest first) and two auto snapshots for one plan."""
    FakeStore.saved = {
        CHANGE_KEY: {str(FUB_ID): {PLAN: [_snap(n) for n in range(FUNCTION_PLAN_CHANGE_BACKUP_SLOTS)]}},
        AUTO_KEY: {str(FUB_ID): {PLAN: [_snap(100), _snap(101)]}},
    }
    monkeypatch.setattr(backup_module, "Store", FakeStore)
    return FunctionPlanBackupManager(MagicMock(), SERVER_ID)


def _history(key: str) -> list[dict[str, Any]]:
    return FakeStore.saved[key][str(FUB_ID)][PLAN]


async def _restore_sequence(manager: FunctionPlanBackupManager, kind: str, slot: int) -> bool:
    """What _restore_plan_in_place does to the store: pre_restore change backup first, then promote."""
    source = await manager.async_get_snapshot(kind, FUB_ID, PLAN, slot)
    assert source is not None
    await manager.async_change_backup(FUB_ID, {"elements": {}, "connections": {}}, PLAN, f"pre_restore {kind}[{slot}]")
    return await manager.async_mark_restored(kind, FUB_ID, PLAN, source)


@pytest.mark.parametrize("slot", [0, 4])
def test_restoring_a_change_backup_promotes_the_source_not_the_pre_restore_backup(
    manager: FunctionPlanBackupManager, slot: int
) -> None:
    assert asyncio.run(_restore_sequence(manager, "change", slot))

    history = _history(CHANGE_KEY)
    assert history[0]["hash"] == f"h{slot}"
    assert history[0]["restored_at"]
    assert history[1]["operation"] == f"pre_restore change[{slot}]"
    assert "restored_at" not in history[1]
    assert [entry["hash"] for entry in history if entry.get("restored_at")] == [f"h{slot}"]
    assert len(history) == FUNCTION_PLAN_CHANGE_BACKUP_SLOTS


def test_source_rotated_out_by_the_pre_restore_backup_marks_nothing(manager: FunctionPlanBackupManager) -> None:
    oldest = FUNCTION_PLAN_CHANGE_BACKUP_SLOTS - 1

    assert not asyncio.run(_restore_sequence(manager, "change", oldest))

    history = _history(CHANGE_KEY)
    assert not any(entry.get("restored_at") for entry in history)
    assert history[0]["operation"] == f"pre_restore change[{oldest}]"


def test_restoring_an_auto_backup_promotes_it_within_the_auto_history(manager: FunctionPlanBackupManager) -> None:
    assert asyncio.run(_restore_sequence(manager, "auto", 1))

    auto = _history(AUTO_KEY)
    assert [entry["hash"] for entry in auto] == ["h101", "h100"]
    assert auto[0]["restored_at"]
    assert not any(entry.get("restored_at") for entry in _history(CHANGE_KEY))


def test_snapshot_with_translated_block_ids_is_still_promoted(manager: FunctionPlanBackupManager) -> None:
    # resolve_block_ids recomputes the hash of a snapshot whose block ids it translated.
    translated = _snap(3) | {"hash": "recomputed"}

    assert asyncio.run(manager.async_mark_restored("change", FUB_ID, PLAN, translated))

    history = _history(CHANGE_KEY)
    assert history[0]["hash"] == "h3"
    assert history[0]["restored_at"]


def test_snapshot_without_capture_time_is_not_promoted(manager: FunctionPlanBackupManager) -> None:
    assert not asyncio.run(manager.async_mark_restored("change", FUB_ID, PLAN, {"hash": "h0"}))
    assert not any(entry.get("restored_at") for entry in _history(CHANGE_KEY))
