"""The periodic reads end quietly once Home Assistant closed the connections (#289, open point eh).

Home Assistant's CLOSE event closes the connector of every session while the entry is still loaded and its timers
still fire. aiohttp then refuses a request with RuntimeError("Session is closed"), which the ticks did not catch and
Home Assistant logged as "Task exception was never retrieved".
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import inspect
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioConnectionError
import pytest

from custom_components.comexio import coordinator as coordinator_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.coordinator import ComexioCoordinator

SESSION_CLOSED = "Session is closed"
OTHER_ERROR = "something else"
SERVER_ID = "iosrv1"
# (tick, the aiocomexio client method its first request goes through)
TICKS: list[tuple[Callable[..., Awaitable[None]], str]] = [
    (ComexioCoordinator._async_bus_load_tick, "get_bus_workload"),
    (ComexioCoordinator._async_system_info_tick, "get_uptime"),
]
TICK_IDS = ["bus load", "uptime"]
# Every periodic read of Comexio that carries the guard, the preview poll included.
GUARDED_TICKS = [
    ComexioCoordinator._async_bus_load_tick,
    ComexioCoordinator._async_system_info_tick,
    ComexioCoordinator._async_plan_run_state_tick,
    ComexioCoordinator._async_poll_connection_values,
]
HOW_THE_SESSION_GOES = ["home assistant stops", "entry unloads"]


def _ha_closes_the_connections(api: ComexioAPI) -> None:
    api.session.closed = True  # type: ignore[misc]


def _entry_unloads(api: ComexioAPI) -> None:
    api.close()


CLOSERS = [_ha_closes_the_connections, _entry_unloads]


def _fake_coordinator(api: ComexioAPI) -> Any:
    return SimpleNamespace(api=api, server_id=SERVER_ID, _bus_load_fail_streak=2, bus_workload=None, hass=MagicMock())


def _fake_plan_coordinator(api: ComexioAPI, refresh: AsyncMock) -> Any:
    return SimpleNamespace(
        api=api, server_id=SERVER_ID, _preview_plan_cache=None, _async_refresh_plan_run_states=refresh
    )


@pytest.fixture
def client(comexio_api: ComexioAPI) -> MagicMock:
    fake = MagicMock()
    comexio_api._client = fake
    comexio_api._client_credentials = comexio_api._credentials()
    return fake


@pytest.fixture
def dispatched() -> Any:
    with patch.object(coordinator_module, "async_dispatcher_send") as send:
        yield send


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
@pytest.mark.parametrize(("tick", "method"), TICKS, ids=TICK_IDS)
def test_a_tick_that_starts_after_the_connections_are_closed_sends_nothing(
    comexio_api: ComexioAPI,
    client: MagicMock,
    tick: Callable[..., Awaitable[None]],
    method: str,
    close: Callable[[ComexioAPI], None],
) -> None:
    close(comexio_api)
    request = AsyncMock()
    setattr(client, method, request)

    asyncio.run(tick(_fake_coordinator(comexio_api)))

    request.assert_not_awaited()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
@pytest.mark.parametrize(("tick", "method"), TICKS, ids=TICK_IDS)
def test_a_tick_whose_request_meets_the_closed_session_ends_quietly(
    comexio_api: ComexioAPI,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    dispatched: MagicMock,
    tick: Callable[..., Awaitable[None]],
    method: str,
    close: Callable[[ComexioAPI], None],
) -> None:
    # The request was under way, or the tick started just before the stop: aiohttp refuses it on the closed connector.
    async def stopped_meanwhile(*_args: object) -> None:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    request = AsyncMock(side_effect=stopped_meanwhile)
    setattr(client, method, request)
    fake = _fake_coordinator(comexio_api)

    # get_bus_workload only catches ComexioError itself, so the refusal reaches the tick; the uptime read is reached
    # through get_system_info, which does the same.
    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        asyncio.run(tick(fake))

    request.assert_awaited_once()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    noted = [record for record in caplog.records if SESSION_CLOSED in record.getMessage()]
    assert len(noted) == 1
    assert tick.__name__ in noted[0].getMessage()
    # Nothing was made of the refusal: no reading, no failure counted, no signal sent.
    assert fake._bus_load_fail_streak == 2
    assert fake.bus_workload is None
    dispatched.assert_not_called()


@pytest.mark.parametrize(("tick", "method"), TICKS, ids=TICK_IDS)
def test_the_closed_session_text_on_an_open_session_is_not_swallowed(
    comexio_api: ComexioAPI, client: MagicMock, tick: Callable[..., Awaitable[None]], method: str
) -> None:
    # Only a session that really is gone explains the refusal; on an open one it is a bug and stays visible.
    setattr(client, method, AsyncMock(side_effect=RuntimeError(SESSION_CLOSED)))
    running = tick(_fake_coordinator(comexio_api))

    with pytest.raises(RuntimeError, match=SESSION_CLOSED):
        asyncio.run(running)


@pytest.mark.parametrize(("tick", "method"), TICKS, ids=TICK_IDS)
def test_another_runtime_error_is_not_swallowed_after_the_close(
    comexio_api: ComexioAPI, client: MagicMock, tick: Callable[..., Awaitable[None]], method: str
) -> None:
    async def stopped_meanwhile(*_args: object) -> None:
        _ha_closes_the_connections(comexio_api)
        raise RuntimeError(OTHER_ERROR)

    setattr(client, method, AsyncMock(side_effect=stopped_meanwhile))
    running = tick(_fake_coordinator(comexio_api))

    with pytest.raises(RuntimeError, match=OTHER_ERROR):
        asyncio.run(running)


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_the_plan_run_state_tick_sends_nothing_after_the_close(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    close(comexio_api)
    refresh = AsyncMock()

    asyncio.run(ComexioCoordinator._async_plan_run_state_tick(_fake_plan_coordinator(comexio_api, refresh)))

    refresh.assert_not_awaited()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_the_plan_run_state_tick_ends_quietly_when_the_refresh_meets_the_closed_session(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None]
) -> None:
    async def stopped_meanwhile(*_args: object) -> None:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    refresh = AsyncMock(side_effect=stopped_meanwhile)

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        asyncio.run(ComexioCoordinator._async_plan_run_state_tick(_fake_plan_coordinator(comexio_api, refresh)))

    refresh.assert_awaited_once()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len([record for record in caplog.records if SESSION_CLOSED in record.getMessage()]) == 1


@pytest.mark.parametrize("error", [SESSION_CLOSED, OTHER_ERROR], ids=["closed text, open session", "other error"])
def test_the_plan_run_state_tick_does_not_swallow_what_is_no_closed_session(
    comexio_api: ComexioAPI, error: str
) -> None:
    # The session is open in both cases (the closed text alone proves nothing); the other error is a bug anyway.
    refresh = AsyncMock(side_effect=RuntimeError(error))
    running = ComexioCoordinator._async_plan_run_state_tick(_fake_plan_coordinator(comexio_api, refresh))

    with pytest.raises(RuntimeError, match=error):
        asyncio.run(running)


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_the_preview_poll_sends_nothing_after_the_close(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    close(comexio_api)
    ensure = AsyncMock()
    comexio_api.ensure_preview_session = ensure  # type: ignore[method-assign]
    fake = SimpleNamespace(api=comexio_api, server_id=SERVER_ID, _preview_plan_cache={"fub_id": 1})

    asyncio.run(ComexioCoordinator._async_poll_connection_values(fake, datetime.now(UTC)))

    ensure.assert_not_awaited()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
@pytest.mark.parametrize(
    "error",
    [RuntimeError(SESSION_CLOSED), ComexioConnectionError("dropped"), TimeoutError()],
    ids=["aiohttp refusal", "transport error", "timeout"],
)
def test_the_preview_poll_counts_no_failure_when_the_connections_close_under_its_request(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None], error: Exception
) -> None:
    # A request in flight when the connector closes fails as aiohttp's refusal, a transport error or a timeout.
    async def stopped_meanwhile(*_args: object, **_kwargs: object) -> None:
        close(comexio_api)
        raise error

    comexio_api.ensure_preview_session = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    cache = {"fub_id": 1}
    fake = SimpleNamespace(
        api=comexio_api, server_id=SERVER_ID, _preview_plan_cache=cache, _connection_poll_fail_count=0
    )

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        asyncio.run(ComexioCoordinator._async_poll_connection_values(fake, datetime.now(UTC)))

    assert fake._connection_poll_fail_count == 0
    assert fake._preview_plan_cache is cache
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record for record in caplog.records if "ended with the session" in record.getMessage()]


@pytest.mark.parametrize(
    ("error", "close"),
    [
        (RuntimeError(OTHER_ERROR), None),
        (RuntimeError(SESSION_CLOSED), None),
        (ComexioConnectionError("dropped"), None),
        (RuntimeError(OTHER_ERROR), _ha_closes_the_connections),
        (ValueError(SESSION_CLOSED), _ha_closes_the_connections),
    ],
    ids=[
        "other error, open session",
        "closed text, open session",
        "transport error, open session",
        "other error after close",
        "not a RuntimeError",
    ],
)
def test_the_preview_poll_still_counts_a_failure_that_is_no_closed_session(
    comexio_api: ComexioAPI,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    close: Callable[[ComexioAPI], None] | None,
) -> None:
    # A session that is gone does not explain an unrelated error: it stays counted and logged.
    # The close comes while the request is under way (before the start the tick would not run at all).
    async def fails(*_args: object, **_kwargs: object) -> None:
        if close is not None:
            close(comexio_api)
        raise error

    comexio_api.ensure_preview_session = AsyncMock(side_effect=fails)  # type: ignore[method-assign]
    fake = SimpleNamespace(
        api=comexio_api,
        server_id=SERVER_ID,
        _preview_plan_cache={"fub_id": 1},
        _connection_poll_fail_count=0,
        _async_refresh_run_states_in_preview=AsyncMock(),
    )

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        asyncio.run(ComexioCoordinator._async_poll_connection_values(fake, datetime.now(UTC)))

    assert fake._connection_poll_fail_count == 1
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


@pytest.mark.parametrize("tick", GUARDED_TICKS, ids=lambda tick: tick.__name__)
def test_every_guarded_tick_stays_a_coroutine_function_under_its_own_name(tick: Callable[..., Awaitable[None]]) -> None:
    # Home Assistant picks how to run a timer callback from the function itself: a plain wrapper that merely returns
    # a coroutine would be run in its event loop thread without being awaited properly.
    assert inspect.iscoroutinefunction(tick)
    assert tick.__name__.startswith("_async_")


def test_the_guard_passes_the_timer_argument_on(comexio_api: ComexioAPI) -> None:
    # async_track_time_interval calls the tick with the time as its only argument.
    seen: list[object] = []

    class Fake:
        api = comexio_api

        @coordinator_module._skip_when_connections_closed
        async def tick(self, now: datetime | None = None) -> None:
            seen.append(now)

    now = datetime.now(UTC)
    asyncio.run(Fake().tick(now))
    asyncio.run(Fake().tick(now=now))

    assert seen == [now, now]
