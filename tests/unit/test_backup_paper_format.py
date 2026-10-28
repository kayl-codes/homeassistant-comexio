"""A plan's paper format/DPI/orientation: backed up on its own, shown in the diff and in a backup's preview."""

import asyncio
from collections.abc import Callable
import copy
from functools import partial
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import (
    coordinator as coordinator_module,
    function_plan_backup as backup_module,
    plan_preview,
)
from custom_components.comexio.api import ComexioAPI, plan_format_settable
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.function_plan_backup import FunctionPlanBackupManager
from custom_components.comexio.function_plan_block_settings import SNAPSHOT_BLOCK_SETTINGS
from custom_components.comexio.function_plan_module_restore import ModuleConfigRestore
from custom_components.comexio.services import backup as backup_service, plan_actions
from tests.common import load_json_fixture

SERVER_ID = "cx1"
A4 = ("A4", 100, "landscape")
A5 = ("A5", 120, "landscape")
SNAPSHOT_SOURCE = "snapshot:auto:1"
PREVIEW_SVG = "<svg/>"


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
    assert backup_module.snapshot_paper_format(history[0]) == A5
    assert backup_module.snapshot_paper_format(history[1]) == A4


def test_a_snapshot_without_a_stored_format_is_no_format_change(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """A snapshot from before the format was tracked: the backfill, not a new backup, gives it one."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: plan}, fub_data))]
        del manager._auto_data["1"]["Lights"][0][backup_module.SNAPSHOT_PAPER_UNKNOWN]
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})))
        return rotated

    assert asyncio.run(run()) == [1, 0]


def test_a_format_readable_again_after_an_unreadable_snapshot_is_backed_up(
    stores: dict[str, Any], plan: dict[str, Any]
) -> None:
    """(Review) otherwise no snapshot ever stores it, and a rebuild from the newest one comes out A3."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> list[int]:
        rotated = [len(await manager.async_auto_backup({1: plan}, fub_data))]
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})))
        rotated.append(len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})))
        return rotated

    assert asyncio.run(run()) == [1, 1, 0]
    history = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert backup_module.snapshot_paper_format(history[0]) == A5


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
        # (Review) damaged values count as not stored instead of raising in diff, preview or restore.
        ({"paper": "A5", "dpi": None}, None),
        ({"paper": "A5", "dpi": "high"}, None),
        ({"paper": "A5", "orientation": "upside"}, None),
        ({"paper": None}, None),
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
    source = (coordinator, None, 7, "Lights", {}, {}, SNAPSHOT_SOURCE, None)
    call = SimpleNamespace(data={"format": "svg", "snapshot": "7:auto:1:Lights"}, return_response=True)
    with (
        patch.object(plan_actions, "_resolve_visualize_snapshot_source", AsyncMock(return_value=source)),
        patch.object(plan_actions.persistent_notification, "async_create"),
    ):
        asyncio.run(plan_actions.handle_function_plan_visualize(SimpleNamespace(), call))  # type: ignore[arg-type]

    coordinator.snapshot_paper_format_for_source.assert_called_once_with(7, "Lights", SNAPSHOT_SOURCE)
    assert coordinator.async_generate_plan_preview.await_args.args[-1] == A5


def test_the_known_format_is_none_while_any_part_is_unread(parsing_api: ComexioAPI) -> None:
    """(Review) the plain getters default to A4/90/landscape — an undecoded $Paper must not read as a change."""
    parsing_api.parse_config(load_json_fixture("config_basic.json"))
    assert parsing_api.get_fub_known_format(2) == ("A3", 120, "portrait")
    assert parsing_api.get_fub_known_format(99) is None

    parsing_api._fub_data["1"]["Orientation"] = 2
    assert parsing_api.get_fub_known_format(1) is None  # neither landscape nor portrait

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
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        # (Review) a legacy snapshot: no format keys and no paper_unknown mark that skips it anyway.
        for key in ("paper", "dpi", "orientation"):
            del manager._auto_data["1"]["Lights"][0][key]
        return await manager.async_backfill_paper_metadata(fub_data, {})

    assert asyncio.run(run()) == 0
    snap = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"][0]
    assert "paper" not in snap
    assert backup_module.SNAPSHOT_PAPER_UNKNOWN not in snap


def test_the_backfill_fills_nothing_once_a_restore_ran_meanwhile(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """(Review) a restore since the format was read may have changed it: stamped, snapshots carry the old one."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        for key in ("paper", "dpi", "orientation"):
            del manager._auto_data["1"]["Lights"][0][key]
        return await manager.async_backfill_paper_metadata(fub_data, {"1": A5}, held_back=lambda: True)

    assert asyncio.run(run()) == 0
    assert "paper" not in manager._auto_data["1"]["Lights"][0]


def test_older_names_under_a_live_id_get_the_fallback_while_its_format_is_unreadable(
    stores: dict[str, Any], plan: dict[str, Any]
) -> None:
    """(Review) only the live identity waits for its format; a former plan under the ID never will."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data)
        manager._auto_data["1"]["Lights"].append({**plan, "hash": "old", "plan_name": "Lights"})
        manager._auto_data["1"]["Former"] = [{**plan, "hash": "former", "plan_name": "Former"}]
        return await manager.async_backfill_paper_metadata(fub_data, {})

    assert asyncio.run(run()) == 1
    identities = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]
    assert backup_module.snapshot_paper_format(identities["Former"][0]) == ("A3", 90, "landscape")
    assert all("paper" not in snap for snap in identities["Lights"])


def test_a_snapshot_taken_while_the_format_was_unreadable_is_never_backfilled(
    stores: dict[str, Any], plan: dict[str, Any]
) -> None:
    """(Review) a later readable format is no statement about the format at capture time."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}
    legacy = {**plan, "hash": "old", "plan_name": "Lights"}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data)
        manager._auto_data["1"]["Lights"].append(legacy)
        return await manager.async_backfill_paper_metadata(fub_data, {"1": A5})

    assert asyncio.run(run()) == 1
    unread, backfilled = stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"]
    assert backup_module.snapshot_paper_format(unread) is None
    assert unread[backup_module.SNAPSHOT_PAPER_UNKNOWN] is True
    assert backup_module.snapshot_paper_format(backfilled) == A5


def _render_coordinator() -> ComexioCoordinator:
    """A coordinator whose plan preview render reaches render_plan_svg with everything around it stubbed."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER_ID
    coordinator._preview_plan_cache = None
    coordinator._preview_cache_generation = 0
    coordinator._connection_values = {}
    coordinator._restart_connection_poll = MagicMock()  # type: ignore[method-assign]
    coordinator._restart_preview_auto_stop = MagicMock()  # type: ignore[method-assign]
    coordinator._connection_poll_fast_requested = False
    coordinator.hass = MagicMock()
    coordinator.function_plan_catalog = SimpleNamespace(async_get_catalog=AsyncMock(return_value={}))
    coordinator._resolve_preview_label_maps = MagicMock(return_value=({}, {}, {}))  # type: ignore[method-assign]
    coordinator._build_preview_title_and_canvas = MagicMock(return_value=("", None))  # type: ignore[method-assign]
    coordinator._resolve_preview_connection_values = AsyncMock(return_value=({}, {}))  # type: ignore[method-assign]
    coordinator.function_plan_knx_label_map = MagicMock(return_value={})  # type: ignore[method-assign]
    coordinator._async_publish_plan_preview = AsyncMock(return_value="/local/x.svg")  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]
    return coordinator


def test_a_backup_render_passes_its_paper_format_to_the_title_and_the_armed_cache() -> None:
    """A dropped argument would render the first frame right and every live-value re-render with the live paper."""
    coordinator = _render_coordinator()

    with (
        patch.object(coordinator_module, "_build_sun_times", return_value={}),
        patch.object(coordinator_module, "expand_autohide_blocks", return_value=({}, {})),
        patch.object(coordinator_module, "render_plan_svg", return_value=PREVIEW_SVG),
    ):
        asyncio.run(coordinator.async_generate_plan_preview(7, "Lights", {}, {}, SNAPSHOT_SOURCE, paper_format=A5))

    coordinator._build_preview_title_and_canvas.assert_called_once_with(7, A5, False)
    assert coordinator._preview_plan_cache["paper_format"] == A5


_LIVE_SETTINGS = {"104": {"in_0": "26"}}
_STORED_SETTINGS = {"104": {"in_0": "10"}}


@pytest.mark.parametrize(
    ("source", "stored", "shown"),
    [
        ("live", None, _LIVE_SETTINGS),
        # The armed re-render of a live preview passes the cached table; the render still shows today's.
        ("live", _STORED_SETTINGS, _LIVE_SETTINGS),
        (SNAPSHOT_SOURCE, _STORED_SETTINGS, _STORED_SETTINGS),
        (SNAPSHOT_SOURCE, None, None),
    ],
    ids=["live", "live-re-render", "snapshot", "snapshot-without-settings"],
)
def test_a_render_shows_the_block_values_of_the_plan_it_draws(
    source: str, stored: dict[str, Any] | None, shown: dict[str, Any] | None
) -> None:
    """A backup's hover lines show the values it stored with its elements, never today's ones."""
    coordinator = _render_coordinator()
    coordinator.api = SimpleNamespace(block_settings=_LIVE_SETTINGS)

    with (
        patch.object(coordinator_module, "_build_sun_times", return_value={}),
        patch.object(coordinator_module, "expand_autohide_blocks", return_value=({}, {})),
        patch.object(coordinator_module, "render_plan_svg", return_value=PREVIEW_SVG) as render,
    ):
        asyncio.run(coordinator.async_generate_plan_preview(7, "Lights", {}, {}, source, block_settings=stored))

    assert render.call_args.kwargs["block_values"] == shown


def test_an_armed_backup_re_render_keeps_the_stored_block_values() -> None:
    """Every live-value re-render of a backup preview renders from the armed cache, not from the slot."""
    coordinator = _render_coordinator()
    coordinator.api = SimpleNamespace(block_settings=_LIVE_SETTINGS)

    async def run() -> None:
        coordinator.preview_follow_lock = asyncio.Lock()
        await coordinator.async_generate_plan_preview(7, "Lights", {}, {}, SNAPSHOT_SOURCE, None, _STORED_SETTINGS)
        await coordinator._render_armed_preview(coordinator._preview_plan_cache)

    with (
        patch.object(coordinator_module, "_build_sun_times", return_value={}),
        patch.object(coordinator_module, "expand_autohide_blocks", return_value=({}, {})),
        patch.object(coordinator_module, "render_plan_svg", return_value=PREVIEW_SVG) as render,
    ):
        asyncio.run(run())

    assert [call.kwargs["block_values"] for call in render.call_args_list] == [_STORED_SETTINGS, _STORED_SETTINGS]


@pytest.mark.parametrize(
    ("source", "expected"),
    [("live", None), ("snapshot:auto:0", A5), (SNAPSHOT_SOURCE, A4), ("snapshot:auto:2", None)],
)
def test_a_preview_source_reads_its_own_slots_paper_format(source: str, expected: tuple | None) -> None:
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    manager._auto_data = {"7": {"Lights": [{"paper": p, "dpi": d, "orientation": o} for p, d, o in (A5, A4)]}}
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.function_plan_backup = manager

    assert coordinator.snapshot_paper_format_for_source(7, "Lights", source) == expected
    assert coordinator.snapshot_paper_format_for_source(7, "Other", source) is None


@pytest.mark.parametrize(
    ("stored", "aligned_to"), [({}, None), ({"paper": "A4", "dpi": 100}, ("A4", 100, "landscape"))]
)
def test_a_restore_onto_the_plan_keeps_its_paper_when_the_backup_stores_none(
    stored: dict[str, Any], aligned_to: tuple | None
) -> None:
    """(Review) without a stored format, "aligning" would switch the live A5 plan to the A3 rebuild default."""
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=A5)
    snapshot = {"elements": {}, "connections": {}, **stored}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Lights", "Lights", False))

    if aligned_to is None:
        api.function_plan_update_paper.assert_not_awaited()
    else:
        api.function_plan_update_paper.assert_awaited_once_with(7, *aligned_to, name="Lights")


def test_a_rebuild_without_a_stored_format_says_it_used_the_default() -> None:
    assert backup_service._rebuild_paper_format({}) == ("A3", 90, "landscape")
    assert "default used" in backup_service._paper_default_note({})
    assert backup_service._rebuild_paper_format({"paper": "A5", "dpi": 120}) == ("A5", 120, "landscape")
    assert backup_service._paper_default_note({"paper": "A5"}) == ""


def test_a_rebuild_never_uses_a_paper_a_plan_cannot_be_set_to() -> None:
    """(Review) Comexio would get A4 instead of the backup's A2 — the spacious default is the safer guess."""
    snapshot = {"paper": "A2", "dpi": 120, "orientation": "portrait"}
    assert backup_service._rebuild_paper_format(snapshot) == ("A3", 90, "landscape")
    assert "A2 @ 120 DPI, portrait cannot be set on a plan" in backup_service._paper_default_note(snapshot)


@pytest.mark.parametrize(("live_name", "renamed"), [("Lights", False), ("Renamed", True)])
def test_a_restore_keeps_the_live_format_when_the_backups_cannot_be_set(live_name: str, renamed: bool) -> None:
    """(Review) restoring an A2 backup must not turn the plan into A4 and report A2 as restored."""
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=A5)
    snapshot = {"elements": {}, "connections": {}, "paper": "A2", "dpi": 90, "orientation": "landscape"}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        apply = asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, live_name, "Lights", False))

    if renamed:
        api.function_plan_update_paper.assert_awaited_once_with(7, *A5, name="Lights")
    else:
        api.function_plan_update_paper.assert_not_awaited()
    assert "A2 @ 90 DPI, landscape cannot be set on a plan" in backup_service._aligned_properties_line(apply, "Lights")


def test_a_restore_onto_a_plan_already_on_the_backups_unsettable_paper_warns_nothing() -> None:
    """(Review) live A2 = backup A2: nothing differs, so no 'cannot be set' warning and no PARTIAL."""
    a2 = ("A2", 90, "landscape")
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=a2)
    snapshot = {"elements": {}, "connections": {}, "paper": "A2", "dpi": 90, "orientation": "landscape"}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        apply = asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Lights", "Lights", False))

    assert apply["unsettable_format"] is None
    assert backup_service._aligned_properties_line(apply, "Lights") == ""


def test_a_restore_that_could_not_set_the_backups_paper_is_partial() -> None:
    apply = {
        "paper_ok": True,
        "unsettable_format": ("A2", 90, "landscape"),
        "block_settings": None,
        "module_configs": ModuleConfigRestore(None),
        "run_ok": True,
    }
    verify = {"reload_ok": True, "counts_match": True}
    with patch.object(backup_service, "_block_settings_ok", return_value=True):
        assert backup_service._restore_status(True, apply, verify) == "PARTIAL"
        assert backup_service._restore_status(True, {**apply, "unsettable_format": None}, verify) == "OK"


def test_a_damaged_stored_format_is_named_unreadable() -> None:
    assert "unreadable" in backup_service._paper_default_note({"paper": "A5", "dpi": None})


@pytest.mark.parametrize(
    ("paper", "dpi", "settable"),
    [("A3", 90, True), ("a4", 45, True), ("A5", 120, True), ("A2", 90, False), ("", 90, False)]
    + [("A4", 44, False), ("A4", 121, False)],
)
def test_which_formats_a_plan_can_be_set_to(paper: str, dpi: int, settable: bool) -> None:
    """(Review) aiocomexio rejects a DPI outside 45-120: such a format is never sent."""
    assert plan_format_settable(paper, dpi) is settable


def test_a_restore_never_sends_a_dpi_comexio_rejects() -> None:
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=A5)
    snapshot = {"elements": {}, "connections": {}, "paper": "A4", "dpi": 300, "orientation": "landscape"}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Renamed", "Lights", False))

    api.function_plan_update_paper.assert_awaited_once_with(7, *A5, name="Lights")
    assert backup_service._rebuild_paper_format(snapshot) == ("A3", 90, "landscape")


def test_a_malformed_stored_format_is_replaced_by_the_readable_one(
    stores: dict[str, Any], plan: dict[str, Any]
) -> None:
    """(Review) no unknown marker and a 'paper' key the backfill skips: only a new backup repairs it."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        manager._auto_data["1"]["Lights"][0]["dpi"] = None
        return len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4}))

    assert asyncio.run(run()) == 1
    assert backup_module.snapshot_paper_format(stores[f"{DOMAIN}_logikplan_auto_{SERVER_ID}"]["1"]["Lights"][0]) == A4


def test_an_auto_backup_that_could_not_be_saved_is_taken_again(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """(Review) left in memory, the unsaved snapshot hid the format change from every later cycle."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=OSError("disk full"))),
            pytest.raises(OSError),
        ):
            await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})
        assert backup_module.snapshot_paper_format(manager._auto_data["1"]["Lights"][0]) == A4
        return len(await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5}))

    assert asyncio.run(run()) == 1


def test_a_failed_auto_backup_save_keeps_a_snapshot_a_restore_promoted_meanwhile(
    stores: dict[str, Any], plan: dict[str, Any]
) -> None:
    """(Review) the rollback took back the snapshot the restore had just marked restored."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def promote_then_fail(_data: Any) -> None:
        manager._auto_data["1"]["Lights"][0]["restored_at"] = "2026-10-14T00:00:00+00:00"
        raise OSError("disk full")

    async def run() -> None:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=promote_then_fail)),
            pytest.raises(OSError),
        ):
            await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})

    asyncio.run(run())
    newest = manager._auto_data["1"]["Lights"][0]
    assert backup_module.snapshot_paper_format(newest) == A5
    assert "restored_at" in newest


def test_an_unsaved_backfill_is_filled_in_again(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """(Review) left in memory, the filled snapshots counted as done and never reached the store."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A5})
        snap = manager._auto_data["1"]["Lights"][0]
        for key in ("paper", "dpi", "orientation"):
            del snap[key]
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=OSError("disk full"))),
            pytest.raises(OSError),
        ):
            await manager.async_backfill_paper_metadata(fub_data, {"1": A5})
        assert "paper" not in snap
        return await manager.async_backfill_paper_metadata(fub_data, {"1": A5})

    assert asyncio.run(run()) == 1


def test_a_promotion_that_could_not_be_saved_is_taken_back(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """(Review) memory showed the snapshot restored while disk and the result said otherwise."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}}

    async def run() -> list[dict[str, Any]]:
        await manager.async_auto_backup({1: plan}, fub_data, plan_format={"1": A4})
        await manager.async_auto_backup({1: {**plan, "elements": {}}}, fub_data, plan_format={"1": A4})
        before = list(manager._auto_data["1"]["Lights"])
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=OSError("disk full"))),
            pytest.raises(OSError),
        ):
            await manager.async_mark_restored("auto", 1, "Lights", before[1])
        return before

    before = asyncio.run(run())
    history = manager._auto_data["1"]["Lights"]
    assert history == before
    assert all("restored_at" not in snap for snap in history)


def test_a_keep_record_that_could_not_be_saved_never_fails_the_lineage_move(
    stores: dict[str, Any], plan: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """(Review) a failing keep-store save made a saved move read as "backups not moved"."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)

    async def run() -> None:
        await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}, plan_format={"1": A4})
        manager._kept.add((1, "Lights"))
        with patch.object(manager, "_async_save_kept", AsyncMock(side_effect=OSError("disk full"))):
            await manager.async_rekey_fub_id(1, 5, "Lights")

    asyncio.run(run())
    assert len(manager._auto_data["5"]["Lights"]) == 1
    assert (1, "Lights") not in manager._kept
    assert "keep decision for 'Lights' (fub=1) not saved" in caplog.text


@pytest.mark.parametrize("old_id_still_live", [False, True])
def test_a_lineage_move_that_could_not_be_saved_is_put_back(
    stores: dict[str, Any], plan: dict[str, Any], old_id_still_live: bool
) -> None:
    """(Review) memory showed the backups moved while the store still had them under the old ID."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)

    move = (
        partial(manager.async_purge_identity, 1, "Lights")
        if old_id_still_live
        else partial(manager.async_rekey_fub_id, 1, 5, "Lights")
    )

    async def run() -> None:
        await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}, plan_format={"1": A4})
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=OSError("disk full"))),
            pytest.raises(OSError),
        ):
            await move()

    asyncio.run(run())
    assert len(manager._auto_data["1"]["Lights"]) == 1
    assert "5" not in manager._auto_data


# Public coroutines that only read the stores (or are serialized through another name).
UNSERIALIZED_BACKUP_READERS = {
    "async_list_backups",
    "async_load",
    "async_backed_up_plans",
    "async_referenced_marker_ids",
    "async_get_snapshot",
}


def test_every_backup_store_write_is_serialized() -> None:
    """A new writing method without @_serialized would bring back the lost/undone writes (#176)."""
    unserialized = {
        name
        for name, method in inspect.getmembers(FunctionPlanBackupManager, inspect.iscoroutinefunction)
        if name.startswith("async_") and not hasattr(method, "__wrapped__")
    }
    assert unserialized == UNSERIALIZED_BACKUP_READERS


@pytest.mark.parametrize("second", ["change_backup", "delete"])
def test_a_backup_write_during_a_failing_lineage_move_survives_its_put_back(
    stores: dict[str, Any], plan: dict[str, Any], second: str
) -> None:
    """(Review) the move's put-back restored the whole fub_id bucket over a write made during its save.

    A pre-change backup taken meanwhile was lost; a deletion meanwhile came back.
    """
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    in_save = asyncio.Event()
    release = asyncio.Event()
    real_save = manager._change_store.async_save
    calls = 0

    async def save(data: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the move's save: fails once the other write had its chance to run
            in_save.set()
            await release.wait()
            raise OSError("disk full")
        await real_save(data)

    async def run() -> None:
        await manager.async_change_backup(1, plan, "Lights", "sort")
        with patch.object(manager._change_store, "async_save", save):
            await manager.async_change_backup(1, plan, "Lights", "sort")  # call 1: a plain save
            move = asyncio.create_task(manager.async_rekey_fub_id(1, 5, "Lights"))
            await in_save.wait()
            other = asyncio.create_task(
                manager.async_change_backup(1, plan, "Lights", "connect")
                if second == "change_backup"
                else manager.async_delete_plan_backups(1, "Lights")
            )
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()
            with pytest.raises(OSError):
                await move
            await other

    asyncio.run(run())
    assert "5" not in manager._change_data
    if second == "change_backup":
        assert [s["operation"] for s in manager._change_data["1"]["Lights"]][0] == "connect"
    else:
        assert "1" not in manager._change_data


@pytest.mark.parametrize("old_id_still_live", [False, True])
def test_a_lineage_move_the_second_store_could_not_save_is_taken_back_in_both(
    stores: dict[str, Any], plan: dict[str, Any], old_id_still_live: bool
) -> None:
    """(Review) saved in the auto store only, the plan's backups sat under two IDs."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    move = (
        partial(manager.async_purge_identity, 1, "Lights")
        if old_id_still_live
        else partial(manager.async_rekey_fub_id, 1, 5, "Lights")
    )
    auto_on_disk: list[dict[str, Any]] = []

    async def record(data: dict[str, Any]) -> None:
        auto_on_disk.append(copy.deepcopy(data))

    async def run() -> None:
        await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}, plan_format={"1": A4})
        await manager.async_change_backup(1, plan, "Lights", "edit", paper="A4", dpi=100, orientation="landscape")
        with (
            patch.object(manager._auto_store, "async_save", AsyncMock(side_effect=record)),
            patch.object(manager._change_store, "async_save", AsyncMock(side_effect=OSError("disk full"))),
            pytest.raises(OSError),
        ):
            await move()

    asyncio.run(run())
    for data in (manager._auto_data, manager._change_data):
        assert len(data["1"]["Lights"]) == 1
        assert "5" not in data
    # Saved by the move, then saved again as it was before.
    assert len(auto_on_disk) == 2
    assert auto_on_disk[-1]["1"]["Lights"]
    assert "5" not in auto_on_disk[-1]


def test_a_restore_never_renames_a_plan_whose_live_paper_cannot_be_set() -> None:
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=("A2", 90, "landscape"))
    snapshot = {"elements": {}, "connections": {}}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        same = asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Lights", "Lights", False))
        renamed = asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Renamed", "Lights", False))

    api.function_plan_update_paper.assert_not_awaited()
    assert same["properties_changed"] is False
    assert renamed["paper_ok"] is False


def test_a_backups_paper_outside_the_size_table_is_sized_from_its_paper_entry(parsing_api: ComexioAPI) -> None:
    parsing_api.parse_config(load_json_fixture("config_basic.json"))
    parsing_api._paper_data["9"] = {"Name": "Letter", "MMX": 279, "MMY": 216}
    live = parsing_api.get_fub_canvas_bounds(1)

    letter = parsing_api.get_fub_canvas_bounds(1, paper_name="Letter")

    parsing_api._fub_data["1"]["Paper"] = "9"
    assert letter == parsing_api.get_fub_canvas_bounds(1) != live


def test_a_restore_never_renames_a_plan_whose_format_nobody_states() -> None:
    """(Review) Comexio saves name and format together: a rename would write the A4/90 getter defaults."""
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=None)
    snapshot = {"elements": {}, "connections": {}}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        apply = asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Renamed", "Lights", False))

    api.function_plan_update_paper.assert_not_awaited()
    assert apply["properties_changed"] is True
    assert apply["paper_ok"] is False
    assert "rename the plan in Comexio Studio" in backup_service._aligned_properties_line(apply, "Lights")


def test_an_unknown_paper_name_is_never_sized_like_the_live_plan(parsing_api: ComexioAPI) -> None:
    """(Review) plan 2 is A3 portrait live; a backup's 'Tabloid' (in neither table) gets the reference size."""
    parsing_api.parse_config(load_json_fixture("config_basic.json"))

    bounds = parsing_api.get_fub_canvas_bounds(2, paper_name="Tabloid", orientation="landscape", resolution=90)

    assert bounds == (parsing_api._CANVAS_REF_X, parsing_api._CANVAS_REF_Y)


def test_a_backup_of_a_deleted_plan_shows_its_own_format() -> None:
    """(Review) the plan is gone, so there is no state to show — but the backup's format still is."""
    coordinator = _preview_coordinator()
    coordinator.api.get_fub_active = lambda _fub_id: None

    assert coordinator._build_preview_title_and_canvas(7, A5) == ("[A5 | 120dpi]", (1.0, 2.0))
    coordinator.api.get_fub_canvas_bounds.assert_called_once_with(
        7, paper_name="A5", orientation="landscape", resolution=120
    )
    assert coordinator._build_preview_title_and_canvas(7) == ("", None)


@pytest.mark.parametrize(
    ("stored", "title", "canvas"),
    [
        (
            {
                "paper": "A5",
                "dpi": 120,
                "captured_at": "2026-01-02T03:04:05+00:00",
                SNAPSHOT_BLOCK_SETTINGS: _STORED_SETTINGS,
            },
            "[verwaist] [A5 | 120dpi]",
            (1.0, 2.0),
        ),
        ({}, "[verwaist]", None),  # also an old snapshot without captured_at
    ],
)
def test_the_preview_of_an_orphaned_backup_uses_its_paper_format(
    stored: dict[str, Any], title: str, canvas: tuple | None
) -> None:
    coordinator = _preview_coordinator()
    coordinator.server_id = SERVER_ID
    coordinator.hass = MagicMock()
    coordinator._preview_cache_generation = 0
    coordinator.stop_preview = MagicMock()  # type: ignore[method-assign]
    snapshot = {"elements": {}, "connections": {}, **stored}
    coordinator.function_plan_backup = SimpleNamespace(async_get_snapshot=AsyncMock(return_value=snapshot))
    coordinator.function_plan_catalog = SimpleNamespace(async_get_catalog=AsyncMock(return_value={}))
    coordinator._resolve_preview_label_maps = MagicMock(return_value=({}, {}, {}))  # type: ignore[method-assign]
    coordinator.function_plan_knx_label_map = MagicMock(return_value={})  # type: ignore[method-assign]
    coordinator._async_publish_plan_preview = AsyncMock(return_value="/local/x.svg")  # type: ignore[method-assign]
    coordinator.async_update_listeners = MagicMock()  # type: ignore[method-assign]

    with (
        patch.object(coordinator_module, "_build_sun_times", return_value={}),
        patch.object(coordinator_module, "expand_autohide_blocks", return_value=({}, {})),
        patch.object(coordinator_module, "render_plan_svg", return_value=PREVIEW_SVG) as render,
    ):
        asyncio.run(coordinator.async_generate_orphaned_plan_preview(7, "Lights", "auto", 0))

    assert render.call_args.args[7] == title
    assert render.call_args.kwargs["canvas"] == canvas
    assert render.call_args.kwargs["block_values"] == stored.get(SNAPSHOT_BLOCK_SETTINGS)
    # comexio-plan-card.js (orphanPreviewMatch) matches the shown image by source and capture time.
    coordinator._async_publish_plan_preview.assert_awaited_once_with(
        7, "Lights", "orphan:auto:0", PREVIEW_SVG, captured_at=stored.get("captured_at")
    )


def _orphan_load_coordinator(options: list[tuple[str, dict[str, Any]]]) -> ComexioCoordinator:
    """A coordinator whose orphaned_plan_backups repair switches both selectors to the first option's label."""
    coordinator = _preview_coordinator()
    coordinator.server_id = SERVER_ID
    coordinator.preview_follow_lock = asyncio.Lock()
    coordinator.preview_follow_generation = 3
    coordinator.orphaned_backup_options = MagicMock(return_value=options)  # type: ignore[method-assign]
    coordinator.orphaned_plans_view_active = MagicMock(return_value=True)  # type: ignore[method-assign]
    coordinator.hass = MagicMock()
    coordinator.hass.services.async_call = AsyncMock()
    coordinator.hass.states.get = MagicMock(return_value=SimpleNamespace(state=options[0][0]))
    return coordinator


_ORPHAN_ROW = {"plan_row": True, "fub_id": 7, "plan_name": "Lights", "kind": "auto", "slot": 0}


def test_loading_an_orphaned_plan_renders_in_turn_with_the_selection_follows() -> None:
    """(Review #233) Under the follows' lock and superseding queued follows: no older pick publishes last."""
    coordinator = _orphan_load_coordinator([("Lights", _ORPHAN_ROW)])
    seen: dict[str, Any] = {}

    async def render(*args: Any) -> str:
        seen.update(locked=coordinator.preview_follow_lock.locked(), generation=coordinator.preview_follow_generation)
        seen["args"] = args
        return "/local/x.svg"

    coordinator.async_generate_orphaned_plan_preview = render  # type: ignore[method-assign]
    registry = SimpleNamespace(async_get_entity_id=lambda _domain, _platform, unique_id: f"select.{unique_id}")
    with patch.object(coordinator_module.er, "async_get", return_value=registry):
        loaded = asyncio.run(coordinator.async_load_orphaned_plan_into_preview(7, "Lights"))

    assert loaded is True
    assert seen == {"locked": True, "generation": 4, "args": (7, "Lights", "auto", 0)}


def test_loading_an_orphaned_plan_reads_the_backup_choice_once_it_holds_the_lock() -> None:
    """(Review #233) A delete while the load waited for the lock: no render of a shifted slot."""
    coordinator = _orphan_load_coordinator([("Lights", _ORPHAN_ROW)])
    coordinator.async_generate_orphaned_plan_preview = AsyncMock()  # type: ignore[method-assign]
    registry = SimpleNamespace(async_get_entity_id=lambda _domain, _platform, unique_id: f"select.{unique_id}")

    async def load_while_a_follow_renders() -> bool:
        async with coordinator.preview_follow_lock:
            task = asyncio.create_task(coordinator.async_load_orphaned_plan_into_preview(7, "Lights"))
            await asyncio.sleep(0)  # the load passes the selector checks and waits for the lock
            assert not task.done()  # else it never read the choice under the lock
            coordinator.orphaned_backup_options.return_value = []  # the backup is deleted meanwhile
        return await task

    with patch.object(coordinator_module.er, "async_get", return_value=registry):
        loaded = asyncio.run(load_while_a_follow_renders())

    assert loaded is False
    coordinator.async_generate_orphaned_plan_preview.assert_not_awaited()
    assert coordinator.preview_follow_generation == 3  # nothing rendered: the queued follow is not superseded


@pytest.mark.parametrize(
    "select_another",
    [
        pytest.param(
            lambda coordinator: setattr(coordinator.hass.states.get, "return_value", SimpleNamespace(state="Garage")),
            id="another backup picked",
        ),
        pytest.param(
            lambda coordinator: setattr(coordinator.orphaned_plans_view_active, "return_value", False),
            id="orphaned-plans view left",
        ),
    ],
)
def test_loading_an_orphaned_plan_leaves_a_newer_selection_to_its_own_follow(
    select_another: Callable[[Any], None],
) -> None:
    """(Review #233) Another pick while the load waited for the lock: neither rendered nor superseded."""
    coordinator = _orphan_load_coordinator([("Lights", _ORPHAN_ROW)])
    coordinator.async_generate_orphaned_plan_preview = AsyncMock()  # type: ignore[method-assign]
    registry = SimpleNamespace(async_get_entity_id=lambda _domain, _platform, unique_id: f"select.{unique_id}")

    async def load_while_a_follow_renders() -> bool:
        async with coordinator.preview_follow_lock:
            task = asyncio.create_task(coordinator.async_load_orphaned_plan_into_preview(7, "Lights"))
            await asyncio.sleep(0)  # the load passes the selector checks and waits for the lock
            assert not task.done()
            select_another(coordinator)
        return await task

    with patch.object(coordinator_module.er, "async_get", return_value=registry):
        loaded = asyncio.run(load_while_a_follow_renders())

    assert loaded is False
    coordinator.async_generate_orphaned_plan_preview.assert_not_awaited()
    assert coordinator.preview_follow_generation == 3


@pytest.mark.parametrize(
    ("svg", "source", "captured_at", "stamped"),
    [
        (
            '<svg xmlns="http://www.w3.org/2000/svg"><svg x="1"/></svg>',
            "orphan:auto:0",
            "2026-01-02T03:04:05+00:00",
            '<svg data-comexio-source="orphan:auto:0" data-comexio-captured-at="2026-01-02T03:04:05+00:00"'
            ' xmlns="http://www.w3.org/2000/svg"><svg x="1"/></svg>',
        ),
        (PREVIEW_SVG, "orphan:auto:0", None, '<svg data-comexio-source="orphan:auto:0"/>'),
        # Empty like None: the card compares `captured_at || null` with the missing attribute.
        (PREVIEW_SVG, "live", "", '<svg data-comexio-source="live"/>'),
        (
            '<?xml version="1.0"?>\n<svg>',
            SNAPSHOT_SOURCE,
            None,
            '<?xml version="1.0"?>\n<svg data-comexio-source="snapshot:auto:1">',
        ),
        (
            "<svg>",
            "orphan:auto:0",
            '"x"',
            '<svg data-comexio-source="orphan:auto:0" data-comexio-captured-at="&quot;x&quot;">',
        ),
        # Only the root tag: an <svg inside a leading comment is not stamped in its place.
        ("<!-- <svg --><svg>", "live", None, "<!-- <svg --><svg>"),
        ("<!DOCTYPE svg><svg>", "live", None, "<!DOCTYPE svg><svg>"),
    ],
)
def test_a_published_preview_names_its_render_on_the_svg_root(
    svg: str, source: str, captured_at: str | None, stamped: str
) -> None:
    """(Review #233) The card trusts the entity's identity only for an image naming that render."""
    assert coordinator_module.stamp_preview_identity(svg, source, captured_at) == stamped


@pytest.mark.parametrize(("svg", "warned"), [(PREVIEW_SVG, False), ("<!-- x --><svg/>", True)])
def test_a_preview_without_a_root_tag_to_stamp_is_published_with_a_warning(
    svg: str, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """(Review #233) Unstamped, the plan card keeps warning before a delete: the log says why."""
    coordinator = _preview_coordinator()
    coordinator.server_id = SERVER_ID
    coordinator.hass = MagicMock()
    coordinator.hass.config.path = lambda *parts: "/".join(("/config", *parts))
    coordinator.hass.async_add_executor_job = AsyncMock()

    with caplog.at_level("WARNING", logger=coordinator_module.__name__):
        asyncio.run(coordinator._async_publish_plan_preview(7, "Lights", "live", svg))

    written = coordinator.hass.async_add_executor_job.await_args.args[2]
    assert ("no <svg> root tag" in caplog.text) is warned
    assert (written == svg) is warned  # unstamped, but published unchanged all the same
    assert coordinator.last_plan_preview_svg == written


def test_a_backup_preview_without_a_known_format_names_none() -> None:
    """(Review) sized like the live plan, but the live "[A4 | 100dpi]" is no statement about the backup."""
    coordinator = _preview_coordinator()

    assert coordinator._build_preview_title_and_canvas(7, None, live=False) == ("[aktiv]", (1.0, 2.0))


def test_a_restore_renaming_the_plan_keeps_its_live_format_when_the_backup_stores_none() -> None:
    api = AsyncMock()
    api.get_fub_known_format = MagicMock(return_value=A5)
    snapshot = {"elements": {}, "connections": {}}

    sanitized = AsyncMock(return_value=(snapshot, {}, None))
    with patch.object(backup_service, "_sanitize_missing_comments", sanitized):
        asyncio.run(backup_service._restore_apply_snapshot(api, 7, snapshot, "Renamed", "Lights", False))

    api.function_plan_update_paper.assert_awaited_once_with(7, *A5, name="Lights")


def test_auto_backup_skips_a_plan_whose_list_entry_is_unreadable(stores: dict[str, Any], plan: dict[str, Any]) -> None:
    """One malformed $Fubs entry must neither abort every plan's backup nor store a plan as (id, "id")."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)
    fub_data = {"1": {"Name": "Lights"}, "2": "garbage", "3": {"Id": 4, "Name": "Pumps"}}

    rotated = asyncio.run(manager.async_auto_backup({1: plan, 2: plan, 3: plan}, fub_data))

    assert len(rotated) == 1
    assert set(manager._auto_data) == {"1"}


@pytest.mark.parametrize(
    ("live_entries", "filled"),
    [({"1": {"Id": 2, "Name": "Lights"}}, 0), ({"01": {"Name": "Lights"}}, 0), ({}, 1)],
    ids=["id-differs-from-key", "key-not-canonical", "deleted"],
)
def test_the_backfill_waits_while_a_plan_may_hide_behind_an_unreadable_entry(
    stores: dict[str, Any], plan: dict[str, Any], live_entries: dict, filled: int
) -> None:
    """Read as deleted, the live plan's legacy snapshots would get a guessed format a restore rebuilds."""
    manager = FunctionPlanBackupManager(MagicMock(), SERVER_ID, None)

    async def run() -> int:
        await manager.async_auto_backup({1: plan}, {"1": {"Name": "Lights"}}, plan_format={"1": A4})
        for key in ("paper", "dpi", "orientation"):
            del manager._auto_data["1"]["Lights"][0][key]
        return await manager.async_backfill_paper_metadata(live_entries, {})

    assert asyncio.run(run()) == filled
    assert ("paper" in manager._auto_data["1"]["Lights"][0]) is bool(filled)
