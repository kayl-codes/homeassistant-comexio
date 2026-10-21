"""The buttons on the server device are configuration, not diagnostics."""

from unittest.mock import MagicMock

from homeassistant.helpers.entity import EntityCategory
import pytest

from custom_components.comexio import button
from custom_components.comexio.const import DOMAIN

SERVER_BUTTON_CLASSES = [
    button.ComexioSyncButton,
    button.ComexioCancelSyncButton,
    button.ComexioFirmwareCheckButton,
    button.ComexioWebioRangeCheckButton,
    button.ComexioEntityIdMigrationButton,
    button.ComexioStatisticsCleanupButton,
    button.ComexioCleanupButton,
]


@pytest.mark.parametrize("button_cls", SERVER_BUTTON_CLASSES)
def test_server_buttons_are_configuration(button_cls: type) -> None:
    coordinator = MagicMock()
    coordinator.server_id = "srv"

    entity = button_cls(coordinator, "srv")

    assert entity.device_info["identifiers"] == {(DOMAIN, "srv")}
    assert entity.entity_category is EntityCategory.CONFIG
