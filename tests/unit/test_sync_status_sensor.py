"""Sync status sensor (sensor.py): a partial sync reads "partial", and the outcome survives the reload."""

from types import SimpleNamespace

from homeassistant.core import State
import pytest

from custom_components.comexio.button import _sync_login_error, _sync_notification_title
from custom_components.comexio.const import (
    SYNC_STATE_ERROR,
    SYNC_STATE_IDLE,
    SYNC_STATE_PARTIAL,
    SYNC_STATE_SYNCING,
)
from custom_components.comexio.sensor import ComexioSyncStatusSensor

ENTITY_ID = "sensor.iosrv1_webio_sync_state"


def _coordinator(**overrides) -> SimpleNamespace:
    fields = {
        "in_sync": False,
        "sync_error": False,
        "sync_failed_writes": [],
        "sync_progress_text": "Idle",
        "sync_progress_pct": None,
        "sync_current_step": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _sensor(coordinator: SimpleNamespace) -> ComexioSyncStatusSensor:
    return ComexioSyncStatusSensor(coordinator, "iosrv1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, SYNC_STATE_IDLE),
        ({"in_sync": True, "sync_failed_writes": ["marker M1 Light"]}, SYNC_STATE_SYNCING),
        ({"sync_error": True, "sync_failed_writes": ["marker M1 Light"]}, SYNC_STATE_ERROR),
        ({"sync_failed_writes": ["marker M1 Light"]}, SYNC_STATE_PARTIAL),
    ],
)
def test_native_value(overrides: dict, expected: str) -> None:
    sensor = _sensor(_coordinator(**overrides))

    assert sensor.native_value == expected
    assert expected in sensor.options


def test_failed_writes_attribute_only_for_a_partial_sync() -> None:
    assert "failed_writes" not in _sensor(_coordinator()).extra_state_attributes

    attrs = _sensor(_coordinator(sync_failed_writes=["marker M1 Light"])).extra_state_attributes

    assert attrs["failed_writes"] == ["marker M1 Light"]


def test_partial_outcome_is_restored_after_the_reload() -> None:
    # Regression: every sync ends with a reload, whose new coordinator read "idle" again.
    coordinator = _coordinator()
    last = State(
        ENTITY_ID,
        SYNC_STATE_PARTIAL,
        {"progress_details": "Sync Finished with errors", "failed_writes": ["marker M1 Light", "io BASE DI1"]},
    )

    _sensor(coordinator)._restore_last_outcome(last)

    assert coordinator.sync_failed_writes == ["marker M1 Light", "io BASE DI1"]
    assert coordinator.sync_progress_text == "Sync Finished with errors"
    assert _sensor(coordinator).native_value == SYNC_STATE_PARTIAL


def test_error_outcome_is_restored_after_the_reload() -> None:
    coordinator = _coordinator()

    _sensor(coordinator)._restore_last_outcome(State(ENTITY_ID, SYNC_STATE_ERROR, {"progress_details": "Error: x"}))

    assert coordinator.sync_error is True
    assert coordinator.sync_progress_text == "Error: x"
    assert coordinator.sync_failed_writes == []


def test_error_outcome_keeps_the_writes_that_failed_before_the_abort() -> None:
    coordinator = _coordinator()
    last = State(ENTITY_ID, SYNC_STATE_ERROR, {"progress_details": "Error: x", "failed_writes": ["marker M1 Light"]})

    _sensor(coordinator)._restore_last_outcome(last)

    assert coordinator.sync_error is True
    assert coordinator.sync_failed_writes == ["marker M1 Light"]
    assert _sensor(coordinator).extra_state_attributes["failed_writes"] == ["marker M1 Light"]


@pytest.mark.parametrize("state", [SYNC_STATE_IDLE, SYNC_STATE_SYNCING, "unavailable", "unknown"])
def test_other_states_are_not_restored(state: str) -> None:
    # A restart during a sync must not come back as "syncing" — no sync is running any more.
    coordinator = _coordinator()

    _sensor(coordinator)._restore_last_outcome(State(ENTITY_ID, state, {"progress_details": "Working..."}))

    assert _sensor(coordinator).native_value == SYNC_STATE_IDLE
    assert coordinator.sync_progress_text == "Idle"


def test_own_outcome_is_not_overwritten_by_the_restored_one() -> None:
    coordinator = _coordinator(sync_error=True)

    _sensor(coordinator)._restore_last_outcome(State(ENTITY_ID, SYNC_STATE_PARTIAL, {"failed_writes": ["x"]}))

    assert coordinator.sync_failed_writes == []
    assert _sensor(coordinator).native_value == SYNC_STATE_ERROR


@pytest.mark.parametrize("failed", [None, [], 5])
def test_damaged_partial_restore_stays_partial(failed: object) -> None:
    coordinator = _coordinator()

    _sensor(coordinator)._restore_last_outcome(State(ENTITY_ID, SYNC_STATE_PARTIAL, {"failed_writes": failed}))

    assert _sensor(coordinator).native_value == SYNC_STATE_PARTIAL
    assert coordinator.sync_failed_writes == ["(names not restored)"]


def test_single_string_failed_writes_is_kept_as_one_name() -> None:
    coordinator = _coordinator()

    _sensor(coordinator)._restore_last_outcome(
        State(ENTITY_ID, SYNC_STATE_PARTIAL, {"failed_writes": "marker M1 Light"})
    )

    assert coordinator.sync_failed_writes == ["marker M1 Light"]


def test_notification_title() -> None:
    assert _sync_notification_title("iosrv1", is_error=True, partial=True) == "Comexio Sync Failed"
    assert _sync_notification_title("iosrv1", is_error=False, partial=True) == (
        "Comexio Sync Finished with errors (iosrv1)"
    )
    assert _sync_notification_title("iosrv1", is_error=False, partial=False) == "Comexio Sync (iosrv1)"


def test_sync_login_error_names_the_cause() -> None:
    # (o): a lapsed session is logged in again at the sync start; only a failed login aborts.
    assert "check the credentials" in str(_sync_login_error("rejected"))
    assert "not reachable" in str(_sync_login_error("connection"))
    assert "not reachable" in str(_sync_login_error(None))
