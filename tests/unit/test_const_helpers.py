"""Pure helper functions in const.py (IO layout order, ignore-list parsing, Web-IO class names)."""

from unittest.mock import patch

import pytest

from custom_components.comexio import const
from custom_components.comexio.const import (
    CONF_IMPORT_DISABLED_IGNORED,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    IgnoredIds,
    format_ignored_ids,
    ignored_id_in_ranges,
    import_disabled_issue_id,
    io_column_rows,
    io_group_headers,
    io_sort_key,
    one_line,
    parse_ignored_ids,
    parse_ignored_marker_tokens,
    prune_import_disabled_ignored,
    split_ignored_ids,
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
    assert parse_ignored_ids("M1, 4-6, junk, m9") == {1, 4, 5, 6, 9}


def test_expand_ignored_ids_with_knx_prefix() -> None:
    assert parse_ignored_ids("K3, k5-K6", prefix_chars="Kk") == {3, 5, 6}


def test_split_ignored_ids_keeps_singles_and_ranges_apart() -> None:
    assert split_ignored_ids("M1, 4-6, junk, m9") == ({1, 9}, {(4, 6)})


def test_split_ignored_ids_orders_a_reversed_range() -> None:
    assert split_ignored_ids("12-10") == (set(), {(10, 12)})


def test_wide_ranges_are_split_and_formatted_without_expanding_them() -> None:
    # Expanding '1-1000000000' into single IDs would exhaust memory; parsing and writing must not.
    with patch(f"{const.__name__}.range", side_effect=AssertionError("range expanded"), create=True):
        singles, ranges = split_ignored_ids("5, 1-1000000000, 2000000000")
        assert format_ignored_ids(singles, ranges) == "1-1000000000,2000000000"


def test_split_ignored_ids_leaves_out_a_single_a_range_covers() -> None:
    # Pruned as a single, 5 would be reported as removed while the range still ignores it.
    assert split_ignored_ids("1-10,5,12") == ({12}, {(1, 10)})


@pytest.mark.parametrize(
    ("singles", "ranges", "prefix", "expected"),
    [
        ({20, 3, 1}, {(8, 12)}, "", "1,3,8-12,20"),
        # A single a range covers is redundant; a one-ID range reads as a single.
        ({2, 9}, {(1, 3), (9, 9)}, "", "1-3,9"),
        # Same start: the shorter range first, so the output does not depend on set order.
        (set(), {(5, 12), (5, 9)}, "", "5-9,5-12"),
        ({3}, {(5, 6)}, "K", "K3,K5-6"),
        (set(), set(), "", ""),
    ],
    ids=["sorted_by_start", "redundant_single_dropped", "same_start", "prefix", "empty"],
)
def test_format_ignored_ids(singles: set[int], ranges: set[tuple[int, int]], prefix: str, expected: str) -> None:
    assert format_ignored_ids(singles, ranges, prefix) == expected


def test_formatted_ignored_ids_parse_back_to_the_same_ids() -> None:
    raw = format_ignored_ids({1, 7}, {(3, 5)}, "K")

    assert parse_ignored_ids(raw, prefix_chars="Kk") == {1, 3, 4, 5, 7}


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Licht Flur", "Licht Flur"),
        ("Licht\nFlur", "Licht Flur"),
        ("  Licht \r\n\t Flur\x07 ", "Licht Flur"),
        ("\n\x00", ""),
        (None, "None"),  # a backup's name read back as null
    ],
)
def test_one_line_cleans_a_comexio_name_for_output(raw: str | None, expected: str) -> None:
    assert one_line(raw) == expected


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [(4, False), (5, True), (9, True), (10, False), (20, True)],
    ids=["below", "start", "end", "above", "second_range"],
)
def test_ignored_id_in_ranges_is_inclusive(source_id: int, expected: bool) -> None:
    assert ignored_id_in_ranges(source_id, {(5, 9), (20, 20)}) is expected


def test_a_wide_range_ignores_its_ids_without_expanding_them() -> None:
    # Every range stays effective however wide; membership must not build the range's IDs.
    with patch(f"{const.__name__}.range", side_effect=AssertionError("range expanded"), create=True):
        ignored = parse_ignored_ids("3, 100-1000000000") - {500}

        assert [i in ignored for i in (3, 99, 100, 499, 500, 501, 1000000000, 1000000001)] == [
            True,
            False,
            True,
            True,
            False,
            True,
            True,
            False,
        ]
        assert len(ignored) == 1 + 999999901 - 1
        assert "100" not in ignored


@pytest.mark.parametrize(
    ("ignored", "expected"),
    [
        (IgnoredIds({1, 7}, {(3, 5)}), {1, 3, 4, 5, 7}),
        # Overlapping and adjacent ranges merge; a single a range covers counts once.
        (IgnoredIds({4, 9}, {(1, 5), (3, 6), (7, 7)}), {1, 2, 3, 4, 5, 6, 7, 9}),
        (IgnoredIds({1, 7}, {(3, 5)}) - {4, 7, 99}, {1, 3, 5}),
        # A second exclusion keeps the first one.
        (IgnoredIds({1, 7}, {(3, 5)}) - {4} - {7}, {1, 3, 5}),
        # A range narrower than the exclusions is walked: one ID is left.
        (IgnoredIds(ranges={(1, 3)}) - {1, 2}, {3}),
        (IgnoredIds(), set()),
    ],
    ids=["singles_and_range", "overlap_merged", "excluded", "excluded_twice", "narrow_range_one_left", "empty"],
)
def test_ignored_ids_iterate_and_count_every_covered_id(ignored: IgnoredIds, expected: set[int]) -> None:
    assert sorted(ignored) == sorted(expected)
    assert len(ignored) == len(expected)
    assert bool(ignored) is bool(expected)
    assert ignored == expected


def test_ignored_ids_beyond_sys_maxsize_are_truthy_without_len() -> None:
    # aiocomexio tests the ignore set's truth; len() of such a range would raise OverflowError.
    ignored = parse_ignored_ids("1-99999999999999999999")

    assert ignored
    assert 99999999999999999999 in ignored
    assert not IgnoredIds({5}, {(1, 2)}) - {1, 2, 5}


def test_subtracting_an_ignored_ids_is_refused_instead_of_expanding_its_ranges() -> None:
    wide = parse_ignored_ids("1-99999999999999999999")

    with pytest.raises(TypeError):
        IgnoredIds({5}) - wide
