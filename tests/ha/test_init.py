"""Setup, setup failures and unload of a config entry."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.webhook import DOMAIN as WEBHOOK_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator

from .conftest import SERVER_ID

WEBHOOK_ID = f"comexio_{SERVER_ID}"

# One entity per kind the fixture config (config_basic.json) yields; None = state not asserted.
EXPECTED_ENTITIES = {
    "switch.iosrv1_m1": "on",  # digital marker, value from get_live_states
    "number.iosrv1_m2": None,  # analog marker
    "binary_sensor.iosrv1_m3": None,  # [RO] digital marker
    "button.iosrv1_m4": None,  # [TRIG] marker
    "binary_sensor.iosrv1_base_i1": "on",  # digital input
    "switch.iosrv1_base_q1": "off",  # digital output
    "sensor.iosrv1_base_ai1": "21.5",  # analog input, Comexio decimal comma in the config
}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], api_returns: dict
) -> None:
    """The entry loads the fixture's entities with their live values; unload closes the session and webhook."""
    api_returns["get_live_states"] = ({"1": "1"}, {})

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    entries = er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
    assert set(EXPECTED_ENTITIES) <= {entry.entity_id for entry in entries}
    for entity_id, expected_state in EXPECTED_ENTITIES.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        if expected_state is not None:
            assert state.state == expected_state, entity_id
    assert WEBHOOK_ID in hass.data[WEBHOOK_DOMAIN]
    api = mock_comexio_api[0]
    api.close.assert_not_called()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("switch.iosrv1_m1").state == "unavailable"
    assert WEBHOOK_ID not in hass.data[WEBHOOK_DOMAIN]
    api.close.assert_called_once()


async def test_setup_keeps_every_entity_it_registers(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The orphan cleanup removes no entity the platforms just registered: the always-active list is complete."""
    removed: list[str] = []

    @callback
    def _record_removal(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] == "remove":
            removed.append(event.data["entity_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _record_removal)

    await _setup(hass, mock_config_entry)

    assert removed == []


async def test_statistics_unit_fix_runs_in_background_and_stops_on_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The fix waits for the recorder without holding up async_block_till_done; unload cancels it."""
    started: list[tuple[Any, ...]] = []
    cancelled = asyncio.Event()

    async def _waiting_fix(*args: Any) -> None:
        started.append(args)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch("custom_components.comexio._async_fix_statistics_units", _waiting_fix):
        async with asyncio.timeout(10):
            await _setup(hass, mock_config_entry)

    assert started == [(hass, SERVER_ID, mock_config_entry.entry_id)]
    assert not cancelled.is_set()

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert cancelled.is_set()


async def test_a_poll_drops_deleted_plans_from_the_snapshot_before_its_audits(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Plan 9 is no longer in the poll's $Fubs: the audits must not wait for the next backup cycle to forget it."""
    await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator._last_referenced_markers_from_snapshot is False  # no bulk load yet: stored backup
    plan = {"elements": {}, "connections": {}}
    coordinator.function_plan_plans = {1: plan, 9: plan}
    audit_webio = coordinator._async_audit_webio
    seen: list[set[int]] = []

    async def recording_audit(*args: Any) -> bool:
        seen.append(set(coordinator.function_plan_plans))
        return await audit_webio(*args)

    with patch.object(coordinator, "_async_audit_webio", recording_audit):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert seen == [{1}]  # plan 1 is still in config_basic.json's $Fubs
    assert coordinator._last_referenced_markers_from_snapshot is True


async def test_the_reference_monitor_judges_findings_by_the_polls_plan_list(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """A plan the poll lists but the snapshot lacks keeps its findings; plan 9, no longer listed, loses them."""
    await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.api.reference_check = None  # no catalog: the findings can only be kept or dropped
    coordinator.reference_monitor._unknown_refs = [("1", "3", "999"), ("9", "4", "998")]

    coordinator.reference_monitor.check_plans({})

    assert coordinator.reference_monitor._unknown_refs == [("1", "3", "999")]  # plan 1 is in config_basic.json


async def test_an_internal_options_write_skips_its_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """R2: the listener run of an internal write is skipped, the write itself is kept."""
    await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    with patch.object(coordinator, "async_reload_entry", AsyncMock()) as reload:
        coordinator.request_options_update_without_reload({**mock_config_entry.options, "probe": 1})
        await hass.async_block_till_done()

    reload.assert_not_awaited()
    assert mock_config_entry.options["probe"] == 1
    assert coordinator.take_pending_reload_skip_options() is None  # consumed by the listener run


async def test_an_unchanged_options_write_leaves_no_skip_behind(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Regression: an unchanged write runs no listener, so its skip must not swallow the next reload."""
    await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    with patch.object(coordinator, "async_reload_entry", AsyncMock()) as reload:
        coordinator.request_options_update_without_reload(dict(mock_config_entry.options))
        assert coordinator.take_pending_reload_skip_options() is None
        await hass.async_block_till_done()
        reload.assert_not_awaited()

        hass.config_entries.async_update_entry(
            mock_config_entry, data={**mock_config_entry.data, CONF_PASSWORD: "changed"}
        )
        await hass.async_block_till_done()

    reload.assert_awaited_once()


async def test_an_unchanged_options_write_keeps_a_pending_skip(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """The skip of an earlier write whose listener has not run yet survives an unchanged write."""
    await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    with patch.object(coordinator, "async_reload_entry", AsyncMock()) as reload:
        coordinator.request_options_update_without_reload({**mock_config_entry.options, "probe": 1})
        coordinator.request_options_update_without_reload(dict(mock_config_entry.options))
        await hass.async_block_till_done()

    reload.assert_not_awaited()
    assert mock_config_entry.options["probe"] == 1
    assert coordinator.take_pending_reload_skip_options() is None  # consumed by the first write's listener run


async def test_an_options_write_during_setup_leaves_no_skip_behind(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI]
) -> None:
    """Regression: a write before the listener is registered runs none, so its skip must not outlive the setup."""

    def _write_during_setup(coordinator: ComexioCoordinator) -> None:
        coordinator.request_options_update_without_reload({**coordinator.config_entry.options, "probe": 1})

    with patch.object(ComexioCoordinator, "check_knx_prerelease_cleanup", _write_during_setup):
        await _setup(hass, mock_config_entry)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert mock_config_entry.options["probe"] == 1
    assert coordinator.take_pending_reload_skip_options() is None

    with patch.object(coordinator, "async_reload_entry", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            mock_config_entry, data={**mock_config_entry.data, CONF_PASSWORD: "changed"}
        )
        await hass.async_block_till_done()

    reload.assert_awaited_once()


@pytest.mark.parametrize("api_attributes", [{"last_login_error": "connection"}])
async def test_setup_retry_when_unreachable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
) -> None:
    """A login that fails on the connection retries the setup and closes the session."""
    api_returns["login"] = False

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_comexio_api[0].close.assert_called_once()


@pytest.mark.logged_exception  # the coordinator logs the failed first refresh with its traceback
async def test_setup_retry_when_first_refresh_fails(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_comexio_api: list[ComexioAPI], api_returns: dict
) -> None:
    """A failing config fetch after a good login retries the setup and closes the session."""
    api_returns["get_raw_config"] = OSError("connection reset")

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_comexio_api[0].close.assert_called_once()


@pytest.mark.parametrize("api_attributes", [{"last_login_error": "rejected"}])
async def test_setup_auth_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
) -> None:
    """Rejected credentials fail the setup for good (no retry) and close the session."""
    api_returns["login"] = False

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    mock_comexio_api[0].close.assert_called_once()


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="ComexioConfigFlow has no async_step_reauth: HA aborts the reauth flow it starts",
)
@pytest.mark.parametrize("api_attributes", [{"last_login_error": "rejected"}])
async def test_setup_auth_failed_offers_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    api_returns: dict,
    api_attributes: dict,
) -> None:
    """Rejected credentials leave a reauth flow for the user to enter new ones."""
    api_returns["login"] = False

    await _setup(hass, mock_config_entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
