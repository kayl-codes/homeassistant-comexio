"""The one-time statistics unit fix (__init__._async_fix_statistics_units) and its repair-issue cleanup."""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio import _STATISTICS_LOOKUP_KEY, _async_fix_statistics_units
from custom_components.comexio.const import DOMAIN, STATISTICS_UNIT_FIX_ATTEMPTS

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


async def _release(hass: HomeAssistant, release: asyncio.Event) -> None:
    """Let the hanging shared lookup finish and wait for it, so the next call submits a new one."""
    lookup = hass.data[_STATISTICS_LOOKUP_KEY]
    release.set()
    async with asyncio.timeout(5):
        await lookup
    assert _STATISTICS_LOOKUP_KEY not in hass.data


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


async def test_a_recorder_failure_is_retried(hass: HomeAssistant, update_metadata: MagicMock) -> None:
    """A recorder not ready at the first check no longer leaves the labels wrong until the next restart."""
    retry_stats = [{"statistic_id": stat_id, "statistics_unit_of_measurement": ""} for stat_id, _ in MISMATCHES]
    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=[RuntimeError("not ready"), retry_stats]))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        # Mismatches only from the retry's rows: a retry that ignored its lookup would fix nothing.
        patch(
            "custom_components.comexio.find_unit_mismatches",
            side_effect=lambda stats, **_: MISMATCHES if stats == retry_stats else [],
        ) as find,
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0),
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, "entry")

    assert recorder.async_add_executor_job.await_count == 2
    # The mismatches come from what the retry looked up, not from the failed first attempt.
    assert [c.args[0] for c in find.call_args_list] == [retry_stats]
    assert update_metadata.call_args_list == [
        call(hass, stat_id, new_unit_of_measurement=unit, new_unit_class=None) for stat_id, unit in MISMATCHES
    ]
    assert _issues(hass) == FOREIGN_ISSUES


async def test_a_recorder_that_stays_down_ends_with_a_warning(
    hass: HomeAssistant, update_metadata: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=RuntimeError("not ready")))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, "entry")

    assert recorder.async_add_executor_job.await_count == STATISTICS_UNIT_FIX_ATTEMPTS
    update_metadata.assert_not_called()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        f"[{SERVER_ID}] Statistics unit fix incomplete after {STATISTICS_UNIT_FIX_ATTEMPTS} attempt(s) "
        "(statistics lookup failed: RuntimeError('not ready')); the next restart or reload tries again"
    ]


@pytest.mark.parametrize(("disabled_by", "checks"), [(None, STATISTICS_UNIT_FIX_ATTEMPTS), ("user", 1)])
async def test_an_enabled_entity_without_a_state_is_checked_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    disabled_by: str | None,
    checks: int,
) -> None:
    """Waiting is only for an entity that will get a state: a disabled one never does."""
    hass.config.components.add("recorder")
    mock_config_entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "comexio_iosrv1_qb346",
        config_entry=mock_config_entry,
        suggested_object_id=f"{SERVER_ID}_qb346",
        disabled_by=er.RegistryEntryDisabler(disabled_by) if disabled_by else None,
    )
    stats = [{"statistic_id": entity.entity_id, "statistics_unit_of_measurement": "lux"}]
    recorder = MagicMock(async_add_executor_job=AsyncMock(return_value=stats))
    with (
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.async_update_statistics_metadata") as update,
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, mock_config_entry.entry_id)

    assert recorder.async_add_executor_job.await_count == checks
    update.assert_not_called()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    expected = f"incomplete after {STATISTICS_UNIT_FIX_ATTEMPTS} attempt(s) (no state yet: {entity.entity_id})"
    assert [expected in w for w in warnings] == ([True] if disabled_by is None else [])


async def test_a_state_that_arrives_late_gets_its_label_fixed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    hass.config.components.add("recorder")
    mock_config_entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "comexio_iosrv1_qb346",
        config_entry=mock_config_entry,
        suggested_object_id=f"{SERVER_ID}_qb346",
    )
    stats = [{"statistic_id": entity.entity_id, "statistics_unit_of_measurement": "lux"}]

    async def _list_stats(*_args: object) -> list[dict[str, str]]:
        # The entity's first state arrives between the first and the second check.
        if recorder.async_add_executor_job.await_count == 2:
            hass.states.async_set(entity.entity_id, "0", {"unit_of_measurement": "lx"})
        return stats

    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=_list_stats))
    with (
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.async_update_statistics_metadata") as update,
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, mock_config_entry.entry_id)

    assert update.call_args_list == [call(hass, entity.entity_id, new_unit_of_measurement="lx", new_unit_class=None)]
    assert recorder.async_add_executor_job.await_count == 2


async def test_a_waiting_entity_does_not_hold_up_the_fix_of_another(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """A statistic whose entity has its state is fixed on the first pass, while another still waits."""
    hass.config.components.add("recorder")
    mock_config_entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ready, waiting = (
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"comexio_iosrv1_qb{n}",
            config_entry=mock_config_entry,
            suggested_object_id=f"{SERVER_ID}_qb{n}",
        )
        for n in (346, 362)
    )
    hass.states.async_set(ready.entity_id, "0", {"unit_of_measurement": "lx"})
    stats = [{"statistic_id": e.entity_id, "statistics_unit_of_measurement": "lux"} for e in (ready, waiting)]
    recorder = MagicMock(async_add_executor_job=AsyncMock(return_value=stats))
    with (
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.async_update_statistics_metadata") as update,
    ):
        task = hass.async_create_task(_async_fix_statistics_units(hass, SERVER_ID, mock_config_entry.entry_id))
        async with asyncio.timeout(5):
            while not update.called:
                await asyncio.sleep(0)
        assert recorder.async_add_executor_job.await_count == 1
        await task

    # The recorder mock keeps answering "lux", so each pass relabels again; only the ready one ever.
    assert {c.args[1] for c in update.call_args_list} == {ready.entity_id}
    assert recorder.async_add_executor_job.await_count == STATISTICS_UNIT_FIX_ATTEMPTS
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert f"(no state yet: {waiting.entity_id})" in warnings[0]


async def test_a_statistic_that_needs_no_fix_does_not_wait_for_a_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """An entity without a state whose statistic already has a proper unit gives no retry and no warning."""
    hass.config.components.add("recorder")
    mock_config_entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "comexio_iosrv1_qb346",
        config_entry=mock_config_entry,
        suggested_object_id=f"{SERVER_ID}_qb346",
    )
    stats = [{"statistic_id": entity.entity_id, "statistics_unit_of_measurement": "V"}]
    recorder = MagicMock(async_add_executor_job=AsyncMock(return_value=stats))
    with (
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("homeassistant.components.recorder.statistics.async_update_statistics_metadata") as update,
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, mock_config_entry.entry_id)

    assert recorder.async_add_executor_job.await_count == 1
    update.assert_not_called()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_stuck_statistics_lookup_ends_the_retries_with_a_warning(
    hass: HomeAssistant, update_metadata: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A lookup hanging behind the recorder still holds a worker, so no second lookup queues behind it."""
    release = asyncio.Event()

    async def _hang(*_args: object) -> list[dict[str, str]]:
        await release.wait()
        return []

    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=_hang))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC", 0.01),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
    ):
        async with asyncio.timeout(5):
            await _async_fix_statistics_units(hass, SERVER_ID, "entry")
    await _release(hass, release)

    assert recorder.async_add_executor_job.await_count == 1
    update_metadata.assert_not_called()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        f"[{SERVER_ID}] Statistics unit fix incomplete after 1 attempt(s) "
        "(statistics lookup timed out after 0.01 s, recorder busy? No further attempt while it still runs); "
        "the next restart or reload tries again"
    ]


async def test_a_reload_waits_for_the_stuck_lookup_instead_of_queueing_another(
    hass: HomeAssistant, update_metadata: MagicMock
) -> None:
    """Setups and reloads share the lookup in flight; a new one is submitted only once it has finished."""
    release = asyncio.Event()

    async def _hang(*_args: object) -> list[dict[str, str]]:
        await release.wait()
        return []

    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=_hang))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC", 0.01),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0),
    ):
        async with asyncio.timeout(5):
            await _async_fix_statistics_units(hass, SERVER_ID, "entry")
            await _async_fix_statistics_units(hass, SERVER_ID, "entry")
        assert recorder.async_add_executor_job.call_count == 1

        await _release(hass, release)
        async with asyncio.timeout(5):
            await _async_fix_statistics_units(hass, SERVER_ID, "entry")

    assert recorder.async_add_executor_job.call_count == 2


async def test_a_database_timeout_is_an_ordinary_failed_attempt(
    hass: HomeAssistant, update_metadata: MagicMock
) -> None:
    """Only our own lookup limit stops the retries; a TimeoutError from the database is retried."""
    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=[TimeoutError("db"), []]))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_COMMIT_WAIT_SEC", 0),
    ):
        await _async_fix_statistics_units(hass, SERVER_ID, "entry")

    assert recorder.async_add_executor_job.await_count == 2


async def test_a_timeout_after_a_failed_attempt_reports_both_attempts(
    hass: HomeAssistant, update_metadata: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning names the attempt that timed out and the timeout as the reason, not the earlier failure."""

    release = asyncio.Event()

    async def _lookup(*_args: object) -> list[dict[str, str]]:
        if recorder.async_add_executor_job.await_count == 1:
            raise RuntimeError("not ready")
        await release.wait()
        return []

    recorder = MagicMock(async_add_executor_job=AsyncMock(side_effect=_lookup))
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_LOOKUP_TIMEOUT_SEC", 0.01),
        patch("custom_components.comexio.STATISTICS_UNIT_FIX_RETRY_DELAY_SEC", 0),
    ):
        async with asyncio.timeout(5):
            await _async_fix_statistics_units(hass, SERVER_ID, "entry")
    await _release(hass, release)

    assert recorder.async_add_executor_job.await_count == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "incomplete after 2 attempt(s) (statistics lookup timed out after 0.01 s" in warnings[0]
