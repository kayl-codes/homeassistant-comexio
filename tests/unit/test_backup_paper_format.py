"""A plan's paper format/DPI/orientation: backed up on its own, shown in the diff and in a backup's preview."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import function_plan_backup as backup_module, plan_preview
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from custom_components.comexio.services import backup as backup_service, plan_actions
from tests.common import load_json_fixture

SERVER_ID = "cx1"
A4 = ("A4", 100, "landscape")
A5 = ("A5", 120, "landscape")


@pytest.fixture
def plan() -> dict[str, Any]:
    fixture = load_json_fixture("function_plan.json")
    return {"elements": fixture["elements"], "connections": fixture["connections"]}


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store, keyed by storage key."""

    saved: dict[str, Any] = {}

    def __init__(self, _hass: Any, _version: int, key: str) -> None:
        self.key = key

    async def async_load(self) -> Any:
        return FakeStore.saved.get(self.key)

    async def async_save(self, data: Any) -> None:
        FakeStore.saved[self.key] = data


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    FakeStore.saved = {}
    monkeypatch.setattr(backup_module, "Store", FakeStore)
    return FakeStore.saved


def test_auto_backup_rotates_on_a_paper_format_change_alone(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """Seen live 2026-10-13: switching a plan to A5/120 DPI made no backup, so a restore came back A4."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4}))]
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})))
        # Not read this poll: no change.
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data)))
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})))
        return rotated

    assert asyncio.run(run()) == [1, 0, 0, 1]
    history = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert (history[0]["paper"], history[0]["dpi"], history[0]["orientation"]) == A5
    assert (history[1]["paper"], history[1]["dpi"], history[1]["orientation"]) == A4


def test_a_snapshot_without_a_stored_format_is_no_format_change(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: plan}, fub_data))]
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})))
        return rotated

    assert asyncio.run(run()) == [1, 0]


def test_the_backup_diff_names_a_changed_paper_format(plan: dict[str, Any]) -> None:
    snapshots = {
        0: {**plan, "paper": "A5", "dpi": 120, "orientation": "landscape"},
        1: {**plan, "paper": "A4", "dpi": 100, "orientation": "landscape"},
        2: {**plan, "paper": "A4", "dpi": 100, "orientation": "landscape"},
        3: dict(plan),
    }

    async def get_snapshot(_kind: str, _fub_id: int, _name: str, slot: int) -> dict[str, Any] | None:
        return snapshots.get(slot)

    coordinator = SimpleNamespace(
        function_plan_label_maps=lambda: ({}, {}, {}),
        function_plan_catalog=SimpleNamespace(async_get_catalog=AsyncMock(return_value={})),
        function_plan_backup=SimpleNamespace(async_get_snapshot=get_snapshot),
    )
    entries = [{"fub_id": 1, "plan_name": "Lights", "slot": slot} for slot in range(3)]

    asyncio.run(backup_service._attach_backup_diffs(coordinator, entries, "auto"))

    assert entries[0]["diff"]["paper_format"] == ["A4 @ 100 DPI, landscape → A5 @ 120 DPI, landscape"]
    assert entries[1]["diff"]["paper_format"] == []
    # The predecessor was captured before the format was stored: nothing to compare.
    assert entries[2]["diff"]["paper_format"] == []


def test_canvas_bounds_take_a_backups_resolution(parsing_api: ComexioAPI) -> None:
    parsing_api.parse_config(load_json_fixture("config_basic.json"))

    assert parsing_api.get_fub_canvas_bounds(1, paper_name="A5", orientation="landscape", resolution=120) == (
        pytest.approx(870.0 * 210 / 297 * 120 / 90),
        pytest.approx(720.0 * 148 / 210 * 120 / 90),
    )


def _preview_coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = SimpleNamespace(
        get_fub_active=lambda _fub_id: True,
        get_fub_paper_format=lambda _fub_id: "A4",
        get_fub_dpi=lambda _fub_id: 100,
        get_fub_canvas_bounds=MagicMock(return_value=(1.0, 2.0)),
    )
    return coordinator


def test_a_backup_preview_shows_the_backups_paper_format() -> None:
    """Seen live 2026-10-13: an A4 backup of a plan switched to A5 was shown as A5."""
    coordinator = _preview_coordinator()

    suffix, canvas = coordinator._build_preview_title_and_canvas(7, ("A4", 90, "portrait"))

    assert suffix == "[aktiv] [A4 | 90dpi]"
    assert canvas == (1.0, 2.0)
    coordinator.api.get_fub_canvas_bounds.assert_called_once_with(
        7, paper_name="A4", orientation="portrait", resolution=90
    )


def test_a_live_preview_shows_the_live_paper_format() -> None:
    coordinator = _preview_coordinator()

    suffix, _canvas = coordinator._build_preview_title_and_canvas(7)

    assert suffix == "[aktiv] [A4 | 100dpi]"
    coordinator.api.get_fub_canvas_bounds.assert_called_once_with(7)


def test_a_re_render_of_an_armed_backup_keeps_its_paper_format() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.preview_follow_lock = asyncio.Lock()
    coordinator._preview_plan_cache = None
    coordinator._preview_cache_generation = 0
    coordinator._restart_connection_poll = MagicMock()  # type: ignore[method-assign]
    coordinator._restart_preview_auto_stop = MagicMock()  # type: ignore[method-assign]
    coordinator._connection_poll_fast_requested = False
    coordinator._update_snapshot_preview_cache(7, "Lights", {}, {}, "auto", 1, None, None, {}, A4)
    cache = coordinator._preview_plan_cache

    with patch.object(coordinator, "async_generate_plan_preview", AsyncMock()) as render:
        asyncio.run(coordinator._render_armed_preview(cache))

    assert render.await_args.args[-1] == A4


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({}, None),
        ({"paper": "A5"}, ("A5", 90, "landscape")),
        ({"paper": "A3", "dpi": 120, "orientation": "portrait"}, ("A3", 120, "portrait")),
    ],
)
def test_snapshot_paper_format(snapshot: dict[str, Any], expected: tuple | None) -> None:
    assert backup_module.snapshot_paper_format(snapshot) == expected


def test_a_live_render_after_a_backup_drops_the_backups_paper_format() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator._preview_plan_cache = None
    coordinator._preview_cache_generation = 0
    coordinator._restart_connection_poll = MagicMock()  # type: ignore[method-assign]
    coordinator._restart_preview_auto_stop = MagicMock()  # type: ignore[method-assign]
    coordinator._connection_poll_fast_requested = False
    coordinator._update_snapshot_preview_cache(7, "Lights", {}, {}, "auto", 1, None, None, {}, A4)

    coordinator._update_live_preview_cache(7, "Lights", {}, {}, plan_switched=False)

    assert coordinator._preview_plan_cache["paper_format"] is None


@pytest.mark.parametrize(("stored", "expected"), [({"paper": "A4", "dpi": 90}, ("A4", 90, "landscape")), ({}, None)])
def test_the_cards_backup_selection_renders_with_the_backups_paper_format(
    stored: dict[str, Any], expected: tuple | None
) -> None:
    snapshot = {"elements": {}, "connections": {}, **stored}
    coordinator = SimpleNamespace(
        orphaned_plans_view_active=lambda: False,
        get_active_function_plan_fub_id=lambda: 7,
        api=SimpleNamespace(fub_data={"7": {"Name": "Lights"}}),
        function_plan_backup=SimpleNamespace(async_get_snapshot=AsyncMock(return_value=snapshot)),
        async_generate_plan_preview=AsyncMock(),
    )
    with patch.object(plan_preview, "_active_backup_choice", return_value=("auto", 1)):
        asyncio.run(plan_preview.async_render_selected_preview(coordinator))  # type: ignore[arg-type]

    assert coordinator.async_generate_plan_preview.await_args.args[-1] == expected


def test_the_svg_visualize_of_a_backup_renders_with_its_paper_format() -> None:
    coordinator = SimpleNamespace(
        preview_follow_lock=asyncio.Lock(),
        preview_follow_generation=0,
        snapshot_block_settings_for_source=MagicMock(return_value=None),
        snapshot_paper_format_for_source=MagicMock(return_value=A5),
        async_generate_plan_preview=AsyncMock(return_value="/local/preview.svg"),
    )
    source = (coordinator, None, 7, "Lights", {}, {}, "snapshot:auto:1", None)
    call = SimpleNamespace(data={"format": "svg", "snapshot": "7:auto:1:Lights"}, return_response=True)
    with (
        patch.object(plan_actions, "_resolve_visualize_snapshot_source", AsyncMock(return_value=source)),
        patch.object(plan_actions.persistent_notification, "async_create"),
    ):
        asyncio.run(plan_actions.handle_function_plan_visualize(SimpleNamespace(), call))  # type: ignore[arg-type]

    coordinator.snapshot_paper_format_for_source.assert_called_once_with(7, "Lights", "snapshot:auto:1")
    assert coordinator.async_generate_plan_preview.await_args.args[-1] == A5


def test_the_known_format_is_none_while_any_part_is_unread(parsing_api: ComexioAPI) -> None:
    """(Review) the plain getters default to A4/90/landscape — an undecoded $Paper must not read as a change."""
    parsing_api.parse_config(load_json_fixture("config_basic.json"))
    assert parsing_api.get_fub_known_format(2) == ("A3", 120, "portrait")
    assert parsing_api.get_fub_known_format(99) is None

    parsing_api._paper_data = {}
    assert parsing_api.get_fub_known_format(2) is None
    assert parsing_api.get_fub_paper_format(2) == "A4"  # the guess the backups no longer take


def test_the_backup_cycle_leaves_out_plans_of_unknown_format() -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = SimpleNamespace(get_fub_known_format=lambda fub_id: A5 if fub_id == 1 else None)
    assert coordinator._current_plan_format({"1": {}, "2": {}}) == {"1": A5}


def test_the_backfill_waits_for_a_live_plans_format(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data)
        return await manager.async_backfill_paper_metadata(fub_data, {})

    assert asyncio.run(run()) == 0
    assert "paper" not in stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"][0]
