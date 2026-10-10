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
from custom_components.comexio.options_flow import ComexioOptionsFlow


def _normalize(user_input: dict) -> dict:
    errors: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, {}, errors, set())
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
    conf = {cat.ignored_conf_key: "5" for cat in ignore_list_categories()}
    user_input: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, conf, {}, set())
    assert all(user_input[cat.ignored_conf_key] == "" for cat in ignore_list_categories())


def test_a_hidden_category_keeps_its_ignore_list() -> None:
    """A hidden category had no field in the form, so its saved list stays."""
    hidden, visible = ignore_list_categories()[0], ignore_list_categories()[1]
    conf = {hidden.ignored_conf_key: "5,7", visible.ignored_conf_key: "9"}
    user_input: dict = {}
    ComexioOptionsFlow._normalize_user_input(user_input, conf, {}, {hidden.key})
    assert user_input[hidden.ignored_conf_key] == "5,7"
    assert user_input[visible.ignored_conf_key] == ""
