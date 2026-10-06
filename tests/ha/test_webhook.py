"""Live values pushed by the Comexio Web-IO commands to the HA webhook."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator

from .conftest import SERVER_ID

WEBHOOK_URL = f"/api/webhook/comexio_{SERVER_ID}"
PUSH_GAP = timedelta(seconds=30)

# One push per webhook type, as the webhook handler dispatches them to the coordinator.
PUSHES: dict[str, Callable[[ComexioCoordinator], None]] = {
    "marker": lambda coordinator: coordinator.update_marker("1", 1),
    "knx": lambda coordinator: coordinator.update_knx("1", 1),
    "io": lambda coordinator: coordinator.update_io_by_name("BASE", "AI1", 22.75),
}


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> MockConfigEntry:
    """A set-up config entry with the fixture config."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


@pytest.mark.usefixtures("loaded_entry")
@pytest.mark.parametrize(
    ("payload", "entity_id", "expected"),
    [
        ({"type": "marker", "id": "1", "value": 1}, "switch.iosrv1_m1", "on"),
        ({"type": "io", "ext": "BASE", "io": "I1", "value": 0}, "binary_sensor.iosrv1_base_i1", "off"),
        # ext/io match case-insensitively; the Web-IO Lua command pushes the value as a number.
        ({"type": "io", "ext": "base", "io": "ai1", "value": 22.75}, "sensor.iosrv1_base_ai1", "22.75"),
    ],
)
async def test_webhook_updates_state(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    payload: dict[str, Any],
    entity_id: str,
    expected: str,
) -> None:
    """A pushed value lands in the entity state without a poll."""
    client = await hass_client_no_auth()

    response = await client.post(WEBHOOK_URL, json=payload)
    await hass.async_block_till_done()

    assert response.status == 200
    assert hass.states.get(entity_id).state == expected


@pytest.mark.usefixtures("loaded_entry")
@pytest.mark.parametrize(
    ("payload", "log_message"),
    [
        ({"type": "marker", "id": "999", "value": 1}, None),
        ({"type": "io", "ext": "NOPE", "io": "X1", "value": 1}, "NOPE"),
    ],
    ids=["unknown_marker", "unknown_io"],
)
async def test_webhook_unknown_target_changes_nothing(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, Any],
    log_message: str | None,
) -> None:
    """A push for an id HA has no entity for is accepted and changes no entity state."""
    client = await hass_client_no_auth()
    before = {state.entity_id: state.state for state in hass.states.async_all()}

    response = await client.post(WEBHOOK_URL, json=payload)
    await hass.async_block_till_done()

    assert response.status == 200
    assert {state.entity_id: state.state for state in hass.states.async_all()} == before
    if log_message:
        assert log_message in caplog.text


@pytest.mark.usefixtures("loaded_entry")
@pytest.mark.parametrize(
    ("body", "log_message"),
    [
        ("kein json", "Received non-JSON payload"),
        ('{"type": "io", "ext": "BASE"}', "Webhook IO event missing ext/io"),
        ('{"type": "marker"}', "Webhook marker event missing id"),
        ('{"type": "knx"}', "Webhook KNX event missing id"),
    ],
)
async def test_webhook_rejects_malformed_payload(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    body: str,
    log_message: str,
) -> None:
    """A malformed push is logged by its own branch (not the catch-all) and changes no state."""
    client = await hass_client_no_auth()
    before = {state.entity_id: state.state for state in hass.states.async_all()}

    await client.post(WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"})
    await hass.async_block_till_done()

    assert log_message in caplog.text
    assert "Webhook Error" not in caplog.text
    assert {state.entity_id: state.state for state in hass.states.async_all()} == before


@pytest.mark.parametrize("push", PUSHES.values(), ids=PUSHES.keys())
async def test_webhook_pushes_do_not_postpone_the_periodic_poll(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    freezer: FrozenDateTimeFactory,
    push: Callable[[ComexioCoordinator], None],
) -> None:
    """Pushes arriving faster than scan_interval still let the periodic poll run (#122).

    async_set_updated_data reschedules the update_interval refresh on every call, so a push
    every few seconds kept postponing the poll — the safety net for missed pushes — forever.
    """
    coordinator: ComexioCoordinator = hass.data[DOMAIN][loaded_entry.entry_id]
    api = mock_comexio_api[-1]
    api.get_raw_config.reset_mock()

    for _ in range(int(coordinator.update_interval / PUSH_GAP) + 2):
        push(coordinator)
        freezer.tick(PUSH_GAP)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert api.get_raw_config.await_count >= 1


@pytest.mark.parametrize("push", PUSHES.values(), ids=PUSHES.keys())
async def test_webhook_push_marks_the_coordinator_reachable(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, push: Callable[[ComexioCoordinator], None]
) -> None:
    """A received push makes the entities available again after a failed poll: it proves the server reachable."""
    coordinator: ComexioCoordinator = hass.data[DOMAIN][loaded_entry.entry_id]
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    assert hass.states.get("switch.iosrv1_m1").state == STATE_UNAVAILABLE

    push(coordinator)

    assert hass.states.get("switch.iosrv1_m1").state != STATE_UNAVAILABLE
