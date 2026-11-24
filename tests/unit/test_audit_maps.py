"""The Web-IO audit's map building and comparison (coordinator._async_audit_webio's pure steps)."""

from typing import Any

import pytest

from custom_components.comexio.const import WebioClass, io_audit_key
from custom_components.comexio.coordinator import (
    ComexioCoordinator,
    _build_com_audit_map,
    _fallback_audit_key,
    _source_max_ids,
    _split_disabled_commands,
)


@pytest.mark.parametrize(
    ("full_name", "key"),
    [
        ("HA M5 Licht", "M5"),
        ("HA K7 Jalousie", "K7"),
        ("HA 1W12 Kitchen", "1W12"),
        # A 1-Wire command is named by the sensor's address: "1Wire" is no "1W<config id>" key.
        ("HA 1Wire OT1", "HA 1Wire OT1"),
        ("HA Markise Sued", "HA Markise Sued"),  # a prefix without an id is no key either
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
    ("full_name", "key"),
    [
        # The whole extension name: "Plant Room" + "AI1", where the positional split reads "Plant" + "Room".
        ("HA IO Plant Room AI1 Alt", io_audit_key("Plant Room", "AI1")),
        ("HA IO Ext1 I1 Alt", io_audit_key("Ext1", "I1")),
        # Only after a space: I10 is no stale name of I1.
        ("HA IO Ext1 I10", io_audit_key("Ext1", "I10")),
        # Fits "Ext1" + "I1" and "Ext1 I1" + "AI1": no guess, it stays out of the audit.
        ("HA IO Ext1 I1 AI1 Alt", None),
    ],
)
def test_com_map_keys_a_stale_io_name_by_the_whole_current_name(full_name: str, key: str | None) -> None:
    ha_map = {
        io_audit_key(ext, ident): {"name": f"HA IO {ext} {ident}", "type": "digital"}
        for ext, ident in [("Plant Room", "AI1"), ("Ext1", "I1"), ("Ext1 I1", "AI1")]
    }

    com_map = _build_com_audit_map({full_name: {"cmdId": 1, "typeId": 1}}, ha_map)

    assert list(com_map) == ([key] if key else [])


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
    assert found["rename"] == [
        {
            "id": 20,
            "name": "HA M2 Neu",
            "previous_name": "HA M2 Alt",
            "rename_changes_type": False,
            "payload": "p2",
            "webio_class": "marker",
        }
    ]
    assert found["type"] == [
        {
            "id": 30,
            "name": "HA M3 Typ",
            "previous_name": "HA M3 Typ",
            "rename_changes_type": True,
            "payload": "p3",
            "webio_class": "marker",
        }
    ]
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


def test_renamed_command_skips_the_type_check() -> None:
    """The rename saves the whole command, the new type with it: no separate type finding."""
    mismatches: set[str] = set()
    found = _FakeCoordinator(unwired=set())._compare_audit_maps(
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
    # A rename that changes the type is more than a rename: it names the old name (its ledger state moves along)
    # and says so, so the write asks for a restart.
    assert found["rename"][0]["previous_name"] == "HA M2 Alt"
    assert found["rename"][0]["rename_changes_type"] is True


def test_a_rename_of_a_command_without_a_type_counts_as_changing_it() -> None:
    """Two unknown types are no proof that nothing but the name changes."""
    command = {k: v for k, v in _com("HA M2 Alt", 20).items() if k != "type"}
    found = _FakeCoordinator(unwired=set())._compare_audit_maps(
        {"M2": {"name": "HA M2 Neu", "type": None}},
        {"M2": [command]},
        {},
        (set(), set(), set()),
        {},
        set(),
        set(),
    )
    assert found["rename"][0]["previous_name"] == "HA M2 Alt"
    assert found["rename"][0]["rename_changes_type"] is True


def test_renamed_command_is_checked_for_its_wiring_too() -> None:
    """Comexio cuts the connection of a marker whose type changed; the sync closes it along with the rename.

    The rename keeps the command's webIoId, so the pair can be judged before the rename ran. The gap says its
    command is still to be renamed, and the sync's later steps are told so (button._gap_items_for_action).
    """
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
    # The sync reads this list to rename: the gap must not take the command out of it.
    assert [(item["id"], item["name"]) for item in found["rename"]] == [(20, "HA M2 Neu")]
    assert found["function_plan_missing"] == [{"name": "HA M2 Neu", "pending_rename": True}]
    assert found["type"] == []
    assert mismatches == {"rename_M2", "function_plan_missing_M2"}


def test_a_gap_of_a_command_with_its_name_carries_no_rename_flag() -> None:
    found = _FakeCoordinator(unwired={"M2"})._compare_audit_maps(
        {"M2": {"name": "HA M2 Neu", "type": "digital"}},
        {"M2": [_com("HA M2 Neu", 20)]},
        {},
        (set(), set(), set()),
        {},
        set(),
        set(),
    )
    assert found["function_plan_missing"] == [{"name": "HA M2 Neu"}]


def test_commands_of_a_switched_off_class_leave_the_audit() -> None:
    """KNX import off: its commands are counted for the import_disabled repair, never as orphans."""
    commands = {
        "HA M5 Licht": {"cmdId": 1, "webioClass": WebioClass.MARKER},
        "HA K1 Tor": {"cmdId": 2, "webioClass": WebioClass.KNX},
        "HA K2 Licht": {"cmdId": 3, "webioClass": "knx"},
        "HA IO BASE Q1": {"cmdId": 4, "webioClass": "io"},
        "Ohne Klasse": {"cmdId": 5},
        "Unbekannt": {"cmdId": 6, "webioClass": "fremd"},
    }

    kept, disabled = _split_disabled_commands(commands, (WebioClass.MARKER, WebioClass.IO))

    # A command without a known class stays in the audit, as before.
    assert set(kept) == {"HA M5 Licht", "HA IO BASE Q1", "Ohne Klasse", "Unbekannt"}
    assert disabled == {WebioClass.KNX: 2}


def test_no_class_switched_off_keeps_every_command() -> None:
    commands = {"HA K1 Tor": {"webioClass": "knx"}}
    assert _split_disabled_commands(commands, tuple(WebioClass)) == (commands, {})


def test_onewire_commands_without_a_sensor_each_keep_a_key_of_their_own() -> None:
    # Read as the key "1Wire", every 1-Wire command without a current sensor would share one audit key.
    commands = {
        "HA 1Wire OT1": {"cmdId": 1, "typeId": 2, "webioClass": "onewire", "webIoId": 11},
        "HA 1Wire OT2": {"cmdId": 2, "typeId": 2, "webioClass": "onewire", "webIoId": 12},
    }

    com_map = _build_com_audit_map(commands, {})

    assert {key: [com["id"] for com in coms] for key, coms in com_map.items()} == {
        "HA 1Wire OT1": [1],
        "HA 1Wire OT2": [2],
    }
