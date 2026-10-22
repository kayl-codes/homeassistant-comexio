"""Resolve the address under which Comexio reaches this Home Assistant instance (webhook target).

The known address comes first (#177): the ones the Web-IO devices already store (the hints), then — only
while no usable internal_url is set — the homeassistant.<domain> name the search found last time
(step 3 below, the only slow step; the others cost nothing to repeat, so a changed internal_url
applies at once). It is kept as long as it still points to this HA host, judged against the host's
interface addresses read fresh on every call (a DHCP change must not keep the old IP alive):

- a loopback address never counts,
- an IP literal must be one of the host's own interface addresses — no DNS at all,
- a name gets exactly one lookup, and one of its addresses must be an own interface address. A name
  the lookup gets no answer for (timeout, resolver not reachable yet) is kept unchecked — no answer
  is no evidence that it is wrong — but only when no other known address is confirmed. Only a
  definite miss (the name does not exist) or a foreign address rejects it.

When the own interface addresses cannot be determined (or reading them fails), a known address is kept
unchecked (warned once).

Only without a usable known address is a new one searched, in this order:

1. the host of HA's ``internal_url`` (unless it is a loopback name),
2. the local IP of the outgoing interface (unless it is a loopback address),
3. the first ``homeassistant.<domain>`` name, in KNOWN_DOMAINS order, that resolves — looked up in
   parallel, each capped at HA_ADDRESS_DNS_TIMEOUT_SEC, and — when the own addresses are known — only
   accepted when it points to this host; definite misses are remembered per candidate for
   HA_ADDRESS_NEGATIVE_CACHE_SEC, timeouts and temporary resolver failures never.

When none of them yields an address, the loopback local IP is the last resort (warned once).

The port is the one of internal_url, else the one HA's HTTP server listens on. Calls are serialized: a
search finishing late must not overwrite the state a newer call (e.g. a changed internal_url) has set.

Accepted limit: HA must see the address Comexio reaches it under on its own interfaces (host network,
as HA requires for container installs). Behind NAT or a Docker bridge a stored host address counts as
foreign; set internal_url to that address there.

All of the above is the automatic webhook address (#165). The other modes take host and port of a URL
as they are, with no check against the own addresses: HA's external_url, or a URL of the user's own.
The protocol follows the URL's scheme; automatically, the scheme of internal_url (HA's own TLS setting
without one). A mode whose URL is unusable (external_url removed since) falls back to the automatic
address, warned once.
"""

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from enum import Enum
import ipaddress
import logging
import socket
import time
from typing import Any, NamedTuple
from urllib.parse import urlparse

from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
from homeassistant.components.network.util import async_load_adapters
from homeassistant.core import HomeAssistant

from .const import (
    HA_ADDRESS_DNS_TIMEOUT_SEC,
    HA_ADDRESS_NEGATIVE_CACHE_SEC,
    KNOWN_DOMAINS,
    WEBHOOK_ADDRESS_AUTO,
    WEBHOOK_ADDRESS_EXTERNAL,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_HA_PORT = 8123
_SCHEME_PORTS = {"http": 80, "https": 443}

HA_HOST_CANDIDATES: tuple[str, ...] = tuple(f"homeassistant.{domain}" for domain in KNOWN_DOMAINS)

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class _Lookup(Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not found"
    NO_ANSWER = "no answer"  # timeout or temporary resolver failure — says nothing about the name


class _Verdict(Enum):
    CONFIRMED = "confirmed"  # an IP literal or a name pointing to one of this host's addresses
    UNCHECKED = "unchecked"  # own IPs unknown, or no DNS answer: kept only without a confirmed one
    REJECTED = "rejected"


class _Answer(NamedTuple):
    status: _Lookup
    addresses: frozenset[IPAddress] = frozenset()


_NO_ANSWER = _Answer(_Lookup.NO_ANSWER)


class WebhookTarget(NamedTuple):
    """Where the Web-IO devices push to: "host:port" (see format_address), and whether over HTTPS."""

    address: str
    https: bool

    @property
    def webio_protocol(self) -> int:
        """The protocol the Web-IO commands carry (aiocomexio WEBIO_PROTOCOL_*) to reach this target.

        Comexio speaks TLS to port 443 by itself, even with the commands on HTTP (live verified with
        FW 11.1.4): only HTTPS on another port needs the HTTPS protocol, and switching to it rewrites
        every command of a class, while a new device address alone is one write.
        """
        if self.https and self.address.rsplit(":", 1)[-1] != str(_SCHEME_PORTS["https"]):
            return WEBIO_PROTOCOL_HTTPS
        return WEBIO_PROTOCOL_HTTP


# Resolver errors that mean "this name does not exist"; anything else (EAI_AGAIN while DNS is not
# reachable yet, EAI_FAIL, ...) is transient and must not be cached for an hour.
_DEFINITE_MISS_ERRNOS = frozenset(
    code for code in (getattr(socket, name, None) for name in ("EAI_NONAME", "EAI_NODATA")) if code is not None
)


# _noticed key of the adapter read (no host name: contains a space)
_ADAPTERS = "network adapters"
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _ip(host: str) -> IPAddress | None:
    """host as an IP address (an IPv6 zone like "%eth0" dropped), or None when it is a name."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _is_loopback(host: str) -> bool:
    """A loopback or unspecified IP (0.0.0.0, ::), "localhost", a *.localhost name (RFC 6761) or the alias
    localhost.localdomain: an address that never leads Comexio to HA."""
    if (ip := _ip(host)) is not None:
        ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:0.0.0.0 is the IPv4 address in disguise
        return ip.is_loopback or ip.is_unspecified
    name = host.rstrip(".").lower()
    return name in _LOOPBACK_NAMES or name.endswith(".localhost")


def _lookup(host: str) -> _Answer:
    """Blocking DNS lookup: the status and every address the name resolves to."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as err:
        return _Answer(_Lookup.NOT_FOUND) if err.errno in _DEFINITE_MISS_ERRNOS else _NO_ANSWER
    except OSError:
        return _NO_ANSWER
    except ValueError:  # UnicodeError included
        # Not a valid hostname at all (empty or over-long label, e.g. a typo "ha..lan" in Comexio):
        # it can never resolve, so it is a definite miss, not an error that aborts the poll.
        return _Answer(_Lookup.NOT_FOUND)
    addresses = frozenset(ip for info in infos if (ip := _ip(str(info[4][0]))) is not None)
    return _Answer(_Lookup.RESOLVED, addresses)


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


async def _async_own_ips() -> frozenset[IPAddress] | None:
    """The non-loopback addresses of this host's network adapters, read now (enabled for HA or not);
    None when the adapters cannot be read.

    Not network.async_get_adapters: that list is loaded once at HA start and goes stale on a DHCP change.
    """
    try:
        adapters = await async_load_adapters()
    except OSError as err:  # ifaddr reads the interfaces from the OS
        _LOGGER.debug("HA address: reading the network adapters failed: %s", err)
        return None
    configured = (entry["address"] for adapter in adapters for entry in (*adapter["ipv4"], *adapter["ipv6"]))
    return frozenset(ip for address in configured if (ip := _ip(address)) is not None and not ip.is_loopback)


# Shared address space (RFC 6598): carrier-grade NAT and VPN overlays such as Tailscale — not public.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _is_lan_host(host: str) -> bool:
    """A host reached only inside the local network: a private, link-local or shared-space IP, a single-label
    name or a name under one of the KNOWN_DOMAINS (fritz.box, local, …). Cleartext there never crosses the
    internet. Names are taken by their suffix, not resolved."""
    name = host.rstrip(".").lower()  # "192.168.1.5." is still the IP
    if (ip := _ip(name)) is not None:
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return ip.is_private or ip.is_link_local or ip in _SHARED_ADDRESS_SPACE
    return "." not in name or any(name == domain or name.endswith(f".{domain}") for domain in KNOWN_DOMAINS)


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


def is_loopback_address(address: str) -> bool:
    """Whether a "host:port" address (see format_address) points to the loopback interface or is unspecified.

    Comexio cannot reach HA there; the resolver returns such an address only as its last resort.
    """
    return _is_loopback(host_of(address) or "")


def webhook_url_target(url: str | None) -> WebhookTarget | None:
    """The target of an http(s) URL with nothing but a host and an optional port; None for anything else.

    No port: the scheme's (443 for https, 80 for http). The Web-IO commands bring their own path, so a
    URL with a path, query, fragment or credentials cannot be reproduced on the device and is refused.
    """
    try:
        parsed = urlparse((url or "").strip())
        port = parsed.port
    except ValueError:  # a port out of range or not numeric
        return None
    if parsed.scheme not in _SCHEME_PORTS or not parsed.hostname or parsed.username is not None:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.params:
        return None
    return WebhookTarget(
        format_address(parsed.hostname, port or _SCHEME_PORTS[parsed.scheme]), parsed.scheme == "https"
    )


def webhook_url_error(url: str | None) -> str | None:
    """The options-flow error key of a webhook URL ("invalid_url", "loopback_url", "insecure_url"), None when usable.

    http:// only for a host inside the local network: elsewhere the pushed values would cross the internet in
    cleartext, to a webhook whose id is predictable.
    """
    if (target := webhook_url_target(url)) is None:
        return "invalid_url"
    if is_loopback_address(target.address):
        return "loopback_url"
    if not target.https and not _is_lan_host(host_of(target.address) or ""):
        return "insecure_url"
    return None


def webio_device_hints(webio_devices: Mapping[str, Mapping[str, Any]] | None) -> tuple[str, ...]:
    """The addresses the Web-IO devices already store (parsed/audited "webio_devices" per class).

    All of them: one class may still store a stale address while another already stores a valid one.
    """
    stored = (dev.get("device_ip") for dev in (webio_devices or {}).values())
    return tuple(dict.fromkeys(address for address in stored if address))


class HaAddressResolver:
    """Per-coordinator resolver: keeps the known address while it still points to this host."""

    def __init__(self, hass: HomeAssistant, mode: str = WEBHOOK_ADDRESS_AUTO, url: str | None = None) -> None:
        self.hass = hass
        # Webhook address mode (const.WEBHOOK_ADDRESS_*) and the custom mode's URL.
        self._mode = mode
        self._url = url
        # Why the external/custom mode's URL was unusable the last time (a webhook_url_error key) and the automatic
        # address was used instead; None while the mode's own URL is used (or in the automatic mode).
        self.fallback_reason: str | None = None
        # homeassistant.<domain> name the last search found — known address next to the hints.
        self._guessed_host: str | None = None
        # host -> what was last logged about it; a notice is logged at its level only when it changes.
        self._noticed: dict[str, str] = {}
        # name -> its lookup still running in the executor (see _lookup_future)
        self._in_flight: dict[str, asyncio.Future[_Answer]] = {}
        # candidate -> monotonic time until which its definite miss is trusted
        self._misses: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def async_get(self, *hints: str | None) -> str:
        """The HA address for Comexio webhooks as "host:port" (see async_target)."""
        return (await self.async_target(*hints)).address

    async def async_target(self, *hints: str | None) -> WebhookTarget:
        """The address and protocol for Comexio webhooks.

        hints: the addresses the Web-IO devices already store (see webio_device_hints), checked in
        order before the homeassistant.<domain> name found last time — only for the automatic address.
        """
        async with self._lock:
            if (target := self._fixed_target()) is not None:
                return target
            return WebhookTarget(await self._async_get(hints), self._auto_https())

    def _fixed_target(self) -> WebhookTarget | None:
        """The target of the external_url / custom mode; None for the automatic address.

        An unusable URL (see webhook_url_error) falls back to the automatic address, so the pushes keep reaching
        HA; fallback_reason tells the repair why it would set that address.
        """
        self.fallback_reason = None
        if self._mode == WEBHOOK_ADDRESS_AUTO:
            return None
        url = self.hass.config.external_url if self._mode == WEBHOOK_ADDRESS_EXTERNAL else self._url
        if (error := webhook_url_error(url)) is not None:
            self.fallback_reason = error
            self._notice(
                self._mode,
                error,
                logging.WARNING,
                "HA address: the webhook address mode %r has no usable URL (%r: %s); using the automatic address",
                url,
                error,
            )
            return None
        self._noticed.pop(self._mode, None)
        return webhook_url_target(url)

    def _auto_https(self) -> bool:
        """Whether the automatic address is reached over HTTPS.

        An internal_url with its own port: its scheme (a TLS proxy on that port). Otherwise the address
        carries HA's own port, which speaks TLS only when HA itself does — an https internal_url without
        a port is a proxy on 443, not on HA's port.
        """
        if internal_url := self.hass.config.internal_url:
            parsed = urlparse(internal_url)
            with suppress(ValueError):
                if parsed.port is not None:
                    return parsed.scheme == "https"
        return bool(getattr(getattr(self.hass.config, "api", None), "use_ssl", False))

    async def _async_get(self, hints: tuple[str | None, ...]) -> str:
        api = getattr(self.hass.config, "api", None)  # None until HA's http component is set up
        port = getattr(api, "port", None) or DEFAULT_HA_PORT
        configured_host = None
        if internal_url := self.hass.config.internal_url:
            parsed = urlparse(internal_url)
            configured_host = parsed.hostname
            with suppress(ValueError):
                port = parsed.port or port
        if configured_host and _is_loopback(configured_host):
            configured_host = None
        if configured_host:
            # An explicitly configured URL outranks a remembered guess.
            self._guessed_host = None

        own_ips = await self._async_own_ips()
        hint_hosts = [host_of(hint) for hint in hints]
        host = await self._async_known_host(hint_hosts, own_ips) or await self._async_search(configured_host, own_ips)
        return format_address(host, port)

    async def _async_own_ips(self) -> frozenset[IPAddress]:
        """This host's addresses; empty (unknown) when the adapters cannot be read — warned once per outage."""
        own_ips = await _async_own_ips()
        if own_ips is None:
            self._notice(_ADAPTERS, "unreadable", logging.WARNING, "HA address: %s unreadable, own IPs unknown")
            return frozenset()
        self._noticed.pop(_ADAPTERS, None)
        return own_ips

    async def _async_known_host(self, hint_hosts: list[str | None], own_ips: frozenset[IPAddress]) -> str | None:
        """The first known address confirmed to point to this host, else the first unchecked one, else None.

        The names are looked up in parallel (each bounded by HA_ADDRESS_DNS_TIMEOUT_SEC), so stale names of
        several Web-IO classes do not add up their timeouts; the verdicts are taken in order.
        """
        hosts = list(dict.fromkeys(host for host in (*hint_hosts, self._guessed_host) if host))
        lookups = {
            host: asyncio.ensure_future(self._async_lookup(host))
            for host in hosts
            if own_ips and _ip(host) is None and not _is_loopback(host)
        }
        unchecked = None
        try:
            for host in hosts:
                verdict = await self._async_verdict(host, own_ips, lookups.get(host))
                if verdict is _Verdict.CONFIRMED:
                    return host
                if verdict is _Verdict.UNCHECKED and unchecked is None:
                    unchecked = host
        finally:
            for task in lookups.values():  # only the asyncio side, as in _async_guessed_name
                task.cancel()
        if unchecked is not None:
            if own_ips:
                self._notice(
                    unchecked, "kept", logging.WARNING, "HA address: DNS lookup of %s got no answer; keeping it"
                )
            else:
                self._notice(
                    unchecked, "unchecked", logging.WARNING, "HA address: own IPs unknown; keeping %s unchecked"
                )
        return unchecked

    async def _async_verdict(
        self, host: str, own_ips: frozenset[IPAddress], lookup: asyncio.Future[_Answer] | None = None
    ) -> _Verdict:
        """Whether host is (an IP literal) or resolves to one of own_ips; lookup: its lookup, already started."""
        if _is_loopback(host):
            self._notice(host, "loopback", logging.WARNING, "HA address: %s is a loopback address; ignoring it")
            return _Verdict.REJECTED
        if not own_ips:
            return _Verdict.UNCHECKED
        if (ip := _ip(host)) is not None:
            addresses = frozenset({ip})
        else:
            answer = await (lookup if lookup is not None else self._async_lookup(host))
            if answer.status is _Lookup.NO_ANSWER:
                return _Verdict.UNCHECKED
            addresses = answer.addresses
        if addresses & own_ips:
            self._noticed[host] = "ok"
            return _Verdict.CONFIRMED
        self._notice(
            host,
            "rejected",
            logging.WARNING,
            "HA address: %s (%s) is none of this host's addresses (%s); looking for a new address",
            ", ".join(sorted(map(str, addresses))) or "does not resolve",
            ", ".join(sorted(map(str, own_ips))),
        )
        return _Verdict.REJECTED

    async def async_addresses(self, host: str) -> frozenset[IPAddress]:
        """Every IPv4/IPv6 address of host (an IP literal: itself); empty when it does not resolve or the
        lookup got no answer. Shares and bounds the lookups like the resolution itself."""
        if (ip := _ip(host)) is not None:
            return frozenset({ip})
        return (await self._async_lookup(host)).addresses

    def _notice(self, host: str, kind: str, level: int, message: str, *args: Any) -> None:
        """Log message (host, then args) at level when kind is new for host, else at debug."""
        repeated = self._noticed.get(host) == kind
        self._noticed[host] = kind
        _LOGGER.log(logging.DEBUG if repeated else level, message, host, *args)

    async def _async_search(self, configured_host: str | None, own_ips: frozenset[IPAddress]) -> str:
        """A new address: internal_url host, then local IP, then the homeassistant.<domain> names."""
        self._guessed_host = None  # reached only when it no longer points here (or there is none)
        if configured_host:
            return configured_host
        local_ip = await self.hass.async_add_executor_job(_local_ip)
        if not _is_loopback(local_ip):
            return local_ip
        self._guessed_host = await self._async_guessed_name(own_ips)
        if self._guessed_host:
            return self._guessed_host
        self._notice(
            local_ip,
            "last resort",
            logging.WARNING,
            "HA address: no internal_url, local IP or homeassistant.<domain> name usable; falling back to %s",
        )
        return local_ip

    async def _async_guessed_name(self, own_ips: frozenset[IPAddress]) -> str | None:
        """Look the candidates up in parallel; the first one in KNOWN_DOMAINS order that resolves
        (to one of own_ips, when they are known) wins."""
        now = time.monotonic()
        candidates = [c for c in HA_HOST_CANDIDATES if self._misses.get(c, 0.0) <= now]
        tasks = [asyncio.ensure_future(self._async_lookup(host)) for host in candidates]
        try:
            for host, task in zip(candidates, tasks, strict=True):
                answer = await task
                if answer.status is _Lookup.RESOLVED and (not own_ips or answer.addresses & own_ips):
                    _LOGGER.debug("HA address: %s (search took %.1f s)", host, time.monotonic() - now)
                    return host
                if answer.status is _Lookup.NOT_FOUND:
                    self._misses[host] = time.monotonic() + HA_ADDRESS_NEGATIVE_CACHE_SEC
            return None
        finally:
            # Only the asyncio side is cancelled; a blocked getaddrinfo thread runs until the OS
            # resolver gives up — and is joined, not duplicated, by later lookups (_lookup_future).
            for task in tasks:
                task.cancel()

    def _lookup_future(self, host: str) -> asyncio.Future[_Answer]:
        """The lookup of host still running from an earlier call, or a new one.

        A timed-out getaddrinfo cannot be stopped; without this, every poll and sync during a DNS
        outage would start another executor thread for the same name. Now at most one per name.
        """
        if (future := self._in_flight.get(host)) is None:
            future = self.hass.async_add_executor_job(_lookup, host)
            self._in_flight[host] = future
            future.add_done_callback(lambda _: self._in_flight.pop(host, None))
        return future

    async def _async_lookup(self, host: str) -> _Answer:
        try:
            # shield: the timeout must not cancel the shared future a later lookup may still join
            return await asyncio.wait_for(asyncio.shield(self._lookup_future(host)), timeout=HA_ADDRESS_DNS_TIMEOUT_SEC)
        except TimeoutError:
            _LOGGER.debug("HA address: lookup of %s timed out after %s s", host, HA_ADDRESS_DNS_TIMEOUT_SEC)
            return _NO_ANSWER
