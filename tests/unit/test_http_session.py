"""The session to Comexio keeps an idle connection only briefly; dropping it cuts no request off (#289)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import socket
from typing import Any
from unittest.mock import MagicMock

from aiocomexio import session_kwargs
import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from homeassistant import config_entries
from homeassistant.helpers import aiohttp_client
from homeassistant.util import ssl as ssl_util
import pytest
from yarl import URL

from custom_components.comexio import http_session
from custom_components.comexio.const import (
    COMEXIO_CONNECTOR_CLOSE_MARGIN_SEC,
    COMEXIO_HTTP_TIMEOUT_SEC,
    COMEXIO_KEEPALIVE_TIMEOUT_SEC,
)
from custom_components.comexio.http_session import ComexioConnector, async_create_session, discard_session


class _Resolver(AbstractResolver):
    """Stands in for Home Assistant's resolver: every name is this machine, and each question is noted."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.closed = False

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[Any]:
        self.asked.append(host)
        return [
            ResolveResult(hostname=host, host="127.0.0.1", port=port, family=socket.AF_INET, proto=0, flags=0),
        ]

    async def close(self) -> None:
        self.closed = True


class _Server:
    """A local HTTP server that notes the connection and the User-Agent of every request."""

    def __init__(self) -> None:
        self.connections: list[Any] = []  # the transports themselves: an id could be handed out again
        self.user_agents: list[str] = []
        self.hold = asyncio.Event()
        self.hold.set()
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def _answer(self, request: web.Request) -> web.Response:
        self.connections.append(request.transport)
        self.user_agents.append(request.headers.get("User-Agent", ""))
        await self.hold.wait()
        return web.Response(text="ok")

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


class _Home:
    """The parts of Home Assistant a session touches: the stop listener, the timer and the resolver."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hass = MagicMock()
        self.unsub_ha_close = MagicMock()
        self.hass.bus.async_listen_once.return_value = self.unsub_ha_close
        self.hass.async_create_task = lambda coro, *_args, **_kwargs: asyncio.ensure_future(coro)
        self.resolver = _Resolver()
        self.timers: list[tuple[float, Callable[[Any], Awaitable[None]]]] = []
        self.cancel_timer = MagicMock()
        monkeypatch.setattr(aiohttp_client, "_async_get_or_create_resolver", lambda _hass: self.resolver, raising=True)
        monkeypatch.setattr(http_session, "async_call_later", self._call_later)
        monkeypatch.setattr(http_session._FallbackNotice, "logged", False)

    def _call_later(self, _hass: Any, delay: float, action: Callable[[Any], Awaitable[None]]) -> MagicMock:
        self.timers.append((delay, action))
        return self.cancel_timer

    @property
    def on_ha_close(self) -> Callable[[Any], Awaitable[None]]:
        (event, listener), _kwargs = self.hass.bus.async_listen_once.call_args
        assert event == "homeassistant_close"
        return listener


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> _Home:
    return _Home(monkeypatch)


def _with_server(scenario: Callable[[_Server], Awaitable[None]]) -> None:
    async def run() -> None:
        server = _Server()
        await server.start()
        try:
            await scenario(server)
        finally:
            server.hold.set()
            await server.stop()

    asyncio.run(run())


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def _own_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == http_session.__name__]


async def _get(session: aiohttp.ClientSession, server: _Server, host: str = "127.0.0.1") -> str:
    async with session.get(f"http://{host}:{server.port}/") as response:
        return await response.text()


def test_the_agreed_keep_alive_stays_clear_of_comexios_close() -> None:
    # Comexio closes an idle connection after about 6 s; the measured failure window began at 5.5 s.
    assert COMEXIO_KEEPALIVE_TIMEOUT_SEC == 3


def test_an_idle_connection_is_used_again_only_within_the_keep_alive(
    home: _Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_session, "COMEXIO_KEEPALIVE_TIMEOUT_SEC", 1.0)

    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        await _get(session, server)
        await _get(session, server)  # at once: the same connection
        await asyncio.sleep(1.5)
        await _get(session, server)  # idle for longer than the keep-alive: a new one
        await session.close()

        first, second, third = server.connections
        assert second is first
        assert third is not first

    _with_server(scenario)


def test_the_session_is_built_like_home_assistants(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False, timeout=aiohttp.ClientTimeout(total=5))
        assert isinstance(session.connector, ComexioConnector)
        # Home Assistant's TLS context (it hands out one per cipher list), not aiohttp's default one.
        context = ssl_util.client_context(ssl_util.SSLCipherList.PYTHON_DEFAULT, ssl_util.SSL_ALPN_HTTP11)
        assert session.connector._ssl is context
        assert session.timeout.total == 5  # the caller's kwargs reach the session
        # The agreed keep-alive itself, not one a test patched in (#289); aiohttp has no public getter for it.
        assert session.connector._keepalive_timeout == COMEXIO_KEEPALIVE_TIMEOUT_SEC

        assert await _get(session, server, host="comexio.local") == "ok"
        await session.close()

        assert home.resolver.asked == ["comexio.local"]  # a .local name is Home Assistant's resolver's to answer
        assert server.user_agents == [aiohttp_client.SERVER_SOFTWARE]
        assert home.resolver.closed is False  # Home Assistant's resolver outlives the session

    _with_server(scenario)


def test_without_home_assistants_resolver_the_session_is_home_assistants_own(
    home: _Home, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delattr(aiohttp_client, "_async_get_or_create_resolver")
    ha_session = MagicMock()
    create = MagicMock(return_value=ha_session)
    monkeypatch.setattr(aiohttp_client, "async_create_clientsession", create)

    with caplog.at_level(logging.WARNING, logger=http_session.__name__):
        assert async_create_session(home.hass, bind_to_entry=False, timeout=1) is ha_session
        assert async_create_session(home.hass, bind_to_entry=False, timeout=1) is ha_session

    create.assert_called_with(home.hass, auto_cleanup=False, timeout=1)
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1  # once, not with every session
    assert "DNS resolver is not available" in warnings[0]

    discard_session(ha_session)
    ha_session.detach.assert_called_once_with()
    assert home.timers == []


def test_a_resolver_of_another_kind_is_not_used(
    home: _Home, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(aiohttp_client, "_async_get_or_create_resolver", lambda _hass: object())
    ha_session = MagicMock()
    monkeypatch.setattr(aiohttp_client, "async_create_clientsession", MagicMock(return_value=ha_session))

    with caplog.at_level(logging.DEBUG, logger=http_session.__name__):
        assert async_create_session(home.hass, bind_to_entry=False) is ha_session
        assert async_create_session(home.hass, bind_to_entry=False) is ha_session

    warning, later = _own_records(caplog)
    assert (warning.levelno, later.levelno) == (logging.WARNING, logging.DEBUG)
    assert warning.exc_info is None  # nothing was raised: no traceback of some other error
    assert "(it handed out a object)" in warning.getMessage()
    assert (
        "(it handed out a object)" in later.getMessage()
    )  # a later reason stays findable, without a warning per session


def test_a_discarded_session_lets_the_request_in_flight_finish_and_takes_no_new_one(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        server.hold.clear()
        in_flight = asyncio.create_task(_get(session, server))
        async with asyncio.timeout(5):
            while not server.connections:
                await asyncio.sleep(0.01)

        discard_session(session)
        discard_session(session)  # a second call changes nothing

        assert connector is not None
        assert connector.closed is False
        with pytest.raises(RuntimeError, match="Session is closed"):
            await _get(session, server)
        server.hold.set()
        assert await in_flight == "ok"

        # One timer, due once no request of the session can be under way any more.
        ((delay, on_timer),) = home.timers
        assert delay == COMEXIO_HTTP_TIMEOUT_SEC + COMEXIO_CONNECTOR_CLOSE_MARGIN_SEC
        await on_timer(None)
        assert connector.closed is True
        home.unsub_ha_close.assert_called_once_with()
        home.cancel_timer.assert_not_called()  # a timer that fired is not cancelled

    _with_server(scenario)


def test_home_assistant_stopping_closes_the_connections_at_once(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        assert connector is not None
        await _get(session, server)
        discard_session(session)

        await home.on_ha_close(None)

        assert connector.closed is True
        home.cancel_timer.assert_called_once_with()
        home.unsub_ha_close.assert_not_called()  # a listener that fired is gone already

        discard_session(session)  # after the close: no new timer
        assert len(home.timers) == 1

    _with_server(scenario)


def test_home_assistant_stopping_closes_a_session_still_in_use(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        assert connector is not None
        await _get(session, server)

        await home.on_ha_close(None)

        assert connector.closed is True
        assert home.timers == []
        home.cancel_timer.assert_not_called()

    _with_server(scenario)


def test_a_session_created_during_an_entrys_setup_goes_with_the_entry(home: _Home) -> None:
    async def scenario(_server: _Server) -> None:
        entry = MagicMock()
        token = config_entries.current_entry.set(entry)
        try:
            session = async_create_session(home.hass, bind_to_entry=True)
            unbound = async_create_session(home.hass, bind_to_entry=False)
        finally:
            config_entries.current_entry.reset(token)
        # Only the bound one: a session replaced while the entry lives would leave a callback each time.
        ((on_unload,), _kwargs) = entry.async_on_unload.call_args
        assert entry.async_on_unload.call_count == 1
        await unbound.close()

        on_unload()

        assert session.closed is True
        assert len(home.timers) == 1
        await home.timers[0][1](None)

    _with_server(scenario)


def test_home_assistant_still_hands_its_resolver_out_the_way_the_session_asks_for_it() -> None:
    # Without it every session falls back to Home Assistant's shared connections and the fix for #289 is off, with
    # one warning as the only trace. So a Home Assistant that renamed or changed it turns this test red.
    assert callable(getattr(aiohttp_client, "_async_get_or_create_resolver", None))
    assert issubclass(aiohttp_client.HassAsyncDNSResolver, AbstractResolver)


def test_a_resolver_lookup_that_raises_falls_back_and_names_the_error(
    home: _Home, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_hass: Any) -> None:
        raise TypeError("signature changed")

    monkeypatch.setattr(aiohttp_client, "_async_get_or_create_resolver", broken)
    ha_session = MagicMock()
    create = MagicMock(return_value=ha_session)
    monkeypatch.setattr(aiohttp_client, "async_create_clientsession", create)

    with caplog.at_level(logging.WARNING, logger=http_session.__name__):
        assert async_create_session(home.hass, bind_to_entry=True) is ha_session

    create.assert_called_once_with(home.hass, auto_cleanup=True)  # bound: Home Assistant drops it with the entry
    (record,) = _own_records(caplog)
    assert "(the lookup raised)" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], TypeError)


def test_a_session_that_never_got_into_use_closes_its_connections_at_once(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        assert connector is not None
        await _get(session, server)

        discard_session(session, unused=True)
        await _until(lambda: connector.closed)

        assert home.timers == []
        home.unsub_ha_close.assert_called_once_with()

    _with_server(scenario)


def test_the_connector_is_closed_when_the_session_cannot_be_built(home: _Home, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[ComexioConnector] = []

    class _Recorded(ComexioConnector):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(http_session, "ComexioConnector", _Recorded)

    async def scenario(_server: _Server) -> None:
        with pytest.raises(TypeError):
            async_create_session(home.hass, bind_to_entry=False, no_such_keyword=1)
        (connector,) = built
        await _until(lambda: connector.closed)  # the connector built for it, itself, not only its listener

        home.unsub_ha_close.assert_called_once_with()

    _with_server(scenario)


def test_the_connections_close_even_if_dropping_the_stop_listener_fails(home: _Home) -> None:
    async def scenario(server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        assert isinstance(connector, ComexioConnector)
        await _get(session, server)
        home.unsub_ha_close.side_effect = RuntimeError("listener already gone")
        discard_session(session)

        with pytest.raises(RuntimeError, match="listener already gone"):
            await connector._async_shut_down()

        assert connector.closed is True
        home.cancel_timer.assert_called_once_with()  # the timer is not left armed either

    _with_server(scenario)


def test_discarding_an_unused_session_whose_connector_is_closed_already_does_nothing(home: _Home) -> None:
    async def scenario(_server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False)
        connector = session.connector
        assert isinstance(connector, ComexioConnector)
        await home.on_ha_close(None)  # Home Assistant stopped: the connector is closed
        tasks = []
        home.hass.async_create_task = lambda coro, *_args, **_kwargs: tasks.append(coro)

        discard_session(session, unused=True)

        assert tasks == []
        assert home.timers == []

    _with_server(scenario)


def test_the_real_accessor_hands_out_the_resolver_the_connector_is_built_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Home Assistant's own accessor (its singleton wrapper included), only the resolver it makes is a stand-in (the
    # `home` fixture is not used: it replaces the accessor): a changed signature or return type makes http_session
    # fall back, which this test turns into a red one.
    monkeypatch.setattr(http_session._FallbackNotice, "logged", False)
    made = _Resolver()
    makes = MagicMock(return_value=made)
    monkeypatch.setattr(aiohttp_client, "_async_make_resolver", makes)
    hass = MagicMock()
    hass.data = {}

    assert http_session._ha_resolver(hass) is made
    assert http_session._ha_resolver(hass) is made
    makes.assert_called_once_with(hass)  # Home Assistant's singleton: one resolver for everybody, not one per session

    async def scenario(server: _Server) -> None:
        session = async_create_session(hass, bind_to_entry=False)
        assert isinstance(session.connector, ComexioConnector)
        assert await _get(session, server, host="comexio.local") == "ok"
        await session.close()

        assert made.asked == ["comexio.local"]  # the lookup really went through the resolver Home Assistant handed out

    _with_server(scenario)


def test_the_sessions_cookie_jar_keeps_the_cookies_of_a_host_given_by_ip_address(home: _Home) -> None:
    # aiohttp's default jar drops cookies from an IP address: aiocomexio's session_kwargs bring a jar that keeps them,
    # and the dedicated session must carry it just as Home Assistant's own session would.
    async def scenario(_server: _Server) -> None:
        session = async_create_session(home.hass, bind_to_entry=False, **session_kwargs())
        url = URL("http://192.168.0.50/")
        session.cookie_jar.update_cookies({"session": "abc"}, url)

        assert [cookie.key for cookie in session.cookie_jar.filter_cookies(url).values()] == ["session"]
        await session.close()

    _with_server(scenario)
