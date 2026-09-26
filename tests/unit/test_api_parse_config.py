"""ComexioAPI.parse_config: scraped Comexio config -> markers / IOs / KNX / Web-IO commands."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_SCHEMA_MARKER,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_KNX,
    WEBIO_CLASS_MARKER,
    MarkerKind,
)
from tests.common import load_json_fixture


@pytest.fixture
def basic_result(parsing_api: ComexioAPI) -> dict[str, Any]:
    return parsing_api.parse_config(
        load_json_fixture("config_basic.json"),
        live_states={"1": "1", "2": "21,5"},
        referenced_markers={"5"},
        knx_live_states={"1": "19,5"},
        knx_dpt_catalog=load_json_fixture("knx_dpt_catalog.json"),
    )


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def test_parse_config_snapshot(basic_result: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    """Characterization snapshot of the complete parse result — the safety net for refactors."""
    assert basic_result == snapshot


def test_markers_named_or_referenced_are_imported(basic_result: dict[str, Any]) -> None:
    markers = _by_id(basic_result["markers"])

    assert sorted(markers, key=int) == ["1", "2", "3", "4", "5", "7"]
    assert markers["1"]["ha_name"] == "M1 Licht Wohnen"
    assert (markers["1"]["type"], markers["1"]["value"]) == ("digital", 1.0)
    assert (markers["2"]["type"], markers["2"]["value"]) == ("analog", 21.5)


def test_unnamed_referenced_marker_gets_placeholder_title(basic_result: dict[str, Any]) -> None:
    marker = _by_id(basic_result["markers"])["5"]

    assert (marker["title"], marker["name"], marker["no_name"]) == ("#nn", "M5 #nn", True)


@pytest.mark.parametrize(
    ("marker_id", "kind"),
    [("1", MarkerKind.NORMAL), ("3", MarkerKind.READ_ONLY), ("4", MarkerKind.TRIGGER), ("7", MarkerKind.KNX_BRIDGE)],
)
def test_marker_kind_from_title_suffix(basic_result: dict[str, Any], marker_id: str, kind: MarkerKind) -> None:
    assert _by_id(basic_result["markers"])[marker_id]["kind"] == kind


def test_io_classification(basic_result: dict[str, Any]) -> None:
    ios = {io["identifier"]: io for io in basic_result["io_all"]}

    assert (ios["I1"]["is_binary"], ios["I1"]["is_input"]) == (True, True)
    assert (ios["Q1"]["is_binary"], ios["Q1"]["is_input"], ios["Q1"]["unit"]) == (True, False, "")
    assert (ios["AI1"]["is_binary"], ios["AI1"]["unit"], ios["AI1"]["value"]) == (False, "°C", 21.5)
    assert ios["AI1"]["name"] == "BASE AI1"
    assert (ios["QI1"]["is_input"], ios["QI1"]["offline"]) == (True, True)


def test_inactive_io_is_labelled_but_gets_no_entity(basic_result: dict[str, Any]) -> None:
    all_ids = {io["identifier"] for io in basic_result["io_all"]}
    active_ids = {io["identifier"] for io in basic_result["io"]}

    assert all_ids - active_ids == {"Q2"}


def test_extensions_carry_name_and_serial(basic_result: dict[str, Any]) -> None:
    assert basic_result["extensions"] == {
        "1": {"name": "BASE", "serial": "1000-2000-3000"},
        "2": {"name": "UD 1", "serial": "5010"},
    }


def test_webio_commands_only_contain_own_classes(basic_result: dict[str, Any]) -> None:
    assert basic_result["webio_commands"] == {
        "HA M1 Licht Wohnen": {"webIoId": "101", "cmdId": 5, "typeId": 1, "webioClass": WEBIO_CLASS_MARKER},
        "HA M2 Solltemperatur": {"webIoId": "102", "cmdId": 6, "typeId": 2, "webioClass": WEBIO_CLASS_MARKER},
        "HA IO BASE Q1": {"webIoId": "103", "cmdId": 7, "typeId": 1, "webioClass": WEBIO_CLASS_IO},
    }


def test_webio_name_lexicon_covers_foreign_devices(basic_result: dict[str, Any]) -> None:
    assert basic_result["webio_names"]["104"] == {"name": "40. Fremdbefehl", "analog": True}
    assert "105" not in basic_result["webio_names"]


def test_webio_device_ip_is_stripped(basic_result: dict[str, Any]) -> None:
    """Regression: Comexio scrapes a leading space into Ip, which faked an IP-mismatch audit."""
    devices = basic_result["webio_devices"]

    assert devices[WEBIO_CLASS_MARKER] == {"device_id": "30", "device_ip": "192.168.1.20:8123", "base_id": "7"}
    assert devices[WEBIO_CLASS_IO]["device_id"] == "31"
    assert devices[WEBIO_CLASS_KNX] == {"device_id": None, "device_ip": None, "base_id": None}


def test_array_shaped_groups_are_parsed_like_objects(parsing_api: ComexioAPI) -> None:
    """Regression #85: gap-free id groups arrive as JSON arrays and crashed setup with `.items()`."""
    result = parsing_api.parse_config(load_json_fixture("config_array_groups.json"))

    assert [(m["id"], m["type"]) for m in result["markers"]] == [("0", "digital"), ("1", "analog")]
    assert {name: cmd["webIoId"] for name, cmd in result["webio_commands"].items()} == {
        "HA M0 Merker Null": "0",
        "HA M1 Merker Eins": "1",
        "HA IO BASE Q1": "7",
    }
    assert result["webio_names"]["1"]["name"] == "0. HA M1 Merker Eins"


def test_empty_config_yields_empty_result(parsing_api: ComexioAPI) -> None:
    result = parsing_api.parse_config({})

    assert (result["markers"], result["io"], result["knx"], result["webio_commands"]) == ([], [], [], {})


def test_legacy_single_webio_device_logs_migration_hint(
    parsing_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    parsing_api.parse_config({"WebDevices": {"1": {"Name": "HomeAssistant", "Ip": "192.168.1.20"}}})

    assert "Found a legacy Web-IO device named 'HomeAssistant'" in caplog.text


def test_config_entry_overrides_names_and_schema(parsing_api: ComexioAPI) -> None:
    parsing_api.config_entry = MagicMock(
        data={"server_id": "srv"},
        options={"webio_name": "Haus", CONF_SCHEMA_MARKER: "{ServerAlias} {MarkerTitle} {Unknown}"},
    )
    conf = {
        "FubModules": {"2": {"1": {"Id": 1, "Name": "Licht", "Type": 1}}},
        "WebDevices": {"9": {"Name": "Haus [M]", "Ip": "192.168.1.20", "WebDeviceBaseId": 1}},
    }

    result = parsing_api.parse_config(conf)

    assert result["markers"][0]["ha_name"] == "srv Licht {Unknown}"
    assert result["webio_devices"][WEBIO_CLASS_MARKER]["device_id"] == "9"


def test_fub_metadata_is_cached_for_canvas_helpers(parsing_api: ComexioAPI) -> None:
    parsing_api.parse_config(load_json_fixture("config_basic.json"))

    assert parsing_api.get_fub_paper_format(1) == "A4"
    assert parsing_api.get_fub_active(1) is True
    assert parsing_api.get_fub_active(2) is False
    assert parsing_api.get_fub_active(99) is None
    assert parsing_api.get_fub_orientation(2) == "portrait"
    assert parsing_api.get_fub_canvas_bounds(1) == pytest.approx((870.0, 720.0))
    # A3 portrait at 120 DPI: long side -> Y, scaled by paper size and resolution.
    assert parsing_api.get_fub_canvas_bounds(2) == pytest.approx(
        (870.0 * 297 / 297 * 120 / 90, 720.0 * 420 / 210 * 120 / 90)
    )


def test_io_without_description_gets_placeholder_title(basic_result: dict[str, Any]) -> None:
    """Regression: an IO with an empty description rendered as "BASE AI1 AI1" under the default schema."""
    io = _by_id(basic_result["io"])["13"]

    assert (io["ha_name"], io["name"]) == ("AI1 #nn", "BASE AI1")


@pytest.mark.parametrize(
    ("desc", "ident", "expected"),
    [
        ("Licht Flur", "Q1", "Licht Flur"),
        ("  Licht Flur ", "Q1", "Licht Flur"),
        ("", "I6", "#nn"),
        ("   ", "I6", "#nn"),
        ("I6", "I6", "#nn"),
        ("i6", "I6", "#nn"),
        ("I6 Diele", "I6", "I6 Diele"),
    ],
)
def test_io_schema_title(desc: str, ident: str, expected: str) -> None:
    assert ComexioAPI._io_schema_title(desc, ident) == expected
