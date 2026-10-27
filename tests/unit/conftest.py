"""Fixtures for the pure-logic unit tests."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.comexio import api as api_module, plan_watchdog
from custom_components.comexio.api import ComexioAPI
from tests.common import load_json_fixture


@pytest.fixture
def comexio_api(monkeypatch: pytest.MonkeyPatch) -> ComexioAPI:
    """ComexioAPI instance for parser tests — no HA instance, no network.

    The only place that constructs a ComexioAPI in the unit tests: once the communication
    layer moves into its own library, only this fixture needs to follow the new constructor.
    """
    monkeypatch.setattr(api_module, "async_create_clientsession", lambda *_args, **_kwargs: MagicMock())
    # The real kwargs build an aiohttp CookieJar, which needs a running event loop.
    monkeypatch.setattr(ComexioAPI, "_build_session_kwargs", lambda _self: {})
    return ComexioAPI(MagicMock(), "192.168.1.10", "admin", "admin")


@pytest.fixture
def parsing_api(comexio_api: ComexioAPI) -> ComexioAPI:
    """ComexioAPI with $IOTypesBinary / $IOInputTypes preloaded, as get_raw_config() leaves it."""
    comexio_api.io_types = load_json_fixture("io_types.json")
    comexio_api.io_input_types = load_json_fixture("io_input_types.json")
    return comexio_api


# IO type tables for config_quickbus.json, as Comexio sends them: 134 = Multitouch gesture, 126 = motion (PIR),
# 12 = temperature, 111 = LED colour channel.
QUICKBUS_IO_TYPES = {
    "1": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "12": {"binary": False, "min": 0, "max": 100, "unit": "°C"},
    "111": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "126": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "134": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
QUICKBUS_IO_INPUT_TYPES = {"1": {"input": True}, "12": {"input": True}, "111": {"input": True}}


@pytest.fixture
def quickbus_parsing_api(parsing_api: ComexioAPI) -> ComexioAPI:
    """parsing_api with the IO type tables of the synthetic Quickbus installation (config_quickbus.json)."""
    parsing_api.io_types = QUICKBUS_IO_TYPES
    parsing_api.io_input_types = QUICKBUS_IO_INPUT_TYPES
    return parsing_api


@pytest.fixture
def quickbus_result(quickbus_parsing_api: ComexioAPI) -> dict[str, Any]:
    """parse_config() result of the synthetic Quickbus installation (config_quickbus.json)."""
    return quickbus_parsing_api.parse_config(load_json_fixture("config_quickbus.json"))


@pytest.fixture(autouse=True)
def _fresh_push_tag_state() -> Iterator[None]:
    """The watchdog's push tag state is module-wide (shared with a reloaded entry's watchdog): a tag one test
    left would lock or clear the next test's pushes."""
    yield
    plan_watchdog._PUSH_LOCKS.clear()
    plan_watchdog._ACTIONABLE_TAGS.clear()
