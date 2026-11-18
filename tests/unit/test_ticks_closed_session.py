"""The periodic reads end quietly once Home Assistant closed the connections (#289, open point eh).

Home Assistant's CLOSE event closes the connector of every session while the entry is still loaded and its timers
still fire. aiohttp then refuses a request with RuntimeError("Session is closed"), which the ticks did not catch and
Home Assistant logged as "Task exception was never retrieved".
"""

import asyncio
import collections
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import functools
import inspect
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioAuthenticationError, ComexioConnectionError, ComexioDataError
import aiohttp
import pytest

from custom_components.comexio import api as api_module, coordinator as coordinator_module
from custom_components.comexio.api import ComexioAPI, SystemInfoReading
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
    [
        RuntimeError(SESSION_CLOSED),
        ComexioConnectionError("dropped"),
        TimeoutError(),
        aiohttp.ServerDisconnectedError(),
    ],
    ids=["aiohttp refusal", "transport error", "timeout", "server disconnected"],
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
        (aiohttp.ClientPayloadError("cut"), _ha_closes_the_connections),
    ],
    ids=[
        "other error, open session",
        "closed text, open session",
        "transport error, open session",
        "other error after close",
        "not a RuntimeError",
        "payload error after close",
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


def _fake_run_state_coordinator(api: ComexioAPI) -> Any:
    fake = SimpleNamespace(
        api=api,
        server_id=SERVER_ID,
        _plan_run_state_fetching=False,
        _plan_run_state_poll_blocked=lambda: False,
        _plan_run_state_last_fetch=0.0,
        _plan_run_state_fail_streak=2,
        _plan_run_state_stale_count=2,
        note_comexio_unreachable=MagicMock(),
        _async_plan_run_state_fetch_failed=AsyncMock(),
        _async_watch_plans_and_refresh=AsyncMock(),
    )
    # The real error accounting runs on the fake's own mocks.
    fake._async_count_run_state_fetch_error = functools.partial(
        ComexioCoordinator._async_count_run_state_fetch_error, fake
    )
    return fake


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
@pytest.mark.parametrize(
    "error",
    [ComexioConnectionError("dropped"), TimeoutError(), aiohttp.ClientConnectionError("dropped")],
    ids=["transport error", "timeout", "connection error"],
)
def test_the_run_state_fetch_counts_no_outage_when_the_connections_close_under_its_request(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None], error: Exception
) -> None:
    # The request in flight fails as a transport error or a timeout (aiohttp's refusal is the tick guard's business).
    async def stopped_meanwhile(*_args: object, **_kwargs: object) -> None:
        close(comexio_api)
        raise error

    comexio_api.get_function_plan_run_states = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_run_state_coordinator(comexio_api)

    with (
        patch.object(coordinator_module, "function_plan_ids", return_value=[1]),
        caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__),
    ):
        asyncio.run(ComexioCoordinator._async_refresh_plan_run_states(fake))

    fake.note_comexio_unreachable.assert_not_called()
    fake._async_plan_run_state_fetch_failed.assert_not_awaited()
    fake._async_watch_plans_and_refresh.assert_not_awaited()
    assert fake._plan_run_state_fetching is False
    assert (fake._plan_run_state_fail_streak, fake._plan_run_state_stale_count) == (2, 2)
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record for record in caplog.records if "ended with the session" in record.getMessage()]


def test_the_run_state_fetch_still_counts_an_outage_while_the_session_is_open(comexio_api: ComexioAPI) -> None:
    comexio_api.get_function_plan_run_states = AsyncMock(  # type: ignore[method-assign]
        side_effect=ComexioConnectionError("dropped")
    )
    fake = _fake_run_state_coordinator(comexio_api)

    with patch.object(coordinator_module, "function_plan_ids", return_value=[1]):
        asyncio.run(ComexioCoordinator._async_refresh_plan_run_states(fake))

    fake.note_comexio_unreachable.assert_called_once()
    fake._async_plan_run_state_fetch_failed.assert_awaited_once()


def _fake_refresh_coordinator(api: ComexioAPI, run: AsyncMock, data: object) -> Any:
    return SimpleNamespace(
        api=api, server_id=SERVER_ID, data=data, _polls_open=0, _poll_overlap=False, _async_run_update=run
    )


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_refresh_that_meets_the_closed_session_keeps_the_data(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None]
) -> None:
    async def stopped_meanwhile() -> None:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    data = {"markers": []}
    fake = _fake_refresh_coordinator(comexio_api, AsyncMock(side_effect=stopped_meanwhile), data)

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        result = asyncio.run(ComexioCoordinator._async_update_data(fake))

    assert result is data
    assert fake._polls_open == 0
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("error", "close", "data"),
    [
        (SESSION_CLOSED, None, {"markers": []}),
        (OTHER_ERROR, _ha_closes_the_connections, {"markers": []}),
        (SESSION_CLOSED, _ha_closes_the_connections, None),
    ],
    ids=["closed text, open session", "other error after close", "first refresh without data"],
)
def test_a_refresh_error_that_is_no_closed_session_stays_visible(
    comexio_api: ComexioAPI, error: str, close: Callable[[ComexioAPI], None] | None, data: object
) -> None:
    async def fails() -> None:
        if close is not None:
            close(comexio_api)
        raise RuntimeError(error)

    fake = _fake_refresh_coordinator(comexio_api, AsyncMock(side_effect=fails), data)
    running = ComexioCoordinator._async_update_data(fake)

    with pytest.raises(RuntimeError, match=error):
        asyncio.run(running)
    assert fake._polls_open == 0


@pytest.mark.parametrize(
    "error",
    [
        ComexioDataError("unreadable"),
        ComexioAuthenticationError("refused"),
        aiohttp.ClientResponseError(MagicMock(), (), status=500),
        aiohttp.ClientPayloadError("cut"),
    ],
    ids=["data error", "authentication error", "response error", "payload error"],
)
def test_the_run_state_fetch_still_counts_an_error_that_is_no_disconnect_after_the_close(
    comexio_api: ComexioAPI, error: Exception
) -> None:
    # A session that is gone explains a dropped request, not an answer Comexio gave or a foreign error.
    async def stopped_meanwhile(*_args: object, **_kwargs: object) -> None:
        _ha_closes_the_connections(comexio_api)
        raise error

    comexio_api.get_function_plan_run_states = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_run_state_coordinator(comexio_api)

    with patch.object(coordinator_module, "function_plan_ids", return_value=[1]):
        asyncio.run(ComexioCoordinator._async_refresh_plan_run_states(fake))

    fake._async_plan_run_state_fetch_failed.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [ComexioConnectionError("dropped"), TimeoutError()], ids=["transport error", "timeout"]
)
def test_a_refresh_leaves_a_transport_error_to_home_assistant_after_the_close(
    comexio_api: ComexioAPI, error: Exception
) -> None:
    # Only aiohttp's refusal is turned into "keep the data"; a transport error or timeout is the refresh's own failure
    # handling (UNREACHABLE_ERRORS in _async_run_update) and passes through.
    async def stopped_meanwhile() -> None:
        _ha_closes_the_connections(comexio_api)
        raise error

    fake = _fake_refresh_coordinator(comexio_api, AsyncMock(side_effect=stopped_meanwhile), {"markers": []})
    running = ComexioCoordinator._async_update_data(fake)

    with pytest.raises(type(error)):
        asyncio.run(running)
    assert fake._polls_open == 0


def _fake_run_update_coordinator(api: ComexioAPI, fetch: AsyncMock) -> Any:
    return SimpleNamespace(
        api=api,
        server_id=SERVER_ID,
        onewire_status=None,
        _full_poll_running=False,
        _scraped_config=None,
        _async_fetch_and_audit=fetch,
        _note_system_info_dropped=MagicMock(),
        system_info=SimpleNamespace(apply_storage=MagicMock(return_value=[])),
    )


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_refresh_that_ends_with_the_session_counts_no_failed_storage_fetch(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    async def stopped_meanwhile() -> None:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    fake = _fake_run_update_coordinator(comexio_api, AsyncMock(side_effect=stopped_meanwhile))
    running = ComexioCoordinator._async_run_update(fake)

    with pytest.raises(RuntimeError, match=SESSION_CLOSED):
        asyncio.run(running)

    fake.system_info.apply_storage.assert_not_called()
    fake._note_system_info_dropped.assert_not_called()
    assert fake._full_poll_running is False


def test_a_failed_refresh_on_an_open_session_still_counts_a_failed_storage_fetch(comexio_api: ComexioAPI) -> None:
    fake = _fake_run_update_coordinator(comexio_api, AsyncMock(side_effect=RuntimeError(OTHER_ERROR)))
    running = ComexioCoordinator._async_run_update(fake)

    with pytest.raises(RuntimeError, match=OTHER_ERROR):
        asyncio.run(running)

    fake.system_info.apply_storage.assert_called_once_with(None)
    fake._note_system_info_dropped.assert_called_once()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_preview_tick_that_waited_for_the_lock_opens_no_session_once_the_connections_closed(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    comexio_api._login = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def run() -> object:
        async with comexio_api._preview_session_lock:
            queued = asyncio.create_task(comexio_api.ensure_preview_session())
            await asyncio.sleep(0)
            close(comexio_api)
        return await queued

    with patch.object(api_module, "async_create_session") as create:
        assert asyncio.run(run()) is None

    create.assert_not_called()
    comexio_api._login.assert_not_awaited()
    assert comexio_api._preview_session is None


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_preview_session_logged_in_while_the_connections_close_is_not_kept(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    async def login_while_closing(*_args: object) -> bool:
        close(comexio_api)
        return True

    comexio_api._login = AsyncMock(side_effect=login_while_closing)  # type: ignore[method-assign]

    with (
        patch.object(api_module, "async_create_session") as create,
        patch.object(api_module, "discard_session") as discard,
        patch.object(comexio_api, "_new_client"),
    ):
        assert asyncio.run(comexio_api.ensure_preview_session()) is None

    # close() drops the main session through the same function; only the preview session matters here.
    discard.assert_any_call(create.return_value, unused=True)
    assert comexio_api._preview_session is None
    assert comexio_api._preview_client is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError(SESSION_CLOSED), True),
        (RuntimeError(OTHER_ERROR), False),
        (ComexioConnectionError("dropped"), True),
        (TimeoutError(), True),
        (aiohttp.ServerDisconnectedError(), True),
        (aiohttp.ClientPayloadError("cut"), False),
        (aiohttp.ClientResponseError(MagicMock(), (), status=500), False),
        (ComexioDataError("unreadable"), False),
        (ComexioAuthenticationError("refused"), False),
        (ValueError(SESSION_CLOSED), False),
    ],
    ids=[
        "aiohttp refusal",
        "other runtime error",
        "transport error",
        "timeout",
        "server disconnected",
        "payload error",
        "response error",
        "data error",
        "authentication error",
        "not a RuntimeError",
    ],
)
@pytest.mark.parametrize("close", [*CLOSERS, None], ids=[*HOW_THE_SESSION_GOES, "session open"])
def test_what_ends_with_the_session_is_told_apart_for_every_caller(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None] | None, error: Exception, expected: bool
) -> None:
    # The one rule behind the settings read after a failed write, the preview poll and the run-state fetch.
    if close is not None:
        close(comexio_api)

    assert comexio_api.read_ended_with_the_session(error) is (expected and close is not None)


_STORAGE_READING = SystemInfoReading(MagicMock(), None)


def _fake_reading_coordinator(api: ComexioAPI) -> Any:
    return SimpleNamespace(
        api=api,
        server_id=SERVER_ID,
        _bus_load_fail_streak=coordinator_module.BUS_LOAD_FAIL_STREAK_THRESHOLD - 1,
        bus_workload=7,
        bus_sd_card=True,
        hass=MagicMock(),
        system_info=SimpleNamespace(apply_uptime=MagicMock(return_value=[]), apply_storage=MagicMock(return_value=[])),
        _note_system_info_dropped=MagicMock(),
        _restore_webhook_migration_notices=MagicMock(),
    )


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_bus_load_read_that_ends_with_the_session_does_not_advance_the_failure_streak(
    comexio_api: ComexioAPI, dispatched: MagicMock, close: Callable[[ComexioAPI], None]
) -> None:
    # aiocomexio turns the failed read into {}: the tick sees no exception, only an empty answer.
    async def stopped_meanwhile() -> dict[str, object]:
        close(comexio_api)
        return {}

    comexio_api.get_bus_workload = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_bus_load_tick(fake))

    assert fake._bus_load_fail_streak == coordinator_module.BUS_LOAD_FAIL_STREAK_THRESHOLD - 1
    assert (fake.bus_workload, fake.bus_sd_card) == (7, True)
    dispatched.assert_not_called()


def test_an_empty_bus_load_read_on_an_open_session_still_advances_the_failure_streak(
    comexio_api: ComexioAPI, dispatched: MagicMock
) -> None:
    comexio_api.get_bus_workload = AsyncMock(return_value={})  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_bus_load_tick(fake))

    assert fake._bus_load_fail_streak == coordinator_module.BUS_LOAD_FAIL_STREAK_THRESHOLD
    assert (fake.bus_workload, fake.bus_sd_card) == (None, None)
    dispatched.assert_called_once()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_an_uptime_read_that_ends_with_the_session_counts_no_failure(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    async def stopped_meanwhile(**_kwargs: object) -> SystemInfoReading:
        close(comexio_api)
        return SystemInfoReading(None, None, None, "dropped")

    comexio_api.get_system_info = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_system_info_tick(fake))

    fake.system_info.apply_uptime.assert_not_called()
    fake._note_system_info_dropped.assert_not_called()
    fake._restore_webhook_migration_notices.assert_not_called()


def test_a_failed_uptime_read_on_an_open_session_is_still_counted(comexio_api: ComexioAPI) -> None:
    comexio_api.get_system_info = AsyncMock(  # type: ignore[method-assign]
        return_value=SystemInfoReading(None, None, None, "dropped")
    )
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_system_info_tick(fake))

    fake.system_info.apply_uptime.assert_called_once()
    fake._note_system_info_dropped.assert_called_once()
    fake._restore_webhook_migration_notices.assert_called_once()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_read_that_succeeded_is_applied_although_the_session_closed_meanwhile(
    comexio_api: ComexioAPI, dispatched: MagicMock, close: Callable[[ComexioAPI], None]
) -> None:
    # Only a read that FAILED while the session went is dropped; an answer that came back is still a valid reading.
    async def answered_then_closed(**kwargs: object) -> SystemInfoReading:
        close(comexio_api)
        return SystemInfoReading(None, timedelta(hours=1)) if kwargs.get("storage") is False else _STORAGE_READING

    comexio_api.get_system_info = AsyncMock(side_effect=answered_then_closed)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    # The uptime tick starts on an open session (the guard would skip it otherwise); the storage read is no tick.
    asyncio.run(ComexioCoordinator._async_system_info_tick(fake))
    fake.system_info.apply_uptime.assert_called_once()
    comexio_api.get_system_info.assert_awaited_once_with(storage=False)

    asyncio.run(ComexioCoordinator._async_refresh_system_info_storage(fake))
    fake.system_info.apply_storage.assert_called_once_with(_STORAGE_READING.info)
    assert comexio_api.get_system_info.await_args_list[-1].kwargs == {"uptime": False}


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_bus_load_answer_is_applied_although_the_session_closed_meanwhile(
    comexio_api: ComexioAPI, dispatched: MagicMock, close: Callable[[ComexioAPI], None]
) -> None:
    async def answered_then_closed() -> dict[str, object]:
        close(comexio_api)
        return {"workload": 5, "sd_card": True}

    comexio_api.get_bus_workload = AsyncMock(side_effect=answered_then_closed)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)
    fake._evaluate_bus_load_watchdog = MagicMock()
    fake._bus_load_samples = collections.deque()

    asyncio.run(ComexioCoordinator._async_bus_load_tick(fake))

    assert (fake.bus_workload, fake.bus_sd_card, fake._bus_load_fail_streak) == (5, True, 0)
    dispatched.assert_called_once()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_storage_read_that_ends_with_the_session_counts_no_failure(
    comexio_api: ComexioAPI, close: Callable[[ComexioAPI], None]
) -> None:
    # The config fetch went through, the storage request met the close: the valid readings must stay.
    async def stopped_meanwhile(**_kwargs: object) -> SystemInfoReading:
        close(comexio_api)
        return SystemInfoReading(None, None, "dropped")

    comexio_api.get_system_info = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_refresh_system_info_storage(fake))

    fake.system_info.apply_storage.assert_not_called()
    fake._note_system_info_dropped.assert_not_called()


def test_a_failed_storage_read_on_an_open_session_is_still_counted(comexio_api: ComexioAPI) -> None:
    comexio_api.get_system_info = AsyncMock(  # type: ignore[method-assign]
        return_value=SystemInfoReading(None, None, "dropped")
    )
    fake = _fake_reading_coordinator(comexio_api)

    asyncio.run(ComexioCoordinator._async_refresh_system_info_storage(fake))

    fake.system_info.apply_storage.assert_called_once_with(None)
    fake._note_system_info_dropped.assert_called_once()


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_the_run_state_helper_ends_quietly_for_the_callers_outside_the_tick(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None]
) -> None:
    # async_plan_transition and async_start_managed_plan call the helper directly, without the tick's guard.
    async def stopped_meanwhile(*_args: object, **_kwargs: object) -> None:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    comexio_api.get_function_plan_run_states = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_run_state_coordinator(comexio_api)

    with (
        patch.object(coordinator_module, "function_plan_ids", return_value=[1]),
        caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__),
    ):
        asyncio.run(ComexioCoordinator._async_refresh_plan_run_states(fake))

    fake.note_comexio_unreachable.assert_not_called()
    fake._async_plan_run_state_fetch_failed.assert_not_awaited()
    fake._async_watch_plans_and_refresh.assert_not_awaited()
    assert fake._plan_run_state_fetching is False
    assert (fake._plan_run_state_fail_streak, fake._plan_run_state_stale_count) == (2, 2)
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record for record in caplog.records if "ended with the session" in record.getMessage()]


@pytest.mark.parametrize(
    ("error", "close"),
    [(SESSION_CLOSED, None), (OTHER_ERROR, _ha_closes_the_connections)],
    ids=["closed text, open session", "other error after close"],
)
def test_the_run_state_helper_does_not_swallow_what_is_no_closed_session(
    comexio_api: ComexioAPI, error: str, close: Callable[[ComexioAPI], None] | None
) -> None:
    async def fails(*_args: object, **_kwargs: object) -> None:
        if close is not None:
            close(comexio_api)
        raise RuntimeError(error)

    comexio_api.get_function_plan_run_states = AsyncMock(side_effect=fails)  # type: ignore[method-assign]
    fake = _fake_run_state_coordinator(comexio_api)

    with patch.object(coordinator_module, "function_plan_ids", return_value=[1]):
        running = ComexioCoordinator._async_refresh_plan_run_states(fake)
        with pytest.raises(RuntimeError, match=error):
            asyncio.run(running)

    assert fake._plan_run_state_fetching is False
    # A bug is no outage either: nothing is counted before it goes on.
    fake.note_comexio_unreachable.assert_not_called()
    fake._async_plan_run_state_fetch_failed.assert_not_awaited()
    assert (fake._plan_run_state_fail_streak, fake._plan_run_state_stale_count) == (2, 2)


def test_the_run_state_helper_hands_the_session_on_when_it_counts_an_outage(comexio_api: ComexioAPI) -> None:
    # The preview poll passes its own session; the failure handling needs it.
    error = ComexioConnectionError("dropped")
    preview_session = MagicMock()
    comexio_api.get_function_plan_run_states = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    fake = _fake_run_state_coordinator(comexio_api)

    with patch.object(coordinator_module, "function_plan_ids", return_value=[1]):
        asyncio.run(ComexioCoordinator._async_refresh_plan_run_states(fake, preview_session))

    fake._async_plan_run_state_fetch_failed.assert_awaited_once_with(error, preview_session)


@pytest.mark.parametrize("close", CLOSERS, ids=HOW_THE_SESSION_GOES)
def test_a_storage_read_met_by_the_closed_session_logs_no_error(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture, close: Callable[[ComexioAPI], None]
) -> None:
    # aiocomexio passes aiohttp's refusal on, so it reaches the broad handler of the storage read.
    async def stopped_meanwhile(**_kwargs: object) -> SystemInfoReading:
        close(comexio_api)
        raise RuntimeError(SESSION_CLOSED)

    comexio_api.get_system_info = AsyncMock(side_effect=stopped_meanwhile)  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        asyncio.run(ComexioCoordinator._async_refresh_system_info_storage(fake))

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    fake.system_info.apply_storage.assert_not_called()
    fake._note_system_info_dropped.assert_not_called()


def test_an_unexpected_storage_read_error_on_an_open_session_is_still_logged_and_counted(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    comexio_api.get_system_info = AsyncMock(side_effect=RuntimeError(SESSION_CLOSED))  # type: ignore[method-assign]
    fake = _fake_reading_coordinator(comexio_api)

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        asyncio.run(ComexioCoordinator._async_refresh_system_info_storage(fake))

    assert [record for record in caplog.records if record.levelno == logging.ERROR]
    fake.system_info.apply_storage.assert_called_once_with(None)
