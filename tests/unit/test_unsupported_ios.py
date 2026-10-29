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
from custom_components.comexio.const import (
    DOMAIN,
    ISSUE_UNSUPPORTED_IO_TYPES,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
)
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
    markdown_text,
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

    api = _api()
    title, body = build_unsupported_io_report(ios, extension_metas(RAW_CONFIG), api, "0.30.0", tables=api)  # type: ignore[arg-type]

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
    # The diagnostics keep the extension names: the user is asked to review the file first.
    assert "It contains the extension names of your installation" in body
    assert "Sie enthält die Namen der Erweiterungen der Installation" in body
    assert "Meter Garage" not in body
    assert SERIAL not in body


INJECTED_IDENTIFIER = "E1](https://evil.example) [x"


def test_the_report_and_the_repair_show_comexio_text_literally(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    """Text set in Comexio adds no link or formatting to what the user reviews before filing."""
    ios = [_io(INJECTED_IDENTIFIER, "Wh", ext_name="Meter_*Garage*")]

    api = _api()
    _title, body = build_unsupported_io_report(ios, {}, api, None, tables=api)  # type: ignore[arg-type]
    asyncio.run(monitor.async_update(RAW_CONFIG, ios))

    escaped = r"E1\]\(https\://evil\.example\) \[x"
    assert f"IO type 81: {escaped}" in body
    listed = issue_registry.async_create_issue.call_args.kwargs["translation_placeholders"]["ios"]
    assert listed == rf"Meter\_\*Garage\*: {escaped}"


def test_markdown_text_escapes_every_markdown_character() -> None:
    assert markdown_text("a\\b`c*d_e[f]g(h)i<j>k!l#m|n~o") == r"a\\b\`c\*d\_e\[f\]g\(h\)i\<j\>k\!l\#m\|n\~o"
    # No bare autolink and no entity either.
    assert markdown_text("https://www.evil.example &amp;") == r"https\://www\.evil\.example \&amp;"


def test_a_backtick_in_the_type_rows_cannot_end_their_code_span() -> None:
    api = _api()
    api.io_types["81"] = {**IO_TYPES["81"], "unit": "W`h](https://evil.example)"}

    _title, body = build_unsupported_io_report([_io("E1", "Wh")], {}, api, None, tables=api)  # type: ignore[arg-type]

    (row,) = [line for line in body.splitlines() if line.startswith("  - `")]
    span = row.removeprefix("  - `").removesuffix("`")
    assert "`" not in span
    # Still the row as delivered: the JSON escape stands for the same character.
    assert json.loads(span)["type"]["unit"] == "W`h](https://evil.example)"


def test_the_report_marks_an_unknown_integration_version() -> None:
    api = _api()
    _title, body = build_unsupported_io_report([_io("E1", "Wh")], {}, api, None, tables=api)  # type: ignore[arg-type]

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


@pytest.mark.parametrize("table", ["io_types", "io_input_types"])
def test_a_poll_without_an_io_type_table_keeps_the_repair(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, table: str
) -> None:
    """Without $IOTypesBinary no IO has a unit, without $IOInputTypes every IO is an input: no news on the types."""
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    setattr(monitor._api, table, {})

    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "", is_input=True)]))

    issue_registry.async_delete_issue.assert_not_called()
    assert issue_registry.async_create_issue.call_count == 1
    # The diagnostics show the poll's tables, the missing one too.
    assert getattr(monitor, table) == {}


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


def _raw_data(monitor: UnsupportedIoMonitor) -> tuple[Any, ...]:
    """What the diagnostics show of the last raw read."""
    return monitor.extensions, monitor.raw_ios, monitor.raw_read_at


@pytest.mark.parametrize("failed_scrape", [{}, {"FubModules": {}}])
def test_a_failed_scrape_keeps_the_raw_data_for_the_diagnostics(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, failed_scrape: dict[str, Any]
) -> None:
    asyncio.run(monitor.async_update(failed_scrape, []))
    assert monitor.raw_ios == []
    assert monitor.raw_read_at is None

    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    read = _raw_data(monitor)
    assert monitor.raw_ios
    assert monitor.raw_read_at is not None

    asyncio.run(monitor.async_update(failed_scrape, []))

    assert _raw_data(monitor) == read


def test_a_failed_check_keeps_the_last_raw_data_and_its_error_across_a_failed_scrape(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))
    read = _raw_data(monitor)
    monkeypatch.setattr(unsupported_ios, "raw_io_rows", MagicMock(side_effect=ValueError("ROW-SECRET")))

    asyncio.run(monitor.async_update({"FubModules": {"1": {}}}, []))
    # A failed scrape runs no check: the error of the last one stays visible.
    asyncio.run(monitor.async_update({}, []))

    # The extensions, read before the failure, are not stored without their IO rows.
    assert _raw_data(monitor) == read
    assert monitor.last_error_type == "ValueError"


def test_a_poll_without_the_io_type_table_still_reads_the_raw_data(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    monitor._api.io_types = {}

    asyncio.run(monitor.async_update(RAW_CONFIG, []))

    assert [row["Identifier"] for row in monitor.raw_ios] == ["E1"]
    assert monitor.raw_read_at is not None


def test_the_diagnostics_keep_the_type_tables_of_the_poll_of_their_rows(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock
) -> None:
    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))

    # A later config fetch, without a check of its own, changes the API's tables: in place or anew.
    monitor._api.io_types["81"] = {"unit": "NEW"}
    monitor._api.io_input_types = {"99": {}}

    assert monitor.io_types == IO_TYPES
    assert monitor.io_input_types == IO_INPUT_TYPES


def test_the_report_takes_the_type_rows_read_with_the_ios(
    monitor: UnsupportedIoMonitor, issue_registry: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config fetch while the first Repair looks up the integration version brings rows of another poll."""

    async def _version_during_a_fetch(_hass: Any) -> str:
        monitor._api.io_types = {"81": {**IO_TYPES["81"], "unit": "NEW-FETCH"}}
        return "0.30.0"

    monkeypatch.setattr(unsupported_ios, "async_integration_version", _version_during_a_fetch)

    asyncio.run(monitor.async_update(RAW_CONFIG, [_io("E1", "Wh")]))

    url = issue_registry.async_create_issue.call_args.kwargs["learn_more_url"]
    body = parse_qs(urlsplit(url).query)["body"][0]
    assert '"unit": "Wh"' in body
    assert "NEW-FETCH" not in body


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


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({}, (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)),
        ({"max": 50}, (WEBIO_MARKER_ANALOG_MIN, 50)),
        ({"min": -5}, (-5, WEBIO_MARKER_ANALOG_MAX)),
    ],
)
def test_an_output_without_a_usable_range_gets_comexios_value_range(
    bounds: dict[str, Any], expected: tuple[float, float]
) -> None:
    """A bound None (unusable type row) once raised in the constructor and took down every number (#196).

    Not HA's 0-100 default: it would reject real set points of the output.
    """
    (entity,) = _number_setup([_io("AO1", "W", **bounds)])

    assert entity.native_min_value == pytest.approx(expected[0])
    assert entity.native_max_value == pytest.approx(expected[1])


def test_the_number_platform_skips_meter_readings_marked_as_outputs() -> None:
    added = _number_setup([_io("E1", "Wh"), _io("Q1", "var"), _io("AO1", "V", min=0, max=10)])

    assert [entity.unique_id for entity in added] == ["comexio_srv_meter garage_ao1"]
    assert added[0].native_max_value == pytest.approx(10.0)
