"""A restore may leave out the blocks Comexio no longer has (function_plan_block_keys.omit_unresolved_blocks)."""

import copy
from typing import Any

from aiocomexio.function_plan import plan_hash
import pytest

from custom_components.comexio.function_plan_block_keys import (
    CHECK_UNRESOLVED,
    CHECK_UNVERIFIED,
    OMITTED_BLOCKS,
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_NO_KEY,
    SNAPSHOT_KEYS,
    SNAPSHOT_KEYS_SOURCE,
    SOURCE_BACKFILLED_UNVERIFIED,
    SOURCE_CAPTURED,
    UNRESOLVED_BLOCKS,
    UNVERIFIED_BLOCK_PROBLEMS,
    block_check,
    capture_block_keys,
    omit_unresolved_blocks,
    omitted_block_lines,
    resolve_block_ids,
)
from custom_components.comexio.function_plan_block_settings import SNAPSHOT_BLOCK_SETTINGS
from tests.common import load_json_fixture

OR_KEY = "or/dd/d"
NOT_KEY = "not/d/d"
OR = {"name": "or", "n_in": 2, "n_out": 1, "autogrow": 16, "in_types": [0, 0], "out_types": [0], "key": OR_KEY}
NOT = {"name": "not", "n_in": 1, "n_out": 1, "autogrow": 0, "in_types": [0], "out_types": [0], "key": NOT_KEY}
# The catalog the fixture plan was captured with: element 4 is an Oder (id 5), element 7 a Nicht (id 23).
ADD = {"name": "add", "n_in": 2, "n_out": 1, "autogrow": 0, "in_types": [1, 1], "out_types": [1], "key": "add/aa/a"}
FUB_BASE = {"5": OR, "23": NOT}
WITHOUT_NOT = {"5": OR}  # the Nicht block is gone: element 7 is missing
WITHOUT_OR = {"23": NOT}  # the Oder block is gone: element 4 is missing
TWO_NOT = {"5": OR, "24": NOT, "25": NOT}  # the Nicht block can no longer be told apart: element 7 is ambiguous


def _wire(source: int, *sinks: int) -> dict[str, Any]:
    return {
        "input": {"FubElementId": source, "IOPos": 0, "Inverted": False},
        "output": [{"FubElementId": sink, "IOPos": 0, "Inverted": False} for sink in sinks],
    }


@pytest.fixture
def snapshot() -> dict[str, Any]:
    fixture = load_json_fixture("function_plan.json")
    plan = {"elements": fixture["elements"], "connections": fixture["connections"]}
    return {
        **plan,
        "hash": plan_hash(plan),
        "captured_at": "2026-09-10T08:00:00+00:00",
        "plan_name": "Lights",
        SNAPSHOT_KEYS: capture_block_keys(plan["elements"], FUB_BASE),
        SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED,
    }


def _omitted(snapshot: dict[str, Any], fub_base: dict[str, Any]) -> dict[str, Any]:
    return omit_unresolved_blocks(resolve_block_ids(snapshot, fub_base))


@pytest.mark.parametrize("malformed", [5, [1], {"input": {"FubElementId": 4}, "output": 5}, {"output": "x"}])
def test_resolve_block_ids_refuses_a_snapshot_with_a_malformed_wire(snapshot: dict[str, Any], malformed: Any) -> None:
    """The cut reads wires as mappings because resolve_block_ids (plan_hash) already refused anything else."""
    snapshot["connections"]["99"] = malformed

    with pytest.raises((AttributeError, TypeError)):
        resolve_block_ids(snapshot, WITHOUT_NOT)


def test_a_block_that_is_gone_is_left_out_with_the_wire_that_ended_at_it(snapshot: dict[str, Any]) -> None:
    result = _omitted(snapshot, WITHOUT_NOT)

    assert "7" not in result["elements"]
    assert set(snapshot["elements"]) - set(result["elements"]) == {"7"}
    assert set(snapshot["connections"]) - set(result["connections"]) == {"13"}  # element 6 -> element 7
    assert result[OMITTED_BLOCKS] == [
        {
            "element": "7",
            "name": "",
            "ref_id": 23,
            "key": NOT_KEY,
            "reason": REASON_MISSING,
            "connections": 1,
            "unfed": [],
        }
    ]
    assert result[UNRESOLVED_BLOCKS] == []


def test_a_block_that_can_no_longer_be_told_apart_is_left_out_too(snapshot: dict[str, Any]) -> None:
    result = _omitted(snapshot, TWO_NOT)

    assert set(result["elements"]) == set(snapshot["elements"]) - {"7"}
    assert [block["reason"] for block in result[OMITTED_BLOCKS]] == [REASON_AMBIGUOUS]


def test_every_wire_of_a_left_out_source_goes_and_its_neighbours_stay(snapshot: dict[str, Any]) -> None:
    result = _omitted(snapshot, WITHOUT_OR)

    # Element 4 is fed by wires 10 and 11 and feeds wire 12; element 5 stays, now without an input.
    assert set(result["connections"]) == {"13", "14", "15"}
    assert set(result["elements"]) == set(snapshot["elements"]) - {"4"}
    assert result[OMITTED_BLOCKS][0]["connections"] == 3


def test_a_fan_out_only_loses_the_end_at_the_left_out_block(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(3, 7, 5)  # element 3 feeds the gone block 7 and the output 5

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result["connections"]["20"] == _wire(3, 5)
    assert result[OMITTED_BLOCKS][0]["connections"] == 2  # wire 13 gone, wire 20 shortened


def test_the_snapshot_is_cut_to_what_a_restore_builds(snapshot: dict[str, Any]) -> None:
    stored = copy.deepcopy(snapshot)
    resolved = resolve_block_ids(snapshot, WITHOUT_NOT)

    result = omit_unresolved_blocks(resolved)

    # The hash is over what is left, so the check after the restore compares like with like.
    assert result["hash"] == plan_hash(result)
    assert result["hash"] != resolved["hash"]
    assert snapshot == stored  # the stored backup is never rewritten
    assert "7" in resolved["elements"]  # neither is the snapshot that came in


def test_an_element_keeps_its_own_name_in_the_report(snapshot: dict[str, Any]) -> None:
    snapshot["elements"]["7"]["name"] = "Kitchen light"

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result[OMITTED_BLOCKS][0]["name"] == "Kitchen light"


def test_the_block_settings_of_a_left_out_block_go_with_it(snapshot: dict[str, Any]) -> None:
    snapshot[SNAPSHOT_BLOCK_SETTINGS] = {"4": {"autohide": "1"}, "7": {"time_up": "30"}}

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result[SNAPSHOT_BLOCK_SETTINGS] == {"4": {"autohide": "1"}}


def test_a_snapshot_without_block_settings_gets_none(snapshot: dict[str, Any]) -> None:
    assert SNAPSHOT_BLOCK_SETTINGS not in _omitted(snapshot, WITHOUT_NOT)


def test_a_block_unknown_at_capture_is_not_left_out_and_still_needs_the_opt_in(snapshot: dict[str, Any]) -> None:
    snapshot[SNAPSHOT_KEYS] = {"5": OR_KEY}  # element 7's block (id 23) was not in the catalog when it was taken
    resolved = resolve_block_ids(snapshot, WITHOUT_NOT)

    assert omit_unresolved_blocks(resolved) is resolved
    assert [block["reason"] for block in resolved[UNRESOLVED_BLOCKS]] == [REASON_NO_KEY]


def test_what_is_left_after_omitting_is_judged_on_its_own(snapshot: dict[str, Any]) -> None:
    """One gone block and one unknown one: the gone one goes, the unknown one still asks for the opt-in."""
    snapshot[SNAPSHOT_KEYS] = {"23": NOT_KEY}  # the Oder block (element 4) was unknown at capture
    result = _omitted(snapshot, WITHOUT_NOT)

    assert [block["element"] for block in result[OMITTED_BLOCKS]] == ["7"]
    assert [(block["element"], block["reason"]) for block in result[UNRESOLVED_BLOCKS]] == [("4", REASON_NO_KEY)]
    assert block_check(result)["status"] == CHECK_UNVERIFIED


def test_once_everything_gone_is_left_out_nothing_stands_in_the_way(snapshot: dict[str, Any]) -> None:
    assert block_check(_omitted(snapshot, WITHOUT_NOT)) is None


def test_a_snapshot_with_nothing_to_leave_out_comes_back_as_it_is(snapshot: dict[str, Any]) -> None:
    assert omit_unresolved_blocks(snapshot) is snapshot
    resolved = resolve_block_ids(snapshot, FUB_BASE)
    assert omit_unresolved_blocks(resolved) is resolved


def test_a_wire_without_an_input_or_an_end_without_an_element_is_kept(snapshot: dict[str, Any]) -> None:
    """Comexio keeps connections without any "input"; such wires and ends name no omitted element."""
    snapshot["connections"]["90"] = {"output": [{"FubElementId": 5, "IOPos": 0, "Inverted": False}]}
    snapshot["connections"]["91"] = {"input": {"IOPos": 0}, "output": [{"IOPos": 0}]}
    snapshot["connections"]["92"] = {"input": {"FubElementId": 3, "IOPos": 0}, "output": []}

    result = _omitted(snapshot, WITHOUT_NOT)

    assert {key: result["connections"][key] for key in ("90", "91", "92")} == {
        key: snapshot["connections"][key] for key in ("90", "91", "92")
    }
    assert result[OMITTED_BLOCKS][0]["connections"] == 1  # only wire 13


def test_the_report_lines_name_the_block_and_what_its_wires_cost(snapshot: dict[str, Any]) -> None:
    snapshot["elements"]["7"]["name"] = "Kitchen light"

    lines = omitted_block_lines(_omitted(snapshot, WITHOUT_NOT)[OMITTED_BLOCKS])

    assert lines == [
        "element 7 (Kitchen light): block id 23 (not/d/d): no block of this kind exists on Comexio today "
        "(app or firmware removed it?); 1 connection(s) dropped or shortened"
    ]


def test_neither_the_resolved_nor_the_stored_snapshot_is_changed_in_place(snapshot: dict[str, Any]) -> None:
    """resolve_block_ids shares its wires and settings with the stored snapshot, so cutting must copy them."""
    snapshot["connections"]["20"] = _wire(3, 7, 5)
    snapshot[SNAPSHOT_BLOCK_SETTINGS] = {"4": {"autohide": "1"}, "7": {"time_up": "30"}}
    stored = copy.deepcopy(snapshot)
    resolved = resolve_block_ids(snapshot, WITHOUT_NOT)
    resolved_before = copy.deepcopy(resolved)

    omit_unresolved_blocks(resolved)

    assert resolved == resolved_before
    assert snapshot == stored


def test_everything_else_of_the_snapshot_is_carried_over(snapshot: dict[str, Any]) -> None:
    """The omission only cuts; keys, source and the other verdicts still decide what a restore needs."""
    snapshot[SNAPSHOT_KEYS_SOURCE] = SOURCE_BACKFILLED_UNVERIFIED
    resolved = resolve_block_ids(snapshot, WITHOUT_NOT)

    result = omit_unresolved_blocks(resolved)

    cut = {"elements", "connections", "hash", UNRESOLVED_BLOCKS, OMITTED_BLOCKS, UNVERIFIED_BLOCK_PROBLEMS}
    assert {key: value for key, value in result.items() if key not in cut} == {
        key: value for key, value in resolved.items() if key not in cut
    }
    assert resolved[UNVERIFIED_BLOCK_PROBLEMS] == ["element 7: block id unresolved:23 unknown today"]
    assert result[UNVERIFIED_BLOCK_PROBLEMS] == []  # the one problem was the left-out block's
    assert block_check(result)["status"] == CHECK_UNVERIFIED  # a backfilled backup still asks for the opt-in


def test_a_block_without_any_wire_is_left_out_with_a_count_of_zero(snapshot: dict[str, Any]) -> None:
    del snapshot["connections"]["13"]

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result["connections"] == snapshot["connections"]
    assert result[OMITTED_BLOCKS][0]["connections"] == 0


def test_a_plan_without_any_wire_loses_only_the_element() -> None:
    elements = {"7": {"name": "", "reference": {"type": 5, "ref_id": 23}}}
    plan = {"elements": elements, "connections": {}}
    snapshot = {**plan, "hash": plan_hash(plan), SNAPSHOT_KEYS: {"23": NOT_KEY}, SNAPSHOT_KEYS_SOURCE: SOURCE_CAPTURED}

    result = _omitted(snapshot, {"5": OR})

    assert result["elements"] == {}
    assert result["connections"] == {}
    assert result[OMITTED_BLOCKS][0]["connections"] == 0


def test_a_wire_between_two_left_out_blocks_counts_for_both(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(4, 7)

    result = _omitted(snapshot, {"40": ADD})  # neither the Oder nor the Nicht block exists any more

    assert "20" not in result["connections"]
    # Element 4: wires 10, 11 (sink), 12, 20 (source). Element 7: wires 13, 20.
    assert {block["element"]: block["connections"] for block in result[OMITTED_BLOCKS]} == {"4": 4, "7": 2}


def test_two_ends_at_one_left_out_block_count_the_wire_once(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(3, 7, 7, 5)

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result["connections"]["20"] == _wire(3, 5)
    assert result[OMITTED_BLOCKS][0]["connections"] == 2  # wire 13 and this one


def test_a_wire_whose_only_ends_are_two_at_the_left_out_block_is_gone_and_counted_once(
    snapshot: dict[str, Any],
) -> None:
    snapshot["connections"]["20"] = _wire(3, 7, 7)

    result = _omitted(snapshot, WITHOUT_NOT)

    assert "20" not in result["connections"]
    assert result[OMITTED_BLOCKS][0]["connections"] == 2  # wire 13 and this one


def test_ids_are_matched_as_text_whatever_type_the_snapshot_wrote_them_in(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(3, 7, 5)
    snapshot["connections"]["20"]["output"][0]["FubElementId"] = "7"  # a wire end written as text
    snapshot[SNAPSHOT_BLOCK_SETTINGS] = {7: {"time_up": "30"}, "4": {"autohide": "1"}}  # a settings key as an int

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result["connections"]["20"] == _wire(3, 5)
    assert result[SNAPSHOT_BLOCK_SETTINGS] == {"4": {"autohide": "1"}}


@pytest.mark.parametrize("fub_base", [WITHOUT_NOT, TWO_NOT])
def test_wires_settings_and_hash_are_cut_whichever_reason_left_the_block_out(
    snapshot: dict[str, Any], fub_base: dict[str, Any]
) -> None:
    snapshot[SNAPSHOT_BLOCK_SETTINGS] = {"7": {"time_up": "30"}}

    result = _omitted(snapshot, fub_base)

    assert "13" not in result["connections"]
    assert result[SNAPSHOT_BLOCK_SETTINGS] == {}
    assert result["hash"] == plan_hash(result)


def test_every_reason_has_its_own_words_in_the_report_line() -> None:
    block = {"element": "7", "name": "", "ref_id": 23, "key": NOT_KEY, "connections": 0, "unfed": []}

    gone, several = (
        omitted_block_lines([{**block, "reason": reason}])[0] for reason in (REASON_MISSING, REASON_AMBIGUOUS)
    )

    tail = "connection(s) dropped or shortened"
    assert gone.endswith(f"no block of this kind exists on Comexio today (app or firmware removed it?); 0 {tail}")
    assert several.endswith(f"several blocks on Comexio today match it; 0 {tail}")


def test_a_report_line_without_name_or_key_still_reads(snapshot: dict[str, Any]) -> None:
    line = omitted_block_lines(
        [
            {
                "element": "7",
                "name": "",
                "ref_id": 23,
                "key": None,
                "reason": REASON_MISSING,
                "connections": 0,
                "unfed": [],
            }
        ]
    )[0]

    assert line.startswith("element 7: block id 23 (no key): ")


def test_the_elements_that_lose_their_only_input_are_reported(snapshot: dict[str, Any]) -> None:
    result = _omitted(snapshot, WITHOUT_OR)

    # Wire 12 ran from the left-out element 4 to element 5, which now has no input.
    assert result[OMITTED_BLOCKS][0]["unfed"] == ["5"]
    assert omitted_block_lines(result[OMITTED_BLOCKS])[0].endswith("; input lost at element 5")


def test_unfed_elements_are_listed_by_number_and_a_left_out_sink_is_not_one(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(4, 7, 5, 9, 12)  # the Nicht (7) is a sink of its own kind: it is left out

    result = _omitted(snapshot, {"40": ADD})  # neither the Oder nor the Nicht block exists any more

    by_element = {block["element"]: block["unfed"] for block in result[OMITTED_BLOCKS]}
    assert by_element["4"] == ["5", "9", "12"]
    assert by_element["7"] == []


def test_a_fan_out_that_only_loses_a_sink_leaves_nobody_unfed(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(3, 7, 5)

    assert _omitted(snapshot, WITHOUT_NOT)[OMITTED_BLOCKS][0]["unfed"] == []


def test_an_output_written_as_an_object_is_cut_like_a_list(snapshot: dict[str, Any]) -> None:
    """Comexio writes "output" as {"0": {...}} too, and plan_hash reads both shapes."""
    snapshot["connections"]["20"] = {
        "input": {"FubElementId": 3, "IOPos": 0, "Inverted": False},
        "output": {
            "0": {"FubElementId": 7, "IOPos": 0, "Inverted": False},
            "1": {"FubElementId": 5, "IOPos": 0, "Inverted": False},
        },
    }
    snapshot["hash"] = plan_hash(snapshot)

    result = _omitted(snapshot, WITHOUT_NOT)

    assert result["connections"]["20"] == _wire(3, 5)
    assert result[OMITTED_BLOCKS][0]["connections"] == 2


def test_an_object_shaped_output_of_a_left_out_source_unfeeds_its_sinks(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["12"]["output"] = {"0": {"FubElementId": 5, "IOPos": 0, "Inverted": False}}
    snapshot["hash"] = plan_hash(snapshot)

    result = _omitted(snapshot, WITHOUT_OR)

    assert "12" not in result["connections"]
    assert result[OMITTED_BLOCKS][0]["unfed"] == ["5"]


def test_a_second_pass_changes_nothing(snapshot: dict[str, Any]) -> None:
    result = _omitted(snapshot, WITHOUT_NOT)

    assert omit_unresolved_blocks(result) is result


def test_a_problem_of_a_left_out_block_does_not_keep_the_opt_in_alive(snapshot: dict[str, Any]) -> None:
    """The lines are per element ("element 7: ..."): only the ones of a block that is gone go, 70 is not 7."""
    resolved = {
        **resolve_block_ids(snapshot, WITHOUT_NOT),
        UNVERIFIED_BLOCK_PROBLEMS: [
            "element 7: output 3 beyond the not block's outputs",
            "element 4: x",
            "element 70: y",
        ],
    }

    result = omit_unresolved_blocks(resolved)

    assert result[UNVERIFIED_BLOCK_PROBLEMS] == ["element 4: x", "element 70: y"]
    assert resolved[UNVERIFIED_BLOCK_PROBLEMS][0].startswith("element 7:")  # the input is not changed


def test_a_snapshot_without_problem_lines_gets_none(snapshot: dict[str, Any]) -> None:
    assert UNVERIFIED_BLOCK_PROBLEMS not in _omitted(snapshot, WITHOUT_NOT)


def test_a_sink_without_an_element_behind_a_left_out_source_is_not_reported_unfed(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["12"]["output"].append({"IOPos": 0})

    result = _omitted(snapshot, WITHOUT_OR)

    assert result[OMITTED_BLOCKS][0]["unfed"] == ["5"]
    assert omitted_block_lines(result[OMITTED_BLOCKS])[0].endswith("; input lost at element 5")


def test_the_unfed_elements_of_every_wire_of_a_left_out_source_are_added_up(snapshot: dict[str, Any]) -> None:
    snapshot["connections"]["20"] = _wire(4, 9)  # a second wire out of element 4, to another sink than wire 12

    result = _omitted(snapshot, WITHOUT_OR)

    assert result[OMITTED_BLOCKS][0]["unfed"] == ["5", "9"]


def test_the_problems_of_every_left_out_block_go(snapshot: dict[str, Any]) -> None:
    resolved = {
        **resolve_block_ids(snapshot, {"40": ADD}),  # neither the Oder (element 4) nor the Nicht block (7) exists
        UNVERIFIED_BLOCK_PROBLEMS: ["element 4: a", "element 7: b", "element 5: c"],
    }

    assert omit_unresolved_blocks(resolved)[UNVERIFIED_BLOCK_PROBLEMS] == ["element 5: c"]


def test_a_comexio_name_with_line_breaks_stays_one_line_in_the_report() -> None:
    block = {"element": "7", "name": "a\nb", "ref_id": 23, "key": NOT_KEY, "reason": REASON_MISSING}

    line = omitted_block_lines([{**block, "connections": 0, "unfed": []}])[0]

    assert "\n" not in line
    assert line.startswith("element 7 (a b): block id 23 ")


def test_a_block_key_with_a_line_break_stays_one_line_in_the_report() -> None:
    block = {"element": "7", "name": "", "ref_id": 23, "key": "not\n/d/d", "reason": REASON_MISSING}

    line = omitted_block_lines([{**block, "connections": 0, "unfed": []}])[0]

    assert "\n" not in line
    assert "(not /d/d)" in line


def test_element_and_block_ids_with_line_breaks_stay_one_line_in_the_report() -> None:
    block = {"element": "7\nx", "name": "", "ref_id": "23\ny", "key": NOT_KEY, "reason": REASON_MISSING}

    line = omitted_block_lines([{**block, "connections": 1, "unfed": ["5\nz", "6"]}])[0]

    assert "\n" not in line
    assert line.startswith("element 7 x: block id 23 y (")
    assert line.endswith("; input lost at element 5 z, 6")


def test_the_refusal_lines_of_a_check_keep_ids_and_keys_on_one_line() -> None:
    entry = {"element": "7\nx", "ref_id": "23\ny", "key": "not\n/d/d", "reason": REASON_MISSING}

    check = block_check({UNRESOLVED_BLOCKS: [entry]})

    assert check is not None
    assert check["status"] == CHECK_UNRESOLVED
    assert len(check["problems"]) == 1
    assert "\n" not in check["problems"][0]
    assert check["problems"][0].startswith("element 7 x: block id 23 y (not /d/d): ")
