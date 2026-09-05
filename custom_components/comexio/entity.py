# Version: 0.7.7
import logging
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)


def hub_device_id(coordinator: ComexioCoordinator) -> str | None:
    """Resolve the hub device's registry id for via_device_id linkage.

    Returns None (instead of raising) if the hub device isn't registered yet, so a
    dependent entity still gets created — just without the via_device_id link — rather
    than having its whole platform setup aborted by an uncaught ValueError.
    """
    try:
        return dr.async_get_device_id_by_identifier(
            coordinator.hass, (DOMAIN, coordinator.server_id), config_entry_id=coordinator.config_entry.entry_id
        )
    except ValueError:
        _LOGGER.error(
            "Hub device for server '%s' (entry %s) not found in the device registry; "
            "affected entities will be created without a via_device_id link",
            coordinator.server_id,
            coordinator.config_entry.entry_id,
        )
        return None


def build_device_info(
    coordinator: ComexioCoordinator, identifiers: set[tuple[str, str]], name: str, model: str
) -> dict[str, Any]:
    """Build a sub-device's device_info dict, linked to the hub device via via_device_id."""
    info: dict[str, Any] = {
        "identifiers": identifiers,
        "name": name,
        "manufacturer": "Comexio",
        "model": model,
    }
    if via_id := hub_device_id(coordinator):
        info["via_device_id"] = via_id
    return info


class ComexioIOEntity(CoordinatorEntity):
    """Shared base for all IO entities attached to an extension module.

    Centralises device_info and offline-availability logic that would otherwise
    be duplicated across sensor, switch, and binary_sensor platforms.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, io: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._io_id: str = io["id"]
        self._ext_name: str = io["ext_name"]
        self._attr_unique_id = f"comexio_{server_id}_{io['ext_name']}_{io['identifier']}".lower()
        self._attr_name = io["ha_name"]

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(
            self.coordinator,
            {(DOMAIN, f"{self.coordinator.server_id}_{self._ext_name}".lower())},
            f"{self.coordinator.server_id} {self._ext_name}",
            "Extension Module",
        )

    @property
    def available(self) -> bool:
        return super().available and self._ext_name not in self.coordinator.offline_extensions


class ComexioMarkerEntity(CoordinatorEntity):
    """Shared base for all marker entities (writable and read-only).

    Centralises unique_id/name/device_info that would otherwise be duplicated
    across the switch, number, binary_sensor, and sensor platforms.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComexioCoordinator, server_id: str, marker: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._marker_id = str(marker["id"])
        self._attr_unique_id = f"comexio_{server_id}_m{self._marker_id}".lower()
        self._attr_name = marker["ha_name"]

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(
            self.coordinator,
            {(DOMAIN, f"{self.coordinator.server_id}_markers")},
            f"{self.coordinator.server_id} Markers",
            "Marker Group",
        )
