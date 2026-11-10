"""Every failed /api/ write is counted, with its error class, for the "API write errors" sensor (#289)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioCertificateMismatchError, ComexioConnectionError, ComexioRequestRejectedError
import aiohttp
import pytest

from custom_components.comexio import api as api_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.api_write import ApiWriteStats, failure_class
from custom_components.comexio.const import API_WRITE_FAILURES_KEPT
from custom_components.comexio.sensor import ComexioApiWriteErrorsSensor

DROPPED_CLASS = "ComexioConnectionError (ServerDisconnectedError)"


def _dropped() -> ComexioConnectionError:
    err = ComexioConnectionError("Writing marker M5 failed: ServerDisconnectedError('Server disconnected')")
    err.__cause__ = aiohttp.ServerDisconnectedError("Server disconnected")
    return err


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.set_marker_value = AsyncMock()
    fake.set_knx_value = AsyncMock()
    fake.set_io_value = AsyncMock()
    monkeypatch.setattr(ComexioAPI, "client", property(lambda _self: fake))
    return fake


@pytest.fixture
def api(comexio_api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> ComexioAPI:
    monkeypatch.setattr(comexio_api, "fetch_replaced_fingerprint", AsyncMock(return_value=None))
    return comexio_api


def test_a_successful_write_is_not_counted(api: ComexioAPI, client: MagicMock) -> None:
    assert _run(api.set_value("marker", 5, 1)) is True

    assert api.write_stats.failed == 0
    assert api.write_stats.last_failures == ()


@pytest.mark.parametrize(
    "error",
    [
        ComexioConnectionError("refused"),
        ComexioRequestRejectedError("Forbidden"),
        ValueError("not a number"),
    ],
    ids=["connection error", "rejected", "value error"],
)
def test_a_failed_write_is_counted_with_its_error_class(api: ComexioAPI, client: MagicMock, error: Exception) -> None:
    client.set_marker_value.side_effect = error

    assert _run(api.set_value("marker", 5, 0)) is False

    assert api.write_stats.failed == 1
    (failure,) = api.write_stats.last_failures
    assert (failure.target, failure.error) == ("marker 5", type(error).__name__)


def test_a_dropped_connection_names_the_closed_connection_as_cause(api: ComexioAPI, client: MagicMock) -> None:
    client.set_knx_value.side_effect = _dropped()

    assert _run(api.set_value("knx", 9, 1)) is False

    assert api.write_stats.last_failures[0].error == DROPPED_CLASS


def test_the_failure_is_logged_once_at_error_level(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.DEBUG, logger=api_module.__name__):
        _run(api.set_value("marker", 5, 0))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Comexio API write failed (marker 5)" in errors[0].getMessage()


def test_a_foreign_certificate_is_counted_with_its_error_class(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = ComexioCertificateMismatchError("x", expected="aa", actual="bb")

    assert _run(api.set_value("marker", 5, 0)) is False

    assert api.write_stats.last_failures[0].error == "ComexioCertificateMismatchError"


def test_an_io_write_without_extension_is_counted_and_logged_once(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("io", "103", 0)) is False

    assert len(caplog.records) == 1
    assert api.write_stats.failed == 1


def test_a_failed_io_write_is_counted_by_its_id(api: ComexioAPI, client: MagicMock) -> None:
    client.set_io_value.side_effect = _dropped()

    assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", "Q5")) is False

    assert api.write_stats.last_failures[0].target == "io 103"


def test_the_stats_keep_only_the_latest_failures_and_tell_their_listeners() -> None:
    stats = ApiWriteStats()
    calls: list[int] = []
    remove = stats.add_listener(lambda: calls.append(stats.failed))

    for number in range(API_WRITE_FAILURES_KEPT + 2):
        stats.record_failure(f"marker {number}", ValueError("x"))
    remove()
    remove()  # twice is harmless
    stats.record_failure("marker 99", ValueError("x"))

    assert stats.failed == API_WRITE_FAILURES_KEPT + 3
    assert calls == list(range(1, API_WRITE_FAILURES_KEPT + 3))
    assert [f.target for f in stats.last_failures] == ["marker 3", "marker 4", "marker 99"]


def test_the_stats_attributes_name_the_failures() -> None:
    stats = ApiWriteStats()
    stats.record_failure("io 7", _dropped())

    (failure,) = stats.attributes()["last_failures"]

    assert (failure["target"], failure["error"]) == ("io 7", DROPPED_CLASS)
    assert failure["at"].endswith("+00:00")


def test_failure_class_names_the_cause() -> None:
    assert failure_class(ValueError("x")) == "ValueError"
    assert failure_class(_dropped()) == DROPPED_CLASS


def test_the_sensor_shows_the_counter_and_its_attributes() -> None:
    stats = ApiWriteStats()
    stats.record_failure("marker 5", _dropped())
    sensor = ComexioApiWriteErrorsSensor(SimpleNamespace(api=SimpleNamespace(write_stats=stats)), "iosrv1")  # type: ignore[arg-type]

    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["last_failures"][0]["target"] == "marker 5"
