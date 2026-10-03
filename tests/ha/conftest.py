"""Fixtures for the Home Assistant integration tests (pytest-homeassistant-custom-component)."""

from collections.abc import Generator
import inspect
from pathlib import Path
import shutil
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio import api as api_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    DOMAIN,
)
from custom_components.comexio.services import _yaml_sync
from tests.common import load_json_fixture

SERVER_ID = "iosrv1"
HOST = "192.168.1.10"
HA_ADDRESS = "192.168.1.2"


LOGGED_EXCEPTION_MARKER = "logged_exception"


def pytest_configure(config: pytest.Config) -> None:
    """Register the opt-out marker of fail_on_logged_exception."""
    config.addinivalue_line(
        "markers", f"{LOGGED_EXCEPTION_MARKER}: the test expects the integration to log an exception traceback"
    )


@pytest.fixture(autouse=True)
def fail_on_logged_exception(request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture) -> Generator[None]:
    """Fail a test in which the integration logged a traceback it then swallowed.

    The integration catches broadly in many places (webhook handler, listeners, background jobs), so a
    crash there leaves the asserted state untouched and the test green; the logged traceback is the
    only trace. Tests that provoke one on purpose carry @pytest.mark.logged_exception.
    """
    yield
    if request.node.get_closest_marker(LOGGED_EXCEPTION_MARKER):
        return
    logged = [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.get_records("call")
        if record.exc_info and record.name.startswith("custom_components.comexio")
    ]
    assert not logged, f"integration logged exception(s): {logged}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/comexio in every test."""


@pytest.fixture(autouse=True)
def services_yaml_copy(tmp_path: Path) -> Generator[Path]:
    """Redirect the runtime rewrite of services.yaml (plan/backup dropdowns) to a copy.

    Without this, every setup rewrites the checked-in file with the test's plans and entry ids.
    """
    copy = tmp_path / "services.yaml"
    shutil.copyfile(_yaml_sync._SERVICES_YAML_PATH, copy)
    with patch.object(_yaml_sync, "_SERVICES_YAML_PATH", copy):
        yield copy


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry as the config flow creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Comexio {SERVER_ID} ({HOST})",
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={CONF_HOST: HOST, CONF_USERNAME: "admin", CONF_PASSWORD: "admin", CONF_SERVER_ID: SERVER_ID},
    )


@pytest.fixture
def api_returns() -> dict[str, Any]:
    """Return values of the stubbed ComexioAPI coroutines; a test changes them before the setup.

    An exception instance as value is raised instead of returned.
    """
    return {
        "login": True,
        "get_raw_config": load_json_fixture("config_basic.json"),
        "get_live_states": ({}, {}),
    }


@pytest.fixture
def api_attributes() -> dict[str, Any]:
    """Plain attributes set on every ComexioAPI instance (e.g. last_login_error)."""
    return {}


@pytest.fixture
def mock_comexio_api(api_returns: dict[str, Any], api_attributes: dict[str, Any]) -> Generator[list[ComexioAPI]]:
    """The one place the HA tests construct a ComexioAPI; yields every instance created.

    Pure parsing (parse_config) stays real and runs on the synthetic fixtures; every coroutine
    method is an AsyncMock (None unless api_returns names it), so no test reaches the network,
    including through a method added later. Once the client moves into aiocomexio, only this
    fixture follows.
    """
    stubbed = {name for name, _ in inspect.getmembers(ComexioAPI, inspect.iscoroutinefunction)}
    assert set(api_returns) <= stubbed, f"api_returns names no ComexioAPI coroutine: {set(api_returns) - stubbed}"
    created: list[ComexioAPI] = []

    def _factory(*args: Any, **kwargs: Any) -> ComexioAPI:
        with (
            patch.object(api_module, "async_create_clientsession", return_value=MagicMock()),
            patch.object(ComexioAPI, "_build_session_kwargs", return_value={}),
        ):
            api = ComexioAPI(*args, **kwargs)
        for name in stubbed:
            value = api_returns.get(name)
            if isinstance(value, BaseException):
                setattr(api, name, AsyncMock(name=name, side_effect=value))
            else:
                setattr(api, name, AsyncMock(name=name, return_value=value))
        api.close = MagicMock(name="close")
        api.io_types = load_json_fixture("io_types.json")
        api.io_input_types = load_json_fixture("io_input_types.json")
        for name, value in api_attributes.items():
            assert hasattr(api, name), f"api_attributes names no ComexioAPI attribute: {name}"
            setattr(api, name, value)
        created.append(api)
        return api

    with (
        patch("custom_components.comexio.ComexioAPI", side_effect=_factory),
        patch("custom_components.comexio.config_flow.ComexioAPI", side_effect=_factory),
        # DNS is disabled in tests; the resolver's own logic has unit tests (test_ha_address.py).
        patch("custom_components.comexio.ha_address.HaAddressResolver.async_get", return_value=HA_ADDRESS),
    ):
        yield created
