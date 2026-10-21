"""Ids the knx_dpt_ambiguous repair adds to knx_dpt_suffix_ignored keep the ranges already in the option."""

import pytest

from custom_components.comexio.repairs import _add_knx_dpt_suffix_ignored_ids


@pytest.mark.parametrize(
    ("existing", "new_ids", "expected"),
    [
        # A range written by hand stays a range instead of being flattened into K5,K6,…,K9.
        ("K5-9, K20", {30}, "K5-9,K20,K30"),
        # An id the range already covers adds nothing.
        ("K5-9", {7}, "K5-9"),
        ("", {4, 2}, "K2,K4"),
    ],
    ids=["range_kept", "covered_id", "empty_option"],
)
def test_added_ids_keep_existing_ranges(existing: str, new_ids: set[int], expected: str) -> None:
    assert _add_knx_dpt_suffix_ignored_ids(existing, new_ids) == expected


def test_an_invalid_hand_edited_entry_is_dropped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    assert _add_knx_dpt_suffix_ignored_ids("K5, abc", {7}) == "K5,K7"
    assert "'abc'" in caplog.text
