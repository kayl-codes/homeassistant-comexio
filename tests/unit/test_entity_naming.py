"""Entity naming rules: stable entity_ids and the config entry schema migration.

The rule behind all of these: ids carry only the technical address (server, extension, IO /
marker / KNX id), only the display name carries the Comexio description.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio.const import (
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_KNX_PRERELEASE_CLEANUP_PENDING,
    CONF_SCHEMA_IO,
    CONFIG_ENTRY_MINOR_VERSION,
    DEFAULT_SCHEMA_IO,
    LEGACY_DEFAULT_SCHEMA_IO,
    entity_id_migration_target,
    migrate_entry_options,
    stable_object_id,
)
from custom_components.comexio.entity import ComexioStableEntityIdMixin


@pytest.mark.parametrize(
    ("unique_id", "expected"),
    [
        ("comexio_iosrv1_iox1_ai7", "iosrv1_iox1_ai7"),
        ("comexio_iosrv1_m12", "iosrv1_m12"),
        ("comexio_iosrv1_k5", "iosrv1_k5"),
        ("comexio_iosrv1_k1_k2", "iosrv1_k1_k2"),
        ("comexio_io-srv_ext 2_ei1_win", "io_srv_ext_2_ei1_win"),
    ],
)
def test_stable_object_id_is_the_unique_id_address(unique_id: str, expected: str) -> None:
    assert stable_object_id(unique_id) == expected


class _FakeEntity:
    """Stands in for homeassistant Entity: records that the base hook ran."""

    started = False

    def add_to_platform_start(self, hass: Any, platform: Any, parallel_updates: Any) -> None:
        self.started = True


class _Entity(ComexioStableEntityIdMixin, _FakeEntity):
    def __init__(self, unique_id: str | None) -> None:
        self.unique_id = unique_id
        self.entity_id = None  # type: ignore[assignment]


def test_entity_requests_stable_entity_id_for_its_platform_domain() -> None:
    entity = _Entity("comexio_iosrv1_iox1_ai7")

    entity.add_to_platform_start(MagicMock(), MagicMock(domain="binary_sensor"), None)

    assert entity.started
    assert entity.entity_id == "binary_sensor.iosrv1_iox1_ai7"


def test_entity_without_unique_id_leaves_entity_id_to_ha() -> None:
    entity = _Entity(None)

    entity.add_to_platform_start(MagicMock(), MagicMock(domain="switch"), None)

    assert entity.entity_id is None


def test_new_default_io_schema_has_no_extension_name() -> None:
    assert DEFAULT_SCHEMA_IO == "{IoId} {IoTitle}"


def test_migration_pins_legacy_io_schema_when_never_saved() -> None:
    options = migrate_entry_options(2, {}, {"scan_interval": 600})

    assert options == {"scan_interval": 600, CONF_SCHEMA_IO: LEGACY_DEFAULT_SCHEMA_IO}


@pytest.mark.parametrize(
    ("data", "options"),
    [({}, {CONF_SCHEMA_IO: "{IoTitle}"}), ({CONF_SCHEMA_IO: "{IoTitle}"}, {})],
)
def test_migration_keeps_a_saved_io_schema(data: dict[str, Any], options: dict[str, Any]) -> None:
    assert migrate_entry_options(2, data, options) == options


def test_migration_from_1_1_sets_knx_flag_and_pins_schema() -> None:
    options = migrate_entry_options(1, {}, {})

    assert options == {CONF_KNX_PRERELEASE_CLEANUP_PENDING: True, CONF_SCHEMA_IO: LEGACY_DEFAULT_SCHEMA_IO}


def test_migration_is_a_no_op_at_current_version() -> None:
    assert migrate_entry_options(CONFIG_ENTRY_MINOR_VERSION, {}, {"a": 1}) == {"a": 1}


@pytest.mark.parametrize(
    ("entity_id", "unique_id", "suggested", "expected"),
    [
        # Name-derived entity_id of a stable-id entity converges on the technical address.
        (
            "binary_sensor.iosrv1_iox1_iox1_ai7_p5_1_flur_pm_bewegung_ws",
            "comexio_iosrv1_iox1_ai7",
            "iosrv1_iox1_ai7",
            "binary_sensor.iosrv1_iox1_ai7",
        ),
        ("switch.iosrv1_markers_m12_licht", "comexio_iosrv1_m12", "iosrv1_m12", "switch.iosrv1_m12"),
        # Already stable: nothing to do.
        ("binary_sensor.iosrv1_iox1_ai7", "comexio_iosrv1_iox1_ai7", "iosrv1_iox1_ai7", None),
        # Not yet re-added since the stable-id change (no suggested_object_id): left alone.
        ("binary_sensor.iosrv1_iox1_iox1_ai7_x", "comexio_iosrv1_iox1_ai7", None, None),
        # A suggested_object_id that is not ours (e.g. set by something else) is not trusted.
        ("sensor.iosrv1_x", "comexio_iosrv1_iox1_ai7", "something_else", None),
        # Legacy doubled server prefix on diagnostic entities is still corrected.
        (
            "button.comexio_iosrv1_iosrv1_sync",
            "comexio_iosrv1_webio_sync_start_btn",
            None,
            "button.comexio_iosrv1_sync",
        ),
        ("sensor.iosrv1_sync_status", "comexio_iosrv1_webio_sync_status_sensor", None, None),
    ],
)
def test_entity_id_migration_target(
    entity_id: str, unique_id: str, suggested: str | None, expected: str | None
) -> None:
    assert entity_id_migration_target(entity_id, unique_id, suggested, "iosrv1") == expected


def test_migration_resets_the_old_entity_id_ignore_flag() -> None:
    options = migrate_entry_options(2, {}, {CONF_SCHEMA_IO: "{IoTitle}", CONF_ENTITY_ID_MIGRATION_IGNORED: True})

    assert options == {CONF_SCHEMA_IO: "{IoTitle}"}
