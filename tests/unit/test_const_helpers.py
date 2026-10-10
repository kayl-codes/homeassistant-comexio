"""Pure helper functions in const.py (IO layout order, ignore-list parsing, Web-IO class names)."""

import pytest

from custom_components.comexio.const import (
    CONF_IMPORT_DISABLED_IGNORED,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    expand_ignored_marker_ids,
    import_disabled_issue_id,
    io_column_rows,
    io_group_headers,
    io_sort_key,
    parse_ignored_marker_tokens,
    prune_import_disabled_ignored,
    webio_class_name,
)


def test_io_sort_key_orders_by_group_then_number() -> None:
    identifiers = ["TL1", "Q10", "I2", "QI1", "XYZ", "Q2", "EI1_WIN", "I10", "UL1", "AI1"]

    assert sorted(identifiers, key=io_sort_key) == [
        "I2",
        "I10",
        "AI1",
        "EI1_WIN",
        "Q2",
        "Q10",
        "QI1",
        "XYZ",
        "TL1",
        "UL1",
    ]


def test_io_column_rows_reserve_header_and_separator_rows() -> None:
    identifiers = ["Q1", "I2", "I1", "TL1", "UL1"]

    assert io_column_rows(identifiers) == {"I1": 1, "I2": 2, "Q1": 5, "TL1": 8, "UL1": 9}
    assert set(io_group_headers(identifiers)) == {0, 4, 7}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("M1, M5; 8-10", [("M1", 1), ("M5", 5), ("8-10", (8, 10))]),
        ("12-M9", [("12-M9", (9, 12))]),
        ("  ", []),
        ("M, abc, 3-x", [("M", None), ("abc", None), ("3-x", None)]),
        ("1.2 3", [("1", 1), ("2", 2), ("3", 3)]),
    ],
)
def test_parse_ignored_marker_tokens(raw: str, expected: list) -> None:
    assert list(parse_ignored_marker_tokens(raw)) == expected


def test_expand_ignored_marker_ids_skips_invalid_tokens() -> None:
    assert expand_ignored_marker_ids("M1, 4-6, junk, m9") == {1, 4, 5, 6, 9}


def test_expand_ignored_ids_with_knx_prefix() -> None:
    assert expand_ignored_marker_ids("K3, k5-K6", prefix_chars="Kk") == {3, 5, 6}


def test_webio_class_name_suffixes() -> None:
    assert [webio_class_name("HA", c) for c in (WEBIO_CLASS_MARKER, WEBIO_CLASS_IO, WEBIO_CLASS_KNX)] == [
        "HA [M]",
        "HA [IO]",
        "HA [KNX]",
    ]


def test_import_disabled_issue_id_names_the_class() -> None:
    assert import_disabled_issue_id(WEBIO_CLASS_KNX, "srv") == "import_disabled_knx_srv"
    assert import_disabled_issue_id("marker", "srv") == "import_disabled_marker_srv"


@pytest.mark.parametrize(
    ("options", "data", "expected"),
    [
        # KNX switched on again in the options: its "ignore" goes, the marker one stays.
        (
            {"import_knx": True, "import_markers": False, CONF_IMPORT_DISABLED_IGNORED: ["knx", "marker"]},
            {},
            ["marker"],
        ),
        # The import flag only in the entry data (as the config flow saves it) counts too.
        ({CONF_IMPORT_DISABLED_IGNORED: ["knx"]}, {"import_knx": True}, None),
        # Options override the data; markers default to on, KNX to off.
        ({"import_knx": False, CONF_IMPORT_DISABLED_IGNORED: ["knx", "marker"]}, {"import_knx": True}, ["knx"]),
        ({}, {}, None),
    ],
)
def test_prune_import_disabled_ignored(options: dict, data: dict, expected: list[str] | None) -> None:
    before = {key: list(value) if isinstance(value, list) else value for key, value in options.items()}
    pruned = prune_import_disabled_ignored(options, data)
    assert pruned.get(CONF_IMPORT_DISABLED_IGNORED) == expected
    assert (CONF_IMPORT_DISABLED_IGNORED in pruned) is (expected is not None)
    assert options == before  # the caller's options stay untouched
