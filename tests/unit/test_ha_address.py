"""HA webhook address resolution (ha_address.py): precedence, parallel search, caches, hint."""

import asyncio
from collections.abc import Callable
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio import ha_address
from custom_components.comexio.ha_address import (
    HA_HOST_CANDIDATES,
    HaAddressResolver,
    format_address,
    host_of,
    webio_device_hint,
)


class _FakeHass:
    """Just enough of HomeAssistant for the resolver: config.internal_url + executor jobs."""

    def __init__(self, internal_url: str | None = None) -> None:
        self.config = SimpleNamespace(internal_url=internal_url)

    def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> asyncio.Future:
        return asyncio.get_running_loop().run_in_executor(None, func, *args)


class _FakeDns:
    """Records lookups; names in `resolving` resolve, names in `slow` block before failing,
    names in `transient` fail like a resolver that is not reachable yet (EAI_AGAIN)."""

    def __init__(
        self, resolving: set[str], slow: set[str] | None = None, delay: float = 0.3, transient: set[str] | None = None
    ) -> None:
        self.resolving = resolving
        self.slow = slow or set()
        self.transient = transient or set()
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def lookup(self, host: str) -> ha_address._Lookup:
        with self._lock:
            self.calls.append(host)
        if host in self.slow:
            time.sleep(self.delay)
        if host in self.transient:
            return ha_address._Lookup.NO_ANSWER
        return ha_address._Lookup.RESOLVED if host in self.resolving else ha_address._Lookup.NOT_FOUND


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeDns]:
    def _install(resolving: set[str], **kwargs: Any) -> _FakeDns:
        fake = _FakeDns(resolving, **kwargs)
        monkeypatch.setattr(ha_address, "_lookup", fake.lookup)
        monkeypatch.setattr(ha_address, "_local_ip", lambda: "10.0.0.7")
        return fake

    return _install


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


def test_first_resolving_candidate_in_known_domains_order_wins(dns) -> None:
    dns({HA_HOST_CANDIDATES[4], HA_HOST_CANDIDATES[1]})

    assert _get(HaAddressResolver(_FakeHass())) == f"{HA_HOST_CANDIDATES[1]}:8123"


def test_port_comes_from_internal_url(dns) -> None:
    dns({HA_HOST_CANDIDATES[0]})

    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8124"))

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8124"


def test_candidates_are_looked_up_in_parallel(dns) -> None:
    # Every miss is slow: sequentially this would take len(candidates) * delay.
    fake = dns(set(), slow=set(HA_HOST_CANDIDATES), delay=0.3)

    result, elapsed = _timed_get(HaAddressResolver(_FakeHass("http://192.168.0.110:8123")))

    assert result == "192.168.0.110:8123"
    assert elapsed < 0.3 * len(HA_HOST_CANDIDATES) / 2
    assert sorted(fake.calls) == sorted(HA_HOST_CANDIDATES)


def test_hanging_lookup_is_capped_by_timeout(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.1)
    dns({HA_HOST_CANDIDATES[2]}, slow={HA_HOST_CANDIDATES[0]}, delay=1.0)

    result, elapsed = _timed_get(HaAddressResolver(_FakeHass()))

    assert result == f"{HA_HOST_CANDIDATES[2]}:8123"
    assert elapsed < 1.0


def test_known_name_bounds_the_search(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[3], HA_HOST_CANDIDATES[5]})
    resolver = HaAddressResolver(_FakeHass())
    assert _get(resolver) == f"{HA_HOST_CANDIDATES[3]}:8123"
    fake.calls.clear()

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[3]}:8123"
    # Candidates 0-2 missed definitively last time and are cached; nothing behind 3 is touched.
    assert fake.calls == [HA_HOST_CANDIDATES[3]]


def test_higher_priority_name_still_wins_over_the_known_one(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_NEGATIVE_CACHE_SEC", 0)
    fake = dns({HA_HOST_CANDIDATES[3]})
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.resolving = {HA_HOST_CANDIDATES[1], HA_HOST_CANDIDATES[3]}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[1]}:8123"


def test_cached_name_that_stops_resolving_triggers_a_new_search(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[3]})
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.resolving = {HA_HOST_CANDIDATES[5]}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[5]}:8123"


def test_definite_misses_are_cached_and_fall_back_to_internal_url(dns) -> None:
    fake = dns(set())
    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8123"))
    assert _get(resolver) == "192.168.0.110:8123"
    fake.calls.clear()

    assert _get(resolver) == "192.168.0.110:8123"
    assert fake.calls == []


def test_misses_are_looked_up_again_after_the_cache_expires(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_NEGATIVE_CACHE_SEC", 0)
    fake = dns(set())
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.resolving = {HA_HOST_CANDIDATES[0]}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_timeout_keeps_the_known_name_instead_of_falling_back(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    fake = dns({HA_HOST_CANDIDATES[0]}, delay=0.3)
    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8123"))
    _get(resolver)
    # DNS hangs: every lookup times out
    fake.slow = set(HA_HOST_CANDIDATES)
    fake.resolving = set()

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_timeouts_are_not_cached_as_misses(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    fake = dns(set(), slow=set(HA_HOST_CANDIDATES), delay=0.3)
    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8123"))
    assert _get(resolver) == "192.168.0.110:8123"
    fake.slow = set()
    fake.resolving = {HA_HOST_CANDIDATES[0]}

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_transient_resolver_failure_keeps_the_known_name_and_is_not_cached(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]})
    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8123"))
    _get(resolver)
    # DNS not reachable (e.g. router restart): EAI_AGAIN for every name
    fake.transient = set(HA_HOST_CANDIDATES)

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"
    fake.transient = set()
    fake.calls.clear()

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[0]}:8123"
    assert fake.calls == [HA_HOST_CANDIDATES[0]]


def test_known_name_that_is_definitely_gone_is_dropped_despite_other_unanswered_names(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[3]})
    resolver = HaAddressResolver(_FakeHass("http://192.168.0.110:8123"))
    _get(resolver)
    # The known name is gone for good (NXDOMAIN) while another candidate gets no answer.
    fake.resolving = set()
    fake.transient = {HA_HOST_CANDIDATES[5]}

    assert _get(resolver) == "192.168.0.110:8123"
    assert _get(resolver) == "192.168.0.110:8123"


def test_unanswered_known_name_is_not_replaced_by_a_lower_priority_name(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[3]})
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.transient = {HA_HOST_CANDIDATES[3]}
    fake.resolving = {HA_HOST_CANDIDATES[5]}
    fake.calls.clear()

    assert _get(resolver) == f"{HA_HOST_CANDIDATES[3]}:8123"
    assert HA_HOST_CANDIDATES[5] not in fake.calls


def test_keeping_an_unanswered_name_warns_once(dns, caplog: pytest.LogCaptureFixture) -> None:
    fake = dns({HA_HOST_CANDIDATES[0]})
    resolver = HaAddressResolver(_FakeHass())
    _get(resolver)
    fake.transient = {HA_HOST_CANDIDATES[0]}

    with caplog.at_level("DEBUG", logger=ha_address.__name__):
        _get(resolver)
        _get(resolver)

    kept = [r.levelname for r in caplog.records if "keeping" in r.getMessage()]
    assert kept == ["WARNING", "DEBUG"]


def test_cold_start_timeout_keeps_the_web_io_devices_name(dns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_address, "HA_ADDRESS_DNS_TIMEOUT_SEC", 0.05)
    dns(set(), slow={HA_HOST_CANDIDATES[6]}, delay=0.3)

    result = _get(HaAddressResolver(_FakeHass("http://192.168.0.110:8123")), hint=f"{HA_HOST_CANDIDATES[6]}:8123")

    assert result == f"{HA_HOST_CANDIDATES[6]}:8123"


def test_cold_start_definite_miss_of_the_hint_falls_back(dns) -> None:
    dns(set())

    result = _get(HaAddressResolver(_FakeHass("http://192.168.0.110:8123")), hint=f"{HA_HOST_CANDIDATES[6]}:8123")

    assert result == "192.168.0.110:8123"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.gaierror(socket.EAI_NONAME, "not known"), ha_address._Lookup.NOT_FOUND),
        (socket.gaierror(socket.EAI_AGAIN, "temporary failure"), ha_address._Lookup.NO_ANSWER),
        (OSError("network unreachable"), ha_address._Lookup.NO_ANSWER),
        (None, ha_address._Lookup.RESOLVED),
    ],
)
def test_lookup_tells_definite_misses_from_transient_failures(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, expected
) -> None:
    def _gethostbyname(host: str) -> str:
        if error is not None:
            raise error
        return "10.0.0.7"

    monkeypatch.setattr(socket, "gethostbyname", _gethostbyname)

    assert ha_address._lookup("homeassistant.lan") is expected


def test_hint_from_the_web_io_device_bounds_a_cold_search(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[6], HA_HOST_CANDIDATES[7]})

    result = _get(HaAddressResolver(_FakeHass()), hint=f"{HA_HOST_CANDIDATES[6]}:8123")

    assert result == f"{HA_HOST_CANDIDATES[6]}:8123"
    assert sorted(fake.calls) == sorted(HA_HOST_CANDIDATES[:7])


def test_hint_does_not_override_a_higher_priority_name(dns) -> None:
    dns({HA_HOST_CANDIDATES[0], HA_HOST_CANDIDATES[6]})

    assert _get(HaAddressResolver(_FakeHass()), hint=f"{HA_HOST_CANDIDATES[6]}:8123") == f"{HA_HOST_CANDIDATES[0]}:8123"


def test_hint_that_is_no_candidate_is_ignored(dns) -> None:
    fake = dns({HA_HOST_CANDIDATES[0], "ha.example.org"})

    assert _get(HaAddressResolver(_FakeHass()), hint="ha.example.org:8123") == f"{HA_HOST_CANDIDATES[0]}:8123"
    assert "ha.example.org" not in fake.calls


def test_invalid_internal_url_port_falls_back_to_default(dns) -> None:
    dns(set())

    assert _get(HaAddressResolver(_FakeHass("http://192.168.0.110:99999"))) == "192.168.0.110:8123"


@pytest.mark.parametrize("internal_url", [None, "http://localhost:8123", "http://127.0.0.1:8123"])
def test_loopback_or_missing_internal_url_falls_back_to_local_ip(dns, internal_url: str | None) -> None:
    dns(set())

    assert _get(HaAddressResolver(_FakeHass(internal_url))) == "10.0.0.7:8123"


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


def test_webio_device_hint_takes_the_first_stored_address() -> None:
    devices = {"marker": {"device_ip": None}, "io": {"device_ip": "homeassistant.lan:8123"}}

    assert webio_device_hint(devices) == "homeassistant.lan:8123"
    assert webio_device_hint({}) is None
    assert webio_device_hint(None) is None
