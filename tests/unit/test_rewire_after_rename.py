"""A marker whose type changed lost its connection: rename, type change and rewiring go through in one sync.

Comexio cuts the connection of a marker whose type changed (an analog marker cannot hang on a digital input).
The first sync used to delete the now unconnected marker element as plan debris, the second renamed the command
(its gap check was skipped while a rename was pending) and only a third one wired the pair again.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio.button import _gap_items_for_action
from custom_components.comexio.const import IO_PLAN_KIND_IO, IO_PLAN_KIND_QUICKBUS, WebioClass, io_audit_key
from custom_components.comexio.coordinator import _RANGE_CATEGORIES, ComexioCoordinator, _io_gap_key

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


def test_a_source_that_dangles_in_several_plans_keeps_its_duplicate_as_debris_whatever_the_gap() -> None:
    """Only a lone gap element is left to the wiring: with a duplicate the cleanup takes both, the wiring places one."""

    class _Duplicate(_FakeCoordinator):
        def _dangling_source_ids(self, source_type: str, *_args: Any) -> dict[str, list[int]]:
            return {"220": [36, 37]} if source_type == "2" else {}

    data = {"markers": [{"id": 220, "name": "M220 Fenster"}], "knx": [], "onewire": []}
    items = _Duplicate({})._function_plan_dangling_items(data, _NO_WIRING, True, set(), set(), wiring_gap_keys={"M220"})

    assert [item["ref_id"] for item in items] == ["220"]


class _IoCoordinator:
    """The IO part of the debris check with real code; the plan scan is replaced by the elements it would find."""

    server_id = "srv"
    _dangling_io_items = ComexioCoordinator._dangling_io_items
    _dangling_io_kind = ComexioCoordinator._dangling_io_kind

    def __init__(self, unconnected_ios: dict[str, list[int]]) -> None:
        self._unconnected_ios = unconnected_ios

    def _dangling_source_ids(self, source_type: str, *_args: Any) -> dict[str, list[int]]:
        return self._unconnected_ios if source_type == "1" else {}

    # Asked only for an IO the config no longer has; every IO here is known.
    managed_column_plan = MagicMock(return_value=None)


def _dangling_ios(unconnected: dict[str, list[int]], wiring_gap_keys: set[str]) -> list[dict[str, Any]]:
    data = {"io_all": [{"id": 5, "ext_name": "Ext1", "identifier": "I1"}]}
    return _IoCoordinator(unconnected)._dangling_io_items(
        data, set(), set(), {(IO_PLAN_KIND_IO.label, "Ext1")}, set(), wiring_gap_keys
    )


@pytest.mark.parametrize(
    ("unconnected", "gap_keys", "debris"),
    [
        ({"5": [36]}, {io_audit_key("Ext1", "I1")}, False),  # the gap element: left to the wiring
        ({"5": [36]}, set(), True),  # no gap: debris as before
        ({"5": [36]}, {io_audit_key("Ext1", "I2")}, True),  # the gap belongs to another IO
        ({"5": [36, 37]}, {io_audit_key("Ext1", "I1")}, True),  # a duplicate in a second plan
    ],
)
def test_the_unconnected_element_of_an_io_with_a_wiring_gap_is_no_debris(
    unconnected: dict[str, list[int]], gap_keys: set[str], debris: bool
) -> None:
    """A renamed IO whose connection was cut (type change) is rewired in the same sync, like a marker."""
    items = _dangling_ios(unconnected, gap_keys)

    assert [item["ref_id"] for item in items] == (["5"] if debris else [])


@pytest.mark.parametrize(("unconnected", "debris"), [({"211": [36]}, False), ({"211": [36, 37]}, True)])
def test_the_unconnected_element_of_a_quickbus_io_with_a_wiring_gap_is_no_debris(
    unconnected: dict[str, list[int]], debris: bool
) -> None:
    """Quickbus IOs are keyed by their IO id, not by extension + identifier."""
    data = {"io_all": [{"id": 211, "ext_name": "MT1", "identifier": "I1", "quickbus": {"device_id": 3}}]}

    items = _IoCoordinator(unconnected)._dangling_io_items(
        data, set(), set(), {(IO_PLAN_KIND_QUICKBUS.label, "3")}, set(), {"QB211"}
    )

    assert [item["ref_id"] for item in items] == (["211"] if debris else [])


@pytest.mark.parametrize(
    ("io", "key"),
    [
        ({"id": 5, "ext_name": "Ext1", "identifier": "I1"}, io_audit_key("Ext1", "I1")),
        ({"id": 211, "ext_name": "MT1", "identifier": "I1", "quickbus": {"device_id": 3}}, "QB211"),
    ],
)
def test_the_gap_key_of_an_io_is_the_key_the_audit_map_gives_it(io: dict[str, Any], key: str) -> None:
    assert _io_gap_key(io) == key


@pytest.mark.parametrize("web_io_id", [None, 12])
def test_a_command_without_a_readable_id_is_no_wiring_gap(web_io_id: int | None) -> None:
    """Nothing can be wired without the command's id: the gap would block the debris cleanup and fail the wiring."""
    best_match = {"name": "HA M220 Fenster", "webIoId": web_io_id}

    gap = ComexioCoordinator._function_plan_gap_item("M220", "HA M220 Fenster", best_match, _NO_WIRING, None, set())

    assert gap == (None if web_io_id is None else {"name": "HA M220 Fenster", "marker_id": 220})


@pytest.mark.parametrize(
    ("action", "kept"), [("full_sync", 2), ("function_plan_add_missing", 1), ("knx_bridge_add_missing", 1)]
)
def test_only_a_full_sync_wires_a_command_that_still_has_to_be_renamed(action: str, kept: int) -> None:
    """The pair writer finds a command by its new name, so a wiring-only action would fail on the old one."""
    gaps = [{"name": "HA M1", "marker_id": 1}, {"name": "HA M2", "marker_id": 2, "pending_rename": True}]

    assert len(_gap_items_for_action(action, gaps)) == kept
