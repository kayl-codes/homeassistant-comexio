"""The aiohttp sessions the integration talks to Comexio with: Home Assistant's, on a short keep-alive (#289).

Comexio closes an idle connection after about 6 s, aiohttp keeps one for 15 s: a request sent in between meets a
closed connection ("Server disconnected"). How long a connection is kept is a setting of the connector, and
Home Assistant's async_create_clientsession always uses its shared one. So a session here gets a connector of its
own (COMEXIO_KEEPALIVE_TIMEOUT_SEC), built like Home Assistant's: its TLS context, its DNS resolver (the one that
answers ".local" names over zeroconf) and its User-Agent.
"""

from datetime import datetime
import logging
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from homeassistant import config_entries
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_call_later
from homeassistant.util import ssl as ssl_util

from .const import COMEXIO_CONNECTOR_CLOSE_MARGIN_SEC, COMEXIO_HTTP_TIMEOUT_SEC, COMEXIO_KEEPALIVE_TIMEOUT_SEC

_LOGGER = logging.getLogger(__name__)

_CONNECTOR_CLOSE_DELAY_SEC = COMEXIO_HTTP_TIMEOUT_SEC + COMEXIO_CONNECTOR_CLOSE_MARGIN_SEC


class _FallbackNotice:
    """Whether the missing resolver was logged already (once per start, not per session)."""

    logged = False


class ComexioConnector(aiohttp.TCPConnector):
    """A connector of one Comexio session; closes with Home Assistant, or some time after its session was dropped."""

    def __init__(self, hass: HomeAssistant, resolver: AbstractResolver) -> None:
        super().__init__(
            keepalive_timeout=COMEXIO_KEEPALIVE_TIMEOUT_SEC,
            ssl=ssl_util.client_context(ssl_util.SSLCipherList.PYTHON_DEFAULT, ssl_util.SSL_ALPN_HTTP11),
            resolver=resolver,
        )
        self._comexio_hass = hass
        self._comexio_unsub_ha_close: CALLBACK_TYPE | None = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_CLOSE, self._async_on_ha_close
        )
        self._comexio_cancel_timer: CALLBACK_TYPE | None = None

    def close_later(self) -> None:
        """Close once no request of the dropped session can be under way any more (a request in flight finishes)."""
        if self._comexio_cancel_timer is None and not self.closed:
            self._comexio_cancel_timer = async_call_later(
                self._comexio_hass, _CONNECTOR_CLOSE_DELAY_SEC, self._async_on_timer
            )

    def close_now(self) -> None:
        """Close at once: for a session no request of which can be under way (it never got into use)."""
        if not self.closed:
            self._comexio_hass.async_create_task(self._async_shut_down(), "comexio: close an unused connector")

    async def _async_on_ha_close(self, _event: Event) -> None:
        self._comexio_unsub_ha_close = None  # a listener that fired is gone already
        await self._async_shut_down()

    async def _async_on_timer(self, _now: datetime) -> None:
        self._comexio_cancel_timer = None
        await self._async_shut_down()

    async def _async_shut_down(self) -> None:
        unsub, self._comexio_unsub_ha_close = self._comexio_unsub_ha_close, None
        cancel, self._comexio_cancel_timer = self._comexio_cancel_timer, None
        # Each step whatever the one before did: a listener that cannot be dropped must not leave the timer armed.
        try:
            if unsub is not None:
                unsub()
        finally:
            try:
                if cancel is not None:
                    cancel()
            finally:
                await self.close()


def _ha_resolver(hass: HomeAssistant) -> AbstractResolver | None:
    """Home Assistant's DNS resolver, None if this Home Assistant does not hand it out the way 2026.11 does."""
    # The resolver has no public accessor (2026.11); without it the session is Home Assistant's own.
    getter = getattr(aiohttp_client, "_async_get_or_create_resolver", None)
    failure: Exception | None = None
    resolver = None
    reason = "no accessor"
    if callable(getter):
        try:
            resolver = getter(hass)
            reason = f"it handed out a {type(resolver).__name__}"
        except Exception as err:
            # A private function of Home Assistant: whatever it raises, fall back.
            failure = err
            reason = "the lookup raised"
    if isinstance(resolver, AbstractResolver):
        return resolver
    if _FallbackNotice.logged:
        # The reason may be another one than the first time: keep it findable without a warning per session.
        _LOGGER.debug("Home Assistant's DNS resolver is still not available (%s)", reason, exc_info=failure)
        return None
    _FallbackNotice.logged = True
    _LOGGER.warning(
        "Home Assistant's DNS resolver is not available to the integration (%s): Comexio is reached over Home"
        " Assistant's shared connections, which stay open longer than Comexio keeps them. A value write may"
        " meet a closed connection more often (it is sent again where that is safe)",
        reason,
        exc_info=failure,
    )
    return None


def async_create_session(hass: HomeAssistant, *, bind_to_entry: bool, **kwargs: Any) -> aiohttp.ClientSession:
    """A session for one ComexioClient: connections of its own, kept COMEXIO_KEEPALIVE_TIMEOUT_SEC while idle.

    kwargs go to aiohttp.ClientSession (aiocomexio's session_kwargs). Drop the session with discard_session, never
    with close() or detach(). Must be called in the event loop.

    bind_to_entry: the session goes with the config entry being set up (none: nothing is bound), also where the
    setup fails before anything else drops it. Not for a session that is replaced while the entry lives (the
    preview session): each one would leave a callback on the entry until it unloads.
    """
    resolver = _ha_resolver(hass)
    if resolver is None:
        return aiohttp_client.async_create_clientsession(hass, auto_cleanup=bind_to_entry, **kwargs)
    connector = ComexioConnector(hass, resolver)
    try:
        session = aiohttp.ClientSession(
            connector=connector, headers={aiohttp.hdrs.USER_AGENT: aiohttp_client.SERVER_SOFTWARE}, **kwargs
        )
    except BaseException:
        connector.close_now()
        raise
    if bind_to_entry and (entry := config_entries.current_entry.get()) is not None:
        entry.async_on_unload(lambda: discard_session(session))
    return session


def discard_session(session: aiohttp.ClientSession, *, unused: bool = False) -> None:
    """Take a session out of use: no new request, one in flight finishes, its connections close afterwards.

    detach() and not close(): closing cuts a request in flight off with "Server disconnected", which reads like
    Comexio's own drop and would be asked or sent once more, on a session that is closed. Safe to call twice.

    unused: no request of the session can be under way (it was never handed out), so its connections close at once.
    """
    connector = session.connector
    session.detach()
    if not isinstance(connector, ComexioConnector):
        return
    if unused:
        connector.close_now()
    else:
        connector.close_later()
