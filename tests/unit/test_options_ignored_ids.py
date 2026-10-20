"""Ignore-list input in the options flow is saved in the one format the pruning writes back."""

import pytest
import voluptuous as vol

from custom_components.comexio.options_flow import _normalize_ignored_ids


@pytest.mark.parametrize(
    ("raw", "prefix_chars", "expected"),
    [
        ("12-8, 3, 1, 2", "Mm", "1,2,3,8-12"),
        ("M5; m7 9", "Mm", "5,7,9"),
        # A single a range covers is redundant; a one-ID range is a single.
        ("1-3,2", "Mm", "1-3"),
        ("8-8", "Mm", "8"),
        # The stored list carries no prefix, for KNX objects too.
        ("K12, k3-K5", "Kk", "3-5,12"),
        ("", "Mm", ""),
        (None, "Mm", ""),
    ],
    ids=["sorted", "prefixes_and_separators", "redundant_single", "one_id_range", "knx_prefix", "empty", "none"],
)
def test_ignored_ids_are_normalized(raw: str | None, prefix_chars: str, expected: str) -> None:
    assert _normalize_ignored_ids(raw, prefix_chars) == expected


def test_invalid_ignored_ids_are_rejected() -> None:
    with pytest.raises(vol.Invalid, match="'abc'"):
        _normalize_ignored_ids("3, abc")


def test_a_wide_range_is_saved_as_written() -> None:
    # Ranges are never expanded into single IDs, so their width needs no limit.
    assert _normalize_ignored_ids("1-1000000000, 5") == "1-1000000000"
