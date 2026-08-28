# Version: 0.7.5
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComexioCoordinator


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
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_{self._ext_name}".lower())},
            "name": f"{self.coordinator.server_id} {self._ext_name}",
            "manufacturer": "Comexio",
            "model": "Extension Module",
            "via_device": (DOMAIN, self.coordinator.server_id),
        }

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
        return {
            "identifiers": {(DOMAIN, f"{self.coordinator.server_id}_markers")},
            "name": f"{self.coordinator.server_id} Markers",
            "manufacturer": "Comexio",
            "model": "Marker Group",
            "via_device": (DOMAIN, self.coordinator.server_id),
        }
