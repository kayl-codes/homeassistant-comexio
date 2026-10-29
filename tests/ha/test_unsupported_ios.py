"""IOs of an unsupported type (#196) in a real setup: no entity, the Repair, the diagnostics download."""

import json
import logging
from typing import Any

from aiocomexio import __version__ as AIOCOMEXIO_VERSION
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.comexio as comexio_init
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN, ISSUE_UNSUPPORTED_IO_TYPES
from custom_components.comexio.diagnostics import async_get_config_entry_diagnostics
from tests.common import load_json_fixture

from .conftest import HOST, SERVER_ID

METER_SERIAL = "9009-0001-0002"
ISSUE_ID = f"{ISSUE_UNSUPPORTED_IO_TYPES}_{SERVER_ID}"
# A Smart Meter like extension: E1 is an energy counter Comexio marks as output, P1 a power reading
# marked as input, Q2 a real analog output whose range arrives as strings (unusable).
METER = {
    "extension": {"Id": 9, "Name": "Meter", "Identifier": METER_SERIAL, "TypeId": 31, "Location": "Cellar"},
    "inoutput": {
        "91": {
            "Id": 91,
            "Identifier": "E1",
            "Description": "Energy",
            "InOutputTypeId": 81,
            "IntIsOutput": 1,
            "Value": "4711.25",
            "Active": True,
        },
        "92": {
            "Id": 92,
            "Identifier": "P1",
            "Description": "Power",
            "InOutputTypeId": 78,
            "Value": "815.5",
            "Active": True,
        },
        "93": {"Id": 93, "Identifier": "Q2", "Description": "Out", "InOutputTypeId": 82, "Value": "0", "Active": True},
    },
}
METER_IO_TYPES = {
    "78": {"binary": False, "min": -2000, "max": 2000, "unit": "W"},
    "81": {"binary": False, "min": "-2147483648", "max": "2147483647", "unit": "Wh"},
    "82": {"binary": False, "min": "-8388.608", "max": "8388.608", "unit": "W"},
}
METER_IO_INPUT_TYPES = {"78": {"input": True}, "81": {"input": False}, "82": {"input": False}}
E1_UNIQUE_ID = f"comexio_{SERVER_ID}_meter_e1"


def _config_with_meter() -> dict[str, Any]:
    config = load_json_fixture("config_basic.json")
    config["FubModules"]["1"]["9"] = METER
    return config


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    return {**api_returns, "get_raw_config": _config_with_meter()}


@pytest.fixture
def api_attributes() -> dict[str, Any]:
    return {
        "io_types": {**load_json_fixture("io_types.json"), **METER_IO_TYPES},
        "io_input_types": {**load_json_fixture("io_input_types.json"), **METER_IO_INPUT_TYPES},
    }


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _unique_ids(hass: HomeAssistant, entry: MockConfigEntry, domain: str) -> set[str]:
    return {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == domain
    }


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_meter_reading_marked_as_output_gets_no_entity_and_a_repair(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    numbers = _unique_ids(hass, mock_config_entry, "number")
    assert E1_UNIQUE_ID not in numbers
    assert E1_UNIQUE_ID not in _unique_ids(hass, mock_config_entry, "sensor")
    # The platform survived the unusable range: the output keeps its number (range: test_unsupported_ios unit test).
    assert f"comexio_{SERVER_ID}_meter_q2" in numbers
    assert f"comexio_{SERVER_ID}_meter_p1" in _unique_ids(hass, mock_config_entry, "sensor")

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["ios"] == "Meter: E1"
    assert issue.learn_more_url is not None
    assert "/issues/new?" in issue.learn_more_url


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize("domain", ["number", "sensor"])
async def test_an_earlier_entity_of_such_an_io_is_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, domain: str
) -> None:
    mock_config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(domain, DOMAIN, E1_UNIQUE_ID, config_entry=mock_config_entry)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get_entity_id(domain, DOMAIN, E1_UNIQUE_ID) is None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_setup_logs_the_versions(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _version(_hass: HomeAssistant) -> str:
        return "9.8.7"

    monkeypatch.setattr(comexio_init, "async_integration_version", _version)
    caplog.set_level(logging.INFO, logger="custom_components.comexio")

    await _setup(hass, mock_config_entry)

    assert (
        f"Entered async_setup_entry for {SERVER_ID} (integration 9.8.7, "
        f"aiocomexio {AIOCOMEXIO_VERSION}, Home Assistant {HA_VERSION})"
    ) in caplog.text


@pytest.mark.usefixtures("mock_comexio_api")
async def test_removing_the_entry_drops_the_repair(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


async def test_the_repair_goes_once_the_io_is_gone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    await _setup(hass, mock_config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is not None

    api = mock_comexio_api[0]
    api.get_raw_config.return_value = load_json_fixture("config_basic.json")  # type: ignore[attr-defined]
    await hass.data[DOMAIN][mock_config_entry.entry_id].async_refresh()
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID) is None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_diagnostics_show_the_raw_comexio_data_and_how_each_io_was_classified_without_secrets(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["loaded"] is True
    comexio = diagnostics["comexio"]
    assert comexio["read_at"] is not None
    assert comexio["check_error"] is None
    assert comexio["extensions"]["Meter"]["TypeId"] == 31
    # The IO rows as Comexio delivers them, also those HA makes no entity of.
    raw_e1 = next(row for row in comexio["ios"] if row.get("Id") == 91)
    assert raw_e1["Identifier"] == "E1"
    assert raw_e1["IntIsOutput"] == 1
    assert raw_e1["InOutputTypeId"] == 81
    # A field outside the allow-list is reported by name, its value stays out.
    assert "Value" not in raw_e1
    assert "Value" in raw_e1["other_keys"]
    assert "Description" not in raw_e1["other_keys"]
    assert comexio["io_types"]["81"]["min"] == "-2147483648"
    assert comexio["io_input_types"]["81"] == {"input": False}
    ios = {(io["ext_name"], io["identifier"]): io for io in diagnostics["home_assistant"]["ios"]}
    assert ios["Meter", "E1"]["platform"] is None
    assert ios["Meter", "E1"]["skipped"] is not None
    assert ios["Meter", "P1"]["platform"] == "sensor"
    assert ios["Meter", "Q2"]["platform"] == "number"
    dumped = json.dumps(diagnostics)
    for secret in (HOST, METER_SERIAL, "admin", "Energy", "Power", "Cellar", "4711.25", "815.5"):
        assert secret not in dumped


async def test_the_diagnostics_of_an_entry_not_loaded_hold_versions_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["loaded"] is False
    assert set(diagnostics) == {"versions", "options", "loaded"}
    assert diagnostics["versions"]["comexio"] is None
