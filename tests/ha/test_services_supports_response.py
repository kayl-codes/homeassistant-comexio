"""Every simple service is registered with the response mode its table entry names."""

from homeassistant.core import HomeAssistant, SupportsResponse
import pytest

from custom_components.comexio.const import DOMAIN
from custom_components.comexio.services import _SIMPLE_SERVICES, async_setup_services

_TABLE_MODES = [(name, expected) for name, _handler, expected in _SIMPLE_SERVICES]


@pytest.fixture
async def registered(hass: HomeAssistant) -> HomeAssistant:
    """The services as the entry setup registers them; the entry normally creates hass.data[DOMAIN] first."""
    hass.data.setdefault(DOMAIN, {})
    await async_setup_services(hass)
    return hass


@pytest.mark.parametrize(("name", "expected"), _TABLE_MODES, ids=[name for name, _ in _TABLE_MODES])
async def test_each_service_registers_with_its_table_response_mode(
    registered: HomeAssistant, name: str, expected: SupportsResponse
) -> None:
    assert registered.services.supports_response(DOMAIN, name) is expected


async def test_list_backups_only_answers_with_a_response(registered: HomeAssistant) -> None:
    """Automations read the backup list from the response; a plain call would break them."""
    assert registered.services.supports_response(DOMAIN, "function_plan_list_backups") is SupportsResponse.ONLY
