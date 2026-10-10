"""The KNX bridge marker phase of the uninstall cleanup (coordinator side)."""

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.cleanup_scope import (
    CLEANUP_SCOPE_IO,
    CLEANUP_SCOPE_KNX,
    SKIPPED_KNX_BRIDGE_MARKERS,
)
from custom_components.comexio.const import CONF_FUNCTION_PLAN_PLAN_MAP, DOMAIN, FUNCTION_PLAN_TRIGGER_PLAN_NAME
from custom_components.comexio.coordinator import BRIDGE_MARKERS_WAIT_FOR_TRIGGER_PLAN


async def _cleanup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    reset_result: Any,
) -> tuple[dict[str, Any], AsyncMock]:
    """Run the cleanup with every phase before the bridge markers stubbed out."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    monkeypatch.setattr(coordinator, "_cleanup_trigger_plan", AsyncMock(return_value=("keep", 0)))
    monkeypatch.setattr(coordinator, "_delete_managed_plans", AsyncMock(return_value=(["KNX 1"], [])))
    monkeypatch.setattr(coordinator, "_delete_webio_devices_and_classes", AsyncMock(return_value=([], [], {}, {})))
    monkeypatch.setattr(coordinator, "_delete_knx_loopback_webio", AsyncMock())
    reset = (
        AsyncMock(side_effect=reset_result)
        if isinstance(reset_result, BaseException)
        else AsyncMock(return_value=reset_result)
    )
    coordinator.api.reset_knx_bridge_markers = reset
    return await coordinator.async_uninstall_cleanup(scope), reset


def _entry_with_plan_map(entry: MockConfigEntry, plan_map: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        minor_version=entry.minor_version,
        data=dict(entry.data),
        options={CONF_FUNCTION_PLAN_PLAN_MAP: plan_map},
    )


@pytest.mark.parametrize(
    ("plan_map", "ignored"),
    [({FUNCTION_PLAN_TRIGGER_PLAN_NAME: 7}, [7]), ({FUNCTION_PLAN_TRIGGER_PLAN_NAME: "7"}, [7]), ({}, [])],
)
async def test_the_trigger_plan_is_no_use_of_a_bridge_marker(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
    plan_map: dict[str, Any],
    ignored: list[int],
) -> None:
    entry = _entry_with_plan_map(mock_config_entry, plan_map)
    _result, reset = await _cleanup(hass, entry, monkeypatch, CLEANUP_SCOPE_KNX, ([], [], [], [], None))

    assert list(reset.await_args.args[1]) == ignored


async def test_bridge_markers_wait_while_the_trigger_plan_keeps_its_knx_pairs(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pair removal leaves pairs a re-run only finds by their "[K<n>]" bridge titles."""
    entry = _entry_with_plan_map(mock_config_entry, {FUNCTION_PLAN_TRIGGER_PLAN_NAME: 7})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    monkeypatch.setattr(coordinator, "_cleanup_trigger_plan", AsyncMock(return_value=("remove_pairs", -1)))
    monkeypatch.setattr(coordinator, "_delete_managed_plans", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(coordinator, "_delete_webio_devices_and_classes", AsyncMock(return_value=([], [], {}, {})))
    monkeypatch.setattr(coordinator, "_delete_knx_loopback_webio", AsyncMock())
    coordinator.api.reset_knx_bridge_markers = reset = AsyncMock(return_value=([1], [], [], [], None))

    result = await coordinator.async_uninstall_cleanup(CLEANUP_SCOPE_KNX)

    reset.assert_not_awaited()
    assert result["failed_plans"] == [FUNCTION_PLAN_TRIGGER_PLAN_NAME]
    assert result["marker_reset_error"] == BRIDGE_MARKERS_WAIT_FOR_TRIGGER_PLAN
    assert (result["reset_markers"], result["normalized_markers"]) == ([], [])


async def test_the_cleanup_reports_each_bridge_marker_list_under_its_own_key(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _reset = await _cleanup(
        hass, mock_config_entry, monkeypatch, CLEANUP_SCOPE_KNX, ([1], [2, 3], [4], [5, 6], None)
    )

    assert result["reset_markers"] == [1]
    assert result["normalized_markers"] == [2, 3]
    assert result["failed_markers"] == [4]
    assert result["marker_reset_error"] is None
    assert result["skipped"][SKIPPED_KNX_BRIDGE_MARKERS].endswith(": M5, M6")


async def test_an_api_error_passes_through_without_a_skip(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _reset = await _cleanup(
        hass, mock_config_entry, monkeypatch, CLEANUP_SCOPE_KNX, ([], [], [], [], "could not load every function plan")
    )

    assert result["marker_reset_error"] == "could not load every function plan"
    assert SKIPPED_KNX_BRIDGE_MARKERS not in result["skipped"]


@pytest.mark.logged_exception  # the coordinator logs the failed phase with its traceback
@pytest.mark.parametrize(
    ("raised", "error"),
    [(TimeoutError("slow"), "connection error: slow"), (RuntimeError("boom"), "unexpected error, see log")],
)
async def test_a_failing_bridge_marker_phase_keeps_the_earlier_results(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    error: str,
) -> None:
    result, _reset = await _cleanup(hass, mock_config_entry, monkeypatch, CLEANUP_SCOPE_KNX, raised)

    assert result["marker_reset_error"] == error
    assert (result["reset_markers"], result["normalized_markers"], result["failed_markers"]) == ([], [], [])
    assert result["deleted_plans"] == ["KNX 1"]  # the earlier phases' results survive


async def test_a_cleanup_outside_knx_leaves_the_bridge_markers_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_comexio_api: list[ComexioAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, reset = await _cleanup(hass, mock_config_entry, monkeypatch, CLEANUP_SCOPE_IO, ([1], [2], [], [], None))

    reset.assert_not_awaited()
    assert (result["reset_markers"], result["normalized_markers"], result["failed_markers"]) == ([], [], [])
