"""A marker whose type changed lost its connection: rename, type change and rewiring go through in one sync.

Comexio cuts the connection of a marker whose type changed (an analog marker cannot hang on a digital input).
The first sync used to delete the now unconnected marker element as plan debris, the second renamed the command
(its gap check was skipped while a rename was pending) and only a third one wired the pair again.
"""

from typing import Any

import pytest

from custom_components.comexio.button import _gap_items_for_action
from custom_components.comexio.const import WebioClass
from custom_components.comexio.coordinator import _RANGE_CATEGORIES, ComexioCoordinator

_NO_WIRING = {ref_type: (set(), set()) for ref_type in ("2", "11", "12", "1")}


class _FakeCoordinator:
    """The debris check with real code; the plan scan is replaced by the unconnected marker elements it would find."""

    server_id = "srv"
    _function_plan_dangling_items = ComexioCoordinator._function_plan_dangling_items

    def __init__(self, unconnected_markers: dict[str, list[int]]) -> None:
        self._unconnected_markers = unconnected_markers

    def _dangling_source_ids(self, source_type: str, *_args: Any) -> dict[str, list[int]]:
        return self._unconnected_markers if source_type == "2" else {}

    def _dangling_io_items(self, *_args: Any) -> list[dict[str, Any]]:
        return []


def _dangling(wiring_gap_keys: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
    data = {"markers": [{"id": 220, "name": "M220 Fenster"}, {"id": 9, "name": "M9 Weg"}], "knx": [], "onewire": []}
    mismatches: set[str] = set()
    items = _FakeCoordinator({"220": [36], "9": [36]})._function_plan_dangling_items(
        data, _NO_WIRING, True, set(), mismatches, wiring_gap_keys=wiring_gap_keys
    )
    return items, mismatches


def test_an_unconnected_marker_element_of_a_source_with_a_wiring_gap_is_no_debris() -> None:
    items, mismatches = _dangling({"M220"})

    assert items == [{"name": "M9 Weg", "ref_id": "9", "webio_class": WebioClass.MARKER}]
    assert mismatches == {"function_plan_dangling_M9"}


def test_an_unconnected_marker_element_without_a_wiring_gap_is_still_debris() -> None:
    """No gap: no command at all, or the pair is wired in another plan and this element is a stale duplicate."""
    items, mismatches = _dangling(set())

    assert {item["ref_id"] for item in items} == {"220", "9"}
    assert mismatches == {"function_plan_dangling_M220", "function_plan_dangling_M9"}


def test_every_range_category_skips_the_elements_of_its_sources_with_a_gap() -> None:
    """Markers, KNX objects and 1-Wire sensors alike: the skip keys on the category's own audit key prefix."""

    class _EveryCategory(_FakeCoordinator):
        def _dangling_source_ids(self, source_type: str, *_args: Any) -> dict[str, list[int]]:
            return {"7": [36]}

    gapped = {f"{cat.audit_key_prefix}7" for cat in _RANGE_CATEGORIES}
    judged = _EveryCategory({})._function_plan_dangling_items(
        {}, _NO_WIRING, True, set(), set(), wiring_gap_keys=gapped
    )
    unjudged = _EveryCategory({})._function_plan_dangling_items({}, _NO_WIRING, True, set(), set())

    assert judged == []
    assert {item["webio_class"] for item in unjudged} == {cat.key for cat in _RANGE_CATEGORIES}


@pytest.mark.parametrize(
    ("action", "kept"), [("full_sync", 2), ("function_plan_add_missing", 1), ("knx_bridge_add_missing", 1)]
)
def test_only_a_full_sync_wires_a_command_that_still_has_to_be_renamed(action: str, kept: int) -> None:
    """The pair writer finds a command by its new name, so a wiring-only action would fail on the old one."""
    gaps = [{"name": "HA M1", "marker_id": 1}, {"name": "HA M2", "marker_id": 2, "pending_rename": True}]

    assert len(_gap_items_for_action(action, gaps)) == kept
