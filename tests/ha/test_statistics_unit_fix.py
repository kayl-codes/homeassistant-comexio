"""The one-time statistics unit fix (__init__._async_fix_statistics_units) and its repair-issue cleanup."""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.comexio import _async_fix_statistics_units

from .conftest import SERVER_ID

MISMATCHES = [(f"sensor.{SERVER_ID}_base_ai1", "V"), (f"sensor.{SERVER_ID}_base_ai2", "mA")]
# As HA's sensor recorder platform files them (domain "sensor", "units_changed_{statistic_id}").
OWN_ISSUES = {("sensor", f"units_changed_{stat_id}") for stat_id, _ in MISMATCHES}
# Kept: an unfixed statistic of this server, a server whose id starts with ours (iosrv10), and
# another issue type of a fixed statistic.
FOREIGN_ISSUES = {
    ("sensor", f"units_changed_sensor.{SERVER_ID}_base_ai3"),
    ("sensor", f"units_changed_sensor.{SERVER_ID}0_base_ai1"),
    ("sensor", f"state_class_removed_{MISMATCHES[0][0]}"),
}


@pytest.fixture
def update_metadata(hass: HomeAssistant) -> Generator[MagicMock]:
    """A recorder with two unit mismatches of this server, plus their repair issues and foreign ones."""
    hass.config.components.add("recorder")
    for domain, issue_id in OWN_ISSUES | FOREIGN_ISSUES:
        ir.async_create_issue(
            hass, domain, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="units_changed"
        )
    recorder = MagicMock(async_add_executor_job=AsyncMock(return_value=[]))
    with (
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.find_unit_mismatches", return_value=MISMATCHES),
        patch("homeassistant.components.recorder.statistics.async_update_statistics_metadata") as update,
    ):
        yield update


def _issues(hass: HomeAssistant) -> set[tuple[str, str]]:
    return set(ir.async_get(hass).issues)


async def test_fix_updates_metadata_and_removes_own_issues(hass: HomeAssistant, update_metadata: MagicMock) -> None:
    """Each mismatch gets a label-only metadata update; only the fixed statistics' unit-change issues go."""
    with patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0):
        await _async_fix_statistics_units(hass, SERVER_ID, "entry")

    assert update_metadata.call_args_list == [
        call(hass, stat_id, new_unit_of_measurement=unit, new_unit_class=None) for stat_id, unit in MISMATCHES
    ]
    assert _issues(hass) == FOREIGN_ISSUES


async def test_cancel_during_commit_wait_still_removes_own_issues(
    hass: HomeAssistant, update_metadata: MagicMock
) -> None:
    """Unload/reload while the recorder commits: the next run finds no mismatch, so clean up now."""
    with patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 3600):
        task = hass.async_create_task(_async_fix_statistics_units(hass, SERVER_ID, "entry"))
        async with asyncio.timeout(5):
            while not update_metadata.called:
                await asyncio.sleep(0)
        assert _issues(hass) == OWN_ISSUES | FOREIGN_ISSUES

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _issues(hass) == FOREIGN_ISSUES
