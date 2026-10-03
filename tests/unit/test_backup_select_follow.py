"""Plan card selectors: a row that vanishes under an open card moves the selection and the preview along."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.comexio import select
from custom_components.comexio.const import FUNCTION_PLAN_ORPHANED_VIEW_OPTION


def _plan_row(fub_id: int, name: str, count: int = 2) -> tuple[str, dict]:
    return f"{name} (ID {fub_id}) — {count} backups", {"fub_id": fub_id, "plan_name": name, "plan_row": True}


def _snapshot_row(fub_id: int, name: str) -> tuple[str, dict]:
    return f"auto[0] {name} 2026-10-01", {"fub_id": fub_id, "plan_name": name, "plan_row": False}


def _plan_with_snapshot(fub_id: int, name: str) -> list[tuple[str, dict]]:
    return [_plan_row(fub_id, name), _snapshot_row(fub_id, name)]


class _Harness:
    """A 'Backup' selector over a mutable orphaned-plans view; updates write the real select state.

    Built via __new__ (no HA): mirror new attributes of ComexioPlanBackupSelectEntity.__init__ here.
    """

    def __init__(self, rows: list[tuple[str, dict]]) -> None:
        self.rows = rows
        self.available = True
        self.written: list[str | None] = [None]
        entity = select.ComexioPlanBackupSelectEntity.__new__(select.ComexioPlanBackupSelectEntity)
        entity._selected = None
        entity._last_orphan = None
        entity._orphan_plans = []
        entity._last_shown_row = None
        entity._shown_plan = None
        entity.entity_id = "select.iosrv1_function_plan_backup"
        entity.coordinator = SimpleNamespace(
            orphaned_plans_view_active=lambda: True,
            orphaned_backup_options=lambda: list(self.rows),
            orphaned_backup_choice=lambda label: next((c for lbl, c in self.rows if lbl == label), None),
        )
        entity.hass = SimpleNamespace(
            states=SimpleNamespace(
                get=lambda _eid: None if self.written[0] is None else SimpleNamespace(state=self.written[0])
            )
        )
        self.entity = entity

    def update(self) -> MagicMock:
        def write_state(entity: select.ComexioPlanBackupSelectEntity) -> None:
            # What async_write_ha_state writes for a select: its option, "unknown" for none.
            self.written[0] = (entity.state or STATE_UNKNOWN) if self.available else STATE_UNAVAILABLE

        with (
            patch.object(select.CoordinatorEntity, "_handle_coordinator_update", write_state),
            patch.object(select, "_follow_in_preview") as follow,
        ):
            self.entity._handle_coordinator_update()
        return follow

    def plan_changed(self, plan_state: str) -> MagicMock:
        """The 'Plan' selector wrote plan_state."""
        event = SimpleNamespace(data={"new_state": SimpleNamespace(state=plan_state)})
        with (
            patch.object(self.entity, "async_write_ha_state", self.update_written, create=True),
            patch.object(select, "_follow_in_preview") as follow,
        ):
            self.entity._handle_plan_change(event)  # type: ignore[arg-type]
        return follow

    def update_written(self) -> None:
        self.written[0] = self.entity.state or STATE_UNKNOWN

    def select(self, label: str) -> None:
        with patch.object(self.entity, "async_write_ha_state", create=True), patch.object(select, "_follow_in_preview"):
            asyncio.run(self.entity.async_select_option(label))
        self.written[0] = label


def _showing(selected: tuple[str, dict], rows: list[tuple[str, dict]]) -> _Harness:
    harness = _Harness(rows)
    harness.entity._selected = selected[0]
    harness.update().assert_not_called()  # first write after start: nothing to follow
    assert harness.written[0] == selected[0]
    return harness


_A, _B, _C, _D = (7, "Old plan"), (9, "Other plan"), (11, "Third plan"), (13, "Fourth plan")


def test_a_deleted_shown_snapshot_moves_the_preview_to_the_plan_row() -> None:
    """Live finding (03.10.): the selector fell back to the plan row, the card kept the deleted snapshot."""
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.rows = [_plan_row(*_A, count=1), _plan_row(*_B)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_A, count=1)[0]
    follow.assert_called_once()


def test_deleting_all_backups_of_the_shown_plan_moves_to_the_next_plan() -> None:
    """Live finding (03.10.): after "delete all" the selector showed unknown and the card the deleted plan."""
    harness = _showing(_snapshot_row(*_B), [_plan_row(*_A), *_plan_with_snapshot(*_B), _plan_row(*_C)])
    harness.rows = [_plan_row(*_A), _plan_row(*_C)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_C)[0]
    follow.assert_called_once()


def test_the_neighbour_follows_the_plan_order_seen_when_picking_a_plan() -> None:
    """A plan picked before any update saw it in the view still has its neighbours."""
    harness = _showing(_plan_row(*_A), [_plan_row(*_A), _plan_row(*_B)])
    harness.rows = [_plan_row(*_A), _plan_row(*_B), _plan_row(*_C)]
    harness.select(_plan_row(*_C)[0])
    harness.rows = [_plan_row(*_A), _plan_row(*_B)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_B)[0]
    follow.assert_called_once()


def test_no_known_plan_left_falls_back_to_the_first_row() -> None:
    """The shown plan emptied while a new deleted plan appeared in the same poll: never stuck on unknown."""
    harness = _showing(_plan_row(*_A), [_plan_row(*_A)])
    harness.rows = [_plan_row(*_C)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_C)[0]
    follow.assert_called_once()


def test_deleting_all_backups_of_the_last_plan_moves_to_the_previous_plan() -> None:
    harness = _showing(_plan_row(*_C), [_plan_row(*_A), _plan_row(*_B), _plan_row(*_C)])
    harness.rows = [_plan_row(*_A), _plan_row(*_B)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_B)[0]
    follow.assert_called_once()


def test_deleting_the_only_deleted_plan_leaves_no_row() -> None:
    """The 'Plan' selector then leaves the view (test below); this selector has nothing to show."""
    harness = _showing(_plan_row(*_A), [_plan_row(*_A)])
    harness.rows = []
    follow = harness.update()
    assert harness.written[0] == STATE_UNKNOWN
    follow.assert_not_called()


def test_an_update_keeping_the_shown_row_renders_nothing() -> None:
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.rows.append(_plan_row(*_C))
    harness.update().assert_not_called()


@pytest.mark.parametrize("recovers", [False, True], ids=["poll-fails", "poll-recovers"])
def test_a_failed_poll_is_no_row_change(recovers: bool) -> None:
    """unavailable shows no row: following it would render the live plan instead of the snapshot."""
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.available = False
    harness.update().assert_not_called()
    assert harness.written[0] == STATE_UNAVAILABLE
    if recovers:
        harness.available = True
        harness.update().assert_not_called()
        assert harness.written[0] == _snapshot_row(*_A)[0]


def test_a_row_vanished_during_a_failed_poll_is_followed_on_recovery() -> None:
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.available = False
    harness.update().assert_not_called()
    harness.rows = [_plan_row(*_A, count=1), _plan_row(*_B)]
    harness.available = True
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_A, count=1)[0]
    follow.assert_called_once()


def test_a_deleted_snapshot_in_the_plan_view_moves_the_preview_to_live() -> None:
    """Same fallback outside the orphaned-plans view: the shown snapshot is gone, the selector shows Live."""
    snapshots = ["auto[0] — 2026-10-01 12:00", "change[0] — 2026-10-02 08:00"]
    harness = _Harness([])
    harness.entity.coordinator = SimpleNamespace(
        orphaned_plans_view_active=lambda: False,
        get_active_function_plan_fub_id=lambda: 7,
        api=SimpleNamespace(fub_data={"7": {"Name": "Plan"}}),
        function_plan_backup=SimpleNamespace(plan_backups_for_identity_sync=lambda _fub_id, _name: list(snapshots)),
    )
    with patch.object(select, "format_backup_label", lambda entry: entry):
        harness.entity._selected = snapshots[0]
        harness.update().assert_not_called()
        snapshots.pop(0)
        follow = harness.update()
    assert harness.written[0] == select.LIVE_BACKUP_OPTION
    follow.assert_called_once()


def test_several_vanished_plans_fall_back_past_them() -> None:
    harness = _showing(_plan_row(*_B), [_plan_row(*_A), _plan_row(*_B), _plan_row(*_C), _plan_row(*_D)])
    harness.rows = [_plan_row(*_A), _plan_row(*_D)]
    follow = harness.update()
    assert harness.written[0] == _plan_row(*_D)[0]
    follow.assert_called_once()


_PLAN = "Managed (ID 3)"


def _showing_after_plan_pick() -> _Harness:
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.plan_changed(_PLAN).assert_called_once()  # a new plan: the choice resets to the first row, the card follows
    harness.select(_snapshot_row(*_A)[0])
    return harness


@pytest.mark.parametrize(
    "plan_state",
    [f"⏸ {_PLAN}", STATE_UNAVAILABLE, STATE_UNKNOWN],
    ids=["plan-stopped", "poll-failed", "plan-fetch-failed"],
)
def test_the_same_plan_keeps_the_backup_choice(plan_state: str) -> None:
    """Round-3 review: the watchdog's ⏸ prefix or a failed poll/plan fetch reset the shown snapshot."""
    harness = _showing_after_plan_pick()
    harness.plan_changed(plan_state).assert_not_called()
    harness.plan_changed(_PLAN).assert_not_called()
    assert harness.entity._selected == _snapshot_row(*_A)[0]
    assert harness.written[0] == _snapshot_row(*_A)[0]


@pytest.mark.parametrize("seed", [_PLAN, f"⏸ {_PLAN}"], ids=["running", "stopped"])
def test_the_first_toggle_after_a_restart_keeps_the_backup_choice(seed: str) -> None:
    """Review: the 'Plan' selector wrote its state before the listener, so no event named the plan."""
    harness = _showing(_snapshot_row(*_A), [*_plan_with_snapshot(*_A), _plan_row(*_B)])
    harness.entity._seed_shown_plan(SimpleNamespace(state=seed))  # type: ignore[arg-type]
    toggled = _PLAN if seed.startswith("⏸") else f"⏸ {_PLAN}"
    harness.plan_changed(toggled).assert_not_called()
    assert harness.written[0] == _snapshot_row(*_A)[0]


@pytest.mark.parametrize("plan_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
def test_a_plan_state_naming_no_plan_seeds_nothing(plan_state: str) -> None:
    harness = _Harness([])
    harness.entity._seed_shown_plan(SimpleNamespace(state=plan_state))  # type: ignore[arg-type]
    harness.entity._seed_shown_plan(None)
    assert harness.entity._shown_plan is None


def test_another_plan_resets_the_backup_choice_and_follows() -> None:
    harness = _showing_after_plan_pick()
    follow = harness.plan_changed("Other (ID 4)")
    assert harness.entity._selected is None
    assert harness.written[0] == _plan_row(*_A)[0]
    follow.assert_called_once()


@pytest.mark.parametrize(
    ("fub_data", "orphans_left"),
    [({"3": {"Name": "Managed"}}, False), ({"3": {"Name": "Managed"}}, True), ({}, False)],
    ids=["view-emptied", "view-kept", "fetch-failed"],
)
def test_the_plan_selector_leaving_the_emptied_view_moves_the_preview(fub_data: dict, orphans_left: bool) -> None:
    """Last deleted plan emptied: back to the managed plan, and an open card shows it.

    No plan data (failed fetch) proves nothing about the view: it stays.
    """
    entity = select.ComexioPlanSelectEntity.__new__(select.ComexioPlanSelectEntity)
    entity._selected = FUNCTION_PLAN_ORPHANED_VIEW_OPTION
    entity.coordinator = SimpleNamespace(
        api=SimpleNamespace(fub_data=fub_data),
        function_plan_backup=SimpleNamespace(orphaned_plans_sync=lambda _fub_data: [object()] if orphans_left else []),
    )
    with (
        patch.object(select.CoordinatorEntity, "_handle_coordinator_update"),
        patch.object(select, "_follow_in_preview") as follow,
    ):
        entity._handle_coordinator_update()
    if orphans_left or not fub_data:
        assert entity._selected == FUNCTION_PLAN_ORPHANED_VIEW_OPTION
        follow.assert_not_called()
    else:
        assert entity._selected is None
        follow.assert_called_once()
