"""Web-IO audit and command building cover only IOs with an entity; the others are held.

An offline extension's IOs (include_offline_extensions off) and IOs of an unsupported type get
no entity: HA expects no Web-IO command for them (no "missing" ones), and the ones Comexio
already has are neither checked nor reported as orphans a sync would delete.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_QUICKBUS_DEVICES,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    QUICKBUS_IO_DATA_KEY,
    WEBIO_COMMAND_DUPLICATES_KEY,
    WebioClass,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.unsupported_ios import io_has_entity


def _io(ext_name: str, identifier: str, **fields: Any) -> dict[str, Any]:
    return {"ext_name": ext_name, "identifier": identifier, "is_binary": True, "is_input": True, **fields}


def _quickbus_io(io_id: str, device_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": io_id,
        "identifier": f"G{io_id}",
        "is_binary": True,
        "quickbus": {"device_id": device_id, "name": f"MT{device_id}"},
        **fields,
    }


ONLINE_IO = _io("EXT-1", "I1")
OFFLINE_IO = _io("vDS1", "I1", offline=True)
# An analog output metering kWh: no writable number entity (#196).
UNSUPPORTED_IO = _io("EXT-1", "Q1", is_binary=False, is_input=False, unit="kWh")
ONLINE_QB_IO = _quickbus_io("201", "1")
OFFLINE_QB_IO = _quickbus_io("301", "3", offline=True)


def _data() -> dict[str, Any]:
    return {
        "markers": [],
        "knx": [],
        "io": [ONLINE_IO, OFFLINE_IO, UNSUPPORTED_IO],
        QUICKBUS_IO_DATA_KEY: [ONLINE_QB_IO, OFFLINE_QB_IO],
    }


def _coordinator(entry_data: dict[str, Any] | None = None, **options: Any) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    coordinator._ignored_ids_for = lambda _cls: set()
    coordinator.config_entry = SimpleNamespace(data=entry_data or {}, options=options)
    coordinator.server_id = "test"
    return coordinator


def _command(cmd_id: int) -> dict[str, Any]:
    return {"typeId": 1, "cmdId": cmd_id, "webioClass": "[IO]", "webIoId": cmd_id}


EVERY_COMMAND = {
    "HA IO EXT-1 I1": _command(1),
    "HA IO vDS1 I1": _command(2),
    "HA IO EXT-1 Q1": _command(3),
    "HA QB1 MT1 G201": _command(4),
    "HA QB3 MT3 G301": _command(5),
}


def test_only_ios_with_an_entity_are_expected() -> None:
    ha_map, io_meta_by_key = _coordinator()._build_ha_audit_map(_data())

    assert {info["name"] for info in ha_map.values()} == {"HA IO EXT-1 I1", "HA QB1 MT1 G201"}
    assert set(io_meta_by_key) == set(ha_map)


def test_include_offline_extensions_expects_the_offline_ios() -> None:
    ha_map, _meta = _coordinator(**{CONF_INCLUDE_OFFLINE_EXTENSIONS: True})._build_ha_audit_map(_data())

    # The unsupported type still has no entity, offline or not.
    assert {info["name"] for info in ha_map.values()} == {
        "HA IO EXT-1 I1",
        "HA IO vDS1 I1",
        "HA QB1 MT1 G201",
        "HA QB3 MT3 G301",
    }


def test_web_io_builders_get_only_ios_with_an_entity() -> None:
    source = _coordinator().webio_source_data(_data())

    assert source["io"] == [ONLINE_IO]
    assert source[QUICKBUS_IO_DATA_KEY] == [ONLINE_QB_IO]


def test_commands_of_held_ios_are_neither_checked_nor_orphans() -> None:
    coordinator = _coordinator()
    ha_map, _meta = coordinator._build_ha_audit_map(_data())

    com_map = coordinator._build_com_audit_map_without_held(EVERY_COMMAND, _data(), ha_map)

    # Exactly the expected keys: nothing missing, the held commands no orphans.
    assert set(com_map) == set(ha_map)
    assert [com["name"] for coms in com_map.values() for com in coms] == ["HA IO EXT-1 I1", "HA QB1 MT1 G201"]


@pytest.mark.parametrize(
    "stale_name",
    [
        "HA IO vDS1 I1 Alt",
        # An extension name with spaces: the positional fallback would key it IO_Plant_Room, an orphan.
        "HA IO Plant Room AI1 Alt",
    ],
)
def test_a_held_io_command_with_a_stale_name_is_held_too(stale_name: str) -> None:
    """A command Comexio still names after the IO's old description falls back to the IO's key."""
    coordinator = _coordinator()
    data = _data()
    data["io"] = [*data["io"], _io("Plant Room", "AI1", offline=True)]
    ha_map, _meta = coordinator._build_ha_audit_map(data)
    commands = {**EVERY_COMMAND, stale_name: _command(6)}

    com_map = coordinator._build_com_audit_map_without_held(commands, data, ha_map)

    assert set(com_map) == set(ha_map)
    assert stale_name not in {com["name"] for coms in com_map.values() for com in coms}


def test_an_unselected_quickbus_device_stays_an_orphan() -> None:
    """Deselecting a Quickbus device still removes its commands: only offline selected ones are held."""
    coordinator = _coordinator(**{CONF_FUNCTION_PLAN_QUICKBUS_DEVICES: ["3"]})
    ha_map, _meta = coordinator._build_ha_audit_map(_data())

    com_map = coordinator._build_com_audit_map_without_held(EVERY_COMMAND, _data(), ha_map)

    assert {info["name"] for info in ha_map.values()} == {"HA IO EXT-1 I1"}
    # Device 1 is no IO of HA's any more: its command is an orphan; offline device 3's is held.
    orphans = {com["name"] for key, coms in com_map.items() if key not in ha_map for com in coms}
    assert orphans == {"HA QB1 MT1 G201"}
    assert "QB301" not in com_map


@pytest.mark.parametrize("include_offline", [False, True])
def test_an_expected_io_without_a_command_is_missing(include_offline: bool) -> None:
    coordinator = _coordinator(**{CONF_INCLUDE_OFFLINE_EXTENSIONS: include_offline})
    ha_map, _meta = coordinator._build_ha_audit_map(_data())

    com_map = coordinator._build_com_audit_map_without_held({}, _data(), ha_map)

    assert com_map == {}
    assert len(ha_map) == (4 if include_offline else 2)


@pytest.mark.parametrize(
    ("io", "include_offline", "expected"),
    [
        (ONLINE_IO, False, True),
        (_io("EXT-1", "I2", offline=False), False, True),
        (OFFLINE_IO, False, False),
        (OFFLINE_IO, True, True),
        (UNSUPPORTED_IO, False, False),
        (UNSUPPORTED_IO, True, False),
        (_io("vDS1", "Q1", is_binary=False, is_input=False, unit="kWh", offline=True), True, False),
    ],
)
def test_io_has_entity(io: dict[str, Any], include_offline: bool, expected: bool) -> None:
    assert io_has_entity(io, include_offline) is expected


def _audit(
    coordinator: ComexioCoordinator,
    commands: dict[str, Any],
    data: dict[str, Any] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """missing / orphan / ... of the audit, as the repair and the sync see them (_async_audit_webio's maps)."""
    data = {**(data or _data()), "webio_commands": commands, WEBIO_COMMAND_DUPLICATES_KEY: duplicates or []}
    ha_map, io_meta_by_key, com_map, _disabled = coordinator._build_audit_maps({}, data)
    return coordinator._compare_audit_maps(ha_map, com_map, {}, (None, None, None), io_meta_by_key, set(), set())


def test_the_audit_reports_no_held_io_as_missing_or_orphan() -> None:
    found = _audit(_coordinator(), EVERY_COMMAND)

    assert found["missing"] == []
    assert found["orphan"] == []
    assert found["type"] == []
    assert found["rename"] == []


def test_the_audit_still_reports_an_expected_io_as_missing() -> None:
    """Holding the other IOs must not mask a command an IO with an entity lacks."""
    commands = {name: cmd for name, cmd in EVERY_COMMAND.items() if name != "HA IO EXT-1 I1"}

    found = _audit(_coordinator(), commands)

    assert [item["name"] for item in found["missing"]] == ["HA IO EXT-1 I1"]
    assert found["orphan"] == []


def test_a_stale_name_on_an_extension_with_spaces_is_a_rename_not_an_orphan() -> None:
    """The command keeps its webIoId (and the user's wiring): renamed, neither deleted nor created anew."""
    data = _data()
    data["io"] = [*data["io"], _io("Plant Room", "AI1")]
    commands = {**EVERY_COMMAND, "HA IO Plant Room AI1 Alt": _command(6)}

    found = _audit(_coordinator(), commands, data)

    assert [item["name"] for item in found["rename"]] == ["HA IO Plant Room AI1"]
    assert found["orphan"] == []
    assert found["missing"] == []


@pytest.mark.parametrize(("offline", "missing"), [(False, ["HA IO EXT-1 I1 AI1"]), (True, [])])
def test_a_stale_name_of_two_ios_is_neither_renamed_nor_an_orphan(offline: bool, missing: list[str]) -> None:
    """It fits EXT-1 I1 and "EXT-1 I1" AI1: no guess hands its wiring over, no sync deletes it.

    Offline, one of the two is held: its command must not turn into an orphan either.
    """
    data = _data()
    data["io"] = [*data["io"], _io("EXT-1 I1", "AI1", offline=offline)]
    commands = {**EVERY_COMMAND, "HA IO EXT-1 I1 AI1 Alt": _command(7)}

    found = _audit(_coordinator(), commands, data)

    assert (found["rename"], found["orphan"]) == ([], [])
    assert [item["name"] for item in found["missing"]] == missing


def _duplicate(name: str, cmd_id: int, webio_class: str = "io") -> dict[str, Any]:
    """An entry of aiocomexio's webio_command_duplicates: a command whose name an earlier one has."""
    return {"name": name, "webIoId": cmd_id, "cmdId": cmd_id, "typeId": 1, "webioClass": webio_class}


def test_every_name_duplicate_is_an_orphan_in_one_audit() -> None:
    """A sync deletes them all at once, not one layer per run while the next duplicate moves up (#251).

    Both cases: a copy of an expected command (the first one stays) and copies of a command
    no IO needs any more, e.g. the IO commands of Quickbus devices from before Quickbus support.
    """
    commands = {**EVERY_COMMAND, "HA IO Multibridge A6 TL1": _command(20)}
    duplicates = [
        _duplicate("HA IO EXT-1 I1", 11),
        _duplicate("HA IO Multibridge A6 TL1", 21),
        _duplicate("HA IO Multibridge A6 TL1", 22),
    ]

    found = _audit(_coordinator(), commands, duplicates=duplicates)

    assert sorted(item["id"] for item in found["orphan"]) == [11, 20, 21, 22]
    assert (found["missing"], found["rename"], found["type"]) == ([], [], [])


def test_a_name_duplicate_of_another_type_is_an_orphan_not_a_type_fix() -> None:
    """The first command of the name keeps the key; a copy of the wrong type goes, the first stays as it is."""
    duplicate = {**_duplicate("HA IO EXT-1 I1", 11), "typeId": 2}

    found = _audit(_coordinator(), EVERY_COMMAND, duplicates=[duplicate])

    assert [item["id"] for item in found["orphan"]] == [11]
    assert (found["missing"], found["rename"], found["type"]) == ([], [], [])


@pytest.mark.parametrize(
    "duplicate",
    [
        # Held like the first command of its name: the offline extension's IO keeps its commands.
        _duplicate("HA IO vDS1 I1", 12),
        # A switched-off category (KNX import is off by default) is left to the import_disabled repair.
        _duplicate("HA K7 Jalousie", 13, "knx"),
    ],
    ids=["held IO", "switched-off class"],
)
def test_a_name_duplicate_the_audit_leaves_alone_is_no_orphan(duplicate: dict[str, Any]) -> None:
    found = _audit(_coordinator(), EVERY_COMMAND, duplicates=[duplicate])

    assert found["orphan"] == []


@pytest.mark.parametrize(
    ("duplicate", "disabled_count"),
    [(_duplicate("HA K7 Jalousie", 14, "knx"), 2), (_duplicate("HA IO EXT-1 I1", 11), 1)],
    ids=["switched-off class", "imported class"],
)
def test_a_name_duplicate_of_a_switched_off_class_counts_as_disabled(
    duplicate: dict[str, Any], disabled_count: int
) -> None:
    """The import_disabled repair names how many commands of the class stay on the server: copies too."""
    commands = {**EVERY_COMMAND, "HA K7 Jalousie": {**_command(13), "webioClass": "knx"}}
    data = {**_data(), "webio_commands": commands, WEBIO_COMMAND_DUPLICATES_KEY: [duplicate]}

    *_maps, disabled = _coordinator()._build_audit_maps({}, data)

    assert disabled == {WebioClass.KNX: disabled_count}


def test_include_offline_extensions_from_the_entry_data_counts_too() -> None:
    """Same merge of entry data and options as the entity platforms."""
    source = _coordinator(entry_data={CONF_INCLUDE_OFFLINE_EXTENSIONS: True}).webio_source_data(_data())

    assert source["io"] == [ONLINE_IO, OFFLINE_IO]
    assert source[QUICKBUS_IO_DATA_KEY] == [ONLINE_QB_IO, OFFLINE_QB_IO]


def test_the_source_with_held_ios_keeps_every_io() -> None:
    """The range check still finds the target of a command kept for an IO without an entity."""
    data = _data()

    source = _coordinator().webio_source_data(data, include_held=True)

    assert source["io"] == [ONLINE_IO, OFFLINE_IO, UNSUPPORTED_IO]
    assert source[QUICKBUS_IO_DATA_KEY] == [ONLINE_QB_IO, OFFLINE_QB_IO]
    assert len(data["io"]) == 3


def test_the_range_check_targets_the_commands_of_held_ios() -> None:
    """A held IO's command keeps a target, so the range check does not count it as excluded."""
    coordinator = _coordinator()
    coordinator.data = _data()
    calls: list[tuple[Any, ...]] = []

    def build_webio_commands(*args: Any) -> list[dict[str, Any]]:
        calls.append(args)
        source = args[1]
        return [{"Name": f"HA IO {io['ext_name']} {io['identifier']}"} for io in source["io"]] + [
            {"Name": f"HA QB {io['id']}"} for io in source[QUICKBUS_IO_DATA_KEY]
        ]

    coordinator.api = SimpleNamespace(build_webio_commands=build_webio_commands)

    assert set(coordinator._webio_range_targets()) == {
        "HA IO EXT-1 I1",
        "HA IO vDS1 I1",
        "HA IO EXT-1 Q1",
        "HA QB 201",
        "HA QB 301",
    }
    # Every class (webio_class None), less the ignored markers and KNX objects.
    assert [(args[0], *args[2:]) for args in calls] == [("test", None, set(), set())]


def _class_coordinator(**options: Any) -> ComexioCoordinator:
    coordinator = _coordinator(**options)
    # Stands in for aiocomexio's builder: one command per IO of the class it is asked for.
    by_class = {WebioClass.IO: "io", WebioClass.QUICKBUS: QUICKBUS_IO_DATA_KEY}
    coordinator.api = SimpleNamespace(
        build_webio_commands=lambda _server_id, source, webio_class, **_ignored: source[by_class[webio_class]]
    )
    return coordinator


def test_a_class_with_only_held_ios_is_not_required() -> None:
    """Comexio creates no device for an empty class: a missing-class repair for it could never be fixed."""
    data = {**_data(), "io": [OFFLINE_IO, UNSUPPORTED_IO], QUICKBUS_IO_DATA_KEY: [OFFLINE_QB_IO]}

    classes = _class_coordinator()._webio_classes_with_commands([WebioClass.IO, WebioClass.QUICKBUS], data)

    assert classes == ()


def test_a_class_with_one_io_with_an_entity_is_required() -> None:
    classes = _class_coordinator()._webio_classes_with_commands([WebioClass.IO, WebioClass.QUICKBUS], _data())

    assert classes == (WebioClass.IO, WebioClass.QUICKBUS)
