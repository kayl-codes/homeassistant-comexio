"""IO types that are not imported (#317): QI, TL and UL report about once per second and flood the bus."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from homeassistant.helpers.selector import SelectSelector
import pytest

from custom_components.comexio.const import (
    CONF_IGNORED_IO_TYPES,
    DEFAULT_IGNORED_IO_TYPES,
    FUNCTION_PLAN_IO_GROUP_ORDER,
    QUICKBUS_IO_DATA_KEY,
    ignored_io_types,
    io_type,
    known_extension_names,
    known_io_types,
    normalize_io_types,
)
from custom_components.comexio.coordinator import ComexioCoordinator, _imported_data
from custom_components.comexio.options_flow import (
    ComexioOptionsFlow,
    _ignored_io_types_field,
    io_type_selector,
    normalize_ignored_io_types,
)


def _io(identifier: str, ext_name: str = "A1_7", **extra: Any) -> dict[str, Any]:
    return {"id": f"{ext_name}-{identifier}", "ext_name": ext_name, "identifier": identifier, **extra}


def _parsed(*identifiers: str) -> dict[str, Any]:
    """A parse_config() result of two extensions with these IOs and one Quickbus IO whose identifier is TL1."""
    ios = [_io(identifier) for identifier in identifiers]
    quickbus_io = _io("TL1", ext_name="MT1", quickbus={"device_id": "3", "name": "MT1"})
    return {
        "markers": [],
        "io": ios,
        "io_all": [*ios, quickbus_io],
        QUICKBUS_IO_DATA_KEY: [quickbus_io],
    }


# The type of an IO


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("QI4", "QI"),
        ("qi4", "QI"),
        ("TL1", "TL"),
        ("UL1", "UL"),
        ("Q1", "Q"),  # a relay output is no power input
        ("AI12", "AI"),
        ("  AO3", "AO"),
        ("DG", "DG"),
        ("1", ""),
        ("", ""),
    ],
)
def test_the_type_is_the_letter_run_of_the_identifier(identifier: str, expected: str) -> None:
    assert io_type({"identifier": identifier}) == expected


def test_an_io_without_an_identifier_has_no_type() -> None:
    assert io_type({}) == ""


def test_q_and_qi_are_two_types() -> None:
    """Q (relay output) stays when QI (power input) is left out: the whole letter run counts, never a prefix."""
    parsed = _parsed("Q1", "QI1", "QI2")

    imported = _imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["QI"]})

    assert [io["identifier"] for io in imported["io"]] == ["Q1"]


# The saved option


def test_an_entry_that_never_saved_the_option_excludes_nothing() -> None:
    """The defaults are the setup flow's: an existing installation keeps its entities and their history."""
    assert ignored_io_types({}) == frozenset()
    parsed = _parsed("QI1", "TL1", "UL1", "I1")
    assert len(_imported_data(parsed, {})["io"]) == 4


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (["qi", " TL ", "QI"], {"QI", "TL"}),
        ([], set()),
        (None, set()),
        ("QI", set()),  # a bare string is no list: never read as its letters
        (["QI4", "", 7, None, "T L", "UL"], {"UL"}),  # what is no type is left out, never an error
    ],
)
def test_the_saved_types_are_read_leniently(saved: Any, expected: set[str]) -> None:
    assert ignored_io_types({CONF_IGNORED_IO_TYPES: saved}) == expected


def test_the_defaults_are_valid_and_unique() -> None:
    assert DEFAULT_IGNORED_IO_TYPES == ("QI", "TL", "UL")
    assert normalize_io_types(DEFAULT_IGNORED_IO_TYPES) == list(DEFAULT_IGNORED_IO_TYPES)


# The import


def test_an_ignored_type_leaves_the_imported_ios() -> None:
    parsed = _parsed("I1", "QI1", "QI2", "TL1", "UL1", "AI1")

    imported = _imported_data(parsed, {CONF_IGNORED_IO_TYPES: list(DEFAULT_IGNORED_IO_TYPES)})

    assert [io["identifier"] for io in imported["io"]] == ["I1", "AI1"]


def test_the_plan_labels_and_quickbus_ios_stay() -> None:
    """io_all still names the ignored IOs (a plan of the user's own may wire them); a Quickbus IO is no extension IO."""
    parsed = _parsed("QI1", "I1")

    imported = _imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["QI", "TL"]})

    assert [io["identifier"] for io in imported["io_all"]] == ["QI1", "I1", "TL1"]
    assert [io["identifier"] for io in imported[QUICKBUS_IO_DATA_KEY]] == ["TL1"]


def test_the_parse_result_is_not_changed() -> None:
    """The filter builds a new list: the scraped config kept for a start from the saved one stays whole."""
    parsed = _parsed("QI1", "I1")

    _imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["QI"]})

    assert [io["identifier"] for io in parsed["io"]] == ["QI1", "I1"]


def test_a_type_that_no_io_has_changes_nothing() -> None:
    parsed = _parsed("I1", "Q1")
    assert _imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["XX"]})["io"] == parsed["io"]


def test_an_io_of_an_ignored_type_activated_in_comexio_is_no_audit_finding() -> None:
    """Activating more IOs of a left-out type in Comexio expects no command: no "missing", so no sync repair."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ignored_ids_for = lambda _cls: set()
    coordinator._ignored_ids_for = lambda _cls: set()
    coordinator.config_entry = SimpleNamespace(data={}, options={CONF_IGNORED_IO_TYPES: ["TL", "UL"]})
    coordinator.server_id = "test"
    parsed = _parsed("I1", "TL1", "TL2", "UL1")
    parsed.update({"knx": [], "onewire": []})

    ha_map, _meta = coordinator._build_ha_audit_map(_imported_data(parsed, coordinator.config_entry.options))

    # The Quickbus IO named TL1 stays: a Quickbus IO is no extension IO.
    assert {info["name"] for info in ha_map.values()} == {"HA IO A1_7 I1", "HA QB3 MT1 TL1"}


def test_a_module_of_left_out_ios_only_keeps_its_extension() -> None:
    """Its device and firmware entity come from the extension list, so the left-out IOs still name the module."""
    parsed = _parsed("I1")
    parsed["io"].append(_io("QI1", "PWR1"))

    imported = _imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["QI"]})

    assert [io["ext_name"] for io in imported["io"]] == ["A1_7"]
    assert known_extension_names(imported, include_offline=False) == {"A1_7", "PWR1", "MT1"}


def test_a_module_of_left_out_ios_only_is_tracked_as_offline() -> None:
    """Its offline flag sits on the left-out IOs: without them the module would never count as offline."""
    parsed = _parsed("I1")
    parsed["io"][0]["offline"] = True  # an imported IO keeps counting beside the left-out ones
    parsed["io"].append(_io("QI1", "PWR1", offline=True))
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "test"
    coordinator.offline_extensions = None

    coordinator._index_ios(_imported_data(parsed, {CONF_IGNORED_IO_TYPES: ["QI"]}))

    assert coordinator.offline_extensions == {"A1_7", "PWR1"}


def test_a_push_of_a_left_out_type_is_no_unknown_io(caplog: pytest.LogCaptureFixture) -> None:
    """Comexio pushes until a sync removed the commands: no warning, and no entry in the warned-IO set."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "test"
    coordinator.config_entry = SimpleNamespace(options={CONF_IGNORED_IO_TYPES: ["QI"]})
    coordinator._io_index = {}
    coordinator._warned_unknown_webhook_ios = set()
    coordinator._async_publish_pushed_value = lambda: None
    coordinator.schedule_plan_preview_refresh = lambda: None

    with caplog.at_level("DEBUG"):
        coordinator.update_io_by_name("PWR1", "QI1", 5)
        coordinator.update_io_by_name("PWR1", "Q9", 5)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Q9" in warnings[0].getMessage()  # an IO of a type that is imported but unknown still warns
    assert len(coordinator._warned_unknown_webhook_ios) == 1


# The form input


def test_the_input_is_normalized_upper_case_and_unique() -> None:
    user_input: dict[str, Any] = {CONF_IGNORED_IO_TYPES: ["qi", "TL", "Qi", " ul "]}
    errors: dict[str, str] = {}

    normalize_ignored_io_types(user_input, errors)

    assert user_input[CONF_IGNORED_IO_TYPES] == ["QI", "TL", "UL"]
    assert not errors


@pytest.mark.parametrize("entry", ["QI4", "Q I", "", "?", "Q-I", "1"])
def test_an_entry_that_is_no_type_is_a_field_error(entry: str) -> None:
    """The submitted list stays as typed, so the form shows the entry to correct it."""
    user_input: dict[str, Any] = {CONF_IGNORED_IO_TYPES: ["QI", entry]}
    errors: dict[str, str] = {}

    normalize_ignored_io_types(user_input, errors)

    assert errors == {CONF_IGNORED_IO_TYPES: "invalid_ignored_io_types"}
    assert user_input[CONF_IGNORED_IO_TYPES] == ["QI", entry]


def test_a_bare_string_is_no_list_of_types() -> None:
    """A string is never read as its letters: "QI" would become the two types Q and I."""
    user_input: dict[str, Any] = {CONF_IGNORED_IO_TYPES: "QI"}
    errors: dict[str, str] = {}

    normalize_ignored_io_types(user_input, errors)

    assert errors == {CONF_IGNORED_IO_TYPES: "invalid_ignored_io_types"}


@pytest.mark.parametrize("user_input", [{}, {CONF_IGNORED_IO_TYPES: None}, {CONF_IGNORED_IO_TYPES: []}])
def test_an_emptied_field_is_saved_empty(user_input: dict[str, Any]) -> None:
    """A form sends no key for an emptied field; it must not bring the saved types back."""
    errors: dict[str, str] = {}

    normalize_ignored_io_types(user_input, errors)

    assert user_input == {CONF_IGNORED_IO_TYPES: []}
    assert not errors


def test_the_options_flow_normalizes_the_types_with_its_other_fields() -> None:
    user_input: dict[str, Any] = {CONF_IGNORED_IO_TYPES: ["ql", "qi"]}
    errors: dict[str, str] = {}

    ComexioOptionsFlow._normalize_user_input(user_input, errors, set())

    assert user_input[CONF_IGNORED_IO_TYPES] == ["QL", "QI"]
    assert not errors


def test_an_invalid_type_blocks_the_options_form() -> None:
    errors: dict[str, str] = {}

    ComexioOptionsFlow._normalize_user_input({CONF_IGNORED_IO_TYPES: ["QI4"]}, errors, set())

    assert errors == {CONF_IGNORED_IO_TYPES: "invalid_ignored_io_types"}


def _submitting_flow(monkeypatch: pytest.MonkeyPatch) -> tuple[ComexioOptionsFlow, list[dict[str, Any]]]:
    """An options flow whose entry creation records what it is asked to save instead of saving it."""
    flow = ComexioOptionsFlow.__new__(ComexioOptionsFlow)
    flow._quickbus_shown = []
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(flow, "_create_options_entry", lambda user_input: saved.append(user_input) or "entry")
    return flow, saved


def test_submitting_an_invalid_type_returns_the_form_and_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    flow, saved = _submitting_flow(monkeypatch)
    errors: dict[str, str] = {}

    result = asyncio.run(flow._async_submit_init({CONF_IGNORED_IO_TYPES: ["QI4"]}, {}, errors, set()))

    assert result is None  # the form again
    assert errors == {CONF_IGNORED_IO_TYPES: "invalid_ignored_io_types"}
    assert saved == []


def test_submitting_valid_types_saves_them_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    flow, saved = _submitting_flow(monkeypatch)
    errors: dict[str, str] = {}

    result = asyncio.run(flow._async_submit_init({CONF_IGNORED_IO_TYPES: ["ql", "qi"]}, {}, errors, set()))

    assert result == "entry"
    assert not errors
    assert [entry[CONF_IGNORED_IO_TYPES] for entry in saved] == [["QL", "QI"]]


# The field


def test_the_field_shows_the_saved_types() -> None:
    key, _selector = _ignored_io_types_field(None, {CONF_IGNORED_IO_TYPES: ["UL", "qi"]}, {})

    assert key.description == {"suggested_value": ["QI", "UL"]}


def test_the_field_shows_nothing_for_an_entry_that_never_saved_it() -> None:
    """The default is not shown for an existing entry: it would read as the current setting."""
    key, _selector = _ignored_io_types_field(None, {}, {})

    assert key.description == {"suggested_value": []}


def test_after_an_error_the_field_shows_the_input_as_typed() -> None:
    key, selector = _ignored_io_types_field({CONF_IGNORED_IO_TYPES: ["QI", "QI4"]}, {CONF_IGNORED_IO_TYPES: ["UL"]}, {})

    assert key.description == {"suggested_value": ["QI", "QI4"]}
    assert "QI4" in selector.config["options"]


def test_the_selector_offers_the_usual_the_servers_and_the_saved_types() -> None:
    data = {"io_all": [_io("QI1"), _io("ZZ9"), _io("TL1", quickbus={"device_id": "3"})]}

    selector = io_type_selector(data, ["SAVED"])

    assert isinstance(selector, SelectSelector)
    options = selector.config["options"]
    assert {kind for kind in FUNCTION_PLAN_IO_GROUP_ORDER if kind.isalpha()} <= set(options)
    assert {"ZZ", "SAVED"} <= set(options)  # the server's own type and the saved one
    assert options == sorted(set(options))  # each once, sorted
    assert selector.config["multiple"] is True
    assert selector.config["custom_value"] is True  # any other type can be typed in


def test_the_known_types_come_from_the_extensions_only() -> None:
    data = {"io_all": [_io("QI1"), _io("QI2"), _io("ZZ9"), _io("TL1", quickbus={"device_id": "3"}), {}]}

    assert known_io_types(data) == ["QI", "ZZ"]


# The texts

TRANSLATIONS = Path(__file__).parents[2] / "custom_components" / "comexio" / "translations"
TRANSLATION_FILES = sorted(path.name for path in TRANSLATIONS.glob("*.json"))


@pytest.mark.parametrize("file_name", TRANSLATION_FILES)
def test_every_translation_names_the_field_and_the_reason(file_name: str) -> None:
    """Label and description in the setup step and in the options, the form error, and the three default types.

    The description is where the user learns why QI, TL and UL are left out of a new installation.
    """
    tree = json.loads((TRANSLATIONS / file_name).read_text(encoding="utf-8"))

    for step in (tree["config"]["step"]["ignore_lists"], tree["options"]["step"]["init"]):
        assert step["data"][CONF_IGNORED_IO_TYPES].strip()
        description = step["data_description"][CONF_IGNORED_IO_TYPES]
        assert all(kind in description for kind in DEFAULT_IGNORED_IO_TYPES)
    for section in ("config", "options"):
        assert tree[section]["error"]["invalid_ignored_io_types"].strip()
