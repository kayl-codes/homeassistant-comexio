"""Fixtures for the pure-logic unit tests."""

from unittest.mock import MagicMock

import pytest

from custom_components.comexio import api as api_module
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
