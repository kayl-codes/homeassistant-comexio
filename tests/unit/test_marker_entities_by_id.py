"""marker_entities_by_id only returns this config entry's entities — the cleanup deletes what it returns (di)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.comexio import button as button_module, coordinator as coordinator_module
from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.const import source_category
from custom_components.comexio.coordinator import ComexioCoordinator

ENTRY_ID = "comexio_entry"
OTHER_ENTRY_ID = "other_entry"


class _FakeEntities:
    """The two lookups of EntityRegistryItems the helper could use: all entries, or one entry's."""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self._entries = entries

    def values(self) -> list[SimpleNamespace]:
        return list(self._entries)

    def get_entries_for_config_entry_id(self, config_entry_id: str) -> list[SimpleNamespace]:
        return [entry for entry in self._entries if entry.config_entry_id == config_entry_id]


def _entity(entity_id: str, unique_id: str | None, config_entry_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(entity_id=entity_id, unique_id=unique_id, config_entry_id=config_entry_id)


def _coordinator() -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "iosrv1"
    coordinator.hass = MagicMock()
    coordinator.config_entry = SimpleNamespace(entry_id=ENTRY_ID)  # type: ignore[assignment]
    return coordinator


def _lookup(entries: list[SimpleNamespace], marker_ids: list[int], infix: str = "m") -> dict[int, SimpleNamespace]:
    registry = SimpleNamespace(entities=_FakeEntities(entries))
    with patch.object(coordinator_module.er, "async_get", return_value=registry):
        return _coordinator().marker_entities_by_id(marker_ids, infix)  # type: ignore[return-value]


def test_entity_of_another_integration_with_the_same_unique_id_is_not_returned() -> None:
    # unique_ids are unique per integration only: another integration may register the very
    # string comexio uses. The cleanup button deletes every entity returned here.
    own = _entity("switch.iosrv1_m7", "comexio_iosrv1_m7", ENTRY_ID)
    foreign = _entity("switch.foreign_m8", "comexio_iosrv1_m8", OTHER_ENTRY_ID)
    yaml_entity = _entity("sensor.yaml_m9", "comexio_iosrv1_m9", None)

    assert _lookup([own, foreign, yaml_entity], [7, 8, 9]) == {7: own}


def test_own_entities_match_case_insensitively_by_category_infix() -> None:
    marker = _entity("switch.iosrv1_m7", "COMEXIO_iosrv1_M7", ENTRY_ID)
    knx = _entity("switch.iosrv1_k7", "comexio_iosrv1_k7", ENTRY_ID)
    no_unique_id = _entity("sensor.iosrv1_x", None, ENTRY_ID)

    assert _lookup([marker, knx, no_unique_id], [7]) == {7: marker}
    assert _lookup([marker, knx, no_unique_id], [7], "k") == {7: knx}


def test_cleanup_removes_only_the_entities_of_this_entry() -> None:
    # The cleanup button deletes through this helper: the foreign entity with a colliding
    # unique_id stays in the registry, only this entry's entity is removed and counted.
    own = _entity("switch.iosrv1_m7", "comexio_iosrv1_m7", ENTRY_ID)
    foreign = _entity("switch.foreign_m8", "comexio_iosrv1_m8", OTHER_ENTRY_ID)
    registry = SimpleNamespace(entities=_FakeEntities([own, foreign]), async_remove=MagicMock())
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = _coordinator()
    button.server_id = "iosrv1"
    button.hass = button.coordinator.hass

    with (
        patch.object(coordinator_module.er, "async_get", return_value=registry),
        patch.object(button_module.er, "async_get", return_value=registry),
    ):
        deleted = button._delete_source_entities([7, 8], source_category("marker"))

    assert deleted == 1
    registry.async_remove.assert_called_once_with(own.entity_id)
