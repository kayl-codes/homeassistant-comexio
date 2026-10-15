"""The backup manager's first load runs once, even when a reader and a writer start it together."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio import function_plan_backup as backup_module
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from tests.common import load_json_fixture

SERVER_ID = "cx1"
AUTO_KEY = f"{DOMAIN}_logikplan_auto_{SERVER_ID}"


class SlowStore:
    """In-memory Store whose load yields to the loop, so concurrent first loads overlap."""

    saved: dict[str, Any] = {}
    loads: dict[str, int] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        SlowStore.loads[self.key] = SlowStore.loads.get(self.key, 0) + 1
        data = SlowStore.saved.get(self.key)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return data

    async def async_save(self, data: Any) -> None:
        SlowStore.saved[self.key] = data


@pytest.fixture(autouse=True)
def slow_store(monkeypatch: pytest.MonkeyPatch) -> None:
    SlowStore.saved = {}
    SlowStore.loads = {}
    monkeypatch.setattr(backup_module, "Store", SlowStore)


def test_a_reader_loading_alongside_a_writer_keeps_the_written_backup() -> None:
    """(Sourcery) Without the load lock the reader's older disk copy replaced the writer's new snapshot."""
    fixture = load_json_fixture("function_plan.json")
    plan = {"elements": fixture["elements"], "connections": fixture["connections"]}
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)

    async def run() -> None:
        await asyncio.gather(manager.async_load(), manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}))

    asyncio.run(run())
    assert SlowStore.loads[AUTO_KEY] == 1
    assert len(manager._auto_data["1"]["Lights"]) == 1
    assert len(SlowStore.saved[AUTO_KEY]["1"]["Lights"]) == 1
