"""HA webhook address resolution (ha_address.py): known address first, then the search order (#177)."""

import asyncio
from collections.abc import Callable
import ipaddress
import logging
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any

from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
import pytest

from custom_components.comexio import ha_address
from custom_components.comexio.ha_address import (
    HA_HOST_CANDIDATES,
    HaAddressResolver,
    WebhookTarget,
    format_address,
    host_of,
    webio_device_hints,
)

OWN_IP = "192.168.0.110"
OTHER_OWN_IP = "10.0.0.7"
FOREIGN_IP = "192.168.178.50"
INTERNAL_URL = "http://ha.example.lan:8123"
HA_NAME = "ha.example.lan"


class _FakeHass:
    """Just enough of HomeAssistant for the resolver: config.internal_url + executor jobs."""

    def __init__(self, internal_url: str | None = None) -> None:
        self.config = SimpleNamespace(internal_url=internal_url)

    def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> asyncio.Future:
        return asyncio.get_running_loop().run_in_executor(None, func, *args)


class _FakeDns:
    """Records lookups; names in `resolving` resolve to the mapped IP(s, comma-separated), names in `slow` block before
    answering, names in `transient` fail like a resolver that is not reachable yet (EAI_AGAIN)."""

    def __init__(
        self,
        resolving: dict[str, str],
        slow: set[str] | None = None,
        delay: float = 0.3,
        transient: set[str] | None = None,
    ) -> None:
        self.resolving = resolving
        self.slow = slow or set()
        self.transient = transient or set()
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def lookup(self, host: str) -> ha_address._Answer:
        with self._lock:
            self.calls.append(host)
        if host in self.slow:
            time.sleep(self.delay)
        if host in self.transient:
            return ha_address._NO_ANSWER
        if host in self.resolving:
            return ha_address._Answer(
                ha_address._Lookup.RESOLVED,
                frozenset(ipaddress.ip_address(ip) for ip in self.resolving[host].split(",")),
            )
        return ha_address._Answer(ha_address._Lookup.NOT_FOUND)


@pytest.fixture
def own_ips(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """This host's interface addresses as the resolver sees them (mutable per test)."""
    ips = {OWN_IP, OTHER_OWN_IP}

    async def _own() -> frozenset:
        return frozenset(ipaddress.ip_address(ip) for ip in ips)

    monkeypatch.setattr(ha_address, "_async_own_ips", _own)
    return ips


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch, own_ips: set[str]) -> Callable[..., _FakeDns]:
    def _install(resolving: dict[str, str] | None = None, local_ip: str = OTHER_OWN_IP, **kwargs: Any) -> _FakeDns:
        fake = _FakeDns(resolving or {}, **kwargs)
        monkeypatch.setattr(ha_address, "_lookup", fake.lookup)
        monkeypatch.setattr(ha_address, "_local_ip", lambda: local_ip)
        return fake

    return _install


_real_lookup = ha_address._lookup


def _get(resolver: HaAddressResolver, hint: str | None = None) -> str:
    return asyncio.run(resolver.async_get(hint))


def _timed_get(resolver: HaAddressResolver) -> tuple[str, float]:
    """Result and the time async_get took — measured inside the loop, because asyncio.run also
    waits for executor threads that are still blocked in an abandoned lookup."""

    async def _run() -> tuple[str, float]:
        started = time.monotonic()
        result = await resolver.async_get()
        return result, time.monotonic() - started

    return asyncio.run(_run())


def _every_candidate_resolves() -> dict[str, str]:
    return dict.fromkeys(HA_HOST_CANDIDATES, OWN_IP)


# --- known address: the Web-IO device's (hint) -------------------------------------------------


def test_stored_own_ip_is_used_without_any_dns_lookup(dns) -> None:
    fake = dns(_every_candidate_resolves())

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{OWN_IP}:8123") == f"{OWN_IP}:8123"
    assert fake.calls == []


def test_stored_own_ipv6_is_used(dns, own_ips: set[str]) -> None:
    own_ips.add("fd00::110")
    dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint="[fd00::110]:8123") == "[fd00::110]:8123"


def test_stored_foreign_ip_falls_back_to_internal_url(dns) -> None:
    fake = dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{FOREIGN_IP}:8123") == f"{HA_NAME}:8123"
    assert fake.calls == []


@pytest.mark.parametrize("hint", ["127.0.0.1:8123", "0.0.0.0:8123", "[::]:8123"])
def test_stored_loopback_ip_is_no_known_address(dns, hint: str) -> None:
    dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=hint) == f"{HA_NAME}:8123"


def test_stored_name_pointing_here_costs_exactly_one_lookup(dns) -> None:
    fake = dns({**_every_candidate_resolves(), HA_HOST_CANDIDATES[6]: OWN_IP})

    result = _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{HA_HOST_CANDIDATES[6]}:8123")

    assert result == f"{HA_HOST_CANDIDATES[6]}:8123"
    assert fake.calls == [HA_HOST_CANDIDATES[6]]


def test_stored_name_that_is_no_candidate_is_checked_too(dns) -> None:
    fake = dns({"comexio-ha.example.org": OWN_IP})

    assert _get(HaAddressResolver(_FakeHass()), hint="comexio-ha.example.org:8123") == "comexio-ha.example.org:8123"
    assert fake.calls == ["comexio-ha.example.org"]


def test_stored_name_pointing_elsewhere_falls_back_to_internal_url(dns) -> None:
    dns({HA_HOST_CANDIDATES[0]: FOREIGN_IP})

    result = _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{HA_HOST_CANDIDATES[0]}:8123")

    assert result == f"{HA_NAME}:8123"


def test_stored_name_that_no_longer_exists_falls_back_to_internal_url(dns) -> None:
    dns()

    result = _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{HA_HOST_CANDIDATES[6]}:8123")

    assert result == f"{HA_NAME}:8123"


def test_stored_name_with_a_timeout_is_kept(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    dns(slow={"homeassistant.local"}, delay=0.3)

    result = _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint="homeassistant.local:8123")

    assert result == "homeassistant.local:8123"


def test_stored_name_with_a_transient_resolver_failure_is_kept(dns) -> None:
    dns(transient={HA_HOST_CANDIDATES[0]})

    result = _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{HA_HOST_CANDIDATES[0]}:8123")

    assert result == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_unknown_own_ips_keep_the_known_address_unchecked(dns, own_ips: set[str]) -> None:
    own_ips.clear()
    fake = dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=f"{FOREIGN_IP}:8123") == f"{FOREIGN_IP}:8123"
    assert fake.calls == []


def test_keeping_an_unanswered_name_warns_once(dns, caplog: pytest.LogCaptureFixture) -> None:
    dns(transient={HA_NAME})
    resolver = HaAddressResolver(_FakeHass())

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        _get(resolver, hint=f"{HA_NAME}:8123")
        _get(resolver, hint=f"{HA_NAME}:8123")

    kept = [r.levelname for r in caplog.records if "keeping" in r.getMessage()]
    assert kept == ["WARNING", "DEBUG"]


def test_rejecting_a_stale_stored_address_logs_once(dns, caplog: pytest.LogCaptureFixture) -> None:
    dns()
    resolver = HaAddressResolver(_FakeHass(INTERNAL_URL))

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        _get(resolver, hint=f"{FOREIGN_IP}:8123")
        _get(resolver, hint=f"{FOREIGN_IP}:8123")

    rejected = [r.levelname for r in caplog.records if "none of this host" in r.getMessage()]
    assert rejected == ["WARNING", "DEBUG"]


def test_hung_lookup_is_joined_not_duplicated_by_the_next_call(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    fake = dns(slow={HA_NAME}, delay=0.5)
    resolver = HaAddressResolver(_FakeHass())

    async def _two_calls() -> None:
        await resolver.async_get(f"{HA_NAME}:8123")
        await resolver.async_get(f"{HA_NAME}:8123")

    asyncio.run(_two_calls())

    assert fake.calls.count(HA_NAME) == 1


def test_stored_name_resolving_to_several_addresses_counts_if_one_is_own(dns) -> None:
    dns({HA_NAME: f"{FOREIGN_IP},{OWN_IP}"})

    assert _get(HaAddressResolver(_FakeHass()), hint=f"{HA_NAME}:8123") == f"{HA_NAME}:8123"


def test_stored_name_resolving_to_an_own_ipv6_address_is_kept(dns, own_ips: set[str]) -> None:
    own_ips.add("fd00::110")
    dns({HA_NAME: "fd00::110"})

    assert _get(HaAddressResolver(_FakeHass()), hint=f"{HA_NAME}:8123") == f"{HA_NAME}:8123"


def test_invalid_stored_hostname_is_rejected_not_raised(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    dns()
    # The real lookup, so the resolver's own UnicodeError handling is exercised ("ha..lan" is no valid name).
    monkeypatch.setattr(ha_address, "_lookup", _real_lookup)

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint="ha..lan:8123") == f"{HA_NAME}:8123"


def test_own_ips_are_read_on_every_call(dns, own_ips: set[str]) -> None:
    # A DHCP change: the old IP must stop counting as this host's without an HA restart.
    dns()
    resolver = HaAddressResolver(_FakeHass(INTERNAL_URL))
    assert _get(resolver, hint=f"{OWN_IP}:8123") == f"{OWN_IP}:8123"
    own_ips.discard(OWN_IP)

    assert _get(resolver, hint=f"{OWN_IP}:8123") == f"{HA_NAME}:8123"


def test_unknown_own_ips_keep_a_stored_name_without_lookup(dns, own_ips: set[str]) -> None:
    own_ips.clear()
    fake = dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint="ha.other.lan:8123") == "ha.other.lan:8123"
    assert fake.calls == []


@pytest.mark.parametrize("hint", ["127.0.0.1:8123", "0.0.0.0:8123", "[::]:8123"])
def test_stored_loopback_is_rejected_even_when_own_ips_are_unknown(dns, own_ips: set[str], hint: str) -> None:
    own_ips.clear()
    dns()

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL)), hint=hint) == f"{HA_NAME}:8123"


def test_unchecked_keep_warns_once(dns, own_ips: set[str], caplog: pytest.LogCaptureFixture) -> None:
    own_ips.clear()
    dns()
    resolver = HaAddressResolver(_FakeHass())

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        _get(resolver, hint=f"{FOREIGN_IP}:8123")
        _get(resolver, hint=f"{FOREIGN_IP}:8123")

    assert [r.levelname for r in caplog.records if "unchecked" in r.getMessage()] == ["WARNING", "DEBUG"]


def test_rejection_names_the_resolved_and_own_addresses(dns, caplog: pytest.LogCaptureFixture) -> None:
    dns({HA_NAME: FOREIGN_IP})

    with caplog.at_level("WARNING", logger=ha_address.__name__):
        _get(HaAddressResolver(_FakeHass()), hint=f"{HA_NAME}:8123")

    message = next(r.getMessage() for r in caplog.records if "none of this host" in r.getMessage())
    assert FOREIGN_IP in message
    assert OWN_IP in message


def test_a_changed_state_warns_again(dns, caplog: pytest.LogCaptureFixture) -> None:
    fake = dns(transient={HA_NAME})
    resolver = HaAddressResolver(_FakeHass(INTERNAL_URL))

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        _get(resolver, hint=f"{HA_NAME}:8123")  # kept: no answer
        fake.transient = set()
        fake.resolving = {HA_NAME: FOREIGN_IP}
        _get(resolver, hint=f"{HA_NAME}:8123")  # rejected
        fake.transient = {HA_NAME}
        _get(resolver, hint=f"{HA_NAME}:8123")  # kept again
        fake.transient = set()
        fake.resolving = {HA_NAME: OWN_IP}
        _get(resolver, hint=f"{HA_NAME}:8123")  # fine
        fake.transient = {HA_NAME}
        _get(resolver, hint=f"{HA_NAME}:8123")  # kept again after a recovery

    levels = [r.levelname for r in caplog.records if "keeping" in r.getMessage() or "none of" in r.getMessage()]
    assert levels == ["WARNING", "WARNING", "WARNING", "WARNING"]


def test_hint_is_checked_before_the_remembered_guessed_name(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]: OWN_IP}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.calls.clear()

    assert _get(resolver, hint=f"{OWN_IP}:8123") == f"{OWN_IP}:8123"
    assert fake.calls == []


# --- known address: the homeassistant.<domain> name found last time ---------------------------


def test_guessed_name_found_last_time_costs_one_lookup(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]: OWN_IP}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"
    fake.calls.clear()

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"
    assert fake.calls == [HA_HOST_CANDIDATES[0]]


def test_guessed_name_found_last_time_is_used_when_the_stored_address_is_stale(dns) -> None:
    dns({HA_HOST_CANDIDATES[3]: OWN_IP}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)

    assert _get(resolver, hint=f"{FOREIGN_IP}:8123") == f"{HA_HOST_CANDIDATES[3]}:8123"


def test_guessed_name_that_stops_pointing_here_is_dropped(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[3]: OWN_IP}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.resolving = {HA_HOST_CANDIDATES[3]: FOREIGN_IP}
    resolver.hass.config.internal_url = INTERNAL_URL

    assert _get(resolver) == f"{HA_NAME}:8123"
    fake.calls.clear()
    assert _get(resolver) == f"{HA_NAME}:8123"
    assert fake.calls == []


def test_changed_internal_url_applies_at_once_without_a_stored_address(dns) -> None:
    fake = dns()
    resolver = HaAddressResolver(_FakeHass(f"http://{OWN_IP}:8123"))
    assert _get(resolver) == f"{OWN_IP}:8123"
    resolver.hass.config.internal_url = INTERNAL_URL

    assert _get(resolver) == f"{HA_NAME}:8123"
    assert fake.calls == []


def test_set_internal_url_outranks_the_remembered_guessed_name(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]: OWN_IP}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"
    resolver.hass.config.internal_url = INTERNAL_URL
    fake.calls.clear()

    assert _get(resolver) == f"{HA_NAME}:8123"
    assert fake.calls == []


# --- search without a usable known address -----------------------------------------------------


def test_internal_url_wins_over_resolving_guessed_names(dns) -> None:
    # The core of #177: a guessed homeassistant.<domain> name no longer outranks the configured URL,
    # and the (possibly slow) guessing does not even start.
    fake = dns(_every_candidate_resolves())

    assert _get(HaAddressResolver(_FakeHass(INTERNAL_URL))) == f"{HA_NAME}:8123"
    assert fake.calls == []


@pytest.mark.parametrize("internal_url", [None, "http://localhost:8123", "http://127.0.0.1:8123", "http://[::1]:8123"])
def test_loopback_or_missing_internal_url_falls_back_to_local_ip(dns, internal_url: str | None) -> None:
    fake = dns(_every_candidate_resolves())

    assert _get(HaAddressResolver(_FakeHass(internal_url))) == f"{OTHER_OWN_IP}:8123"
    assert fake.calls == []


def test_port_comes_from_internal_url(dns) -> None:
    dns()

    assert _get(HaAddressResolver(_FakeHass("http://ha.example.lan:8124")), hint=f"{OWN_IP}:8123") == f"{OWN_IP}:8124"


def test_invalid_internal_url_port_falls_back_to_default(dns) -> None:
    dns()

    assert _get(HaAddressResolver(_FakeHass("http://ha.example.lan:99999"))) == f"{HA_NAME}:8123"


@pytest.mark.parametrize("local_ip", ["127.0.0.1", "0.0.0.0"])
def test_guessed_names_come_last_in_known_domains_order(dns, local_ip: str) -> None:
    dns({HA_HOST_CANDIDATES[4]: OWN_IP, HA_HOST_CANDIDATES[1]: OWN_IP}, local_ip=local_ip)

    assert _get(HaAddressResolver(_FakeHass())) == f"{HA_HOST_CANDIDATES[1]}:8123"


def test_without_any_address_the_loopback_local_ip_is_the_last_resort(dns, caplog: pytest.LogCaptureFixture) -> None:
    dns(local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        assert _get(resolver) == "127.0.0.1:8123"
        assert _get(resolver) == "127.0.0.1:8123"

    assert [r.levelname for r in caplog.records if "falling back" in r.getMessage()] == ["WARNING", "DEBUG"]


def test_guessed_name_pointing_elsewhere_is_skipped(dns) -> None:
    # e.g. homeassistant.local of a second HA instance in the network
    dns({HA_HOST_CANDIDATES[0]: FOREIGN_IP, HA_HOST_CANDIDATES[2]: OWN_IP}, local_ip="127.0.0.1")

    assert _get(HaAddressResolver(_FakeHass())) == f"{HA_HOST_CANDIDATES[2]}:8123"


def test_guessed_names_are_looked_up_in_parallel(dns) -> None:
    # Every miss is slow: sequentially this would take len(candidates) * delay.
    fake = dns(slow=set(HA_HOST_CANDIDATES), delay=0.3, local_ip="127.0.0.1")

    result, elapsed = _timed_get(HaAddressResolver(_FakeHass()))

    assert result == "127.0.0.1:8123"
    assert elapsed < 0.3 * len(HA_HOST_CANDIDATES) / 2
    assert sorted(fake.calls) == sorted(HA_HOST_CANDIDATES)


def test_hanging_guess_is_capped_by_timeout(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.1)
    dns({HA_HOST_CANDIDATES[2]: OWN_IP}, slow={HA_HOST_CANDIDATES[0]}, delay=1.0, local_ip="127.0.0.1")

    result, elapsed = _timed_get(HaAddressResolver(_FakeHass()))

    assert result == f"{HA_HOST_CANDIDATES[2]}:8123"
    assert elapsed < 1.0


def test_definite_misses_of_guessed_names_are_cached(dns) -> None:
    fake = dns(local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == "127.0.0.1:8123"
    assert sorted(fake.calls) == sorted(HA_HOST_CANDIDATES)  # every guess was looked up and missed
    fake.calls.clear()

    _get(resolver)
    assert fake.calls == []


def test_guessed_misses_are_looked_up_again_after_the_cache_expires(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_NEGATIVE_CACHE_SEC", 0)
    fake = dns(local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == "127.0.0.1:8123"
    assert sorted(fake.calls) == sorted(HA_HOST_CANDIDATES)  # the misses the expired cache must not keep
    fake.resolving = {HA_HOST_CANDIDATES[0]: OWN_IP}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_guessed_timeouts_are_not_cached_as_misses(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    fake = dns(slow=set(HA_HOST_CANDIDATES), delay=0.3, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == "127.0.0.1:8123"
    fake.slow = set()
    fake.resolving = {HA_HOST_CANDIDATES[0]: OWN_IP}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"


# --- helpers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.gaierror(socket.EAI_NONAME, "not known"), ha_address._Answer(ha_address._Lookup.NOT_FOUND)),
        (socket.gaierror(socket.EAI_AGAIN, "temporary failure"), ha_address._NO_ANSWER),
        (OSError("network unreachable"), ha_address._NO_ANSWER),
        (UnicodeError("label empty or too long"), ha_address._Answer(ha_address._Lookup.NOT_FOUND)),
        (
            None,
            ha_address._Answer(
                ha_address._Lookup.RESOLVED,
                frozenset({ipaddress.ip_address(OWN_IP), ipaddress.ip_address("fd00::110")}),
            ),
        ),
    ],
)
def test_lookup_tells_definite_misses_from_transient_failures(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, expected
) -> None:
    def _getaddrinfo(host: str, port: Any) -> list[tuple]:
        if error is not None:
            raise error
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (OWN_IP, 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (OWN_IP, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::110", 0, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)

    assert ha_address._lookup("homeassistant.lan") == expected


def test_own_ips_come_from_the_network_adapters_without_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = [
        {"ipv4": [{"address": "127.0.0.1"}], "ipv6": [{"address": "::1"}]},
        {"ipv4": [{"address": OWN_IP}], "ipv6": [{"address": "fe80::1%eth0"}]},
        {"ipv4": [], "ipv6": []},
    ]

    async def _adapters() -> list[dict]:
        return adapters

    monkeypatch.setattr(ha_address, "async_load_adapters", _adapters)

    own = asyncio.run(ha_address._async_own_ips())

    assert own == {ipaddress.ip_address(OWN_IP), ipaddress.ip_address("fe80::1")}


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("homeassistant.fritz.box:8123", "homeassistant.fritz.box"),
        (" 192.168.0.110:8123", "192.168.0.110"),
        ("[fd00::1]:8123", "fd00::1"),
        ("fd00::1", "fd00::1"),
        ("homeassistant.local", "homeassistant.local"),
        ("", None),
        (None, None),
    ],
)
def test_host_of(address: str | None, expected: str | None) -> None:
    assert host_of(address) == expected


def test_format_address_brackets_ipv6() -> None:
    assert format_address("fd00::1", 8123) == "[fd00::1]:8123"
    assert format_address("192.168.0.110", 8123) == "192.168.0.110:8123"


def test_webio_device_hints_are_every_stored_address() -> None:
    devices = {
        "marker": {"device_ip": None},
        "io": {"device_ip": "homeassistant.lan:8123"},
        "knx": {"device_ip": f"{OWN_IP}:8123"},
        "other": {"device_ip": "homeassistant.lan:8123"},
    }

    assert webio_device_hints(devices) == ("homeassistant.lan:8123", f"{OWN_IP}:8123")
    assert webio_device_hints({}) == ()
    assert webio_device_hints(None) == ()


def test_a_valid_address_of_another_class_beats_a_stale_first_one(dns) -> None:
    """(Review) only the first class's address was checked; a stale one there replaced the valid one."""
    fake = dns({HA_NAME: OWN_IP})

    result = asyncio.run(HaAddressResolver(_FakeHass(INTERNAL_URL)).async_get(f"{FOREIGN_IP}:8123", f"{OWN_IP}:8123"))

    assert result == f"{OWN_IP}:8123"
    assert fake.calls == []  # both are IP literals: no DNS, and no search


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "localhost.localdomain",
        "ha.localhost",
        "127.0.0.2",
        "::1",
        "0.0.0.0",
        "::",
        "::ffff:0.0.0.0",
    ],
)
def test_loopback_names(host: str) -> None:
    assert ha_address._is_loopback(host) is True


@pytest.mark.parametrize("host", ["ha.example.lan", "localhost-ha.lan", "mylocalhost", OWN_IP, "fd00::10", "0.0.0.1"])
def test_no_loopback_names(host: str) -> None:
    assert ha_address._is_loopback(host) is False


@pytest.mark.parametrize(
    "internal_url",
    ["http://localhost.localdomain:8123", "http://ha.localhost:8123", "http://0.0.0.0:8123", "http://[::]:8123"],
)
def test_a_loopback_alias_in_internal_url_is_skipped(dns, internal_url: str) -> None:
    """(Review) only the exact name "localhost" was caught; the alias or an unspecified address reached Comexio."""
    dns(local_ip=OWN_IP)

    assert _get(HaAddressResolver(_FakeHass(internal_url))) == f"{OWN_IP}:8123"


def test_unreadable_adapters_count_as_unknown_own_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    """(Review) an adapter read error aborted the whole resolution, and with it the audit or sync."""

    async def _fail() -> list:
        raise OSError("no interfaces")

    monkeypatch.setattr(ha_address, "async_load_adapters", _fail)

    assert asyncio.run(ha_address._async_own_ips()) is None


@pytest.mark.parametrize(("hint", "expected"), [(f"{OWN_IP}:8123", f"{OWN_IP}:8123"), ("127.0.0.1:8123", None)])
def test_unreadable_adapters_keep_the_hint(monkeypatch: pytest.MonkeyPatch, hint: str, expected: str | None) -> None:
    """The resolution goes on: a stored address is kept unchecked, a loopback one is still ignored."""

    async def _fail() -> list:
        raise OSError("no interfaces")

    monkeypatch.setattr(ha_address, "async_load_adapters", _fail)
    monkeypatch.setattr(ha_address, "_lookup", lambda host: ha_address._Answer(ha_address._Lookup.NOT_FOUND))
    monkeypatch.setattr(ha_address, "_local_ip", lambda: OTHER_OWN_IP)

    result = asyncio.run(HaAddressResolver(_FakeHass()).async_get(None, hint))

    assert result == (expected or f"{OTHER_OWN_IP}:8123")


def test_unreadable_adapters_warn_once_per_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(Review) the read error warned on every poll and sync; now once, and again after a successful read."""
    readable = [False, False, True, False]

    async def _adapters() -> list:
        if not readable.pop(0):
            raise OSError("no interfaces")
        return [{"ipv4": [{"address": OWN_IP}], "ipv6": []}]

    monkeypatch.setattr(ha_address, "async_load_adapters", _adapters)
    resolver = HaAddressResolver(_FakeHass())

    async def _run() -> None:
        for _ in range(4):
            await resolver.async_get(f"{OWN_IP}:8123")

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        asyncio.run(_run())

    levels = [r.levelname for r in caplog.records if "unreadable" in r.getMessage()]
    assert levels == ["WARNING", "DEBUG", "WARNING"]


def test_a_value_error_of_the_lookup_is_a_definite_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname getaddrinfo rejects with ValueError must not abort the audit or the sync."""

    def _reject(host: str, port: None) -> list:
        raise ValueError("invalid hostname")

    monkeypatch.setattr(ha_address.socket, "getaddrinfo", _reject)

    assert _real_lookup("ha.lan").status is ha_address._Lookup.NOT_FOUND
    assert asyncio.run(HaAddressResolver(_FakeHass()).async_addresses("ha.lan")) == frozenset()


@pytest.mark.parametrize(
    ("data", "hints"),
    [
        (
            {"webio_devices": {"marker": {"device_ip": "a.lan:8123"}, "io": {"device_ip": f"{OWN_IP}:8123"}}},
            ("a.lan:8123", f"{OWN_IP}:8123"),
        ),
        (None, ()),
    ],
)
def test_the_coordinator_passes_every_stored_address(data: dict | None, hints: tuple[str, ...]) -> None:
    from unittest.mock import AsyncMock

    from custom_components.comexio.coordinator import ComexioCoordinator

    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.data = data
    coordinator.api = SimpleNamespace(webhook_protocol=None)
    coordinator.ha_address = SimpleNamespace(async_target=AsyncMock(return_value=WebhookTarget("x:8123", False)))

    assert asyncio.run(coordinator.async_ha_address()) == "x:8123"
    coordinator.ha_address.async_target.assert_awaited_once_with(*hints)


@pytest.mark.parametrize(
    ("target", "protocol"),
    [
        (WebhookTarget("x:8123", False), WEBIO_PROTOCOL_HTTP),
        (WebhookTarget("x:8443", True), WEBIO_PROTOCOL_HTTPS),
        # Comexio speaks TLS to 443 by itself: switching there must not rewrite every command.
        (WebhookTarget("x:443", True), WEBIO_PROTOCOL_HTTP),
    ],
)
def test_the_webhook_target_hands_its_protocol_to_the_command_builders(target: WebhookTarget, protocol: int) -> None:
    from unittest.mock import AsyncMock

    from custom_components.comexio.coordinator import ComexioCoordinator

    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.data = None
    coordinator.api = SimpleNamespace(webhook_protocol=None)
    coordinator.ha_address = SimpleNamespace(async_target=AsyncMock(return_value=target))

    assert asyncio.run(coordinator.async_webhook_target()) == target
    assert coordinator.api.webhook_protocol == protocol


@pytest.mark.parametrize(
    ("target", "protocol"),
    [
        (WebhookTarget("ha.example.com:443", True), WEBIO_PROTOCOL_HTTP),
        (WebhookTarget("[fd00::10]:443", True), WEBIO_PROTOCOL_HTTP),
        (WebhookTarget("ha.example.com:8443", True), WEBIO_PROTOCOL_HTTPS),
        (WebhookTarget("[fd00::10]:8123", True), WEBIO_PROTOCOL_HTTPS),
        (WebhookTarget("ha.example.com:4430", True), WEBIO_PROTOCOL_HTTPS),
        (WebhookTarget("ha.example.com:443", False), WEBIO_PROTOCOL_HTTP),
        (WebhookTarget("ha.example.com:8123", False), WEBIO_PROTOCOL_HTTP),
    ],
)
def test_only_https_on_a_port_other_than_443_needs_the_https_protocol(target: WebhookTarget, protocol: int) -> None:
    assert target.webio_protocol == protocol


@pytest.mark.parametrize(
    ("ha", "stored", "mismatch"),
    [
        ("[fd00::110]:8123", "ha6.example.lan:8123", False),  # name resolving to the same IPv6 address
        (f"{OWN_IP}:8123", "ha.example.lan:8123", False),  # name resolving to the same IPv4 address
        (f"{OWN_IP}:8123", "dual.example.lan:8123", False),  # name with v4 + v6, one shared
        (f"{OWN_IP}:8123", f"{FOREIGN_IP}:8123", True),
        (f"{OWN_IP}:8123", "gone.example.lan:8123", True),  # does not resolve
        ("Gone.Example.lan:8123", "gone.example.lan:8123", False),  # neither resolves: same text
        (f"{OWN_IP}:8123", f"{OWN_IP}:8124", True),  # other port
        (f"{OWN_IP}:8123", OWN_IP, True),  # no port
    ],
)
def test_ip_audit_compares_ipv4_and_ipv6_addresses(dns, ha: str, stored: str, mismatch: bool) -> None:
    """(Review) the audit resolved IPv4 only (gethostbyname): a name of an IPv6-only host always mismatched."""
    from custom_components.comexio.coordinator import _device_ip_mismatch

    resolving = {
        "ha6.example.lan": "fd00::110",
        HA_NAME: OWN_IP,
        "dual.example.lan": f"{OWN_IP},fd00::110",
        OWN_IP: OWN_IP,
        FOREIGN_IP: FOREIGN_IP,
        "fd00::110": "fd00::110",
    }
    dns(resolving)

    assert asyncio.run(_device_ip_mismatch(HaAddressResolver(_FakeHass()), ha, stored, "7")) is mismatch


def test_ip_audit_survives_an_invalid_stored_hostname() -> None:
    # coordinator._device_ip_mismatch resolves the stored name too; "ha..lan" raised UnicodeError there.
    from custom_components.comexio.coordinator import _device_ip_mismatch

    mismatch = asyncio.run(_device_ip_mismatch(HaAddressResolver(_FakeHass()), f"{OWN_IP}:8123", "ha..lan:8123", "7"))

    assert mismatch is True


def test_a_confirmed_hint_beats_an_earlier_unchecked_one(dns, caplog: pytest.LogCaptureFixture) -> None:
    """(Review) a stored name without DNS answer was returned before a later hint was even checked."""
    dns({HA_NAME: OWN_IP}, transient={"stale.example.lan", "stale2.example.lan"})
    resolver = HaAddressResolver(_FakeHass())

    with caplog.at_level(logging.WARNING, logger=ha_address.__name__):
        assert asyncio.run(resolver.async_get("stale.example.lan:8123", f"{HA_NAME}:8123")) == f"{HA_NAME}:8123"
    assert "keeping" not in caplog.text  # nothing is "kept" that was not chosen

    # Without a confirmed one the first unchecked hint is kept, ahead of a search — and only it is warned about.
    hints = ("stale.example.lan:8123", f"{FOREIGN_IP}:8123", "stale2.example.lan:8123")
    with caplog.at_level(logging.WARNING, logger=ha_address.__name__):
        assert asyncio.run(resolver.async_get(*hints)) == "stale.example.lan:8123"
    assert "stale.example.lan got no answer; keeping it" in caplog.text
    assert "stale2" not in caplog.text


def test_a_confirmed_remembered_name_beats_an_unchecked_hint(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]: OWN_IP}, transient={"stale.example.lan"}, local_ip="127.0.0.1")
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"  # found by the search, remembered
    fake.calls.clear()

    assert _get(resolver, "stale.example.lan:8123") == f"{HA_HOST_CANDIDATES[0]}:8123"
    assert fake.calls == ["stale.example.lan", HA_HOST_CANDIDATES[0]]  # no new search


def test_with_unknown_own_ips_the_first_hint_is_kept(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    async def _none() -> frozenset:
        return frozenset()

    monkeypatch.setattr(ha_address, "_async_own_ips", _none)

    with caplog.at_level(logging.WARNING, logger=ha_address.__name__):
        result = asyncio.run(HaAddressResolver(_FakeHass()).async_get("a.example.lan:8123", "b.example.lan:8123"))

    assert result == "a.example.lan:8123"
    assert "keeping a.example.lan unchecked" in caplog.text
    assert "b.example.lan" not in caplog.text


@pytest.mark.parametrize(
    ("internal_url", "expected"),
    [
        (None, f"{OWN_IP}:8124"),
        ("http://ha.example.lan", f"{HA_NAME}:8124"),
        ("http://ha.example.lan:8125", f"{HA_NAME}:8125"),
    ],
)
def test_the_port_is_internal_urls_else_the_one_ha_listens_on(dns, internal_url: str | None, expected: str) -> None:
    """(Review) without a port in internal_url the default 8123 was used, even when HA listens elsewhere."""
    dns(local_ip=OWN_IP)
    hass = _FakeHass(internal_url)
    hass.config.api = SimpleNamespace(port=8124)

    assert _get(HaAddressResolver(hass)) == expected


def test_without_a_known_listening_port_it_is_8123(dns) -> None:
    dns(local_ip=OWN_IP)
    hass = _FakeHass()
    hass.config.api = SimpleNamespace(port=None)

    assert _get(HaAddressResolver(hass)) == f"{OWN_IP}:8123"


def test_a_late_search_does_not_undo_a_newer_internal_url(dns) -> None:
    """(Review) a search finishing after a call with internal_url re-published its guess over it."""
    dns(dict.fromkeys(HA_HOST_CANDIDATES, OWN_IP), slow=set(HA_HOST_CANDIDATES), delay=0.2, local_ip="127.0.0.1")
    hass = _FakeHass()
    resolver = HaAddressResolver(hass)

    async def _run() -> tuple[str, str]:
        searching = asyncio.create_task(resolver.async_get())
        await asyncio.sleep(0)  # the search has read the (empty) internal_url and waits for DNS
        hass.config.internal_url = INTERNAL_URL
        configured = await resolver.async_get()
        return await searching, configured

    guessed, configured = asyncio.run(_run())

    assert guessed == f"{HA_HOST_CANDIDATES[0]}:8123"
    assert configured == f"{HA_NAME}:8123"
    assert resolver._guessed_host is None  # the configured URL outranks the guess, also afterwards


def test_the_stored_names_are_looked_up_in_parallel(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    """(Review) the stored names were checked one after the other, so the DNS timeouts of several
    unanswered names added up; the first one in order still wins."""
    names = ("slow1.example.lan", "slow2.example.lan", "slow3.example.lan")
    dns(slow=set(names), delay=1.0)
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.2)
    resolver = HaAddressResolver(_FakeHass())

    async def _run() -> tuple[str, float]:
        started = time.monotonic()
        result = await resolver.async_get(*(f"{name}:8123" for name in names))
        return result, time.monotonic() - started

    result, took = asyncio.run(_run())

    assert result == "slow1.example.lan:8123"
    assert took < 0.4  # one timeout, not three (0.6 s)


@pytest.mark.parametrize(
    ("ha", "stored", "mismatch"),
    [
        (f"{OWN_IP}:8123", "slow.example.lan:8123", True),  # only the stored name hangs
        ("slow.example.lan:8123", f"{FOREIGN_IP}:8123", True),  # only HA's name hangs
        ("slow.example.lan:8123", "slow2.example.lan:8123", True),  # both hang
        ("slow.example.lan:8123", "SLOW.example.lan:8123", False),  # no answer, same name
    ],
)
def test_ip_audit_lookups_are_bounded(
    dns, monkeypatch: pytest.MonkeyPatch, ha: str, stored: str, mismatch: bool
) -> None:
    """(Review) a resolver that does not answer stalled the audit and the sync without limit. Without an
    answer a name only matches itself: the resolver shares these lookups, so a stored name without answer
    differs from ha_address only when the resolver confirmed another address — the one to write."""
    from custom_components.comexio.coordinator import _device_ip_mismatch

    fake = dns(slow={"slow.example.lan", "SLOW.example.lan", "slow2.example.lan"}, delay=0.5)
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    resolver = HaAddressResolver(_FakeHass())

    async def _run() -> tuple[bool, float]:
        started = time.monotonic()
        result = await _device_ip_mismatch(resolver, ha, stored, "7")
        return result, time.monotonic() - started

    result, took = asyncio.run(_run())

    assert result is mismatch
    assert took < 0.4  # both lookups run in parallel, each bounded
    assert sorted(set(fake.calls)) == sorted({host_of(ha), host_of(stored)} - {OWN_IP, FOREIGN_IP})
