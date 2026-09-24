"""KNX objects: DPT resolution chain, DPT metadata and DPT3.x composite pairing in parse_config."""

from typing import Any

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import KNX_DPT_ANALOG_RANGES
from tests.common import load_json_fixture


@pytest.fixture
def catalog() -> dict[str, Any]:
    return load_json_fixture("knx_dpt_catalog.json")


@pytest.fixture
def knx_items(parsing_api: ComexioAPI, catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = parsing_api.parse_config(
        load_json_fixture("config_basic.json"), knx_live_states={"1": "19,5"}, knx_dpt_catalog=catalog
    )
    return {item["id"]: item for item in result["knx"]}


def test_resolve_knx_dpt_follows_point_device_dpt_chain(catalog: dict[str, Any]) -> None:
    assert ComexioAPI._resolve_knx_dpt(catalog, "1") == (9, 1)
    assert ComexioAPI._resolve_knx_dpt(catalog, "3") == (3, 8)


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"KnxPoints": [], "KnxDevices": {}, "KnxDpt": {}},
        {"KnxPoints": {"1": {"KnxDeviceId": 10}}, "KnxDevices": {}, "KnxDpt": {}},
        {
            "KnxPoints": {"1": {"KnxDeviceId": 10}},
            "KnxDevices": {"10": {"KnxDptId": 20}},
            "KnxDpt": {"20": {"KnxBaseTypeId": "9", "KnxSubId": 1}},
        },
    ],
)
def test_resolve_knx_dpt_returns_none_for_broken_chain(broken: dict[str, Any]) -> None:
    assert ComexioAPI._resolve_knx_dpt(broken, "1") is None


def test_resolve_knx_dpt_unknown_point(catalog: dict[str, Any]) -> None:
    assert ComexioAPI._resolve_knx_dpt(catalog, "99") is None


def test_unnamed_knx_object_is_not_imported(knx_items: dict[str, dict[str, Any]]) -> None:
    assert sorted(knx_items) == ["1", "2", "3", "4"]


def test_analog_knx_object_gets_dpt_range_and_live_value(knx_items: dict[str, dict[str, Any]]) -> None:
    item = knx_items["1"]
    dpt_min, dpt_max, dpt_unit, dpt_step = KNX_DPT_ANALOG_RANGES[(9, 1)]

    assert (item["type"], item["value"], item["ha_name"]) == ("analog", 19.5, "K1 Wohnen Temperatur")
    assert (item["dpt_min"], item["dpt_max"], item["dpt_unit"], item["dpt_step"]) == (
        dpt_min,
        dpt_max,
        dpt_unit,
        dpt_step,
    )
    assert item["dpt_device_class"] == "temperature"


def test_dpt3_pair_is_tagged_as_cover_composite(knx_items: dict[str, dict[str, Any]]) -> None:
    assert knx_items["2"]["knx_composite"] == {"role": "direction", "domain": "cover", "partner_id": "3"}
    assert knx_items["3"]["knx_composite"] == {"role": "stepcode", "domain": "cover", "partner_id": "2"}


def test_knx_type_without_io_type_entry_is_flagged_ambiguous(knx_items: dict[str, dict[str, Any]]) -> None:
    item = knx_items["4"]

    assert (item["type"], item["dpt_ambiguous"]) == ("digital", True)
    assert "dpt_type_unresolved" not in item
    # An ambiguous item must not also be auto-classified from the DPT chain.
    assert "dpt_min" not in item
    assert "dpt_device_class" not in item


def test_knx_without_catalog_has_no_dpt_metadata(parsing_api: ComexioAPI) -> None:
    result = parsing_api.parse_config(load_json_fixture("config_basic.json"))
    items = {item["id"]: item for item in result["knx"]}

    assert "dpt_min" not in items["1"]
    assert "knx_composite" not in items["2"]
    assert items["4"]["dpt_ambiguous"] is True


def test_knx_live_values_are_not_mixed_with_marker_values(parsing_api: ComexioAPI) -> None:
    """Markers and KNX objects share one numeric id space — their live values must stay apart."""
    result = parsing_api.parse_config(
        load_json_fixture("config_basic.json"), live_states={"1": "1"}, knx_live_states={"1": "19,5"}
    )

    assert result["markers"][0]["value"] == 1.0
    assert result["knx"][0]["value"] == 19.5
