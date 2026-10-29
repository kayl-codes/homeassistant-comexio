"""Entity-name schemas: rejected on save in the options flow, ignored with a warning if already saved."""

import pytest

from custom_components.comexio.api import _saved_schema
from custom_components.comexio.const import (
    CONF_SCHEMA_IO,
    CONF_SCHEMA_KNX,
    DEFAULT_SCHEMA_IO,
    ignore_list_categories,
    is_valid_entity_name_schema,
)
from custom_components.comexio.options_flow import (
    ComexioOptionsFlow,
    _hidden_ignore_lists,
    import_enabled_ignore_list_categories,
    normalize_ignore_lists,
)


def _normalize(user_input: dict) -> dict:
    errors: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, errors, set())
    return errors


@pytest.mark.parametrize("schema", ["{IoId", "{0} {IoTitle}", "IoId}"])
def test_malformed_io_schema_sets_a_field_error(schema: str) -> None:
    assert _normalize({CONF_SCHEMA_IO: schema}) == {CONF_SCHEMA_IO: "invalid_schema"}


def test_malformed_category_schema_sets_its_field_error() -> None:
    key = ignore_list_categories()[0].schema_conf_key
    assert _normalize({key: "M{MarkerId"}) == {key: "invalid_schema"}


def test_valid_schemas_pass_including_unknown_placeholders() -> None:
    user_input = {CONF_SCHEMA_IO: DEFAULT_SCHEMA_IO}
    user_input.update({cat.schema_conf_key: "{Unknown} {MarkerTitle}" for cat in ignore_list_categories()})
    assert _normalize(user_input) == {}


def test_invalid_saved_schema_falls_back_to_the_default(caplog: pytest.LogCaptureFixture) -> None:
    conf = {CONF_SCHEMA_IO: "{IoId", CONF_SCHEMA_KNX: "K{KnxId} {KnxTitle}"}
    assert _saved_schema(conf, CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO) == DEFAULT_SCHEMA_IO
    assert "invalid saved" in caplog.text
    assert _saved_schema(conf, CONF_SCHEMA_KNX, "unused") == "K{KnxId} {KnxTitle}"
    assert _saved_schema({}, CONF_SCHEMA_KNX, "K{KnxId}") == "K{KnxId}"


@pytest.mark.parametrize("schema", [None, 42, ["{IoId}"], b"{IoId}"])
def test_non_string_schema_is_invalid_not_an_exception(schema: object) -> None:
    # aiocomexio turns format_map's AttributeError/TypeError into ValueError; pin that contract.
    assert not is_valid_entity_name_schema(schema)
    assert _saved_schema({CONF_SCHEMA_IO: schema}, CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO) == DEFAULT_SCHEMA_IO


def test_an_emptied_ignore_list_is_saved_empty() -> None:
    """HA sends no key for an emptied optional field; it must not bring the old list back."""
    user_input: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, {}, set())
    assert all(user_input[cat.ignored_conf_key] == "" for cat in ignore_list_categories())


def test_a_hidden_category_keeps_its_ignore_list() -> None:
    """A hidden category had no field in the form: its list stays out of the input, so the entry keeps it."""
    hidden, visible = ignore_list_categories()[0], ignore_list_categories()[1]
    user_input: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, {}, {hidden.key})
    assert hidden.ignored_conf_key not in user_input
    assert user_input[visible.ignored_conf_key] == ""


def test_an_ignore_list_hidden_for_its_import_keeps_its_list() -> None:
    """A category shown in the form but not imported had no ignore-list field, so its saved list stays."""
    off, on = ignore_list_categories()[0], ignore_list_categories()[1]
    user_input: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, {}, set(), {off.key})
    assert off.ignored_conf_key not in user_input
    assert user_input[on.ignored_conf_key] == ""


def test_an_invalid_hidden_list_does_not_block_the_form() -> None:
    """A hidden list is never re-validated: its error would sit on a field nobody sees."""
    hidden, visible = ignore_list_categories()
    user_input: dict = {visible.ignored_conf_key: "x"}
    errors: dict[str, str] = {}
    ComexioOptionsFlow._normalize_user_input(user_input, errors, set(), {hidden.key})
    assert hidden.ignored_conf_key not in user_input
    assert set(errors) == {visible.ignored_conf_key}  # the visible field is still validated


def test_ignore_list_fields_follow_the_import_flags() -> None:
    """Only an imported category's list acts on anything; a category the form hides stays hidden."""
    marker, knx = ignore_list_categories()
    cats = ignore_list_categories()
    # Defaults: markers imported, KNX not.
    assert _hidden_ignore_lists(cats, {}, set()) == {knx.key}
    assert _hidden_ignore_lists(cats, {knx.import_conf_key: True}, set()) == set()
    assert _hidden_ignore_lists(cats, {marker.import_conf_key: False}, set()) == {marker.key, knx.key}
    assert _hidden_ignore_lists(cats, {knx.import_conf_key: True}, {knx.key}) == {knx.key}
    assert import_enabled_ignore_list_categories({knx.import_conf_key: True}) == [marker, knx]


def test_normalize_ignore_lists_reports_each_invalid_list() -> None:
    marker, knx = ignore_list_categories()
    user_input = {marker.ignored_conf_key: "m3; 1-2, 3", knx.ignored_conf_key: "K4, x"}
    errors: dict[str, str] = {}
    normalize_ignore_lists(user_input, [marker, knx], errors)
    assert user_input[marker.ignored_conf_key] == "1-2,3"
    # A translation key, not a message: the form shows it in the user's language.
    assert errors == {knx.ignored_conf_key: "invalid_ignored_ids"}
