"""A /api/ write that failed without a clear answer names the API settings of Comexio that explain it."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
import itertools
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ACCESS_FORBIDDEN,
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_SELECTION,
    ApiSettings,
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from aiocomexio.client import parse_api_settings
import aiohttp
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.comexio import api as api_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.api_write import (
    api_settings_diagnosis,
    failure_class,
    settings_check_failed,
    worth_diagnosing,
)
from custom_components.comexio.const import API_WRITE_RETRY_MAX, CERTIFICATE_CHANGED_MSG
from custom_components.comexio.entity import write_failed
from custom_components.comexio.services import misc
from tests.common import load_fixture

SETTINGS_ERROR = "Comexio API settings"
NOT_EXPLAINED = "Comexio API settings do not explain it"
WRITE_FAILED_LOG = "Comexio API write failed"
# The user and password of tests/fixtures/comexio/api_settings_page.html (synthetic).
PAGE_USER = "api-user"
PAGE_PASSWORD = "s3cret-api-pw"  # noqa: S105  # nosec B105 - the synthetic fixture's password


def _timeout() -> ComexioConnectionError:
    err = ComexioConnectionError("Writing marker M251 failed: TimeoutError()")
    err.__cause__ = TimeoutError()
    return err


def _dropped() -> ComexioConnectionError:
    err = ComexioConnectionError("Writing marker M251 failed: ServerDisconnectedError('Server disconnected')")
    err.__cause__ = aiohttp.ServerDisconnectedError("Server disconnected")
    return err


def _settings(**changes: Any) -> ApiSettings:
    """API settings that allow every write; a test changes what it is about."""
    defaults: dict[str, Any] = {
        "active": True,
        "tcp_enabled": True,
        "tcp_port": 1574,
        "login_required": True,
        "username": PAGE_USER,
        "username_matches": True,
        "password_matches": True,
        "in_output": ACCESS_READ_WRITE,
        "flag": ACCESS_READ_WRITE,
        "one_wire": ACCESS_READ,
        "marker_rights": {251: 3},
    }
    return ApiSettings(**(defaults | changes))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _write(api: ComexioAPI, *args: Any, **kwargs: Any) -> tuple[bool, str | None]:
    """set_value and, read in the same task right after it, what the API settings said (last_write_diagnosis)."""

    async def write() -> tuple[bool, str | None]:
        written = await asyncio.wait_for(api.set_value(*args, **kwargs), timeout=10)
        return written, api.last_write_diagnosis

    return _run(write())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.set_marker_value = AsyncMock()
    fake.set_knx_value = AsyncMock()
    fake.set_io_value = AsyncMock()
    fake.get_api_settings = AsyncMock(return_value=_settings(active=False))
    monkeypatch.setattr(ComexioAPI, "client", property(lambda _self: fake))
    return fake


@pytest.fixture
def api(comexio_api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> ComexioAPI:
    monkeypatch.setattr(comexio_api, "fetch_replaced_fingerprint", AsyncMock(return_value=None))
    return comexio_api


# ---- worth_diagnosing --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        pytest.param(_timeout(), True, id="timeout"),
        pytest.param(_dropped(), True, id="dropped-connection"),
        pytest.param(ComexioResponseError("HTTP 500", status=500), True, id="http-500"),
        pytest.param(ComexioResponseError("HTTP 404", status=404), True, id="http-404"),
        pytest.param(ComexioDataError("unexpected answer"), True, id="unexpected-answer"),
        pytest.param(ComexioResponseError("Forbidden", status=403), False, id="http-403"),
        pytest.param(ComexioRequestRejectedError("Forbidden"), False, id="forbidden"),
        pytest.param(ComexioAuthenticationError("login"), False, id="login-rejected"),
        pytest.param(ComexioError("the entry was unloaded: not sent"), False, id="plain-error"),
        pytest.param(ValueError("nan"), False, id="bad-value"),
    ],
)
def test_only_an_answer_that_is_not_clear_is_worth_a_look_at_the_settings(err: BaseException, expected: bool) -> None:
    assert worth_diagnosing(err) is expected


def test_a_certificate_mismatch_is_not_worth_a_look_at_the_settings() -> None:
    err = ComexioCertificateMismatchError("foreign certificate", expected="aa", actual="bb")

    assert worth_diagnosing(err) is False


# ---- api_settings_diagnosis --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "target", "wanted"),
    [
        pytest.param(_settings(active=False), ("marker", 251), "the API is switched off", id="inactive"),
        pytest.param(_settings(flag=ACCESS_READ), ("marker", 251), "access to markers is read only", id="marker-read"),
        pytest.param(
            _settings(flag=ACCESS_FORBIDDEN), ("marker", "251"), "access to markers is forbidden", id="marker-forbidden"
        ),
        pytest.param(
            _settings(flag=ACCESS_SELECTION, marker_rights={251: 1}),
            ("marker", 251),
            "M251 is not released for writing",
            id="selection-read-only",
        ),
        pytest.param(
            _settings(flag=ACCESS_SELECTION, marker_rights={1: 3}),
            ("marker", 251),
            "M251 is not released for writing",
            id="selection-not-listed",
        ),
        pytest.param(
            _settings(flag=ACCESS_SELECTION, marker_rights=None),
            ("marker", 251),
            "its list could not be read (M251 is unknown)",
            id="selection-list-unreadable",
        ),
        pytest.param(_settings(in_output=ACCESS_READ), ("io", "Q1"), "access to IOs is read only", id="io-read"),
        pytest.param(_settings(login_required=True, username_matches=False), ("marker", 1), "user differs", id="user"),
        pytest.param(
            _settings(login_required=True, password_matches=False), ("knx", 3), "password differs", id="password"
        ),
    ],
)
def test_the_diagnosis_names_the_setting_that_stops_the_write(
    settings: ApiSettings, target: tuple[str, str | int], wanted: str
) -> None:
    text = api_settings_diagnosis(settings, *target)

    assert text.startswith(SETTINGS_ERROR)
    assert wanted in text
    assert NOT_EXPLAINED not in text


def test_the_diagnosis_says_so_when_no_setting_stops_the_write() -> None:
    text = api_settings_diagnosis(_settings(), "marker", 251)

    assert text.startswith(NOT_EXPLAINED)
    assert "API active" in text
    assert "port 1574" in text


def test_a_single_selection_with_the_marker_released_stops_nothing() -> None:
    settings = _settings(flag=ACCESS_SELECTION, marker_rights={251: 2})

    assert api_settings_diagnosis(settings, "marker", 251).startswith(NOT_EXPLAINED)


def test_a_marker_id_that_is_no_number_is_named_as_unknown_in_a_single_selection() -> None:
    settings = _settings(flag=ACCESS_SELECTION, marker_rights={251: 3})

    text = api_settings_diagnosis(settings, "marker", "abc")

    assert "the marker is not released for writing" in text


@pytest.mark.parametrize(
    ("err", "reason"),
    [
        pytest.param(ComexioAuthenticationError("admin session lapsed"), "admin session not logged in", id="login"),
        pytest.param(TimeoutError(), "timed out", id="timeout"),
        pytest.param(ComexioDataError("changed page"), "ComexioDataError", id="comexio-error"),
        pytest.param(_dropped(), "ComexioConnectionError (ServerDisconnectedError)", id="comexio-error-with-cause"),
        pytest.param(RuntimeError("secret text"), "RuntimeError", id="foreign-error"),
    ],
)
def test_a_page_that_cannot_be_read_is_named_by_its_reason_not_by_the_error_text(
    err: BaseException, reason: str
) -> None:
    assert settings_check_failed(err) == f"Comexio API settings could not be read ({reason})"


# ---- the error path of set_value -----------------------------------------------------------------------------------


def test_a_timeout_reads_the_settings_once_and_names_what_they_say(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _timeout()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert written is False
    client.get_api_settings.assert_awaited_once_with()
    [record] = [r for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]
    assert "the API is switched off (access: inactive)" in record.getMessage()
    assert diagnosis is not None
    assert "the API is switched off" in diagnosis


def test_the_counter_keeps_the_error_of_the_write_not_the_diagnosis(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _timeout()

    _run(api.set_value("marker", 251, 1))

    assert api.write_stats.failed == 1
    [failure] = api.write_stats.last_failures
    assert failure.target == "marker 251"
    assert failure.error == failure_class(_timeout())
    assert set(api.write_stats.attributes()) == {"retries", "recovered_by_retry", "last_failures"}


def test_a_write_that_went_through_reads_no_settings(api: ComexioAPI, client: MagicMock) -> None:
    assert _write(api, "marker", 251, 1) == (True, None)

    client.get_api_settings.assert_not_awaited()


@pytest.mark.parametrize(
    "err",
    [
        pytest.param(ComexioRequestRejectedError("marker write was not accepted: Forbidden"), id="forbidden"),
        pytest.param(ComexioResponseError("Forbidden", status=403), id="http-403"),
        pytest.param(ComexioAuthenticationError("login"), id="login"),
    ],
)
def test_a_clear_refusal_stays_an_immediate_error_without_a_look_at_the_settings(
    api: ComexioAPI, client: MagicMock, err: BaseException
) -> None:
    client.set_marker_value.side_effect = err

    assert _write(api, "marker", 251, 1) == (False, None)

    client.get_api_settings.assert_not_awaited()


@pytest.mark.parametrize(
    ("target_type", "target_id", "kwargs", "method"),
    [
        pytest.param("marker", 251, {}, "set_marker_value", id="marker"),
        pytest.param("knx", 9, {}, "set_knx_value", id="knx"),
        pytest.param("io", 103, {"ext": "LED-Dimmer A3", "identifier": "Q5"}, "set_io_value", id="io-over-api"),
    ],
)
def test_every_write_over_the_api_is_diagnosed(
    api: ComexioAPI, client: MagicMock, target_type: str, target_id: int, kwargs: dict[str, str], method: str
) -> None:
    getattr(client, method).side_effect = _timeout()

    written, diagnosis = _write(api, target_type, target_id, 1, **kwargs)

    assert written is False
    client.get_api_settings.assert_awaited_once_with()
    assert diagnosis is not None
    assert "the API is switched off" in diagnosis  # the fixture's settings have the API switched off


def test_a_write_over_the_admin_session_reads_no_api_settings(api: ComexioAPI, client: MagicMock) -> None:
    client.set_io_value.side_effect = _timeout()

    assert _write(api, "io", 12, 5, ext="IO-Server", identifier="AO1") == (False, None)

    client.get_api_settings.assert_not_awaited()


def test_a_drop_that_exhausted_the_repetitions_reads_the_settings_once(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 251, 1)) is False

    assert client.set_marker_value.await_count == API_WRITE_RETRY_MAX + 1
    client.get_api_settings.assert_awaited_once_with()
    assert api.write_stats.retries == API_WRITE_RETRY_MAX


def test_a_write_that_is_sent_once_reads_the_settings_once(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 251, 1, repeat=False)) is False

    assert client.set_marker_value.await_count == 1
    client.get_api_settings.assert_awaited_once_with()
    assert api.write_stats.retries == 0


def test_a_drop_that_a_repetition_saved_reads_no_settings(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("marker", 251, 1)) is True

    client.get_api_settings.assert_not_awaited()
    assert api.write_stats.recovered == 1


def test_a_page_that_cannot_be_read_keeps_the_write_error_and_names_the_reason(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = ComexioAuthenticationError("admin session lapsed")

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 251, 1)) is False

    client.get_api_settings.assert_awaited_once_with()
    [record] = [r for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]
    assert "TimeoutError" in record.getMessage()
    assert "Comexio API settings could not be read (admin session not logged in)" in record.getMessage()
    [failure] = api.write_stats.last_failures
    assert failure.error == failure_class(_timeout())


def test_a_page_that_does_not_answer_is_given_up_after_its_own_timeout(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never() -> ApiSettings:
        await asyncio.sleep(3600)
        raise AssertionError  # pragma: no cover

    monkeypatch.setattr(api_module, "API_SETTINGS_CHECK_TIMEOUT_SEC", 0.01)
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = never

    written, diagnosis = _write(api, "marker", 251, 1)

    assert written is False
    assert diagnosis == "Comexio API settings could not be read (timed out)"


def test_an_entry_unloaded_meanwhile_reads_no_settings(api: ComexioAPI, client: MagicMock) -> None:
    async def unload_then_fail(*_args: Any) -> None:
        api._closed = True
        raise _timeout()

    client.set_marker_value.side_effect = unload_then_fail

    assert _run(api.set_value("marker", 251, 1)) is False

    client.get_api_settings.assert_not_awaited()


def test_home_assistant_stopping_meanwhile_reads_no_settings(api: ComexioAPI, client: MagicMock) -> None:
    # Its CLOSE event closes the connector of the session while the entry is still loaded (_closed stays False).
    async def close_then_fail(*_args: Any) -> None:
        api.session.closed = True  # type: ignore[misc]
        raise _timeout()

    client.set_marker_value.side_effect = close_then_fail

    assert _run(api.set_value("marker", 251, 1)) is False

    client.get_api_settings.assert_not_awaited()
    assert api.write_stats.failed == 1


def test_a_read_refused_by_a_session_home_assistant_closed_is_no_news(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    async def close_then_refuse() -> ApiSettings:
        api.session.closed = True  # type: ignore[misc]
        raise RuntimeError("Session is closed")

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = close_then_refuse

    with caplog.at_level(logging.WARNING, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert (written, diagnosis) == (False, None)  # no note, not "could not be read"
    assert api.write_stats.failed == 1
    assert "failed unexpectedly" not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ComexioConnectionError("Server disconnected"), id="transport-error"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
def test_a_read_cut_off_by_the_stop_is_no_news_about_the_settings(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture, failure: BaseException
) -> None:
    # Home Assistant closes the connector while the settings request is in flight: it ends as a transport error.
    async def close_then_fail() -> ApiSettings:
        api.session.closed = True  # type: ignore[misc]
        raise failure

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = close_then_fail

    with caplog.at_level(logging.DEBUG, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert (written, diagnosis) == (False, None)  # no "could not be read" note in the line
    assert api.write_stats.failed == 1
    assert "failed unexpectedly" not in caplog.text
    assert f"ended with the session ({type(failure).__name__})" in caplog.text  # a trace for the debug log


@pytest.mark.parametrize(
    ("gone", "failure", "expected_note", "warned"),
    [
        pytest.param(
            None,
            _dropped(),
            "Comexio API settings could not be read (ComexioConnectionError (ServerDisconnectedError))",
            False,
            id="session-open-transport-error",
        ),
        pytest.param(
            "closed", ValueError("x"), "Comexio API settings could not be read (ValueError)", True, id="closed-foreign"
        ),
        pytest.param("unloaded", _dropped(), None, False, id="unloaded-transport-error"),
        pytest.param("unloaded", TimeoutError(), None, False, id="unloaded-timeout"),
    ],
)
def test_only_a_read_that_ended_with_the_session_is_no_news(
    api: ComexioAPI,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    gone: str | None,
    failure: BaseException,
    expected_note: str | None,
    warned: bool,
) -> None:
    # A transport error on an open session is the read's own failure and is named; a bug stays visible whatever the
    # session does; the unloaded entry (_closed) ends the read as quietly as Home Assistant stopping does.
    async def end_the_read() -> ApiSettings:
        if gone == "closed":
            api.session.closed = True  # type: ignore[misc]
        elif gone == "unloaded":
            api._closed = True
        raise failure

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = end_the_read

    with caplog.at_level(logging.WARNING, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert (written, diagnosis) == (False, expected_note)
    assert api.write_stats.failed == 1
    assert ("failed unexpectedly" in caplog.text) is warned


def test_a_runtime_error_of_the_read_is_no_unload_while_the_entry_is_open(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Only aiohttp's "Session is closed" on an unloaded entry is no news: the same text on a loaded one, or another
    # RuntimeError on an unloaded one, is a bug and says so.
    async def unload_then_fail() -> ApiSettings:
        api._closed = True
        raise RuntimeError("parser bug")

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = unload_then_fail

    with caplog.at_level(logging.WARNING, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert (written, diagnosis) == (False, "Comexio API settings could not be read (RuntimeError)")
    assert api.write_stats.failed == 1
    assert "failed unexpectedly (RuntimeError)" in caplog.text


def test_an_unexpected_error_of_the_read_is_visible_in_the_log_by_class_only(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = ValueError(f"page said {PAGE_PASSWORD}")

    with caplog.at_level(logging.WARNING, logger=api_module.__name__):
        _write(api, "marker", 251, 1)

    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "Reading the Comexio API settings failed unexpectedly (ValueError)"
    assert PAGE_PASSWORD not in caplog.text


def test_the_debug_line_of_an_unexpected_error_names_the_places_but_never_the_text(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _timeout()

    async def parser_bug() -> ApiSettings:
        # The error and its cause both quote the page, as a parser bug might.
        raise ValueError(f"page said {PAGE_PASSWORD}") from ValueError(f"cause said {PAGE_PASSWORD}")

    client.get_api_settings.side_effect = parser_bug

    with caplog.at_level(logging.DEBUG, logger=api_module.__name__):
        _write(api, "marker", 251, 1)

    [debug] = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and "raised at" in r.getMessage()]
    assert "(ValueError)" in debug
    assert "_api_settings_text" in debug  # the frames are there: the place is named, the text is not
    assert PAGE_PASSWORD not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_failed_writes_in_a_burst_share_one_read_of_the_settings(api: ComexioAPI, client: MagicMock) -> None:
    async def scenario() -> list[tuple[bool, str | None]]:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_read() -> ApiSettings:
            reading.set()
            await release.wait()
            return _settings(active=False)

        async def write(marker_id: int) -> tuple[bool, str | None]:
            written = await api.set_value("marker", marker_id, 1)
            return written, api.last_write_diagnosis

        client.set_marker_value.side_effect = _timeout()
        client.get_api_settings.side_effect = slow_read
        tasks = [asyncio.create_task(write(251))]
        await reading.wait()
        tasks += [asyncio.create_task(write(252)), asyncio.create_task(write(253))]
        for _ in range(20):  # the other two fail while the read is under way
            await asyncio.sleep(0)
        release.set()
        return list(await asyncio.gather(*tasks))

    results = _run(scenario())

    client.get_api_settings.assert_awaited_once_with()
    assert [written for written, _ in results] == [False, False, False]
    assert all(text and "the API is switched off" in text for _, text in results)
    assert api.write_stats.failed == 3


def test_a_read_that_is_over_is_not_reused_by_the_next_failed_write(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _timeout()

    assert _write(api, "marker", 251, 1)[0] is False
    assert _write(api, "marker", 251, 1)[0] is False

    assert client.get_api_settings.await_count == 2


def test_the_failed_write_is_logged_before_it_is_counted(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The counter notifies a sensor: when that breaks, the line of the failed write is already in the log.
    client.set_marker_value.side_effect = _timeout()
    monkeypatch.setattr(api.write_stats, "record_failure", MagicMock(side_effect=RuntimeError("sensor")))

    def write() -> None:
        _write(api, "marker", 251, 1)

    with caplog.at_level(logging.ERROR, logger=api_module.__name__), pytest.raises(RuntimeError, match="sensor"):
        write()

    assert [r for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]


def test_the_error_line_of_an_exhausted_drop_names_the_repetitions_before_the_settings(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert written is False
    [line] = [r.getMessage() for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]
    assert line == (
        f"Comexio API write failed (marker 251) after {API_WRITE_RETRY_MAX} repetition(s): {_dropped()}. {diagnosis}"
    )


def test_an_entry_unloaded_during_the_read_adds_no_note_but_the_failure_is_counted(
    api: ComexioAPI, client: MagicMock
) -> None:
    async def unload_during_the_read() -> ApiSettings:
        api._closed = True
        raise RuntimeError("Session is closed")

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = unload_during_the_read

    assert _write(api, "marker", 251, 1) == (False, None)

    assert api.write_stats.failed == 1


@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(RuntimeError("Session is closed"), id="runtime-error-while-open"),
        pytest.param(ValueError("parser bug"), id="parser-bug"),
        pytest.param(aiohttp.ClientError("not wrapped"), id="unwrapped-aiohttp"),
    ],
)
def test_an_unexpected_error_of_the_read_never_costs_the_failed_write_its_line_or_count(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture, boom: Exception
) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = boom

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1)

    assert written is False
    assert diagnosis == f"Comexio API settings could not be read ({type(boom).__name__})"
    [record] = [r for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]
    assert diagnosis in record.getMessage()
    assert api.write_stats.failed == 1


def test_a_cancelled_read_still_logs_and_counts_the_failed_write_and_stays_cancelled(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = asyncio.CancelledError

    def write() -> None:
        _run(api.set_value("marker", 251, 1))

    with caplog.at_level(logging.ERROR, logger=api_module.__name__), pytest.raises(asyncio.CancelledError):
        write()

    assert [r for r in caplog.records if WRITE_FAILED_LOG in r.getMessage()]
    assert api.write_stats.failed == 1


def test_connection_settings_changed_during_the_write_read_no_settings_of_the_new_server(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reconfigure_then_fail(*_args: Any) -> None:
        monkeypatch.setattr(api, "host", "other-comexio.example")
        raise _timeout()

    client.set_marker_value.side_effect = reconfigure_then_fail

    assert _write(api, "marker", 251, 1) == (False, None)

    client.get_api_settings.assert_not_awaited()
    assert api.write_stats.failed == 1


def test_one_certificate_mismatch_of_a_shared_read_is_kept_once_but_named_by_every_waiting_write(
    api: ComexioAPI, client: MagicMock
) -> None:
    kept: list[str] = []
    api.on_certificate_mismatch = kept.append

    async def scenario() -> list[bool]:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_mismatch() -> ApiSettings:
            reading.set()
            await release.wait()
            raise ComexioCertificateMismatchError("foreign", expected="aa", actual="bb")

        async def write(marker_id: int) -> bool:
            await api.set_value("marker", marker_id, 1)
            return api.last_write_met_another_certificate

        client.set_marker_value.side_effect = _timeout()
        client.get_api_settings.side_effect = slow_mismatch
        tasks = [asyncio.create_task(write(251))]
        await reading.wait()
        tasks += [asyncio.create_task(write(252)), asyncio.create_task(write(253))]
        for _ in range(20):  # the other two fail while the read is under way
            await asyncio.sleep(0)
        release.set()
        return list(await asyncio.gather(*tasks))

    assert _run(scenario()) == [True, True, True]  # each write names the certificate as its own cause
    client.get_api_settings.assert_awaited_once_with()
    assert (api.certificate_mismatch_count, kept) == (1, ["bb"])  # kept and handed to the repair once


def test_a_failure_is_dated_when_the_write_failed_not_when_the_settings_check_ended(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = itertools.count()
    base = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base + timedelta(seconds=next(ticks)))  # every call is later
    read_at: list[datetime] = []

    async def read() -> ApiSettings:
        read_at.append(dt_util.utcnow())
        return _settings(active=False)

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = read

    assert _write(api, "marker", 251, 1)[0] is False

    (failure,) = api.write_stats.last_failures
    assert len(read_at) == 1  # the check ran
    assert failure.at < read_at[0]  # dated before the check began, not after it ended


def test_a_finished_settings_read_is_not_kept(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.return_value = _settings(active=False)

    assert _write(api, "marker", 251, 1) == (False, "Comexio API settings: the API is switched off (access: inactive)")

    client.get_api_settings.assert_awaited_once_with()
    assert api._settings_read is None  # its result holds the API password of the page


def test_a_read_that_finishes_after_a_newer_one_started_leaves_the_newer_one_to_be_joined(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        started = [asyncio.Event(), asyncio.Event()]
        released = [asyncio.Event(), asyncio.Event()]
        calls = iter(range(2))

        async def read() -> ApiSettings:
            number = next(calls)
            started[number].set()
            await released[number].wait()
            return _settings(active=False)

        client.set_marker_value.side_effect = _timeout()
        client.get_api_settings.side_effect = read
        old = asyncio.create_task(api.set_value("marker", 251, 1))
        await started[0].wait()
        monkeypatch.setattr(api, "host", "other-comexio.example")  # a reconfigure: the next write reads on its own
        newer = asyncio.create_task(api.set_value("marker", 252, 1))
        await started[1].wait()
        held = api._settings_read
        released[0].set()
        await old
        assert held is not None
        assert api._settings_read is held  # the old read's end did not clear the slot of the newer one
        released[1].set()
        await newer
        assert api._settings_read is None

    _run(scenario())


def test_a_write_issued_under_new_connection_settings_does_not_join_the_read_of_the_old_ones(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> list[bool]:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_read() -> ApiSettings:
            reading.set()
            await release.wait()
            return _settings(active=False)

        client.set_marker_value.side_effect = _timeout()
        client.get_api_settings.side_effect = slow_read
        old = asyncio.create_task(api.set_value("marker", 251, 1))
        await reading.wait()
        monkeypatch.setattr(api, "host", "other-comexio.example")  # a reconfigure while the read is under way
        reading.clear()
        new = asyncio.create_task(api.set_value("marker", 252, 1))
        await asyncio.wait_for(reading.wait(), timeout=1)  # the new write reads for itself
        release.set()
        return list(await asyncio.gather(old, new))

    assert _run(scenario()) == [False, False]
    assert client.get_api_settings.await_count == 2


def test_a_later_certificate_mismatch_is_kept_again_not_taken_for_the_one_already_kept(
    api: ComexioAPI, client: MagicMock
) -> None:
    kept: list[str] = []
    api.on_certificate_mismatch = kept.append
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = [
        ComexioCertificateMismatchError("foreign", expected="aa", actual="bb"),
        ComexioCertificateMismatchError("foreign", expected="aa", actual="cc"),
    ]

    _write(api, "marker", 251, 1)
    _write(api, "marker", 251, 1)

    assert (api.certificate_mismatch_count, kept) == (2, ["bb", "cc"])


def test_a_certificate_mismatch_of_the_old_server_is_not_kept_for_the_one_reconfigured_meanwhile(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept: list[str] = []
    api.on_certificate_mismatch = kept.append

    async def reconfigure_then_mismatch() -> ApiSettings:
        monkeypatch.setattr(api, "host", "other-comexio.example")
        raise ComexioCertificateMismatchError("foreign", expected="aa", actual="bb")

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = reconfigure_then_mismatch

    assert _write(api, "marker", 251, 1) == (False, None)  # no text, no repair for the new configuration

    client.get_api_settings.assert_awaited_once_with()  # the read ran, its result was discarded
    assert (api.certificate_mismatch_count, kept) == (0, [])
    assert api.write_stats.failed == 1


def test_settings_read_from_the_old_server_are_not_named_after_a_reconfigure(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reconfigure_then_answer() -> ApiSettings:
        monkeypatch.setattr(api, "host", "other-comexio.example")
        return _settings(active=False)

    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = reconfigure_then_answer

    assert _write(api, "marker", 251, 1) == (False, None)
    client.get_api_settings.assert_awaited_once_with()  # the read ran, its result was discarded
    assert api.write_stats.failed == 1


def test_a_waiting_write_that_is_cancelled_leaves_the_shared_read_to_the_others(
    api: ComexioAPI, client: MagicMock
) -> None:
    async def scenario() -> list[tuple[bool, str | None]]:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_read() -> ApiSettings:
            reading.set()
            await release.wait()
            return _settings(active=False)

        async def write(marker_id: int) -> tuple[bool, str | None]:
            return await api.set_value("marker", marker_id, 1), api.last_write_diagnosis

        client.set_marker_value.side_effect = _timeout()
        client.get_api_settings.side_effect = slow_read
        starter = asyncio.create_task(write(251))
        await reading.wait()
        others = [asyncio.create_task(write(252)), asyncio.create_task(write(253))]
        for _ in range(20):  # the other two fail while the read is under way
            await asyncio.sleep(0)
        starter.cancel()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*others), timeout=5)
        with pytest.raises(asyncio.CancelledError):
            await starter
        return list(results)

    results = _run(scenario())

    assert [written for written, _ in results] == [False, False]
    assert all(diagnosis is not None and "the API is switched off" in diagnosis for _, diagnosis in results)
    client.get_api_settings.assert_awaited_once_with()


def test_a_certificate_mismatch_met_by_the_read_is_the_cause_the_caller_names(
    api: ComexioAPI, client: MagicMock
) -> None:
    client.set_marker_value.side_effect = _timeout()
    client.get_api_settings.side_effect = ComexioCertificateMismatchError("foreign", expected="aa", actual="bb")

    async def write() -> tuple[bool, bool]:
        written = await api.set_value("marker", 251, 1)
        return written, api.last_write_met_another_certificate

    assert _run(write()) == (False, True)
    # The counter names the certificate, as for a mismatch the write itself met, and the repair is raised.
    [failure] = api.write_stats.last_failures
    assert failure.error == "ComexioCertificateMismatchError"
    assert api.certificate_mismatch is not None


def test_the_look_at_the_settings_does_not_hold_back_the_next_write_to_the_same_target(
    api: ComexioAPI, client: MagicMock
) -> None:
    """A DPT3 stop (even_if_held) queues behind the write that failed, not behind the diagnosis of it."""
    sent: list[int] = []

    async def set_marker_value(_marker_id: int, value: float) -> None:
        sent.append(int(value))
        if value == 1:
            raise _timeout()

    async def scenario() -> tuple[list[int], bool]:
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_read() -> ApiSettings:
            reading.set()
            await release.wait()
            return _settings(active=False)

        client.set_marker_value.side_effect = set_marker_value
        client.get_api_settings.side_effect = slow_read
        first = asyncio.create_task(api.set_value("marker", 251, 1))
        await reading.wait()
        stop_done = await asyncio.wait_for(api.set_value("marker", 251, 0, even_if_held=True), timeout=1)
        held_back = first.done()
        release.set()
        await first
        return sent, stop_done and not held_back

    assert _run(scenario()) == ([1, 0], True)


def test_a_write_that_asks_for_no_diagnosis_reads_no_settings_but_is_still_logged_and_counted(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """The move of a DPT3 step: the stop after it must not wait for the look at the settings (up to 10 s)."""
    client.set_marker_value.side_effect = _timeout()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        written, diagnosis = _write(api, "marker", 251, 1, repeat=False, diagnose=False)

    assert (written, diagnosis) == (False, None)
    client.get_api_settings.assert_not_awaited()
    assert api.write_stats.failed == 1
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.startswith("Comexio API write failed (marker 251)")
    assert SETTINGS_ERROR not in line


def test_the_next_write_does_not_inherit_the_diagnosis_of_the_last(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = [_timeout(), None]

    async def two_writes() -> tuple[bool, str | None, bool, str | None]:
        first = await api.set_value("marker", 251, 1)
        first_text = api.last_write_diagnosis
        second = await api.set_value("marker", 251, 1)
        return first, first_text, second, api.last_write_diagnosis

    first, first_text, second, second_text = _run(two_writes())

    assert (first, second) == (False, True)
    assert first_text is not None
    assert second_text is None


def test_overlapping_writes_each_keep_their_own_diagnosis(api: ComexioAPI, client: MagicMock) -> None:
    async def write(marker_id: int, value: int) -> tuple[bool, str | None]:
        written = await api.set_value("marker", marker_id, value)
        return written, api.last_write_diagnosis

    async def set_marker_value(marker_id: int, _value: float) -> None:
        await asyncio.sleep(0)
        if marker_id == 251:
            raise _timeout()

    client.set_marker_value.side_effect = set_marker_value

    async def both() -> list[tuple[bool, str | None]]:
        return list(await asyncio.gather(write(251, 1), write(7, 1)))

    failed, fine = _run(both())

    assert failed[0] is False
    assert failed[1] is not None
    assert fine == (True, None)


def test_the_diagnosis_of_another_api_is_not_taken_for_this_ones(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _timeout()
    other = copy.copy(api)

    async def write_then_ask_both() -> tuple[str | None, str | None]:
        await api.set_value("marker", 251, 1)
        return api.last_write_diagnosis, other.last_write_diagnosis

    here, there = _run(write_then_ask_both())

    assert here is not None
    assert there is None


# ---- what reaches the caller -----------------------------------------------------------------------------------


def _coordinator(api: Any) -> Any:
    return SimpleNamespace(api=api)


def test_the_error_of_an_entity_names_what_the_settings_say() -> None:
    api = SimpleNamespace(
        last_write_met_another_certificate=False,
        last_write_diagnosis="Comexio API settings: the API is switched off (access: inactive)",
    )

    err = write_failed(_coordinator(api), "Failed to set M251")

    assert isinstance(err, HomeAssistantError)
    assert str(err) == "Failed to set M251: Comexio API settings: the API is switched off (access: inactive)"


def test_the_error_of_an_entity_is_unchanged_without_a_diagnosis() -> None:
    api = SimpleNamespace(last_write_met_another_certificate=False, last_write_diagnosis=None)

    assert str(write_failed(_coordinator(api), "Failed to set M251")) == "Failed to set M251"


def test_another_certificate_stays_the_cause_an_entity_names() -> None:
    api = SimpleNamespace(last_write_met_another_certificate=True, last_write_diagnosis="Comexio API settings: x")

    message = str(write_failed(_coordinator(api), "Failed to set M251"))

    assert CERTIFICATE_CHANGED_MSG in message
    assert "Comexio API settings" not in message


# ---- the notification of the set_value service ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("certificate", "expected"),
    [
        pytest.param(False, "Write failed: M1 = 1. Comexio API settings: x.\nDuration: ", id="settings-only"),
        pytest.param(
            True,
            f"Write failed: M1 = 1. {CERTIFICATE_CHANGED_MSG} Comexio API settings: x.\nDuration: ",
            id="certificate-first",
        ),
    ],
)
def test_the_set_value_service_names_the_settings_after_the_certificate(
    monkeypatch: pytest.MonkeyPatch, certificate: bool, expected: str
) -> None:
    api = MagicMock(
        set_value=AsyncMock(return_value=False),
        last_write_met_another_certificate=certificate,
        last_write_diagnosis="Comexio API settings: x",
    )
    monkeypatch.setattr(misc, "_async_get_service_context", AsyncMock(return_value=(MagicMock(), api, None)))
    monkeypatch.setattr(misc, "_resolve_set_value_target", lambda *_: ({"target_type": "marker", "target_id": 1}, ""))
    notify = MagicMock()
    monkeypatch.setattr(misc.persistent_notification, "async_create", notify)

    response = _run(misc._handle_set_value(MagicMock(), MagicMock(data={"target": "M1", "value": "1"})))

    assert response["success"] is False
    assert notify.call_args.args[1].startswith(expected)


def test_the_set_value_service_adds_nothing_without_a_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    api = MagicMock(
        set_value=AsyncMock(return_value=False), last_write_met_another_certificate=False, last_write_diagnosis=None
    )
    monkeypatch.setattr(misc, "_async_get_service_context", AsyncMock(return_value=(MagicMock(), api, None)))
    monkeypatch.setattr(misc, "_resolve_set_value_target", lambda *_: ({"target_type": "marker", "target_id": 1}, ""))
    notify = MagicMock()
    monkeypatch.setattr(misc.persistent_notification, "async_create", notify)

    _run(misc._handle_set_value(MagicMock(), MagicMock(data={"target": "M1", "value": "1"})))

    assert notify.call_args.args[1].startswith("Write failed: M1 = 1.\nDuration: ")


# ---- no password in the log ------------------------------------------------------------------------------------


def test_neither_the_password_nor_the_user_of_the_page_reaches_the_log_or_the_error(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    html = load_fixture("api_settings_page.html")
    assert PAGE_PASSWORD in html
    # Another password than the page's: the verdict names "differs", never the value.
    client.get_api_settings.return_value = parse_api_settings(html, api_username=PAGE_USER, api_password="other-pw")
    client.get_api_settings.side_effect = None
    client.set_marker_value.side_effect = _timeout()

    async def write_and_build_the_error() -> tuple[bool, str, str | None]:
        written = await api.set_value("marker", 251, 1)
        return written, str(write_failed(_coordinator(api), "Failed to set M251")), api.last_write_diagnosis

    with caplog.at_level(logging.DEBUG):
        ok, err, diagnosis = _run(write_and_build_the_error())

    assert ok is False
    assert diagnosis is not None
    assert diagnosis in err
    written = "\n".join(r.getMessage() for r in caplog.records) + "\n" + diagnosis
    assert "password differs from the configured API password" in written
    for text in (written, err):
        assert PAGE_PASSWORD not in text
        assert "other-pw" not in text
        assert PAGE_USER not in text
