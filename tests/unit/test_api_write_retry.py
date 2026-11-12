"""A /api/ write that Comexio dropped is sent again under strict conditions, and every failure is counted (#289)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import re
import textwrap
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioRequestRejectedError,
)
import aiohttp
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import api as api_module, entity as entity_module, light as light_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.api_write import (
    ApiWriteStats,
    connection_dropped,
    held_value_objection,
    is_same_reading,
    is_same_value,
)
from custom_components.comexio.button import ComexioKnxTriggerButton, ComexioMarkerTriggerButton
from custom_components.comexio.const import (
    API_WRITE_RETRY_MAX,
    API_WRITE_RETRY_WINDOW_SEC,
    CERTIFICATE_CHANGED_MSG,
    KNX_DPT3_COVER_DIRECTION_DOWN,
    KNX_DPT3_COVER_DIRECTION_UP,
    KNX_DPT3_STEPCODE_BREAK,
    KNX_DPT3_STEPCODE_MOVE,
    SOURCE_CATEGORIES,
    MarkerKind,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.cover import ComexioKnxCover
from custom_components.comexio.light import ComexioKnxLight

DROPPED_CLASS = "ComexioConnectionError (ServerDisconnectedError)"


CHANGED_REASON = "HA's value changed since the write was issued, a repetition could overwrite a newer one"


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


def test_a_dropped_write_is_sent_again_and_counted_as_recovered(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("marker", 5, 0)) is True

    assert client.set_marker_value.await_args_list == [call(5, 0)] * 2  # the repetition sends the same write
    stats = api.write_stats
    assert (stats.failed, stats.retries, stats.recovered) == (0, 1, 1)


def test_a_write_is_repeated_at_most_the_configured_number_of_times(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1 + API_WRITE_RETRY_MAX
    stats = api.write_stats
    assert (stats.failed, stats.retries, stats.recovered) == (1, API_WRITE_RETRY_MAX, 0)
    (failure,) = stats.last_failures
    assert (failure.target, failure.error) == ("marker 5", DROPPED_CLASS)


@pytest.mark.parametrize("cached", [0, "0", 0.0])
def test_a_drop_is_not_repeated_once_ha_got_the_wanted_value(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture, cached: Any
) -> None:
    # The push of the wanted value came in while the write was under way: Comexio took it.
    api.cached_value = lambda _type, _id: cached if client.set_marker_value.await_count else 1
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.endswith("(not sent again: HA got the wanted value since the write was issued)")


TARGETS = [
    ("marker", 5, None, None, "set_marker_value"),
    ("knx", 9, None, None, "set_knx_value"),
    ("io", "103", "LED-Dimmer A3", "Q5", "set_io_value"),
]
TARGET_ARGS = ("target_type", "target_id", "ext", "identifier", "method")


@pytest.mark.parametrize("cached", [2, "2", None])
@pytest.mark.parametrize(TARGET_ARGS, TARGETS)
def test_a_drop_is_not_repeated_once_ha_got_another_value(
    api: ComexioAPI,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    cached: Any,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
    method: str,
) -> None:
    # A value that came in after the write was issued is newer than it: a repetition would overwrite it.
    send = getattr(client, method)
    api.cached_value = lambda _type, _id: cached if send.await_count else 1
    send.side_effect = _dropped()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value(target_type, target_id, 0, ext, identifier)) is False

    assert send.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)
    # The error line says why the drop was not sent again.
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.endswith(f"(not sent again: {CHANGED_REASON})")


@pytest.mark.parametrize("cached", [0, "0", 0.0])
@pytest.mark.parametrize(TARGET_ARGS, TARGETS)
def test_a_drop_is_repeated_although_ha_held_the_wanted_value_all_along(
    api: ComexioAPI,
    client: MagicMock,
    cached: Any,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
    method: str,
) -> None:
    # A value that was there before the write proves nothing: it may be stale (a set_value service write leaves the
    # cache alone), and setting a target to the value it holds changes nothing.
    api.cached_value = lambda _type, _id: cached
    send = getattr(client, method)
    send.side_effect = [_dropped(), None]

    assert _run(api.set_value(target_type, target_id, 0, ext, identifier)) is True

    assert send.await_count == 2
    assert (api.write_stats.failed, api.write_stats.retries, api.write_stats.recovered) == (0, 1, 1)


@pytest.mark.parametrize("flip_at", [2, 3])
def test_a_value_that_comes_in_between_two_repetitions_stops_them(
    api: ComexioAPI, client: MagicMock, flip_at: int
) -> None:
    # The value HA held when the write was issued is the reference for every repetition, not only the first.
    api.cached_value = lambda _type, _id: 2 if client.set_marker_value.await_count >= flip_at else 1
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == flip_at
    assert (api.write_stats.failed, api.write_stats.retries) == (1, flip_at - 1)


@pytest.mark.parametrize("cached", [1, None, "unknown"])
def test_a_drop_is_repeated_while_ha_holds_another_or_no_value(api: ComexioAPI, client: MagicMock, cached: Any) -> None:
    api.cached_value = lambda _type, _id: cached
    client.set_marker_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("marker", 5, 0)) is True

    assert client.set_marker_value.await_count == 2
    assert (api.write_stats.failed, api.write_stats.retries, api.write_stats.recovered) == (0, 1, 1)


@pytest.mark.parametrize(
    ("before", "now", "same"),
    [
        (1, 1, True),
        (1, 1.0, True),
        ("1", 1, True),
        (1, "1", True),
        (None, None, True),
        ("unknown", "unknown", True),
        (1, 2, False),
        (None, 1, False),
        (1, None, False),
        ("unknown", 1, False),
    ],
)
def test_two_cached_values_are_one_reading_however_spelled(before: Any, now: Any, same: bool) -> None:
    assert is_same_reading(before, now) is same


@pytest.mark.parametrize(
    ("before", "now", "reason"),
    [
        (0, 0, None),
        (1, 1, None),
        (None, None, None),
        (1, "1.0", None),
        (1, 0, "HA got the wanted value since the write was issued"),
        (None, 0, "HA got the wanted value since the write was issued"),
        (1, 2, CHANGED_REASON),
        (1, None, CHANGED_REASON),
    ],
)
def test_only_a_value_that_came_in_since_the_write_was_issued_objects(
    before: Any, now: Any, reason: str | None
) -> None:
    assert held_value_objection(before, now, 0) == reason


def test_the_value_lookup_gets_the_type_and_id_of_the_target(api: ComexioAPI, client: MagicMock) -> None:
    seen: list[tuple[str, str | int]] = []

    def lookup(target_type: str, target_id: str | int) -> None:
        seen.append((target_type, target_id))

    api.cached_value = lookup
    client.set_marker_value.side_effect = [_dropped(), None]

    _run(api.set_value("marker", 7, 1))

    # Once when the write is issued, once after the drop.
    assert seen == [("marker", "7"), ("marker", "7")]


def test_a_drop_that_came_back_late_is_not_repeated(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Comexio may have taken the command and hung up afterwards: sending it again could do it twice.
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 0.0)

    async def late_drop(*_args: Any) -> None:
        await asyncio.sleep(0.01)
        raise _dropped()

    client.set_marker_value.side_effect = late_drop

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1
    assert api.write_stats.retries == 0


@pytest.mark.parametrize(
    "error",
    [
        ComexioConnectionError("refused"),
        ComexioRequestRejectedError("Forbidden"),
        ValueError("not a number"),
    ],
    ids=["other connection error", "rejected", "value error"],
)
def test_other_failures_are_not_repeated_but_counted(api: ComexioAPI, client: MagicMock, error: Exception) -> None:
    client.set_marker_value.side_effect = error

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)
    assert api.write_stats.last_failures[0].error == type(error).__name__


def test_a_digital_output_is_repeated_like_a_marker(api: ComexioAPI, client: MagicMock) -> None:
    client.set_io_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", "Q5")) is True

    assert client.set_io_value.await_args_list == [call("LED-Dimmer A3", "Q5", 0)] * 2


def test_an_analog_output_over_the_admin_session_is_not_repeated_here(api: ComexioAPI, client: MagicMock) -> None:
    # aiocomexio asks once more by itself on that path; this loop only covers /api/.
    client.set_io_value.side_effect = _dropped()

    assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", "AO1")) is False

    assert client.set_io_value.await_count == 1
    assert api.write_stats.retries == 0


def test_a_knx_write_is_repeated(api: ComexioAPI, client: MagicMock) -> None:
    client.set_knx_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("knx", 9, 1)) is True

    assert client.set_knx_value.await_args_list == [call(9, 1)] * 2
    client.set_marker_value.assert_not_awaited()
    client.set_io_value.assert_not_awaited()
    assert (api.write_stats.failed, api.write_stats.retries, api.write_stats.recovered) == (0, 1, 1)


@pytest.mark.parametrize("target_type", ["marker", "knx"])
@pytest.mark.parametrize("spelling", [5, "5", "05", " 5 "])
def test_the_value_lookup_gets_a_number_id_in_one_spelling(
    api: ComexioAPI, client: MagicMock, target_type: str, spelling: str | int
) -> None:
    # The coordinator keys its values by "5": asked for "05", it would never see the push of the wanted value, and
    # the drop would be repeated.
    send = getattr(client, f"set_{target_type}_value")
    api.cached_value = lambda kind, key: 1 if (kind, key) == (target_type, "5") and send.await_count else None
    send.side_effect = _dropped()

    assert _run(api.set_value(target_type, spelling, 1)) is False

    assert getattr(client, f"set_{target_type}_value").await_count == 1


@pytest.mark.parametrize(
    ("target_type", "target_id", "key"),
    [
        ("marker", 5, ("marker", "5")),
        ("marker", "05", ("marker", "5")),
        ("knx", "007", ("knx", "7")),
        ("marker", "no number", ("marker", "no number")),
        ("io", "05", ("io", "05")),
    ],
)
def test_the_key_of_a_write_target(target_type: str, target_id: str | int, key: tuple[str, str]) -> None:
    assert api_module._write_target_key(target_type, target_id) == key


def test_a_repetition_is_not_sent_once_the_connection_settings_changed(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    async def drop_and_reconfigure(_marker: int, _value: int) -> None:
        api.host = "other-comexio.example"
        raise _dropped()

    client.set_marker_value.side_effect = drop_and_reconfigure

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 5, 1)) is False

    # The error line names the drop and why it was not sent again, and claims no repetition.
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.startswith("Comexio API write failed (marker 5): ")
    assert "Server disconnected" in line
    assert line.endswith("(not sent again: the connection settings changed since the write was issued)")

    # Marker 5 of the new server may be something else: the one send went to the old one, none to the new one.
    assert client.set_marker_value.await_count == 1
    stats = api.write_stats
    # A repetition that is not sent is not counted, and the failure is the drop the write met.
    assert (stats.failed, stats.retries, stats.recovered) == (1, 0, 0)
    assert stats.last_failures[0].error == DROPPED_CLASS


def test_a_write_that_waited_for_its_turn_is_not_sent_once_the_connection_settings_changed(
    api: ComexioAPI, client: MagicMock
) -> None:
    sent: list[int] = []

    async def send(_marker: int, value: int) -> None:
        sent.append(value)
        await asyncio.sleep(0.02)
        api.host = "other-comexio.example"

    client.set_marker_value.side_effect = send

    async def both() -> list[bool]:
        return list(await asyncio.gather(api.set_value("marker", 5, 0), api.set_value("marker", 5, 1)))

    assert _run(both()) == [True, False]

    assert sent == [0]
    assert api.write_stats.failed == 1


@pytest.mark.parametrize("attribute", ["username", "password"])
def test_a_waiting_api_write_does_not_mind_a_new_admin_user(api: ComexioAPI, client: MagicMock, attribute: str) -> None:
    sent: list[int] = []

    async def send(_marker: int, value: int) -> None:
        sent.append(value)
        await asyncio.sleep(0.02)
        setattr(api, attribute, "changed")

    client.set_marker_value.side_effect = send

    async def both() -> list[bool]:
        return list(await asyncio.gather(api.set_value("marker", 5, 0), api.set_value("marker", 5, 1)))

    assert _run(both()) == [True, True]

    assert sent == [0, 1]


def test_a_write_refused_for_changed_settings_is_logged_as_an_error(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = api.write_settings("marker")
    monkeypatch.setattr(api, "host", "other-comexio.example")

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        sent = _run(api._write_and_repeat("marker", 5, 1, None, None, api_module._RepeatRule(), taken))

    assert sent is False
    client.set_marker_value.assert_not_awaited()
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line == (
        "Comexio API write failed (marker 5): connection settings changed since the write was issued: not sent"
    )


def test_a_write_with_unchanged_connection_settings_is_sent(api: ComexioAPI, client: MagicMock) -> None:
    assert _run(api.set_value("marker", 5, 1)) is True

    client.set_marker_value.assert_awaited_once_with(5, 1)


@pytest.mark.parametrize("pushed", [0, 2])
def test_a_stop_is_repeated_whatever_value_ha_gets_meanwhile(api: ComexioAPI, client: MagicMock, pushed: int) -> None:
    # A DPT3 stop has to reach the actuator: neither a 0 nor another value coming in holds it back.
    api.cached_value = lambda _type, _id: pushed if client.set_marker_value.await_count else 1
    client.set_marker_value.side_effect = [_dropped(), None]

    assert _run(api.set_value("marker", 5, 0, even_if_held=True)) is True

    assert client.set_marker_value.await_count == 2
    assert (api.write_stats.failed, api.write_stats.retries, api.write_stats.recovered) == (0, 1, 1)


def test_a_stop_is_repeated_no_more_often_than_any_other_write(api: ComexioAPI, client: MagicMock) -> None:
    api.cached_value = lambda _type, _id: 2 if client.set_marker_value.await_count else 1
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 5, 0, even_if_held=True)) is False

    assert client.set_marker_value.await_count == 1 + API_WRITE_RETRY_MAX


@pytest.mark.parametrize(
    ("attempt", "reason"),
    [
        (api_module._WriteAttempt(_dropped(), elapsed=API_WRITE_RETRY_WINDOW_SEC + 0.1), "the connection dropped"),
        (api_module._WriteAttempt(_dropped(), reported=True), "already reported"),
        (api_module._WriteAttempt(ComexioRequestRejectedError("no")), "not a connection closed"),
    ],
)
def test_a_stop_is_not_repeated_where_no_write_is(api: ComexioAPI, attempt: Any, reason: str) -> None:
    rule = api_module._RepeatRule(repeat=True, even_if_held=True)

    assert (api._why_not_repeated(attempt, "marker", None, 0, rule) or "").startswith(reason)


def test_a_write_sent_once_stays_sent_once_even_if_held_is_allowed(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 5, 0, repeat=False, even_if_held=True)) is False

    assert client.set_marker_value.await_count == 1


@pytest.mark.parametrize(
    ("target_type", "identifier", "attribute", "counts"),
    [
        ("marker", None, "api_user", True),
        ("marker", None, "api_pass", True),
        ("marker", None, "host", True),
        ("marker", None, "fingerprint", True),
        ("marker", None, "username", False),
        ("marker", None, "password", False),
        ("knx", "AO1", "password", False),  # an identifier means nothing for a KNX or marker write
        ("marker", "AO1", "password", False),
        ("marker", "AO1", "api_pass", True),
        ("io", "AI1", "password", True),  # every IO but Q* and DG* goes over the admin session
        ("io", "AI1", "api_user", False),
        ("io", "Q5", "api_user", True),
        ("io", "Q5", "password", False),
        ("io", "AO1", "username", True),
        ("io", "AO1", "password", True),
        ("io", "AO1", "host", True),
        ("io", "AO1", "fingerprint", True),
        ("io", "AO1", "api_user", False),
        ("io", "AO1", "api_pass", False),
        ("io", None, "api_user", True),
    ],
)
def test_the_settings_of_a_write_are_those_of_the_way_it_goes(
    api: ComexioAPI, target_type: str, identifier: str | None, attribute: str, counts: bool
) -> None:
    before = api.write_settings(target_type, identifier)

    setattr(api, attribute, "changed")

    assert (api.write_settings(target_type, identifier) != before) is counts


@pytest.mark.parametrize(("attribute", "sent", "second_ok"), [("api_user", [0, 1], True), ("password", [0], False)])
def test_a_waiting_analog_output_write_only_minds_the_admin_session_settings(
    api: ComexioAPI, client: MagicMock, attribute: str, sent: list[int], second_ok: bool
) -> None:
    # AO* goes over the admin session: a new API user leaves the waiting write valid, a new admin password does not.
    values: list[int] = []

    async def send(_ext: str, _identifier: str, value: int) -> None:
        values.append(value)
        await asyncio.sleep(0.02)
        setattr(api, attribute, "changed")

    client.set_io_value.side_effect = send

    async def both() -> list[bool]:
        first = api.set_value("io", "103", 0, "LED-Dimmer A3", "AO1")
        second = api.set_value("io", "103", 1, "LED-Dimmer A3", "AO1")
        return list(await asyncio.gather(first, second))

    assert _run(both()) == [True, second_ok]

    assert values == sent


def _dpt3_entity(set_value: AsyncMock) -> entity_module.ComexioKnxDpt3Entity:
    entity = entity_module.ComexioKnxDpt3Entity.__new__(entity_module.ComexioKnxDpt3Entity)
    entity.coordinator = SimpleNamespace(  # type: ignore[assignment]
        _knx_bridge_marker_by_k_id=lambda: {"9": "77", "10": "78"}, api=SimpleNamespace(set_value=set_value)
    )
    entity._direction_id = "9"
    entity._stepcode_id = "10"
    return entity


@pytest.mark.parametrize("stepcode", [1, 4, 7])
def test_a_dpt3_move_step_is_written_once_because_it_moves_from_where_the_target_stands(stepcode: int) -> None:
    set_value = AsyncMock(return_value=True)

    assert _run(_dpt3_entity(set_value)._async_write_stepcode(stepcode)) is True

    set_value.assert_awaited_once_with("marker", "78", stepcode, repeat=False, even_if_held=False)


def _dimmer(set_value: AsyncMock) -> ComexioKnxLight:
    light = ComexioKnxLight.__new__(ComexioKnxLight)
    light.coordinator = MagicMock()
    light.coordinator.api.set_value = set_value
    light.coordinator._knx_bridge_marker_by_k_id.return_value = {"9": "77", "10": "78"}
    light._direction_id, light._stepcode_id = "9", "10"
    light._attr_brightness, light._move_lock = 0, asyncio.Lock()
    light.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    return light


def _sent(set_value: AsyncMock) -> list[tuple[str, int, bool]]:
    return [(call.args[1], call.args[2], call.kwargs["repeat"]) for call in set_value.await_args_list]


def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """light.py's clock, standing still until the test moves it: a cancelled hold is estimated from it."""
    now = [100.0]
    monkeypatch.setattr(light_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_a_dimmer_whose_move_was_not_confirmed_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The move is sent once; Comexio may have taken it before the connection dropped.
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    set_value = AsyncMock(side_effect=[True, False, True])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Failed to dim K9/K10$"):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]
    assert light._attr_brightness == 0


def test_a_stop_the_bridge_data_cannot_send_after_a_refused_move_says_the_dimmer_may_still_be_moving(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The bridge Marker data went away between the move and its stop: the error must not stop at why it was not sent.
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    set_value = AsyncMock(side_effect=[True, False])
    light = _dimmer(set_value)
    light.coordinator._knx_bridge_marker_by_k_id.side_effect = [{"9": "77"}, {"10": "78"}, None]

    move = light._async_move_to(255)
    still_moving = r"^Failed to dim K9/K10, and the stop telegram after it did not get through — the actuator may"
    with (
        caplog.at_level(logging.WARNING, logger=light_module.__name__),
        pytest.raises(RuntimeError, match=still_moving),
    ):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert light._attr_brightness == 0
    assert [record.getMessage() for record in caplog.records] == [
        "The stop telegram of dimming K9/K10 was not sent: Comexio K10: bridge Marker data not loaded yet — try again "
        "shortly"
    ]


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        ("bridge", "Comexio K10: bridge Marker data not loaded yet — try again shortly"),
        ("reconfigure", "the connection settings changed"),
    ],
)
def test_a_timed_stop_that_is_not_sent_moves_the_estimate_and_says_the_dimmer_may_still_be_moving(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cause: str, reason: str
) -> None:
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    set_value = AsyncMock(return_value=True)
    light = _dimmer(set_value)
    host = ["comexio-a"]
    light.coordinator.api.write_settings.side_effect = lambda _type: (host[0],)
    if cause == "bridge":
        light.coordinator._knx_bridge_marker_by_k_id.side_effect = [{"9": "77"}, {"10": "78"}, None]

    async def hold(_seconds: float) -> None:
        if cause == "reconfigure":
            host[0] = "comexio-b"  # a reconfigure during the hold: the stop belongs to the server the move went to

    monkeypatch.setattr(light_module.asyncio, "sleep", hold)

    move = light._async_move_to(255)
    failed = r"^Stop telegram failed after dimming K9/K10 — the actuator may still be moving$"
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(RuntimeError, match=failed):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert (light._attr_brightness, light._attr_is_on) == (255, True)
    light.async_write_ha_state.assert_called_once()
    assert [record.getMessage() for record in caplog.records] == [
        f"The stop telegram of dimming K9/K10 was not sent: {reason}"
    ]


def test_a_dimmer_whose_move_write_was_cancelled_is_stopped_before_the_cancellation_goes_on() -> None:
    # Comexio may have taken the move before the task was cancelled; the timed hold's finally has not started yet.
    set_value = AsyncMock(side_effect=[True, asyncio.CancelledError(), True])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]
    assert light._attr_brightness == 0


def test_a_failed_stop_after_a_cancelled_move_write_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    # The cancellation goes on, so no error reaches a caller: the warning is the only trace of a dimmer still moving.
    set_value = AsyncMock(side_effect=[True, asyncio.CancelledError(), False])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through — "
        "the actuator may still be moving"
    ]


def test_a_stop_that_raises_after_a_cancelled_move_write_does_not_take_the_cancellations_place(
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_value = AsyncMock(side_effect=[True, asyncio.CancelledError()])
    light = _dimmer(set_value)
    light.coordinator._knx_bridge_marker_by_k_id.side_effect = [{"9": "77"}, {"10": "78"}, None]

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through "
        "(Comexio K10: bridge Marker data not loaded yet — try again shortly) — the actuator may still be moving"
    ]


def test_a_cancelled_move_write_gets_no_stop_once_the_connection_settings_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The stop would go to the newly configured server, where the same marker may be another one.
    host = ["old-host"]

    async def write(_type: str, marker: str, value: int, **_kwargs: object) -> bool:
        if (marker, value) == ("78", 1):  # the move: a reconfigure lands while it is on its way, then HA cancels
            host[0] = "new-host"
            raise asyncio.CancelledError
        return True

    set_value = AsyncMock(side_effect=write)
    light = _dimmer(set_value)
    light.coordinator.api.write_settings.side_effect = lambda _type: (host[0],)

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through "
        "(not sent: the connection settings changed) — the actuator may still be moving"
    ]


def test_a_dimmer_cancelled_while_it_holds_the_move_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _frozen_clock(monkeypatch)  # cancelled before any time went by: the estimate stays
    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    set_value = AsyncMock(return_value=True)
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]
    assert light._attr_brightness == 0


def test_a_stop_that_raises_after_a_cancelled_hold_does_not_take_the_cancellations_place(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    set_value = AsyncMock(return_value=True)
    light = _dimmer(set_value)
    light.coordinator._knx_bridge_marker_by_k_id.side_effect = [{"9": "77"}, {"10": "78"}, None]

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through "
        "(Comexio K10: bridge Marker data not loaded yet — try again shortly) — the actuator may still be moving"
    ]


def test_a_cancelled_direction_write_gets_no_stop() -> None:
    # Nothing was moved yet: a stop would be a telegram for a move that never started.
    set_value = AsyncMock(side_effect=asyncio.CancelledError())
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True)]


def test_a_timed_stop_cancelled_in_flight_is_sent_once_more_before_the_cancellation_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The cancelled stop request may not have reached Comexio; a second stop telegram does no harm.
    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(return_value=None))
    set_value = AsyncMock(side_effect=[True, True, asyncio.CancelledError(), True])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True), ("78", 0, True)]
    assert caplog.records == []
    assert (light._attr_brightness, light._attr_is_on) == (255, True)  # the whole hold went by before the stop
    light.async_write_ha_state.assert_called_once()  # a restart restores the estimate


@pytest.mark.parametrize(
    ("start", "target", "stop_takes", "expected"),
    [(0, 200, 0.25, 50), (200, 0, 0.25, 150), (100, 0, 0.5, 50), (0, 255, 2.0, 255), (255, 0, 2.0, 0)],
)
def test_a_dimmer_cancelled_during_its_hold_is_estimated_where_the_stop_after_it_caught_it(
    monkeypatch: pytest.MonkeyPatch, start: int, target: int, stop_takes: float, expected: int
) -> None:
    # The hold is cancelled at once; the actuator moves on until the stop arrives, but not past its target.
    # A later move is timed from this estimate, and a restart restores the state written from it.
    duration = abs(target - start) / 255 * light_module.KNX_DPT3_LIGHT_FULL_RANGE_SECONDS
    now = _frozen_clock(monkeypatch)

    async def write(_type: str, marker: str, value: int, **_kwargs: object) -> bool:
        if (marker, value) == ("78", 0):  # the stop after the cancel takes its share of the hold
            now[0] += duration * stop_takes
        return True

    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    set_value = AsyncMock(side_effect=write)
    light = _dimmer(set_value)
    light._attr_brightness = start

    move = light._async_move_to(target)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value)[-1] == ("78", 0, True)  # stopped
    assert (light._attr_brightness, light._attr_is_on) == (expected, expected > 0)
    light.async_write_ha_state.assert_called_once()


def test_a_second_cancellation_during_the_stop_after_a_cancelled_hold_still_moves_the_estimate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = _frozen_clock(monkeypatch)

    async def write(_type: str, marker: str, value: int, **_kwargs: object) -> bool:
        if (marker, value) == ("78", 0):
            now[0] += light_module.KNX_DPT3_LIGHT_FULL_RANGE_SECONDS / 4
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    light = _dimmer(AsyncMock(side_effect=write))

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through "
        "(cancelled again) — the actuator may still be moving"
    ]
    assert light._attr_brightness == 64  # a quarter of the way


def test_a_state_write_that_fails_after_a_cancelled_hold_does_not_take_the_cancellations_place(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _frozen_clock(monkeypatch)
    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    light = _dimmer(AsyncMock(return_value=True))
    light.async_write_ha_state.side_effect = RuntimeError("entity gone")  # type: ignore[attr-defined]

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled; its estimated brightness 0 could not be written to the state"
    ]


def _cover_whose_open_hangs(
    with_stop: bool,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], list[BaseException | None]]:
    """Open a cover whose direction write hangs, then close it (and stop it) while the open waits.

    Returns the writes sent while the open hung, all writes, and the outcome of open, close (and stop).
    """
    sent: list[tuple[str, int]] = []
    while_hung: list[tuple[str, int]] = []

    async def scenario() -> list[BaseException | None]:
        open_direction_sent, release = asyncio.Event(), asyncio.Event()

        async def write(_type: str, marker: str, value: int, **_kwargs: object) -> bool:
            sent.append((marker, value))
            if (marker, value) == ("77", KNX_DPT3_COVER_DIRECTION_UP):
                open_direction_sent.set()
                await release.wait()  # the open's direction write hangs, as when it is repeated
            return True

        coordinator = MagicMock()
        coordinator.api.set_value = AsyncMock(side_effect=write)
        coordinator._knx_bridge_marker_by_k_id.return_value = {"9": "77", "10": "78"}
        cover = ComexioKnxCover(coordinator, "srv", {"id": 9}, {"id": 10, "ha_name": "Blind"})

        commands = [asyncio.create_task(cover.async_open_cover())]
        await open_direction_sent.wait()
        commands.append(asyncio.create_task(cover.async_close_cover()))
        if with_stop:
            commands.append(asyncio.create_task(cover.async_stop_cover()))
        for _ in range(3):
            await asyncio.sleep(0)
        while_hung.extend(sent)
        release.set()
        return await asyncio.gather(*commands, return_exceptions=True)

    outcomes = asyncio.run(scenario())
    return while_hung, sent, outcomes


def test_overlapping_cover_commands_do_not_interleave_their_writes() -> None:
    # Without one lock per cover, the close would set the direction while the open's move is on its way, and the
    # late move would start in the close's direction.
    _while_hung, sent, outcomes = _cover_whose_open_hangs(with_stop=False)

    assert sent == [
        ("77", KNX_DPT3_COVER_DIRECTION_UP),
        ("78", KNX_DPT3_STEPCODE_MOVE),
        ("77", KNX_DPT3_COVER_DIRECTION_DOWN),
        ("78", KNX_DPT3_STEPCODE_MOVE),
    ]
    assert outcomes == [None, None]


def test_a_stop_goes_out_at_once_and_cancels_the_moves_issued_before_it() -> None:
    # The cover may still be moving from an earlier command: the stop must not wait for a hung open or close.
    while_hung, sent, outcomes = _cover_whose_open_hangs(with_stop=True)

    assert while_hung == [("77", KNX_DPT3_COVER_DIRECTION_UP), ("78", KNX_DPT3_STEPCODE_BREAK)]
    assert sent == while_hung  # neither the open nor the queued close sends its move telegram afterwards
    opened, closed, stopped = outcomes
    assert [str(opened), str(closed), stopped] == [
        "Failed to open cover K9/K10: a stop came first",
        "Failed to close cover K9/K10: a stop came first",
        None,
    ]
    assert isinstance(opened, HomeAssistantError)
    assert isinstance(closed, HomeAssistantError)


def _timed_stop_cancelled_then(monkeypatch: pytest.MonkeyPatch, second_stop: Any) -> tuple[ComexioKnxLight, AsyncMock]:
    """A dimmer whose timed stop is cancelled in flight; second_stop is what the stop after it does."""
    monkeypatch.setattr(light_module.asyncio, "sleep", AsyncMock(return_value=None))
    return _dimmer(set_value := AsyncMock(side_effect=[True, True, asyncio.CancelledError(), second_stop])), set_value


@pytest.mark.parametrize(
    ("second_stop", "reason"),
    [(asyncio.CancelledError(), " (cancelled again)"), (KeyError(), " (KeyError())")],
    ids=["cancelled again", "unexpected error"],
)
def test_the_stop_after_a_cancelled_stop_is_sent_once_and_its_failure_does_not_replace_the_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, second_stop: BaseException, reason: str
) -> None:
    light, set_value = _timed_stop_cancelled_then(monkeypatch, second_stop)

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True), ("78", 0, True)]
    assert [record.getMessage() for record in caplog.records] == [
        f"Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through{reason} "
        "— the actuator may still be moving"
    ]


def test_a_bridge_error_in_the_stop_after_a_cancelled_stop_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    light, set_value = _timed_stop_cancelled_then(monkeypatch, True)
    light.coordinator._knx_bridge_marker_by_k_id.side_effect = [{"9": "77"}, {"10": "78"}, {"10": "78"}, None]

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]
    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through "
        "(Comexio K10: bridge Marker data not loaded yet — try again shortly) — the actuator may still be moving"
    ]


def test_a_safety_stop_cancelled_in_flight_is_sent_once_more_and_a_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The move was not confirmed, its stop is cancelled on the way, and the stop after that does not get through.
    set_value = AsyncMock(side_effect=[True, False, asyncio.CancelledError(), False])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with caplog.at_level(logging.WARNING, logger=light_module.__name__), pytest.raises(asyncio.CancelledError):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True), ("78", 0, True)]
    assert [record.getMessage() for record in caplog.records] == [
        "Dimming K9/K10 was cancelled during its move, and the stop telegram after it did not get through — "
        "the actuator may still be moving"
    ]


def test_a_dimmer_whose_direction_write_failed_is_not_moved_and_gets_no_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    set_value = AsyncMock(return_value=False)
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Failed to dim K9/K10$"):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True)]


def test_a_failed_stop_after_an_unconfirmed_move_tells_the_user_the_actuator_may_still_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    set_value = AsyncMock(side_effect=[True, False, False])
    light = _dimmer(set_value)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Failed to dim K9/K10, and the stop .* may still be moving$"):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]


def test_a_dimmer_gets_no_stop_once_the_connection_settings_changed_during_the_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stop would go to the newly configured server, where the same marker may be another one.
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    host = ["old-host"]

    async def write(_type: str, marker: str, value: int, **_kwargs: object) -> bool:
        if (marker, value) == ("78", 1):  # the move: it fails, and a reconfigure lands while it is on its way
            host[0] = "new-host"
            return False
        return True

    set_value = AsyncMock(side_effect=write)
    light = _dimmer(set_value)
    light.coordinator.api.write_settings.side_effect = lambda _type: (host[0],)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Failed to dim K9/K10, and the stop .* may still be moving$"):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    light.coordinator.api.write_settings.assert_called_with("marker")


def test_a_dimmer_is_not_moved_once_the_connection_settings_changed_during_the_direction_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    host = ["old-host"]

    async def write(*_args: object, **_kwargs: object) -> bool:
        host[0] = "new-host"
        return True

    set_value = AsyncMock(side_effect=write)
    light = _dimmer(set_value)
    light.coordinator.api.write_settings.side_effect = lambda _type: (host[0],)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Failed to dim K9/K10$"):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True)]


def test_the_timed_stop_of_a_dimmer_is_not_sent_to_a_reconfigured_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(light_module, "write_failed", lambda _coordinator, message: RuntimeError(message))
    host = ["old-host"]

    async def reconfigure_while_dimming(_seconds: float) -> None:
        host[0] = "new-host"

    monkeypatch.setattr(light_module.asyncio, "sleep", reconfigure_while_dimming)
    set_value = AsyncMock(return_value=True)
    light = _dimmer(set_value)
    light.coordinator.api.write_settings.side_effect = lambda _type: (host[0],)

    move = light._async_move_to(255)
    with pytest.raises(RuntimeError, match=r"^Stop telegram failed after dimming K9/K10 "):
        asyncio.run(move)

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False)]
    assert light._attr_brightness == 255


@pytest.mark.parametrize("stop_ok", [True, False])
def test_the_error_of_an_unconfirmed_move_names_the_certificate_the_move_met_not_the_stop(stop_ok: bool) -> None:
    light = _dimmer(AsyncMock())
    api = light.coordinator.api
    outcomes = iter([(True, False), (False, True), (stop_ok, False)])  # direction, move, stop

    async def write(*_args: object, **_kwargs: object) -> bool:
        ok, api.last_write_met_another_certificate = next(outcomes)
        return ok

    api.set_value = write

    move = light._async_move_to(255)
    with pytest.raises(HomeAssistantError, match=re.escape(CERTIFICATE_CHANGED_MSG)):
        asyncio.run(move)


def test_the_error_of_an_unconfirmed_move_names_no_certificate_the_stop_alone_met() -> None:
    light = _dimmer(AsyncMock())
    api = light.coordinator.api
    outcomes = iter([(True, False), (False, False), (False, True)])  # direction, move, stop

    async def write(*_args: object, **_kwargs: object) -> bool:
        ok, api.last_write_met_another_certificate = next(outcomes)
        return ok

    api.set_value = write

    move = light._async_move_to(255)
    with pytest.raises(HomeAssistantError) as raised:
        asyncio.run(move)

    assert CERTIFICATE_CHANGED_MSG not in str(raised.value)


def test_a_dimmer_move_that_went_through_is_held_and_then_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(light_module, "KNX_DPT3_LIGHT_FULL_RANGE_SECONDS", 0.0)
    set_value = AsyncMock(return_value=True)
    light = _dimmer(set_value)

    _run(light._async_move_to(255))

    assert _sent(set_value) == [("77", 1, True), ("78", 1, False), ("78", 0, True)]
    assert light._attr_brightness == 255


def test_a_dpt3_stop_and_a_dpt3_direction_stay_repeatable() -> None:
    set_value = AsyncMock(return_value=True)
    entity = _dpt3_entity(set_value)

    _run(entity._async_write_stepcode(0))
    _run(entity._async_write_direction(1))

    assert [(call.args, call.kwargs) for call in set_value.await_args_list] == [
        (("marker", "78", 0), {"repeat": True, "even_if_held": True}),
        (("marker", "77", 1), {"repeat": True, "even_if_held": False}),
    ]


def test_the_stats_attributes_name_retries_recoveries_and_the_failures() -> None:
    stats = ApiWriteStats()
    stats.record_retry()
    stats.record_recovered()
    stats.record_failure("io 7", _dropped())

    attributes = stats.attributes()

    assert (attributes["retries"], attributes["recovered_by_retry"]) == (1, 1)
    (failure,) = attributes["last_failures"]
    assert (failure["target"], failure["error"]) == ("io 7", DROPPED_CLASS)


def test_connection_dropped_needs_a_closed_connection_behind_a_connection_error() -> None:
    assert connection_dropped(_dropped()) is True
    assert connection_dropped(ComexioConnectionError("refused")) is False
    assert connection_dropped(aiohttp.ServerDisconnectedError()) is False


@pytest.mark.parametrize(
    ("cached", "wanted", "expected"),
    [
        (0, 0, True),
        ("1", 1, True),
        (21.5, 21.5, True),
        (0, 1, False),
        (None, 0, False),
        ("n/a", 0, False),
        (float("nan"), 0, False),
        (True, 1, False),
        (False, 0, False),
        (10**400, 0, False),
        ([], 0, False),
    ],
)
def test_is_same_value(cached: object, wanted: float, expected: bool) -> None:
    assert is_same_value(cached, wanted) is expected


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(marker_states={}, knx_states={}, io_states={}, _io_real_value_ids=set())


def test_the_coordinator_gives_the_cached_value_of_each_target_kind() -> None:
    fake = _coordinator()
    fake.marker_states["5"] = 1
    fake.knx_states["9"] = 2
    fake.io_states["103"] = 3
    fake._io_real_value_ids.add("103")

    assert ComexioCoordinator.cached_value(fake, "marker", 5) == 1  # type: ignore[arg-type]
    assert ComexioCoordinator.cached_value(fake, "knx", "9") == 2  # type: ignore[arg-type]
    assert ComexioCoordinator.cached_value(fake, "io", "103") == 3  # type: ignore[arg-type]
    assert ComexioCoordinator.cached_value(fake, "marker", 6) is None  # type: ignore[arg-type]


def test_an_io_placeholder_value_is_no_cached_value() -> None:
    fake = _coordinator()
    fake.io_states["103"] = 0  # nothing read yet

    assert ComexioCoordinator.cached_value(fake, "io", "103") is None  # type: ignore[arg-type]


def test_a_first_try_success_counts_nothing(api: ComexioAPI, client: MagicMock) -> None:
    assert _run(api.set_value("marker", 5, 1)) is True

    stats = api.write_stats
    assert (stats.failed, stats.retries, stats.recovered) == (0, 0, 0)
    assert stats.last_failures == ()


def test_the_last_allowed_repetition_can_still_save_the_write(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = [_dropped()] * API_WRITE_RETRY_MAX + [None]

    assert _run(api.set_value("marker", 5, 0)) is True

    assert client.set_marker_value.await_count == 1 + API_WRITE_RETRY_MAX
    stats = api.write_stats
    assert (stats.failed, stats.retries, stats.recovered) == (0, API_WRITE_RETRY_MAX, 1)


def test_the_cached_value_is_read_again_before_every_repetition(api: ComexioAPI, client: MagicMock) -> None:
    # The webhook of the Comexio push can arrive between two attempts and set the wanted value in HA.
    cache = {"value": 1}
    api.cached_value = lambda _type, _id: cache["value"]

    calls: list[int] = []

    async def drop_and_push(*_args: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            cache["value"] = 0
        raise _dropped()

    client.set_marker_value.side_effect = drop_and_push

    assert _run(api.set_value("marker", 5, 0)) is False

    stats = api.write_stats
    assert (stats.failed, stats.retries) == (1, 1)


@pytest.mark.parametrize(
    ("target_type", "target_id", "ext", "identifier", "expected"),
    [
        ("io", "103", "LED-Dimmer A3", "Q5", ("io", "103")),
        ("knx", 9, None, None, ("knx", "9")),
    ],
)
def test_the_value_lookup_gets_io_and_knx_targets_too(
    api: ComexioAPI,
    client: MagicMock,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
    expected: tuple[str, str | int],
) -> None:
    seen: list[tuple[str, str | int]] = []
    api.cached_value = lambda kind, ident: seen.append((kind, ident))
    client.set_io_value.side_effect = [_dropped(), None]
    client.set_knx_value.side_effect = [_dropped(), None]

    _run(api.set_value(target_type, target_id, 1, ext, identifier))

    assert seen == [expected, expected]


@pytest.mark.parametrize(
    ("identifier", "repeated"), [("Q5", True), ("DG1", True), ("AO1", False), ("AI1", False), ("QI1", False)]
)
def test_only_the_api_outputs_are_repeated(api: ComexioAPI, client: MagicMock, identifier: str, repeated: bool) -> None:
    client.set_io_value.side_effect = _dropped()

    _run(api.set_value("io", "103", 0, "LED-Dimmer A3", identifier))

    assert (client.set_io_value.await_count > 1) is repeated


def test_a_write_without_identifier_is_not_repeated_and_names_why(api: ComexioAPI) -> None:
    reason = api._why_not_repeated(api_module._WriteAttempt(_dropped()), "io", None, 0, api_module._RepeatRule())

    assert reason == "not an /api/ write"


def test_an_already_reported_failure_is_not_repeated_whatever_its_cause(api: ComexioAPI) -> None:
    attempt = api_module._WriteAttempt(_dropped(), reported=True)

    reason = api._why_not_repeated(attempt, "marker", None, 0, api_module._RepeatRule())

    assert reason is not None
    assert reason.startswith("already reported")


@pytest.mark.parametrize(
    "cause",
    [None, TimeoutError("slow"), aiohttp.ClientOSError("reset")],
    ids=["no cause", "timeout", "os error"],
)
def test_only_a_server_disconnect_counts_as_a_dropped_connection(cause: BaseException | None) -> None:
    err = ComexioConnectionError("x")
    err.__cause__ = cause

    assert connection_dropped(err) is False


def test_the_window_is_measured_per_attempt(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 0.3)

    calls: list[int] = []

    async def quick_then_late_drop(*_args: Any) -> None:
        calls.append(1)
        if len(calls) > 1:
            await asyncio.sleep(0.5)
        raise _dropped()

    client.set_marker_value.side_effect = quick_then_late_drop

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 2
    assert (api.write_stats.retries, api.write_stats.failed) == (1, 1)


def test_quick_drops_are_repeated_even_when_together_they_take_longer_than_the_window(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each send drops after 0.2 s, inside the 0.3 s window; counted from the first send, the second drop (0.4 s) is
    # already past it. A fake clock instead of sleeping keeps the margin exact on a slow runner.
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 0.3)
    now = [1000.0]

    class _Clock:
        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

        def monotonic(self) -> float:
            return now[0]

    monkeypatch.setattr(api_module, "time", _Clock())

    async def drop_after_a_while(*_args: Any) -> None:
        now[0] += 0.2
        raise _dropped()

    client.set_marker_value.side_effect = drop_after_a_while

    assert _run(api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1 + API_WRITE_RETRY_MAX
    assert (api.write_stats.retries, api.write_stats.failed) == (API_WRITE_RETRY_MAX, 1)


def test_a_slow_certificate_probe_does_not_use_up_the_window(
    comexio_api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 0.3)

    async def slow_probe() -> None:
        await asyncio.sleep(0.6)

    monkeypatch.setattr(comexio_api, "fetch_replaced_fingerprint", slow_probe)
    client.set_marker_value.side_effect = [_dropped(), None]

    assert _run(comexio_api.set_value("marker", 5, 0)) is True

    assert client.set_marker_value.await_count == 2


def test_a_newer_write_to_the_same_target_waits_and_lands_after_the_repetitions_of_the_older_one(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 10.0)
    sent: list[int] = []

    async def send(_marker: int, value: int) -> None:
        sent.append(value)
        if value == 0:
            await asyncio.sleep(0.02)
            raise _dropped()

    client.set_marker_value.side_effect = send

    async def both() -> list[bool]:
        first = asyncio.create_task(api.set_value("marker", 5, 0))
        await asyncio.sleep(0.01)
        return list(await asyncio.gather(first, api.set_value("marker", 5, 1)))

    assert _run(both()) == [False, True]

    # The last word is the newer write, whatever the older one did before.
    assert sent == [0] * (1 + API_WRITE_RETRY_MAX) + [1]


@pytest.mark.parametrize("other_spelling", ["5", "05", " 5 "])
def test_the_same_target_under_another_id_type_shares_the_lock(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch, other_spelling: str
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 10.0)
    running = 0
    overlapped = False

    async def send(_marker: int, _value: int) -> None:
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        await asyncio.sleep(0.02)
        running -= 1

    client.set_marker_value.side_effect = send

    async def both() -> None:
        await asyncio.gather(api.set_value("marker", 5, 0), api.set_value("marker", other_spelling, 1))

    _run(both())

    assert not overlapped


def test_the_lock_of_a_target_is_dropped_with_its_last_write(api: ComexioAPI, client: MagicMock) -> None:
    held_during_the_write: list[bool] = []

    async def send(_marker: int, _value: int) -> None:
        held_during_the_write.append([lock.locked() for lock in api._write_locks.values()] == [True])

    client.set_marker_value.side_effect = send

    _run(api.set_value("marker", 5, 1))

    assert held_during_the_write == [True]
    assert len(api._write_locks) == 0


def test_a_write_that_must_not_be_repeated_is_sent_once(api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value.side_effect = _dropped()

    assert _run(api.set_value("marker", 5, 1, repeat=False)) is False

    assert client.set_marker_value.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)


def test_a_write_to_another_target_does_not_stop_a_repetition(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 10.0)
    attempts: list[int] = []

    async def send(marker: int, _value: int) -> None:
        if marker == 5 and not attempts:
            attempts.append(marker)
            await asyncio.sleep(0.05)
            raise _dropped()

    client.set_marker_value.side_effect = send

    async def both() -> list[bool]:
        first = asyncio.create_task(api.set_value("marker", 5, 0))
        await asyncio.sleep(0.01)
        return list(await asyncio.gather(first, api.set_value("marker", 6, 1)))

    assert _run(both()) == [True, True]

    assert [c for c in client.set_marker_value.await_args_list if c.args[0] == 6] == [call(6, 1)]  # really sent
    assert api.write_stats.recovered == 1


def test_the_stats_tell_their_listeners_about_retries_and_rescues() -> None:
    stats = ApiWriteStats()
    calls: list[int] = []
    stats.add_listener(lambda: calls.append(1))

    stats.record_retry()
    stats.record_recovered()

    assert calls == [1, 1]


def test_the_retry_limits_are_the_agreed_ones() -> None:
    assert API_WRITE_RETRY_MAX == 3
    assert API_WRITE_RETRY_WINDOW_SEC == 0.5


def test_the_coordinator_hands_its_cache_lookup_to_the_api() -> None:
    init = ast.parse(textwrap.dedent(inspect.getsource(ComexioCoordinator.__init__)))
    wired = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "self.api.cached_value"
        and ast.unparse(node.value) == "self.cached_value"
    ]

    assert len(wired) == 1


def test_a_dropped_write_with_a_foreign_certificate_behind_it_is_not_repeated(
    comexio_api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comexio_api, "fingerprint", "aa")
    monkeypatch.setattr(comexio_api, "fetch_replaced_fingerprint", AsyncMock(return_value="bb"))
    client.set_marker_value.side_effect = _dropped()

    assert _run(comexio_api.set_value("marker", 5, 0)) is False

    assert client.set_marker_value.await_count == 1
    assert (comexio_api.write_stats.failed, comexio_api.write_stats.retries) == (1, 0)
    assert comexio_api.write_stats.last_failures[0].error == ComexioCertificateMismatchError.__name__


@pytest.mark.parametrize(
    ("target_type", "target_id", "ext", "identifier"),
    [("knx", 9, None, None), ("io", "103", "LED-Dimmer A3", "Q5")],
)
def test_a_drop_is_not_repeated_for_a_knx_object_or_io_that_already_holds_the_value(
    api: ComexioAPI,
    client: MagicMock,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
) -> None:
    # The push of the wanted value came in while the write was under way.
    api.cached_value = lambda _type, _id: 1 if client.set_knx_value.await_count + client.set_io_value.await_count else 0
    client.set_knx_value.side_effect = _dropped()
    client.set_io_value.side_effect = _dropped()

    assert _run(api.set_value(target_type, target_id, 1, ext, identifier)) is False

    assert client.set_knx_value.await_count + client.set_io_value.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)


def test_an_io_write_without_identifier_is_not_repeated(api: ComexioAPI, client: MagicMock) -> None:
    assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", None)) is False

    assert client.set_io_value.await_count == 0
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)


def test_a_multiline_target_id_stays_on_one_line_in_every_log_line(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.DEBUG, logger=api_module.__name__):
        _run(api.set_value("marker", "5\nforged line", 0))

    assert caplog.records
    assert all("\n" not in record.getMessage() for record in caplog.records)


def test_a_recovered_write_is_logged_once_at_info_level(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = [_dropped(), None]

    with caplog.at_level(logging.INFO, logger=api_module.__name__):
        _run(api.set_value("marker", 5, 0))

    (record,) = caplog.records
    assert "went through after 1 retry(ies)" in record.getMessage()


def test_the_error_line_of_a_write_that_was_repeated_says_so(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        _run(api.set_value("marker", 5, 0))

    (record,) = caplog.records
    assert f"after {API_WRITE_RETRY_MAX} repetition(s)" in record.getMessage()


def _trigger_button(set_value: AsyncMock) -> ComexioMarkerTriggerButton:
    button = ComexioMarkerTriggerButton.__new__(ComexioMarkerTriggerButton)
    button.coordinator = SimpleNamespace(  # type: ignore[assignment]
        data={"markers": [{"id": "5", "kind": MarkerKind.TRIGGER}]}, api=SimpleNamespace(set_value=set_value)
    )
    button._marker_id = "5"
    return button


def test_a_trigger_marker_is_written_once_because_it_toggles() -> None:
    set_value = AsyncMock(return_value=True)

    _run(_trigger_button(set_value).async_press())

    set_value.assert_awaited_once_with("marker", "5", 1, repeat=False)


@pytest.mark.parametrize(
    "data",
    [{"markers": [{"id": 5, "kind": MarkerKind.NORMAL}]}, {"markers": []}, None],
    ids=["no longer a trigger", "gone from the latest poll", "no poll data"],
)
def test_a_trigger_button_whose_marker_is_no_longer_a_trigger_writes_nothing(data: dict[str, Any] | None) -> None:
    set_value = AsyncMock(return_value=True)
    button = _trigger_button(set_value)
    button.coordinator.data = data  # type: ignore[misc]

    press = button.async_press()
    with pytest.raises(HomeAssistantError, match="no longer a trigger"):
        _run(press)

    set_value.assert_not_awaited()


def _marker_entity(set_value: AsyncMock, data: dict[str, Any] | None) -> entity_module.ComexioMarkerEntity:
    entity = entity_module.ComexioMarkerEntity.__new__(entity_module.ComexioMarkerEntity)
    entity.coordinator = SimpleNamespace(data=data, api=SimpleNamespace(set_value=set_value))  # type: ignore[assignment]
    entity._marker_id = "5"
    return entity


@pytest.mark.parametrize("repeat", [True, False])
def test_a_plain_marker_write_is_repeatable_as_the_caller_asked(repeat: bool) -> None:
    set_value = AsyncMock(return_value=True)
    entity = _marker_entity(set_value, {"markers": [{"id": 5, "kind": MarkerKind.NORMAL}]})

    # Parsed marker ids are ints, the entity keeps its id as a string.
    _run(entity._async_source_write(1, repeat=repeat))

    set_value.assert_awaited_once_with("marker", "5", 1, repeat=repeat)


@pytest.mark.parametrize(
    "data",
    [{"markers": [{"id": 5, "kind": MarkerKind.TRIGGER}]}, {"markers": []}, None],
    ids=["has since become a trigger", "gone from the latest poll", "no poll data"],
)
def test_a_marker_write_is_sent_once_unless_the_latest_poll_shows_a_plain_marker(data: dict[str, Any] | None) -> None:
    # The entity outlives the title edit that added [TRIG] until the next reload; the plan input toggles per edge.
    # A marker the poll no longer has may be anything by now, so it is not repeated either.
    set_value = AsyncMock(return_value=True)

    _run(_marker_entity(set_value, data)._async_source_write(1))

    set_value.assert_awaited_once_with("marker", "5", 1, repeat=False)


def test_a_knx_trigger_write_reaches_the_bridge_marker_without_repetition() -> None:
    set_value = AsyncMock(return_value=True)
    coordinator = SimpleNamespace(
        _knx_bridge_marker_by_k_id=lambda: {"9": "77"}, api=SimpleNamespace(set_value=set_value)
    )

    write = entity_module._async_write_via_bridge(coordinator, "KNX 9", "9", 1, repeat=False)  # type: ignore[arg-type]

    assert _run(write) is True

    set_value.assert_awaited_once_with("marker", "77", 1, repeat=False, even_if_held=False)


def test_a_knx_trigger_button_writes_its_bridge_marker_once() -> None:
    set_value = AsyncMock(return_value=True)
    button = ComexioKnxTriggerButton.__new__(ComexioKnxTriggerButton)
    data_key = SOURCE_CATEGORIES[button._SOURCE].data_key
    button.coordinator = SimpleNamespace(  # type: ignore[assignment]
        data={data_key: [{"id": "9", "kind": MarkerKind.TRIGGER}]},
        _knx_bridge_marker_by_k_id=lambda: {"9": "77"},
        api=SimpleNamespace(set_value=set_value),
    )
    button._marker_id = "9"

    _run(button.async_press())

    set_value.assert_awaited_once_with("marker", "77", 1, repeat=False, even_if_held=False)


@pytest.mark.parametrize(("kind", "repeat"), [(MarkerKind.NORMAL, True), (MarkerKind.TRIGGER, False)])
def test_a_knx_write_is_repeatable_unless_the_object_is_a_trigger_by_now(kind: MarkerKind, repeat: bool) -> None:
    set_value = AsyncMock(return_value=True)
    entity = entity_module.ComexioKnxEntity.__new__(entity_module.ComexioKnxEntity)
    entity.coordinator = SimpleNamespace(  # type: ignore[assignment]
        data={SOURCE_CATEGORIES[entity._SOURCE].data_key: [{"id": "9", "kind": kind}]},
        _knx_bridge_marker_by_k_id=lambda: {"9": "77"},
        api=SimpleNamespace(set_value=set_value),
    )
    entity._marker_id = "9"

    _run(entity._async_source_write(1))

    set_value.assert_awaited_once_with("marker", "77", 1, repeat=repeat, even_if_held=False)


def test_a_write_to_another_target_does_not_wait_for_the_repetitions_of_this_one(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "API_WRITE_RETRY_WINDOW_SEC", 10.0)

    async def scenario() -> bool:
        release = asyncio.Event()
        repeating = asyncio.Event()
        sends_to_5 = 0

        async def send(marker: int, _value: int) -> None:
            nonlocal sends_to_5
            if marker != 5:
                return
            sends_to_5 += 1
            if sends_to_5 == 1:
                raise _dropped()
            repeating.set()  # marker 5 is now in its repetition, and that one hangs
            await release.wait()

        client.set_marker_value.side_effect = send
        blocked = asyncio.create_task(api.set_value("marker", 5, 0))
        await asyncio.wait_for(repeating.wait(), 1)
        other = await asyncio.wait_for(api.set_value("marker", 6, 1), 1)
        release.set()
        return other and await blocked

    assert _run(scenario()) is True
    assert api.write_stats.retries == 1


def test_a_write_that_is_not_repeated_does_not_claim_a_repetition_in_its_error_line(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    client.set_marker_value.side_effect = _dropped()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        _run(api.set_value("marker", 5, 0, repeat=False))

    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "repetition" not in record.getMessage()


@pytest.mark.parametrize(
    ("target_type", "target_id", "ext", "identifier", "method"),
    [("knx", 9, None, None, "set_knx_value"), ("io", "103", "LED-Dimmer A3", "Q5", "set_io_value")],
)
def test_a_drop_is_repeated_for_a_knx_object_or_io_that_does_not_yet_hold_the_value(
    api: ComexioAPI,
    client: MagicMock,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
    method: str,
) -> None:
    api.cached_value = lambda _type, _id: 0
    getattr(client, method).side_effect = [_dropped(), None]

    assert _run(api.set_value(target_type, target_id, 1, ext, identifier)) is True

    assert getattr(client, method).await_count == 2
    assert api.write_stats.retries == 1


def test_a_write_issued_after_the_entry_was_unloaded_is_not_sent(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # The session is gone with the entry: aiohttp would answer the request with a RuntimeError no caller expects.
    api.close()

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 5, 1)) is False

    assert client.set_marker_value.await_count == 0
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line == "Comexio API write failed (marker 5): the entry was unloaded: not sent"
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)


def test_a_repetition_is_not_sent_once_the_entry_was_unloaded(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    async def drop_and_unload(_marker: int, _value: int) -> None:
        api.close()
        raise _dropped()

    client.set_marker_value.side_effect = drop_and_unload

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 5, 1)) is False

    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.endswith("(not sent again: the entry was unloaded meanwhile)")
    assert client.set_marker_value.await_count == 1
    assert (api.write_stats.failed, api.write_stats.retries) == (1, 0)  # a repetition that is not sent is not counted


def test_an_unloaded_entry_is_the_reason_named_even_if_the_value_changed_too(
    api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    held = {"value": 0}
    api.cached_value = lambda _type, _id: held["value"]

    async def drop_unload_and_push(_marker: int, _value: int) -> None:
        api.close()
        held["value"] = 7
        raise _dropped()

    client.set_marker_value.side_effect = drop_unload_and_push

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("marker", 5, 1)) is False

    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert line.endswith("(not sent again: the entry was unloaded meanwhile)")


@pytest.mark.parametrize(
    ("target_type", "target_id", "ext", "identifier", "method"),
    [("knx", 9, None, None, "set_knx_value"), ("io", "103", "LED-Dimmer A3", "Q5", "set_io_value")],
)
def test_no_kind_of_write_is_sent_after_the_entry_was_unloaded(
    api: ComexioAPI,
    client: MagicMock,
    target_type: str,
    target_id: str | int,
    ext: str | None,
    identifier: str | None,
    method: str,
) -> None:
    api.close()

    assert _run(api.set_value(target_type, target_id, 1, ext, identifier)) is False

    assert getattr(client, method).await_count == 0


@pytest.mark.parametrize("login_result", [True, False], ids=["login went through", "login failed for the unload"])
def test_an_analog_output_write_whose_login_ends_with_the_unload_is_reported_not_raised(
    api: ComexioAPI,
    client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    login_result: bool,
) -> None:
    # The lapsed admin session is renewed, the entry unloads meanwhile: the repetition would meet the closed session,
    # and a login that fails because of it must not be reported as a refusal by Comexio.
    client.set_io_value.side_effect = ComexioAuthenticationError("lapsed")

    async def login_then_unload(_generation: int) -> bool:
        api.close()
        return login_result

    monkeypatch.setattr(api, "_full_login", login_then_unload)

    with caplog.at_level(logging.ERROR, logger=api_module.__name__):
        assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", "AO1")) is False

    assert client.set_io_value.await_count == 1  # not sent again to the closed session
    assert api.write_stats.failed == 1  # counted, as every failed write
    (line,) = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert "the entry was unloaded meanwhile: not repeated" in line


def test_an_analog_output_write_unloaded_while_in_flight_does_not_log_in_again(
    api: ComexioAPI, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lapse_while_unloading(_ext: str, _identifier: str, _value: int) -> None:
        api.close()
        raise ComexioAuthenticationError("lapsed")

    client.set_io_value.side_effect = lapse_while_unloading
    full_login = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "_full_login", full_login)

    assert _run(api.set_value("io", "103", 0, "LED-Dimmer A3", "AO1")) is False

    full_login.assert_not_awaited()  # the detached session is not logged in on
    assert api.write_stats.failed == 1
