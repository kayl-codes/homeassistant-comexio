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
        # "BASE" is not a separate physical extension module — it's Comexio's own name for the
        # IO-Server's built-in IOs, i.e. the hub itself. Group these under the hub device instead
        # of a synthetic "... BASE" sub-device.
        if self._ext_name.upper() == "BASE":
            return {
                "identifiers": {(DOMAIN, self.coordinator.server_id)},
                "name": self.coordinator.server_id,
                "manufacturer": "Comexio",
                "model": "IO-Server",
            }
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
