"""The pin's date (certificate.pin_data) and the certificate expiry sensor, without a Home Assistant instance."""

import asyncio
from datetime import UTC, datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import CertificateInfo, ComexioConnectionError
import pytest

from custom_components.comexio import SERVER_LEVEL_UNIQUE_ID_SUFFIXES, _server_level_unique_ids, certificate
from custom_components.comexio.const import (
    CERTIFICATE_EXPIRY_UNIQUE_ID_SUFFIX,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_CERTIFICATE_PINNED_AT,
    CONF_HOST,
)
from custom_components.comexio.sensor import ComexioCertificateExpirySensor

PINNED = "a" * 64
OTHER = "b" * 64
HOST = "192.0.2.10"
VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
VALID_UNTIL = datetime(2027, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 24, 8, 30, tzinfo=UTC)


def _entry(data: dict[str, Any]) -> MagicMock:
    entry = MagicMock()
    entry.data = data
    return entry


# pin_data / unpin_data


def test_a_pin_carries_the_time_it_was_written() -> None:
    with patch.object(certificate.dt_util, "utcnow", return_value=NOW):
        data = certificate.pin_data(PINNED)

    assert data == {CONF_CERTIFICATE_FINGERPRINT: PINNED, CONF_CERTIFICATE_PINNED_AT: NOW.isoformat()}


def test_restamping_dates_a_pin_from_now_and_leaves_an_entry_without_a_pin_alone() -> None:
    pinned = {
        CONF_HOST: HOST,
        CONF_CERTIFICATE_FINGERPRINT: PINNED,
        CONF_CERTIFICATE_PINNED_AT: "2020-01-01T00:00:00+00:00",
    }
    plain = {CONF_HOST: HOST}

    with patch.object(certificate.dt_util, "utcnow", return_value=NOW):
        certificate.restamp_pin(pinned)
        certificate.restamp_pin(plain)

    assert pinned == {
        CONF_HOST: HOST,
        CONF_CERTIFICATE_FINGERPRINT: PINNED,
        CONF_CERTIFICATE_PINNED_AT: NOW.isoformat(),
    }
    assert plain == {CONF_HOST: HOST}


@pytest.mark.parametrize(
    ("data", "had_pin"),
    [
        ({CONF_HOST: HOST, CONF_CERTIFICATE_FINGERPRINT: PINNED, CONF_CERTIFICATE_PINNED_AT: NOW.isoformat()}, True),
        ({CONF_HOST: HOST, CONF_CERTIFICATE_FINGERPRINT: PINNED}, True),  # pinned before the date was recorded
        ({CONF_HOST: HOST}, False),
    ],
    ids=["pin with date", "pin without date", "no pin"],
)
def test_dropping_the_pin_drops_its_date(data: dict[str, Any], had_pin: bool) -> None:
    assert certificate.unpin_data(data) is had_pin
    assert data == {CONF_HOST: HOST}


# Orphan cleanup


def test_the_orphan_cleanup_keeps_the_expiry_sensor_only_on_a_pinned_entry() -> None:
    expiry = f"comexio_srv_{CERTIFICATE_EXPIRY_UNIQUE_ID_SUFFIX}"
    pinned = _server_level_unique_ids(_entry({CONF_CERTIFICATE_FINGERPRINT: PINNED}), "srv")
    plain = _server_level_unique_ids(_entry({}), "srv")

    assert expiry in pinned
    assert expiry not in plain
    assert plain == {f"comexio_srv_{suffix}" for suffix in SERVER_LEVEL_UNIQUE_ID_SUFFIXES}
    assert pinned - plain == {expiry}


def test_the_expiry_sensor_has_the_unique_id_the_orphan_cleanup_keeps() -> None:
    sensor = _sensor()

    assert sensor.unique_id in _server_level_unique_ids(sensor.coordinator.config_entry, "srv")


# The sensor's reads


def _sensor() -> ComexioCertificateExpirySensor:
    coordinator = MagicMock()
    coordinator.config_entry = _entry({CONF_HOST: HOST, CONF_CERTIFICATE_FINGERPRINT: PINNED})
    sensor = ComexioCertificateExpirySensor(coordinator, "srv")
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    return sensor


def _read(sensor: ComexioCertificateExpirySensor, outcome: CertificateInfo | Exception) -> AsyncMock:
    fetch = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    with patch("custom_components.comexio.sensor.fetch_certificate", fetch):
        asyncio.run(sensor._async_read())
    return fetch


def test_a_read_of_the_pinned_certificate_shows_its_dates() -> None:
    sensor = _sensor()

    fetch = _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    fetch.assert_awaited_once_with(HOST)
    assert sensor.native_value == VALID_UNTIL
    assert sensor.extra_state_attributes == {"valid_from": VALID_FROM}
    sensor.async_write_ha_state.assert_called_once()


def test_another_certificate_leaves_the_dates_of_the_pinned_one() -> None:
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    _read(sensor, CertificateInfo(OTHER, datetime(2030, 1, 1, tzinfo=UTC), datetime(2031, 1, 1, tzinfo=UTC)))

    assert sensor.native_value == VALID_UNTIL
    assert sensor.extra_state_attributes == {"valid_from": VALID_FROM}
    sensor.async_write_ha_state.assert_called_once()  # the first read only


def test_a_pinned_certificate_without_readable_dates_is_unknown() -> None:
    # Unknown, never "does not expire": the dates of an earlier read do not stay for a certificate now unreadable.
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    _read(sensor, CertificateInfo(PINNED, None, None))

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"valid_from": None}
    assert sensor.extra_restore_state_data is None


def test_a_read_with_the_end_before_the_start_is_unknown_not_expired() -> None:
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    _read(sensor, CertificateInfo(PINNED, VALID_UNTIL, VALID_FROM))

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"valid_from": None}
    assert sensor.extra_restore_state_data is None


def test_stored_dates_with_start_equal_to_end_are_restored() -> None:
    sensor = _sensor()

    sensor._restore(
        {
            "fingerprint": PINNED,
            "not_valid_before": VALID_FROM.isoformat(),
            "not_valid_after": VALID_FROM.isoformat(),
        }
    )

    assert sensor.native_value == VALID_FROM


def test_a_failed_read_keeps_the_last_dates_and_logs_once_per_outage(caplog: pytest.LogCaptureFixture) -> None:
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.sensor"):
        _read(sensor, ComexioConnectionError("handshake timed out"))
        _read(sensor, ComexioConnectionError("handshake timed out"))
        _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))
        _read(sensor, ValueError("no host"))

    assert sensor.native_value == VALID_UNTIL
    levels = [r.levelno for r in caplog.records if "certificate validity not read" in r.getMessage()]
    assert levels == [logging.INFO, logging.DEBUG, logging.INFO]  # a working read ends the outage


def test_an_unexpected_error_is_logged_with_its_traceback_and_keeps_the_dates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.sensor"):
        _read(sensor, RuntimeError("bug"))

    assert sensor.native_value == VALID_UNTIL
    (record,) = (r for r in caplog.records if "read failed unexpectedly" in r.getMessage())
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


@pytest.mark.parametrize(
    ("done", "starts"), [(None, True), (False, False), (True, True)], ids=["first read", "running", "finished"]
)
def test_a_read_starts_only_while_no_other_runs(done: bool | None, starts: bool) -> None:
    sensor = _sensor()
    sensor._async_read = MagicMock()  # type: ignore[method-assign]  # no coroutine left unawaited
    if done is not None:
        sensor._read_task = MagicMock()
        sensor._read_task.done.return_value = done

    sensor._start_read()

    assert sensor.coordinator.config_entry.async_create_background_task.called is starts
    assert sensor._async_read.called is starts  # the read is only set up when it is going to start


def test_removing_the_sensor_cancels_a_running_read() -> None:
    sensor = _sensor()
    running = MagicMock()
    sensor._read_task = running

    asyncio.run(sensor.async_will_remove_from_hass())

    running.cancel.assert_called_once()


def test_removing_the_sensor_before_any_read_is_fine() -> None:
    asyncio.run(_sensor().async_will_remove_from_hass())


# Restore


def test_the_stored_dates_round_trip() -> None:
    sensor = _sensor()
    _read(sensor, CertificateInfo(PINNED, VALID_FROM, VALID_UNTIL))
    restored = _sensor()

    restored._restore(sensor.extra_restore_state_data.as_dict())

    assert restored.native_value == VALID_UNTIL
    assert restored.extra_state_attributes == {"valid_from": VALID_FROM}


def test_restored_dates_in_another_zone_come_back_in_utc() -> None:
    sensor = _sensor()

    sensor._restore(
        {
            "fingerprint": PINNED,
            "not_valid_before": "2026-01-01T05:00:00+05:00",
            "not_valid_after": "2026-12-31T19:00:00-05:00",
        }
    )

    assert sensor.native_value == VALID_UNTIL
    assert sensor.native_value.utcoffset() == VALID_UNTIL.utcoffset()
    assert sensor.extra_state_attributes == {"valid_from": VALID_FROM}


def test_restored_dates_stay_when_the_handshake_presents_another_certificate() -> None:
    sensor = _sensor()
    sensor._restore(
        {"fingerprint": PINNED, "not_valid_before": VALID_FROM.isoformat(), "not_valid_after": VALID_UNTIL.isoformat()}
    )

    _read(sensor, CertificateInfo(OTHER, datetime(2030, 1, 1, tzinfo=UTC), datetime(2031, 1, 1, tzinfo=UTC)))

    assert sensor.native_value == VALID_UNTIL
    assert sensor.extra_state_attributes == {"valid_from": VALID_FROM}
    sensor.async_write_ha_state.assert_not_called()


@pytest.mark.parametrize(
    "stored",
    [
        {"fingerprint": OTHER, "not_valid_before": VALID_FROM.isoformat(), "not_valid_after": VALID_UNTIL.isoformat()},
        {"fingerprint": PINNED, "not_valid_before": VALID_FROM.isoformat()},
        {"fingerprint": PINNED, "not_valid_before": "garbage", "not_valid_after": VALID_UNTIL.isoformat()},
        {"fingerprint": PINNED, "not_valid_before": VALID_FROM.isoformat(), "not_valid_after": "2027-01-01T00:00:00"},
        {"fingerprint": PINNED, "not_valid_before": "2026-01-01T00:00:00", "not_valid_after": VALID_UNTIL.isoformat()},
        {
            "fingerprint": PINNED,
            "not_valid_before": VALID_FROM.isoformat(),
            "not_valid_after": "2026-13-45T10:00:00+00:00",
        },
        {
            "fingerprint": PINNED,
            "not_valid_before": "2026-01-01T00:00:00+99:00",
            "not_valid_after": VALID_UNTIL.isoformat(),
        },
        {
            "fingerprint": PINNED,
            "not_valid_before": "0001-01-01T00:00:00+05:00",
            "not_valid_after": VALID_UNTIL.isoformat(),
        },
        {
            "fingerprint": PINNED,
            "not_valid_before": VALID_FROM.isoformat(),
            "not_valid_after": "9999-12-31T23:59:59-05:00",
        },
        {
            "fingerprint": PINNED,
            "not_valid_before": VALID_UNTIL.isoformat(),
            "not_valid_after": VALID_FROM.isoformat(),
        },
        {},
    ],
    ids=[
        "another pin",
        "one date missing",
        "unparsable date",
        "end date without zone",
        "start date without zone",
        "no such day",
        "no such zone",
        "start before the calendar in UTC",
        "end after the calendar in UTC",
        "end before the start",
        "nothing",
    ],
)
def test_stored_dates_that_do_not_belong_to_the_pin_are_not_restored(stored: dict[str, Any]) -> None:
    sensor = _sensor()

    sensor._restore(stored)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"valid_from": None}
