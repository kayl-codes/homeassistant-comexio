"""Resolve the address under which Comexio reaches this Home Assistant instance (webhook target).

Resolution order (the same as the former ComexioAPI.get_ha_address — the IP audit compares the
Web-IO device's stored address against exactly this result):

1. the first ``homeassistant.<domain>`` name, in KNOWN_DOMAINS order, that resolves,
2. the host of HA's ``internal_url``,
3. the local IP of the outgoing interface.

Only step 1 is slow: every candidate that does not exist costs a DNS (or mDNS) miss, and the old code
paid them one after another on every poll and sync. The resolver keeps the precedence but
- looks candidates up in parallel, each capped at HA_ADDRESS_DNS_TIMEOUT_SEC,
- stops at the name that is already known (resolved last time, or stored in the Web-IO device):
  only it and the candidates before it are checked, so a higher-priority name that starts resolving
  still wins and the list behind the known name is never touched,
- remembers definite misses (the resolver says the name does not exist) per candidate for
  HA_ADDRESS_NEGATIVE_CACHE_SEC. A timeout or a temporary resolver failure is no answer: it is
  never cached, and when the known name (resolved last time, or the Web-IO device's) itself gets no
  answer, that name is kept — neither the IP fallback nor a lower-priority name replaces it. A known
  name the resolver definitely no longer knows is dropped, whatever the other candidates answered.
"""

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from enum import Enum
import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant

from .const import HA_ADDRESS_DNS_TIMEOUT_SEC, HA_ADDRESS_NEGATIVE_CACHE_SEC, KNOWN_DOMAINS

_LOGGER = logging.getLogger(__name__)

DEFAULT_HA_PORT = 8123
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

HA_HOST_CANDIDATES: tuple[str, ...] = tuple(f"homeassistant.{domain}" for domain in KNOWN_DOMAINS)


class _Lookup(Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not found"
    NO_ANSWER = "no answer"  # timeout or temporary resolver failure — says nothing about the name


# Resolver errors that mean "this name does not exist"; anything else (EAI_AGAIN while DNS is not
# reachable yet, EAI_FAIL, ...) is transient and must not be cached for an hour.
_DEFINITE_MISS_ERRNOS = frozenset(
    code for code in (getattr(socket, name, None) for name in ("EAI_NONAME", "EAI_NODATA")) if code is not None
)


def _lookup(host: str) -> _Lookup:
    """Blocking DNS lookup."""
    try:
        socket.gethostbyname(host)
    except socket.gaierror as err:
        return _Lookup.NOT_FOUND if err.errno in _DEFINITE_MISS_ERRNOS else _Lookup.NO_ANSWER
    except OSError:
        return _Lookup.NO_ANSWER
    return _Lookup.RESOLVED


def _local_ip() -> str:
    """IP of the interface that routes to the private networks (no packet is sent)."""
    # Try private IPv4 routing first
    with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]

    # Fallback to IPv6 local routing (ULA prefix)
    with suppress(OSError), socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.connect(("fd00::", 1))
        return s.getsockname()[0]

    with suppress(OSError):
        return socket.gethostbyname(socket.gethostname())

    return "127.0.0.1"


def host_of(address: str | None) -> str | None:
    """Host part of a stored Web-IO address ("host:port", "[v6]:port" or bare host)."""
    if not address:
        return None
    address = address.strip()
    host = address.rsplit(":", 1)[0] if address.count(":") == 1 or address.startswith("[") else address
    return host.strip("[]") or None


def format_address(host: str, port: int) -> str:
    """host:port, with IPv6 addresses wrapped in brackets for URL compatibility."""
    with suppress(ValueError):
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    return f"{host}:{port}"


def webio_device_hint(webio_devices: Mapping[str, Mapping[str, Any]] | None) -> str | None:
    """The address a Web-IO device already stores (parsed/audited "webio_devices" per class)."""
    return next((dev["device_ip"] for dev in (webio_devices or {}).values() if dev.get("device_ip")), None)


class HaAddressResolver:
    """Per-coordinator resolver that caches the slow homeassistant.<domain> search."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._dns_host: str | None = None
        # Known name currently kept without an answer — its warning is logged once, not every poll.
        self._kept_unanswered: str | None = None
        # candidate -> its lookup still running in the executor (see _lookup_future)
        self._in_flight: dict[str, asyncio.Future[_Lookup]] = {}
        # candidate -> monotonic time until which its definite miss is trusted
        self._misses: dict[str, float] = {}

    async def async_get(self, hint: str | None = None) -> str:
        """The HA address for Comexio webhooks as "host:port".

        hint: the address a Web-IO device already stores (see webio_device_hint). When its host is a
        candidate, the search stops there, like for the name resolved last time.
        """
        port = DEFAULT_HA_PORT
        fallback_host = None
        if internal_url := self.hass.config.internal_url:
            parsed = urlparse(internal_url)
            fallback_host = parsed.hostname
            with suppress(ValueError):
                port = parsed.port or DEFAULT_HA_PORT

        host = await self._async_dns_host(host_of(hint))
        if not host:
            if not fallback_host or fallback_host in _LOOPBACK_HOSTS:
                host = await self.hass.async_add_executor_job(_local_ip)
            else:
                host = fallback_host
        return format_address(host, port)

    async def _async_dns_host(self, hint_host: str | None) -> str | None:
        """The first resolving homeassistant.<domain> name, or None when none resolves."""
        now = time.monotonic()
        open_candidates = [c for c in HA_HOST_CANDIDATES if self._misses.get(c, 0.0) <= now]
        known = next((c for c in (self._dns_host, hint_host) if c in open_candidates), None)
        # Up to and including the known name — what comes after it can never win against it.
        batches = [open_candidates]
        if known:
            split = open_candidates.index(known) + 1
            batches = [open_candidates[:split], open_candidates[split:]]

        started = now
        unanswered: list[str] = []
        for batch in batches:
            host = await self._async_first_resolving(batch, unanswered)
            if host:
                if host != self._dns_host:
                    _LOGGER.debug("HA address: %s (search took %.1f s)", host, time.monotonic() - started)
                self._dns_host = host
                self._kept_unanswered = None
                return host
            if known in unanswered:
                # No answer is no evidence the known name is gone: keep it rather than let a
                # lower-priority name from the next batch replace it.
                self._log_kept(known, unanswered)
                return known

        self._kept_unanswered = None
        if self._dns_host:
            _LOGGER.warning(
                "HA address: %s no longer resolves and no other homeassistant.<domain> name does; "
                "falling back to the internal_url host / local IP",
                self._dns_host,
            )
        self._dns_host = None
        return None

    def _log_kept(self, known: str, unanswered: list[str]) -> None:
        """Warn once when a known name starts being kept without an answer; debug on repeats."""
        level = logging.DEBUG if known == self._kept_unanswered else logging.WARNING
        self._kept_unanswered = known
        _LOGGER.log(level, "HA address: DNS lookups got no answer (%s); keeping %s", ", ".join(unanswered), known)

    async def _async_first_resolving(self, batch: Sequence[str], unanswered: list[str]) -> str | None:
        """Look the batch up in parallel; the first resolving name in batch order wins."""
        tasks = [asyncio.ensure_future(self._async_lookup(host)) for host in batch]
        try:
            for host, task in zip(batch, tasks, strict=True):
                result = await task
                if result is _Lookup.RESOLVED:
                    return host
                if result is _Lookup.NOT_FOUND:
                    self._misses[host] = time.monotonic() + HA_ADDRESS_NEGATIVE_CACHE_SEC
                else:
                    unanswered.append(host)
            return None
        finally:
            # Only the asyncio side is cancelled; a blocked gethostbyname thread runs until the OS
            # resolver gives up — and is joined, not duplicated, by later searches (_lookup_future).
            for task in tasks:
                task.cancel()

    def _lookup_future(self, host: str) -> asyncio.Future[_Lookup]:
        """The lookup of host still running from an earlier search, or a new one.

        A timed-out gethostbyname cannot be stopped; without this, every poll and sync during a DNS
        outage would start another executor thread for the same name. Now at most one per candidate.
        """
        if (future := self._in_flight.get(host)) is None:
            future = self.hass.async_add_executor_job(_lookup, host)
            self._in_flight[host] = future
            future.add_done_callback(lambda _: self._in_flight.pop(host, None))
        return future

    async def _async_lookup(self, host: str) -> _Lookup:
        try:
            # shield: the timeout must not cancel the shared future a later search may still join
            return await asyncio.wait_for(asyncio.shield(self._lookup_future(host)), timeout=HA_ADDRESS_DNS_TIMEOUT_SEC)
        except TimeoutError:
            _LOGGER.debug("HA address: lookup of %s timed out after %s s", host, HA_ADDRESS_DNS_TIMEOUT_SEC)
            return _Lookup.NO_ANSWER
