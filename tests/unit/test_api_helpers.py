"""Pure module-level and static helpers of api.py."""

from typing import Any

import pytest

from custom_components.comexio.api import (
    ComexioAPI,
    SafeDict,
    _balanced_rows_per_col,
    _blank_marker_id_candidates,
    _is_extension_offline,
    _is_local_address,
    _placed_marker_ids,
)
from custom_components.comexio.const import (
    WEBIO_CLASS_KNX,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    MarkerKind,
)


def test_safe_dict_keeps_unknown_placeholders() -> None:
    assert "{Known} {Unknown}".format_map(SafeDict(Known="x")) == "x {Unknown}"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.1.10", True),
        ("10.0.0.1:80", True),
        ("127.0.0.1", True),
        ("169.254.1.1", True),
        ("[fe80::1]:8123", True),
        ("[::1]", True),
        ("comexio.local", True),
        ("comexio.lan.", True),
        ("localhost", True),
        ("  192.168.1.10  ", True),
        ("8.8.8.8", False),
        ("comexio.example.com", False),
        ("[fe80::1", False),
        ("", False),
    ],
)
def test_is_local_address(host: str, expected: bool) -> None:
    assert _is_local_address(host) is expected


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [("1000-2000-3000", False), ("5010", True), ("", True)],
)
def test_is_extension_offline(identifier: str, expected: bool) -> None:
    assert _is_extension_offline(identifier) is expected


@pytest.mark.parametrize(
    ("n_items", "max_rows", "expected"),
    [
        (50, 26, 25),  # even 25/25 split, not a greedy 26/24 (live report 2026-09-21)
        (26, 26, 26),
        (27, 26, 14),
        (10, 26, 10),
        (0, 26, 26),
        (0, 0, 1),
    ],
)
def test_balanced_rows_per_col(n_items: int, max_rows: int, expected: int) -> None:
    assert _balanced_rows_per_col(n_items, max_rows) == expected


def test_blank_marker_id_candidates_only_returns_untitled_ids_in_range() -> None:
    items = [
        {"Id": 1, "Name": ""},
        {"Id": 2, "Name": "Titled"},
        {"Id": 3},
        {"Id": 9, "Name": ""},
        {"Id": "4", "Name": ""},
        "not-a-dict",
    ]

    assert _blank_marker_id_candidates(items, 1, 9) == {1, 3}
    assert _blank_marker_id_candidates(items, 2, None) == {3, 9}


def test_placed_marker_ids_collects_matching_references_across_plans() -> None:
    plans: dict[int, dict] = {
        1: {
            "elements": {
                "a": {"reference": {"type": 2, "ref_id": 5}},
                "b": {"reference": {"type": "2", "ref_id": "6"}},
                "c": {"reference": {"type": 1, "ref_id": 7}},
                "d": {"reference": {"type": 2, "ref_id": "not-a-number"}},
                "e": "not-a-dict",
            }
        },
        2: {"elements": None},
        3: {},
    }

    assert _placed_marker_ids(plans, 2) == {5, 6}


def test_clean_value(comexio_api: ComexioAPI) -> None:
    assert comexio_api._clean_value("21,5") == 21.5
    assert comexio_api._clean_value(3) == 3.0
    assert comexio_api._clean_value(None) == 0
    assert comexio_api._clean_value("n/a") == 0


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        ({"3": "a", 7: "b"}, [("3", "a"), ("7", "b")]),
        (["a", "b"], [("0", "a"), ("1", "b")]),
        (None, []),
    ],
)
def test_iter_group_normalizes_array_and_object_shape(group: Any, expected: list[tuple[str, Any]]) -> None:
    assert list(ComexioAPI._iter_group(group)) == expected


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("°C", "°C"), ("C", "°C"), ("\\u00b0C", "°C"), ("0/1", ""), ("1/0", ""), ("?", ""), ("%", "%")],
)
def test_normalize_io_unit(unit: str, expected: str) -> None:
    assert ComexioAPI._normalize_io_unit(unit) == expected


@pytest.mark.parametrize(
    ("title", "module_key", "expected"),
    [
        ("Licht", "2", MarkerKind.NORMAL),
        ("Status [RO]", "2", MarkerKind.READ_ONLY),
        ("Status [RO]  ", "2", MarkerKind.READ_ONLY),
        ("Klingel [TRIG]", "2", MarkerKind.TRIGGER),
        ("Impuls [TP]", "2", MarkerKind.TRIGGER),
        ("Klingel [TRIG] [RO]", "2", MarkerKind.READ_ONLY),
        ("Rollo [K12]", "2", MarkerKind.KNX_BRIDGE),
        ("Rollo [K12]", "11", MarkerKind.NORMAL),
        ("[RO] Prefix only", "2", MarkerKind.NORMAL),
    ],
)
def test_marker_kind(title: str, module_key: str, expected: MarkerKind) -> None:
    assert ComexioAPI._marker_kind(title, module_key=module_key) == expected


@pytest.mark.parametrize(
    ("v_min", "v_max", "expected"),
    [
        (0, 100, (0, 100)),
        (-32768, 32767, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
        (0, 30000, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
        (0, 40001, (0, 40001)),
    ],
)
def test_safe_webio_range_widens_int16_danger_zone(v_min: float, v_max: float, expected: tuple[float, float]) -> None:
    assert ComexioAPI._safe_webio_range(v_min, v_max) == expected


@pytest.mark.parametrize(
    ("dpt_min", "dpt_max", "expected"),
    [
        (None, 100, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
        (0, None, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
        (-273, 670760, (-273, 670760)),
        (0, 4294967295, (0, 4294967295)),
        (-32768, 32767, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
    ],
)
def test_knx_webio_range(dpt_min: float | None, dpt_max: float | None, expected: tuple[float, float]) -> None:
    assert ComexioAPI._knx_webio_range(dpt_min, dpt_max) == expected


def test_lua_escape_escapes_backslash_before_quote() -> None:
    assert ComexioAPI._lua_escape('a"b\\c') == 'a\\"b\\\\c'


def test_build_marker_webio_command_digital() -> None:
    command = ComexioAPI._build_marker_webio_command(
        {"id": "5", "name": "M5 Licht", "type": "digital"}, "/api/webhook/comexio_srv"
    )

    assert command["Name"] == "HA M5 Licht"
    assert (command["TypeId"], command["Min"], command["Max"]) == (1, 0, 1)
    assert command["Parameter"] == "/api/webhook/comexio_srv"
    assert command["Data"] == (
        'function data(a)\r\n  local d = { id="5", value=a, type="marker" }\r\n  return json_stringify(d)\r\nend'
    )


def test_build_marker_webio_command_analog_marker_uses_generic_range() -> None:
    command = ComexioAPI._build_marker_webio_command(
        {"id": "2", "name": "M2 Soll", "type": "analog", "dpt_min": 0, "dpt_max": 10}, "/hook"
    )

    assert (command["TypeId"], command["Min"], command["Max"]) == (2, WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)


def test_build_marker_webio_command_analog_knx_uses_dpt_range() -> None:
    command = ComexioAPI._build_marker_webio_command(
        {"id": "8", "name": "K8 Temp", "type": "analog", "dpt_min": -273, "dpt_max": 670760},
        "/hook",
        source_type=WEBIO_CLASS_KNX,
    )

    assert (command["Min"], command["Max"]) == (-273, 670760)
    assert 'type="knx"' in command["Data"]


def test_build_io_webio_command_coerces_missing_bounds() -> None:
    command = ComexioAPI._build_io_webio_command(
        {"ext_name": 'UD "1"', "identifier": "QI1", "is_binary": False, "min": None, "max": None}, "/hook"
    )

    assert command["Name"] == 'HA IO UD "1" QI1'
    assert (command["TypeId"], command["Min"], command["Max"]) == (2, 0, 100)
    assert 'ext="UD \\"1\\"", io="QI1", value=a, type="io"' in command["Data"]
