"""The Web-IO audit's map building and comparison (coordinator._async_audit_webio's pure steps)."""

from typing import Any

import pytest

from custom_components.comexio.const import io_audit_key
from custom_components.comexio.coordinator import (
    ComexioCoordinator,
    _build_com_audit_map,
    _fallback_audit_key,
    _source_max_ids,
)


@pytest.mark.parametrize(
    ("full_name", "key"),
    [
        ("HA M5 Licht", "M5"),
        ("HA K7 Jalousie", "K7"),
        ("HA IO Ext1 I1", io_audit_key("Ext1", "I1")),
        ("HA IO Ext1", "HA IO Ext1"),  # too short for an IO key
        ("Fremd", "Fremd"),
    ],
)
def test_fallback_audit_key(full_name: str, key: str) -> None:
    assert _fallback_audit_key(full_name) == key


def test_com_map_prefers_the_exact_name_and_groups_by_key() -> None:
    ha_map = {io_audit_key("Ext With Space", "I1"): {"name": "HA IO Ext With Space I1", "type": "digital"}}
    commands = {
        "HA IO Ext With Space I1": {"cmdId": 1, "typeId": 1, "webioClass": "io", "webIoId": 11},
        "HA M5 Licht": {"cmdId": 2, "typeId": "2", "webioClass": "marker", "webIoId": 12},
        "HA M5 Kopie": {"cmdId": 3, "webioClass": "marker", "webIoId": 13},
    }

    com_map = _build_com_audit_map(commands, ha_map)

    # The positional heuristic would have split "Ext With Space" — the exact lookup does not.
    assert [c["id"] for c in com_map[io_audit_key("Ext With Space", "I1")]] == [1]
    assert com_map["M5"] == [
        {"name": "HA M5 Licht", "type": "analog", "id": 2, "webio_class": "marker", "webIoId": 12},
        {"name": "HA M5 Kopie", "type": "digital", "id": 3, "webio_class": "marker", "webIoId": 13},
    ]


@pytest.mark.parametrize(
    ("fub_modules", "expected"),
    [
        ({"2": {"1": {"Id": 4}, "2": {"Id": 9}}, "11": [{"Id": 3}, "kaputt", {"Id": None}]}, (9, 3)),
        ({"11": {"5": {"Id": 5}}}, (0, 5)),
        ({}, (0, 0)),
    ],
)
def test_source_max_ids(fub_modules: dict, expected: tuple[int, int]) -> None:
    assert _source_max_ids({"FubModules": fub_modules}) == expected


class _FakeCoordinator:
    """Just what the map comparison calls, with the real coordinator methods."""

    _compare_audit_maps = ComexioCoordinator._compare_audit_maps
    _compare_audit_key = ComexioCoordinator._compare_audit_key

    def __init__(self, unwired: set[str]) -> None:
        self._unwired = unwired

    def _function_plan_gap_item(self, key: str, name: str, *_args: Any) -> dict[str, Any] | None:
        return {"name": name} if key in self._unwired else None


def _com(name: str, cmd_id: int, type_: str = "digital") -> dict[str, Any]:
    return {"name": name, "type": type_, "id": cmd_id, "webio_class": "marker", "webIoId": cmd_id + 100}


def test_compare_finds_every_audit_category() -> None:
    ha_map = {
        "M1": {"name": "HA M1 Ok", "type": "digital"},
        "M2": {"name": "HA M2 Neu", "type": "digital"},
        "M3": {"name": "HA M3 Typ", "type": "analog"},
        "M4": {"name": "HA M4 Fehlt", "type": "digital"},
    }
    com_map = {
        "M1": [_com("HA M1 Kopie", 10), _com("HA M1 Ok", 11)],
        "M2": [_com("HA M2 Alt", 20)],
        "M3": [_com("HA M3 Typ", 30)],
        "M9": [_com("HA M9 Weg", 90)],
    }
    payload_map = {"HA M2 Neu": "p2", "HA M3 Typ": "p3", "HA M4 Fehlt": "p4"}
    mismatches: set[str] = set()

    found = _FakeCoordinator(unwired={"M1"})._compare_audit_maps(
        ha_map, com_map, payload_map, (set(), set(), set()), {}, set(), mismatches
    )

    assert found["missing"] == [{"name": "HA M4 Fehlt", "payload": "p4", "webio_class": "marker"}]
    assert found["rename"] == [{"id": 20, "name": "HA M2 Neu", "payload": "p2", "webio_class": "marker"}]
    assert found["type"] == [{"id": 30, "name": "HA M3 Typ", "payload": "p3", "webio_class": "marker"}]
    assert found["function_plan_missing"] == [{"name": "HA M1 Ok"}]
    # The duplicate next to the exact name match first, then the command whose source is gone.
    assert [o["id"] for o in found["orphan"]] == [10, 90]
    assert found["orphan"][0] == {"id": 10, "name": "HA M1 Kopie", "webio_class": "marker", "webIoId": 110}
    assert mismatches == {
        "missing_M4",
        "rename_M2",
        "type_M3",
        "function_plan_missing_M1",
        "orphan_10",
        "orphan_90",
    }


def test_renamed_command_skips_the_type_and_wiring_checks() -> None:
    mismatches: set[str] = set()
    found = _FakeCoordinator(unwired={"M2"})._compare_audit_maps(
        {"M2": {"name": "HA M2 Neu", "type": "analog"}},
        {"M2": [_com("HA M2 Alt", 20)]},
        {},
        (set(), set(), set()),
        {},
        set(),
        mismatches,
    )
    assert mismatches == {"rename_M2"}
    assert found["type"] == []
    assert found["function_plan_missing"] == []
