"""Pure module-level and static helpers of api.py."""

import json

import pytest

from custom_components.comexio.api import (
    ComexioAPI,
    _balanced_rows_per_col,
    _blank_marker_id_candidates,
    _placed_marker_ids,
)
from custom_components.comexio.const import WEBIO_CLASS_IO, WEBIO_CLASS_KNX, WEBIO_MARKER_ANALOG_MIN
from tests.common import load_json_fixture


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


PARSED = {
    "markers": [{"id": "5", "name": "M5 Licht", "type": "digital"}, {"id": "6", "name": "M6 Soll", "type": "analog"}],
    "io": [{"ext_name": "BASE", "identifier": "Q1", "is_binary": True, "min": 0, "max": 1}],
    "knx": [{"id": "8", "name": "K8 Temp", "type": "analog", "dpt_min": -273, "dpt_max": 670760}],
}


def test_build_webio_commands_targets_the_servers_webhook(comexio_api: ComexioAPI) -> None:
    commands = comexio_api.build_webio_commands("srv", PARSED, ignored_marker_ids={6})

    assert [c["Name"] for c in commands] == ["HA M5 Licht", "HA IO BASE Q1", "HA K8 Temp"]
    assert {c["Parameter"] for c in commands} == {"/api/webhook/comexio_srv"}
    assert (commands[2]["Min"], commands[2]["Max"]) == (-273, 670760)


def test_build_webio_commands_restricts_to_one_class(comexio_api: ComexioAPI) -> None:
    assert [c["Name"] for c in comexio_api.build_webio_commands("srv", PARSED, WEBIO_CLASS_IO)] == ["HA IO BASE Q1"]


def test_generate_webio_json_wraps_one_class(comexio_api: ComexioAPI) -> None:
    payload = json.loads(comexio_api.generate_webio_json("srv", "HomeAssistant [KNX]", PARSED, WEBIO_CLASS_KNX))

    assert payload["base"] == {"Identifier": "HomeAssistant [KNX]", "UseCookies": 0, "Login": 2, "BaseId": 0}
    assert [c["Name"] for c in payload["commands"]] == ["HA K8 Temp"]


@pytest.mark.parametrize(
    ("catalog_version", "expected_min"),
    [("11.0.2", -273), ("11.0.1", WEBIO_MARKER_ANALOG_MIN)],
)
def test_knx_loopback_command_ignores_a_stale_catalog(
    comexio_api: ComexioAPI, catalog_version: str, expected_min: float
) -> None:
    """The cached DPT catalog only counts while comexio_version still matches its fetch."""
    comexio_api.comexio_version = "11.0.2"
    comexio_api.seed_knx_dpt_catalog(load_json_fixture("knx_dpt_catalog.json"), catalog_version)

    command = comexio_api._build_knx_loopback_webio_command(k_id=1, marker_id=300, is_analog=True)

    assert (command["Name"], command["Min"]) == ("KNX K1 to M300", expected_min)
