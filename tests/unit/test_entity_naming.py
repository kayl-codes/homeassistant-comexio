"""Entity naming rules: stable entity_ids and the config entry schema migration.

The rule behind all of these: ids carry only the technical address (server, extension, IO /
marker / KNX id), only the display name carries the Comexio description.
"""

from typing import Any
from unittest.mock import MagicMock

from homeassistant.helpers.entity import Entity
import pytest

from custom_components.comexio.const import (
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_KNX_PRERELEASE_CLEANUP_PENDING,
    CONF_SCHEMA_IO,
    CONFIG_ENTRY_MINOR_VERSION,
    DEFAULT_SCHEMA_IO,
    LEGACY_DEFAULT_SCHEMA_IO,
    entity_id_migration_target,
    hub_era_object_id,
    migrate_entry_options,
    stable_object_id,
)
from custom_components.comexio.entity import ComexioFunctionPlanEntityMixin, ComexioStableEntityIdMixin


@pytest.mark.parametrize(
    ("unique_id", "expected"),
    [
        ("comexio_iosrv1_iox1_ai7", "iosrv1_iox1_ai7"),
        ("comexio_iosrv1_m12", "iosrv1_m12"),
        ("comexio_iosrv1_k5", "iosrv1_k5"),
        ("comexio_iosrv1_k1_k2", "iosrv1_k1_k2"),
        ("comexio_iosrv1_fub19", "iosrv1_fub19"),
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


def test_function_plan_device_sorts_right_below_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan sub-device keeps its own identifier and its '#' name sorts before every extension."""
    from custom_components.comexio import entity

    monkeypatch.setattr(entity, "hub_device_id", lambda _coordinator: "hub-device")
    coordinator = MagicMock(server_id="iosrv1")

    info = entity.function_plan_device_info(coordinator)

    assert info["identifiers"] == {("comexio", "iosrv1_function_plans")}
    assert info["via_device_id"] == "hub-device"
    device_names = ["iosrv1 IOX1", "iosrv1 BASE", "iosrv1 0815", info["name"], "iosrv1 Markers", "iosrv1"]
    assert sorted(device_names, key=str.casefold)[:2] == ["iosrv1", "iosrv1 # Function plans"]


def _function_plan_entity_classes() -> list[type]:
    from custom_components.comexio import button, image, select, sensor

    return [
        select.ComexioPlanSelectEntity,
        select.ComexioPlanBackupSelectEntity,
        image.ComexioPlanPreviewImage,
        sensor.ComexioFunctionPlanBackupSensor,
        sensor.ComexioPlanChangedSensor,
        sensor.ComexioFunctionPlanCountSensor,
        sensor.ComexioPlanPreviewSensor,
        button.ComexioPlanPreviewButton,
        button.ComexioPlanToggleButton,
    ]


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        # Object ids of the entity_ids the plan card docs (FUNCTION_PLAN_PREVIEW.md) and existing installs use.
        ("ComexioPlanSelectEntity", "iosrv1_function_plans"),
        ("ComexioPlanBackupSelectEntity", "iosrv1_function_plan_backup"),
        ("ComexioPlanPreviewImage", "iosrv1_plan_preview"),
        ("ComexioFunctionPlanBackupSensor", "iosrv1_function_plan_backups"),
        ("ComexioPlanChangedSensor", "iosrv1_plan_changed"),
        ("ComexioFunctionPlanCountSensor", "iosrv1_function_plan_count"),
        ("ComexioPlanPreviewSensor", "iosrv1_plan_preview_info"),
        ("ComexioPlanPreviewButton", "iosrv1_preview"),
        ("ComexioPlanToggleButton", "iosrv1_function_plan_toggle"),
    ],
)
def test_function_plan_entities_keep_their_hub_era_entity_id(class_name: str, expected: str) -> None:
    """Moving to the function plan sub-device must not change the entity_id a new install gets."""
    cls = next(c for c in _function_plan_entity_classes() if c.__name__ == class_name)

    assert issubclass(cls, ComexioFunctionPlanEntityMixin)
    assert hub_era_object_id("iosrv1", cls._hub_era_name) == expected


class _PlanEntity(ComexioFunctionPlanEntityMixin, _FakeEntity):
    _hub_era_name = "Plan Preview"

    def __init__(self, server_id: str) -> None:
        self.coordinator = MagicMock(server_id=server_id)
        self.entity_id = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    ("server_id", "expected"),
    [("iosrv1", "image.iosrv1_plan_preview"), ("io-srv 2", "image.io_srv_2_plan_preview")],
)
def test_function_plan_mixin_requests_the_hub_era_entity_id(server_id: str, expected: str) -> None:
    entity = _PlanEntity(server_id)

    entity.add_to_platform_start(MagicMock(), MagicMock(domain="image"), None)

    assert entity.started
    assert entity.entity_id == expected


# Entity classes whose entity_id HA derives from the device name + translated entity name (see the
# "HA-derived" rows of the unique_id table in CLAUDE.md). Existing installs already carry these ids;
# a new entity class must not be added here by default — give it ComexioStableEntityIdMixin (or
# ComexioFunctionPlanEntityMixin on the function plan device) unless HA-derived is a deliberate choice.
HA_DERIVED_ENTITY_ID_CLASSES = frozenset(
    {
        "binary_sensor.ComexioSdCardSensor",
        "button.ComexioCancelSyncButton",
        "button.ComexioCleanupButton",
        "button.ComexioEntityIdMigrationButton",
        "button.ComexioFirmwareCheckButton",
        "button.ComexioStatisticsCleanupButton",
        "button.ComexioSyncButton",
        "button.ComexioWebioRangeCheckButton",
        "sensor.ComexioActiveMarkerCountSensor",
        "sensor.ComexioBusLoadSensor",
        "sensor.ComexioExtensionCountSensor",
        "sensor.ComexioOfflineExtensionsSensor",
        "sensor.ComexioSyncStatusSensor",
        "sensor.ComexioVersionSensor",
        "sensor.ComexioWatchdogEventSensor",
        "sensor.ComexioWebioCommandCountSensor",
        "update.ComexioBaseFirmwareUpdate",
        "update.ComexioExtensionFirmwareUpdate",
        "update.ComexioFirmwareUpdateBase",
    }
)


def _integration_entity_classes() -> dict[str, type]:
    """Every Entity subclass defined in the integration's modules (subpackages included), keyed "module.Class"."""
    import importlib
    import inspect
    import pkgutil

    import custom_components.comexio as package

    prefix = f"{package.__name__}."
    classes: dict[str, type] = {}
    for module_info in pkgutil.walk_packages(package.__path__, prefix):
        module = importlib.import_module(module_info.name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, Entity):
                classes[f"{module.__name__.removeprefix(prefix)}.{name}"] = cls
    return classes


def _requests_its_entity_id(cls: type) -> bool:
    """The mixin's add_to_platform_start runs only if it precedes Entity in the MRO (Entity's hook calls no super)."""
    mro = cls.__mro__
    return any(
        mixin in mro and mro.index(mixin) < mro.index(Entity)
        for mixin in (ComexioStableEntityIdMixin, ComexioFunctionPlanEntityMixin)
    )


def test_every_entity_class_requests_a_stable_entity_id() -> None:
    """A new entity class without an id mixin would get a language-dependent, name-derived entity_id."""
    classes = _integration_entity_classes()
    # Guards against a discovery that silently finds nothing: one class per id mixin and platform kind.
    assert {"sensor.ComexioMarkerSensor", "select.ComexioPlanSelectEntity", "light.ComexioKnxLight"} <= classes.keys()
    unstable = sorted(
        key
        for key, cls in classes.items()
        if not _requests_its_entity_id(cls) and key not in HA_DERIVED_ENTITY_ID_CLASSES
    )

    assert not unstable, (
        "Entity classes without ComexioStableEntityIdMixin / ComexioFunctionPlanEntityMixin "
        f"(add the mixin, or list them in HA_DERIVED_ENTITY_ID_CLASSES if that is deliberate): {unstable}"
    )


def test_ha_derived_entity_id_allowlist_has_no_stale_entries() -> None:
    classes = _integration_entity_classes()

    assert sorted(HA_DERIVED_ENTITY_ID_CLASSES - classes.keys()) == []
    assert sorted(key for key in HA_DERIVED_ENTITY_ID_CLASSES if _requests_its_entity_id(classes[key])) == []
