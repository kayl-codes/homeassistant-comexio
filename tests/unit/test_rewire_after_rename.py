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


@pytest.mark.parametrize("fub_ids", [[36, 37], [36, 36]], ids=["two plans", "twice in one plan"])
def test_a_source_with_several_unconnected_elements_keeps_its_duplicate_as_debris_whatever_the_gap(
    fub_ids: list[int],
) -> None:
    """Only a lone gap element is left to the wiring: with a duplicate the cleanup takes both, the wiring places one."""

    class _Duplicate(_FakeCoordinator):
        def _dangling_source_ids(self, source_type: str, *_args: Any) -> dict[str, list[int]]:
            return {"220": fub_ids} if source_type == "2" else {}

    data = {"markers": [{"id": 220, "name": "M220 Fenster"}], "knx": [], "onewire": []}
    items = _Duplicate({})._function_plan_dangling_items(data, _NO_WIRING, True, set(), set(), wiring_gap_keys={"M220"})

    assert [item["ref_id"] for item in items] == ["220"]


@pytest.mark.parametrize("source_type", ["1", "2"], ids=["io", "marker"])
def test_the_scan_lists_a_plan_twice_that_holds_two_unconnected_elements_of_one_source(source_type: str) -> None:
    """The element count per plan is what tells a duplicate in the same plan from a lone element."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    element = {"reference": {"type": source_type, "ref_id": "220"}}
    coordinator.function_plan_plans = {36: {"elements": {"1": element, "2": element}}, 37: {"elements": {"1": element}}}
    coordinator._function_plan_check_fub_ids = lambda: {36, 37}  # type: ignore[method-assign]
    coordinator._is_managed_function_plan = lambda _fub_id: True  # type: ignore[method-assign]
    coordinator._plan_wired_pairs = lambda _plan, _type: set()  # type: ignore[method-assign]
    coordinator._plan_connected_source_ids = lambda _plan, _type: set()  # type: ignore[method-assign]

    assert coordinator._dangling_source_ids(source_type, set(), set()) == {"220": [36, 36, 37]}


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
        ({"5": [36, 36]}, {io_audit_key("Ext1", "I1")}, True),  # a duplicate in the same plan
    ],
)
def test_the_unconnected_element_of_an_io_with_a_wiring_gap_is_no_debris(
    unconnected: dict[str, list[int]], gap_keys: set[str], debris: bool
) -> None:
    """A renamed IO whose connection was cut (type change) is rewired in the same sync, like a marker."""
    items = _dangling_ios(unconnected, gap_keys)

    assert [item["ref_id"] for item in items] == (["5"] if debris else [])


@pytest.mark.parametrize(
    ("unconnected", "debris"), [({"211": [36]}, False), ({"211": [36, 37]}, True), ({"211": [36, 36]}, True)]
)
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


@pytest.mark.parametrize(
    ("web_io_id", "readable"),
    [(None, False), ("n/a", False), (True, False), (12.5, False), (12, True), ("12", True)],
    ids=["none", "text", "bool", "fraction", "int", "numeric text"],
)
def test_a_command_without_a_readable_id_is_no_wiring_gap(web_io_id: object, readable: bool) -> None:
    """Nothing can be wired without the command's id as an int: the gap would block the debris cleanup, and the pair
    writer would fail on the id.
    """
    best_match = {"name": "HA M220 Fenster", "webIoId": web_io_id}

    gap = ComexioCoordinator._function_plan_gap_item("M220", "HA M220 Fenster", best_match, _NO_WIRING, None, set())

    assert gap == ({"name": "HA M220 Fenster", "marker_id": 220} if readable else None)


@pytest.mark.parametrize("web_io_id", [12, "12", 12.0], ids=["int", "numeric text", "whole float"])
def test_a_readable_command_id_finds_its_wired_pair_whatever_its_type(web_io_id: object) -> None:
    """The pair is looked up by the id as an int, so a pair wired under it is no gap."""
    wired = {**_NO_WIRING, "2": ({("220", "12")}, set())}
    best_match = {"name": "HA M220 Fenster", "webIoId": web_io_id}

    gap = ComexioCoordinator._function_plan_gap_item("M220", "HA M220 Fenster", best_match, wired, None, set())

    assert gap is None


_ORDINARY_GAP = {"name": "HA M1", "marker_id": 1}
_RENAME_GAP = {"name": "HA M2", "marker_id": 2, "pending_rename": True}


@pytest.mark.parametrize(
    ("action", "kept"),
    [
        ("full_sync", [_ORDINARY_GAP, _RENAME_GAP]),
        ("function_plan_add_missing", [_ORDINARY_GAP]),
        ("knx_bridge_add_missing", [_ORDINARY_GAP]),
    ],
)
def test_only_a_full_sync_wires_a_command_that_still_has_to_be_renamed(action: str, kept: list[dict[str, Any]]) -> None:
    """The pair writer finds a command by its new name, so a wiring-only action would fail on the old one."""
    assert _gap_items_for_action(action, [_ORDINARY_GAP, _RENAME_GAP]) == kept
