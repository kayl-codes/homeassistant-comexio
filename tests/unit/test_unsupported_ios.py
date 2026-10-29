"""IOs of an unsupported type (#196): which get no entity, the Repair with its GitHub report, the number fix."""

import asyncio
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.comexio import number, unsupported_ios
from custom_components.comexio.const import DOMAIN, ISSUE_UNSUPPORTED_IO_TYPES
from custom_components.comexio.reference_monitor import ISSUE_URL_MAX_CHARS
from custom_components.comexio.unsupported_ios import (
    EXTENSION_META_KEYS,
    RAW_IO_KEYS,
    RAW_IO_PRIVATE_KEYS,
    REASON_MEASUREMENT_OUTPUT,
    REPAIR_IO_LIST_LIMIT,
    UnsupportedIoMonitor,
    build_unsupported_io_report,
    extension_metas,
    io_platform,
    raw_io_rows,
    unsupported_io_reason,
)

TRANSLATIONS_DIR = Path(unsupported_ios.__file__).parent / "translations"
SERIAL = "4711-0815-1234"
RAW_CONFIG: dict[str, Any] = {
    "FubModules": {
        "1": {
            "1": {"extension": {"Id": 1, "Name": "BASE", "Identifier": "1111-2222-3333", "TypeId": 30}},
            "7": {
                "extension": {
                    "Id": 7,
                    "Name": "Meter Garage",
                    "Identifier": SERIAL,
                    "TypeId": 31,
                    "Mode": 0,
                    "Pos": 2,
                    "Location": "Garage",
                },
                "inoutput": {
                    "71": {
                        "Id": 71,
                        "ExtensionId": 7,
                        "Identifier": "E1",
                        "Description": "DESC-SECRET",
                        "InOutputTypeId": 81,
                        "IntIsOutput": 1,
                        "Active": True,
                        "Timeout": 1.5,
                        "TimeoutGroup": None,
                        "Value": "VALUE-SECRET",
                        "NewField": "x",
                        # A listed field of an unexpected shape: by name only, like an unknown field.
                        "Flags": ["FLAGS-SECRET"],
                    },
                    "72": "not a row",
                },
            },
            "8": {"inoutput": {}},
            "9": {"inoutput": []},
        }
    }
}
# Rows as a legacy firmware delivers them: the Smart Meter counter's range as strings.
IO_TYPES = {"81": {"id": 81, "binary": False, "min": "-2147483648", "max": "2147483647", "unit": "Wh"}}
IO_INPUT_TYPES = {"81": {"id": 81, "input": False}}


def _io(identifier: str, unit: str, *, is_input: bool = False, is_binary: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "ext_name": "Meter Garage",
        "identifier": identifier,
        "ha_name": identifier,
        "unit": unit,
        "is_input": is_input,
        "is_binary": is_binary,
        "type_id_raw": 81,
        "min": None,
        "max": None,
        **extra,
    }


@pytest.mark.parametrize(
    ("io", "reason", "platform"),
    [
        (_io("E1", "Wh"), REASON_MEASUREMENT_OUTPUT, None),
        (_io("E2", "kWh"), REASON_MEASUREMENT_OUTPUT, None),
        (_io("S1", "VA"), REASON_MEASUREMENT_OUTPUT, None),
        (_io("Q1", "var"), REASON_MEASUREMENT_OUTPUT, None),
        # A meter reading Comexio marks as input stays a sensor.
        (_io("DS1", "VA", is_input=True), None, "sensor"),
        (_io("EI1", "kWh", is_input=True), None, "sensor"),
        # A real analog output keeps its number, whatever its range.
        (_io("AO1", "V"), None, "number"),
        (_io("AO2", ""), None, "number"),
        (_io("Q3", "Wh", is_binary=True), None, "switch"),
        (_io("I1", "", is_input=True, is_binary=True), None, "binary_sensor"),
    ],
)
def test_only_outputs_with_a_meter_unit_get_no_entity(
    io: dict[str, Any], reason: str | None, platform: str | None
) -> None:
    assert unsupported_io_reason(io) == reason
    assert io_platform(io) == platform


def test_extension_metas_name_every_extension_and_leave_out_its_serial() -> None:
    metas = extension_metas(RAW_CONFIG)

    assert metas == {
        "BASE": {"Id": 1, "Name": "BASE", "TypeId": 30},
        # Neither the serial nor the free-text location.
        "Meter Garage": {"Id": 7, "Name": "Meter Garage", "TypeId": 31, "Mode": 0, "Pos": 2},
        # Named like aiocomexio names an extension without meta.
        "Ext8": {},
        "Ext9": {},
    }


@pytest.mark.parametrize("raw_config", [{}, {"FubModules": []}, {"FubModules": {"1": []}}])
def test_extension_metas_of_an_unreadable_config_are_empty(raw_config: dict[str, Any]) -> None:
    assert extension_metas(raw_config) == {}


def test_raw_io_rows_keep_comexio_fields_but_leave_out_the_description() -> None:
    assert raw_io_rows(RAW_CONFIG) == [
        {
            "Id": 71,
            "ExtensionId": 7,
            "Identifier": "E1",
            "InOutputTypeId": 81,
            "IntIsOutput": 1,
            "Active": True,
            "Timeout": 1.5,
            "TimeoutGroup": None,
            # An unknown field by name only: its value could be anything.
            "other_keys": ["Flags", "NewField", "Value"],
        }
    ]
    assert "SECRET" not in json.dumps(raw_io_rows(RAW_CONFIG))


def test_the_allow_lists_leave_out_serials_locations_and_descriptions() -> None:
    assert not {"Identifier", "Location"} & set(EXTENSION_META_KEYS)
    assert not ({"Description", "Value"} | RAW_IO_PRIVATE_KEYS) & set(RAW_IO_KEYS)


@pytest.mark.parametrize("raw_config", [{}, {"FubModules": []}, {"FubModules": {"1": {"7": []}}}])
def test_raw_io_rows_of_an_unreadable_config_are_empty(raw_config: dict[str, Any]) -> None:
    assert raw_io_rows(raw_config) == []


def _api() -> SimpleNamespace:
    return SimpleNamespace(comexio_version="11.0.2", io_types=dict(IO_TYPES), io_input_types=IO_INPUT_TYPES)


def test_the_report_names_types_and_identifiers_but_no_names_or_serials() -> None:
    ios = [_io("E1", "Wh"), _io("E2", "Wh")]

    title, body = build_unsupported_io_report(ios, extension_metas(RAW_CONFIG), _api(), "0.30.0")  # type: ignore[arg-type]

    assert title == "Comexio IO types not supported (extension type 31)"
    assert "- Extension type 31, IO type 81: E1, E2" in body
    # The raw rows as delivered, strings quoted: they show why the range was unusable.
    assert '"min": "-2147483648"' in body
    assert '"input": {"id": 81, "input": false}' in body
    assert "- Integration: 0.30.0" in body
    assert "- Comexio: 11.0.2" in body
    assert "Download diagnostics" in body
    assert "Enable debug logging" in body
    # The user files it in either language: the steps come in English and German.
    assert "Diagnosedaten herunterladen" in body
    assert "Debug-Protokollierung aktivieren" in body
    assert "Meter Garage" not in body
    assert SERIAL not in body


def test_the_report_marks_an_unknown_integration_version() -> None:
    _title, body = build_unsupported_io_report([_io("E1", "Wh")], {}, _api(), None)  # type: ignore[arg-type]

    assert "- Integration: ?" in body
    assert "- Extension type None, IO type 81: E1" in body


@pytest.fixture
def issue_registry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    registry = MagicMock()
    registry.IssueSeverity = unsupported_ios.ir.IssueSeverity
    monkeypatch.setattr(unsupported_ios, "ir", registry)
    return registry


@pytest.fixture
def monitor(monkeypatch: pytest.MonkeyPatch) -> UnsupportedIoMonitor:
    monkeypatch.setattr(unsupported_ios, "async_integration_version", AsyncMock(return_value="0.30.0"))
    return UnsupportedIoMonitor(MagicMock(), _api(), "srv")  # type: ignore[arg-type]


def test_an_unsupported_io_raises_the_repair_with_a_prefilled_issue(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh"), _io("AO1", "V")]))

    args, kwargs = issue_registry.async_create_issue.call_args
    assert args[1:] == (DOMAIN, f"{ISSUE_UNSUPPORTED_IO_TYPES}_srv")
    assert kwargs["translation_key"] == ISSUE_UNSUPPORTED_IO_TYPES
    assert kwargs["severity"] == unsupported_ios.ir.IssueSeverity.WARNING
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_placeholders"]["ios"] == "Meter Garage: E1"
    url = kwargs["learn_more_url"]
    assert kwargs["translation_placeholders"]["issue_url"] == url
    assert url.startswith("https://github.com/kayl-codes/homeassistant-comexio/issues/new?")
    assert "IO type 81: E1" in parse_qs(urlsplit(url).query)["body"][0]
    assert monitor.extensions["Meter Garage"]["TypeId"] == 31
    issue_registry.async_delete_issue.assert_not_called()


def test_the_repair_goes_once_no_unsupported_io_is_left(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    with caplog.at_level(logging.INFO):
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("AO1", "V")]))

    issue_registry.async_delete_issue.assert_called_once_with(monitor._hass, DOMAIN, monitor.issue_id)
    assert "Repair cleared: no imported IO of an unsupported type left" in caplog.text


def test_a_fresh_monitor_clears_a_repair_raised_before_the_reload(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("AO1", "V")]))

    issue_registry.async_delete_issue.assert_called_once_with(monitor._hass, DOMAIN, monitor.issue_id)


def test_a_poll_without_the_io_type_table_keeps_the_repair(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    """Without the table no IO has a unit and every IO is an input: that says nothing about the types."""
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    monitor._api.io_types = {}

    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "", is_input=True)]))

    issue_registry.async_delete_issue.assert_not_called()
    assert issue_registry.async_create_issue.call_count == 1


def test_a_failing_check_does_not_fail_the_poll(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    issue_registry.async_create_issue.side_effect = ValueError("bad row")

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
        issue_registry.async_create_issue.side_effect = None
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
        issue_registry.async_create_issue.side_effect = ValueError("bad row")
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))

    # The traceback once per cause; a success in between makes the next failure news again.
    tracebacks = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in tracebacks] == ["[srv] Check for IOs without an entity failed"] * 2
    assert all(r.exc_info for r in tracebacks)
    assert "failed again: ValueError: bad row" in caplog.text
    # The diagnostics get the type only: the exception text may quote a row.
    assert monitor.last_error_type == "ValueError"
    issue_registry.async_create_issue.side_effect = None
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    assert monitor.last_error_type is None


def test_the_repair_names_the_units_of_the_rule(monitor: UnsupportedIoMonitor, issue_registry: MagicMock) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))

    placeholders = issue_registry.async_create_issue.call_args.kwargs["translation_placeholders"]
    assert placeholders["units"] == "VA, Wh, kWh, var"
    # Every language uses exactly the placeholders the monitor passes.
    for path in TRANSLATIONS_DIR.glob("*.json"):
        issue = json.loads(path.read_text(encoding="utf-8"))["issues"][ISSUE_UNSUPPORTED_IO_TYPES]
        assert set(re.findall(r"\{(\w+)\}", issue["title"] + issue["description"])) == set(placeholders), path.name


def test_the_issue_link_of_many_ios_stays_within_the_url_limit(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io(f"E{n}", "Wh", type_id_raw=n) for n in range(200)]))

    url = issue_registry.async_create_issue.call_args.kwargs["learn_more_url"]
    assert len(url) <= ISSUE_URL_MAX_CHARS
    assert parse_qs(urlsplit(url).query)["title"] == ["Comexio IO types not supported (extension type 31)"]


def test_a_failed_scrape_keeps_the_repair_and_the_extensions(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    """An empty IO list from a failed scrape says nothing about the types: no flashing Repair."""
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    asyncio.run(monitor.async_update({}, []))

    issue_registry.async_delete_issue.assert_not_called()
    assert issue_registry.async_create_issue.call_count == 1
    assert "Meter Garage" in monitor.extensions


@pytest.mark.parametrize("failed_scrape", [{}, {"FubModules": {}}])
def test_a_failed_scrape_keeps_the_raw_data_for_the_diagnostics(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, failed_scrape: dict[str, Any]
) -> None:
    asyncio.run(monitor.async_update(failed_scrape, []))
    assert (monitor.raw_ios, monitor.raw_read_at) == ([], None)

    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    read = (monitor.extensions, monitor.raw_ios, monitor.raw_read_at)
    assert monitor.raw_ios
    assert monitor.raw_read_at is not None

    asyncio.run(monitor.async_update(failed_scrape, []))

    assert (monitor.extensions, monitor.raw_ios, monitor.raw_read_at) == read


def test_a_failed_check_keeps_the_last_raw_data_and_its_error_across_a_failed_scrape(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    read = (monitor.extensions, monitor.raw_ios, monitor.raw_read_at)
    monkeypatch.setattr(unsupported_ios, "raw_io_rows", MagicMock(side_effect=ValueError("ROW-SECRET")))

    asyncio.run(monitor.async_update({"FubModules": {"1": {}}}, []))
    # A failed scrape runs no check: the error of the last one stays visible.
    asyncio.run(monitor.async_update({}, []))

    # The extensions, read before the failure, are not stored without their IO rows.
    assert (monitor.extensions, monitor.raw_ios, monitor.raw_read_at) == read
    assert monitor.last_error_type == "ValueError"


def test_a_poll_without_the_io_type_table_still_reads_the_raw_data(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    monitor._api.io_types = {}

    asyncio.run(monitor.async_update(RAW_CONFIG, []))

    assert [row["Identifier"] for row in monitor.raw_ios] == ["E1"]
    assert monitor.raw_read_at is not None


def test_the_warning_is_logged_only_when_the_ios_change(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
        asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh"), _io("E2", "Wh")]))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "[srv] 1 IOs get no entity, their type is not supported yet (see Repairs): Meter Garage E1",
        "[srv] 2 IOs get no entity, their type is not supported yet (see Repairs): Meter Garage E1, Meter Garage E2",
    ]
    assert issue_registry.async_create_issue.call_count == 3


def test_a_long_repair_list_counts_the_rest(monitor: UnsupportedIoMonitor, issue_registry: MagicMock) -> None:
    ios = [_io(f"E{n}", "Wh") for n in range(REPAIR_IO_LIST_LIMIT + 2)]

    asyncio.run(monitor.async_update(RAW_CONFIG, ios))

    listed = issue_registry.async_create_issue.call_args.kwargs["translation_placeholders"]["ios"]
    assert listed.endswith(f"E{REPAIR_IO_LIST_LIMIT - 1}; … 2 more")


def _number_setup(ios: list[dict[str, Any]]) -> list[Any]:
    coordinator = MagicMock(server_id="srv", data={"io": ios})
    entry = SimpleNamespace(entry_id="e1", data={"import_markers": False, "import_knx": False}, options={})
    hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}})
    added: list[Any] = []
    asyncio.run(number.async_setup_entry(hass, entry, added.extend))  # type: ignore[arg-type]
    return added


def test_an_output_without_a_usable_range_gets_the_default_range() -> None:
    """Both bounds None (unusable type row) once raised in the constructor and took down every number (#196)."""
    (entity,) = _number_setup([_io("AO1", "W")])

    assert entity.native_min_value == pytest.approx(0.0)
    assert entity.native_max_value == pytest.approx(100.0)


def test_the_number_platform_skips_meter_readings_marked_as_outputs() -> None:
    added = _number_setup([_io("E1", "Wh"), _io("Q1", "var"), _io("AO1", "V", min=0, max=10)])

    assert [entity.unique_id for entity in added] == ["comexio_srv_meter garage_ao1"]
    assert added[0].native_max_value == pytest.approx(10.0)
