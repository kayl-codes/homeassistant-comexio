"""ComexioAPI's adapters over aiocomexio.ComexioClient: each ComexioError onto the old return contract."""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioConnectionError,
    ComexioDataError,
    ComexioRequestRejectedError,
    ComexioResponseError,
    LiveStates,
    RawConfig,
    WebioBaseInfo,
)
import aiohttp
import pytest

from custom_components.comexio import api as api_module
from custom_components.comexio.api import ComexioAPI


def _connection_error() -> ComexioConnectionError:
    try:
        try:
            raise aiohttp.ServerDisconnectedError
        except aiohttp.ClientError as err:
            raise ComexioConnectionError("gone") from err
    except ComexioConnectionError as err:
        return err


HTTP_ERROR = ComexioResponseError("HTTP 500", status=500)


@pytest.fixture
def client(comexio_api: ComexioAPI) -> MagicMock:
    """Replace the main-session client with a mock (kept as long as the credentials stay)."""
    fake = MagicMock()
    comexio_api._client = fake
    comexio_api._client_credentials = comexio_api._credentials()
    return fake


def _fail(client: MagicMock, method: str, err: Exception) -> None:
    setattr(client, method, AsyncMock(side_effect=err))


def _install_preview(comexio_api: ComexioAPI, session: MagicMock, preview: MagicMock) -> None:
    """A preview session logged in with the current connection settings."""
    comexio_api._preview_session, comexio_api._preview_client = session, preview
    comexio_api._preview_credentials = comexio_api._credentials()


@pytest.mark.parametrize(
    ("err", "reason"),
    [(ComexioAuthenticationError("no"), "rejected"), (_connection_error(), "connection"), (HTTP_ERROR, "connection")],
)
def test_login_failure_sets_reason(comexio_api: ComexioAPI, client: MagicMock, err: Exception, reason: str) -> None:
    _fail(client, "login", err)
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == reason


def test_login_success_clears_reason(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api.last_login_error = "connection"
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    assert comexio_api.last_login_error is None


def test_login_keeps_a_session_that_is_still_logged_in(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression (review 8b-2a): the lib's login clears the cookie jar, which would log out the
    # session a running poll or sync is using on every service call.
    comexio_api._logged_in_client = client
    client.get_bus_workload = AsyncMock(return_value={})
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_not_awaited()


def test_login_logs_in_again_when_the_probe_fails(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._logged_in_client = client
    _fail(client, "get_bus_workload", ComexioAuthenticationError("lapsed"))
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_awaited_once()


@pytest.mark.parametrize("err", [HTTP_ERROR, _connection_error()])
def test_login_keeps_the_session_when_the_probe_cannot_reach_comexio(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # Review 8b-2a: a busy or unreachable server is no lapsed login — no jar-clearing full login.
    comexio_api._logged_in_client = client
    _fail(client, "get_bus_workload", err)
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == "connection"
    client.login.assert_not_awaited()


def test_concurrent_full_logins_log_in_once(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Review 8b-2a: a second full login would clear the jar of the one that just succeeded.
    async def slow_login() -> None:
        await asyncio.sleep(0)

    client.login = AsyncMock(side_effect=slow_login)

    async def both() -> list[bool]:
        return list(await asyncio.gather(comexio_api._full_login(), comexio_api._full_login()))

    assert asyncio.run(both()) == [True, True]
    client.login.assert_awaited_once()


def test_failed_login_forgets_the_logged_in_client(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._logged_in_client = client
    _fail(client, "get_bus_workload", ComexioAuthenticationError("lapsed"))
    _fail(client, "login", _connection_error())
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api._logged_in_client is None


def test_client_is_rebuilt_on_new_credentials(comexio_api: ComexioAPI) -> None:
    first = comexio_api.client
    assert comexio_api.client is first
    comexio_api.password = "changed"
    assert comexio_api.client is not first


@pytest.mark.parametrize("main_client_built", [True, False])
def test_new_connection_settings_drop_the_preview_session(comexio_api: ComexioAPI, main_client_built: bool) -> None:
    # The preview client and its cookie belong to the old host / credentials — also when the
    # main client was never built before the settings changed.
    if main_client_built:
        comexio_api.client  # noqa: B018 - build the client for the current settings
    session = MagicMock()
    _install_preview(comexio_api, session, MagicMock())
    comexio_api.host = "10.0.0.2"
    comexio_api.client  # noqa: B018 - reading the client is what drops the preview session
    assert comexio_api._preview_session is None
    assert comexio_api._preview_client is None
    session.detach.assert_called_once()


def test_preview_session_is_kept_while_the_settings_are_unchanged(comexio_api: ComexioAPI) -> None:
    session = MagicMock()
    _install_preview(comexio_api, session, MagicMock())
    comexio_api.client  # noqa: B018 - first build of the main client
    assert asyncio.run(comexio_api.ensure_preview_session()) is session
    session.detach.assert_not_called()


def test_logged_in_preview_session_is_kept(comexio_api: ComexioAPI) -> None:
    # A preview session that logged in under the current settings survives later client reads.
    async def login_ok(_client: object) -> bool:
        return True

    comexio_api._login = login_ok  # type: ignore[method-assign]
    session = asyncio.run(comexio_api.ensure_preview_session())
    assert session is not None
    comexio_api.client  # noqa: B018 - must not drop the fresh preview session
    assert asyncio.run(comexio_api.ensure_preview_session()) is session
    session.detach.assert_not_called()


def test_waiting_preview_tick_does_not_reuse_an_outdated_session(comexio_api: ComexioAPI) -> None:
    # A tick that waited for the lock while another one logged in must not return that session
    # when the settings changed in between.
    session = MagicMock()

    async def scenario() -> aiohttp.ClientSession | None:
        comexio_api._login = AsyncMock(return_value=False)  # type: ignore[method-assign]
        async with comexio_api._preview_session_lock:
            waiter = asyncio.ensure_future(comexio_api.ensure_preview_session())
            await asyncio.sleep(0)
            _install_preview(comexio_api, session, MagicMock())
            comexio_api.host = "10.0.0.2"
        return await waiter

    assert asyncio.run(scenario()) is None
    session.detach.assert_called_once()


def test_preview_login_under_old_settings_is_discarded(comexio_api: ComexioAPI) -> None:
    # Settings that change while the preview login is in flight must not leave a session
    # bound to the old host behind; the next poll opens a fresh one.
    async def login_while_reconfigured(_client: object) -> bool:
        comexio_api.host = "10.0.0.2"
        return True

    comexio_api._login = login_while_reconfigured  # type: ignore[method-assign]
    assert asyncio.run(comexio_api.ensure_preview_session()) is None
    assert comexio_api._preview_session is None
    assert comexio_api._preview_client is None


def test_get_raw_config_reraises_the_transport_error(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Callers (and DataUpdateCoordinator) have always seen transport failures as aiohttp errors.
    _fail(client, "get_raw_config", _connection_error())
    call = comexio_api.get_raw_config()
    with pytest.raises(aiohttp.ServerDisconnectedError):
        asyncio.run(call)


@pytest.mark.parametrize("err", [HTTP_ERROR, ComexioDataError("no $FubModules")])
def test_get_raw_config_returns_empty_when_the_scrape_fails(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    _fail(client, "get_raw_config", err)
    assert asyncio.run(comexio_api.get_raw_config()) == {}


def test_get_raw_config_logs_in_again_on_a_lapsed_session(comexio_api: ComexioAPI, client: MagicMock) -> None:
    variables = {"FubModules": {"2": {}}}
    client.get_raw_config = AsyncMock(
        side_effect=[ComexioAuthenticationError("lapsed"), RawConfig(variables, {}, {}, None)]
    )
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.get_raw_config()) is variables
    client.login.assert_awaited_once()
    assert comexio_api._logged_in_client is client


def test_get_raw_config_failed_re_login_is_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "get_raw_config", ComexioAuthenticationError("lapsed"))
    _fail(client, "login", ComexioAuthenticationError("rejected"))
    assert asyncio.run(comexio_api.get_raw_config()) == {}
    assert comexio_api.last_login_error == "rejected"


def test_get_raw_config_keeps_io_types_and_version(comexio_api: ComexioAPI, client: MagicMock) -> None:
    variables = {"FubModules": {"2": {}}}
    client.get_raw_config = AsyncMock(return_value=RawConfig(variables, {"1": {}}, {"2": {}}, "11.0.2"))
    assert asyncio.run(comexio_api.get_raw_config()) is variables
    assert (comexio_api.io_types, comexio_api.io_input_types, comexio_api.comexio_version) == (
        {"1": {}},
        {"2": {}},
        "11.0.2",
    )


def test_get_live_states_failure_is_none_not_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "get_live_states", _connection_error())
    assert asyncio.run(comexio_api.get_live_states(5, 2)) == (None, None)


def test_get_live_states_splits_markers_and_knx(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_live_states = AsyncMock(return_value=LiveStates(markers={"5": 1}, knx={"5": 0}))
    assert asyncio.run(comexio_api.get_live_states(5, 5)) == ({"5": 1}, {"5": 0})


def test_knx_catalog_failure_keeps_last_known_good(comexio_api: ComexioAPI, client: MagicMock) -> None:
    catalog = {"KnxPoints": {"1": {}}}
    comexio_api.seed_knx_dpt_catalog(catalog, "11.0.1")
    comexio_api.comexio_version = "11.0.2"
    _fail(client, "get_knx_dpt_catalog", ComexioDataError("no data"))
    assert asyncio.run(comexio_api.get_knx_dpt_catalog()) is catalog
    # The failed fetch is not cached as current, so the next poll retries.
    assert comexio_api.get_knx_dpt_catalog_snapshot() == (catalog, "11.0.1")


def test_knx_catalog_failure_without_cache_is_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "get_knx_dpt_catalog", _connection_error())
    assert asyncio.run(comexio_api.get_knx_dpt_catalog()) == {}
    assert comexio_api.get_knx_dpt_catalog_snapshot() is None


def test_connection_values_shape_error_is_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "get_function_plan_connection_values", ComexioDataError("no result.connection"))
    assert asyncio.run(comexio_api.get_function_plan_connection_values(3)) == {}


def test_connection_values_shape_error_warns_once_per_plan(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    _fail(client, "get_function_plan_connection_values", ComexioDataError("no result.connection"))
    caplog.set_level(logging.DEBUG, logger=api_module.__name__)
    for _ in range(3):
        asyncio.run(comexio_api.get_function_plan_connection_values(3))
    levels = [r.levelno for r in caplog.records if r.name == api_module.__name__]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    client.get_function_plan_connection_values = AsyncMock(return_value={"7": [1]})
    assert asyncio.run(comexio_api.get_function_plan_connection_values(3)) == {"7": [1]}
    assert 3 not in comexio_api._connection_values_shape_warned


@pytest.mark.parametrize("err", [HTTP_ERROR, _connection_error()])
def test_connection_values_failure_reaches_the_circuit_breaker(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # Regression (#75): a failure must not pass as the "plan not running" {}.
    _fail(client, "get_function_plan_connection_values", err)
    call = comexio_api.get_function_plan_connection_values(3)
    with pytest.raises(type(err)):
        asyncio.run(call)


def test_lapsed_preview_session_is_dropped(comexio_api: ComexioAPI) -> None:
    session, preview = MagicMock(), MagicMock()
    _fail(preview, "get_function_plan_connection_values", ComexioAuthenticationError("lapsed"))
    _install_preview(comexio_api, session, preview)
    call = comexio_api.get_function_plan_connection_values(3, session=session)
    with pytest.raises(ComexioAuthenticationError):
        asyncio.run(call)
    preview.get_function_plan_connection_values.assert_awaited_once_with(3)
    assert comexio_api._preview_session is None
    assert comexio_api._preview_client is None
    session.detach.assert_called_once()


def test_replaced_preview_session_is_not_used(comexio_api: ComexioAPI) -> None:
    preview = MagicMock()
    _install_preview(comexio_api, MagicMock(), preview)
    call = comexio_api.get_function_plan_connection_values(3, session=MagicMock())
    with pytest.raises(ComexioAuthenticationError):
        asyncio.run(call)
    preview.get_function_plan_connection_values.assert_not_called()


def test_load_elements_failure_is_none(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "load_function_plan", ComexioDataError("no connections collection"))
    assert asyncio.run(comexio_api.function_plan_load_elements(3, strict=True)) is None


def test_load_all_plans_asks_only_for_cached_plans(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._fub_data = {"1": {}, "4": {}}
    plans = {1: {"elements": {}, "connections": {}}}
    client.load_all_function_plans = AsyncMock(return_value=plans)
    assert asyncio.run(comexio_api.function_plan_load_all_plans(strict=True)) == plans
    client.load_all_function_plans.assert_awaited_once_with({1, 4}, strict=True)


def test_load_all_plans_failure_is_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._fub_data = {"1": {}}
    _fail(client, "load_all_function_plans", HTTP_ERROR)
    assert asyncio.run(comexio_api.function_plan_load_all_plans()) == {}


@pytest.mark.parametrize(
    ("args", "method", "expected_call"),
    [
        (("marker", "5", 1), "set_marker_value", (5, 1)),
        (("knx", 7, 0.5), "set_knx_value", (7, 0.5)),
        (("io", "x", 1, "UD1", "DO3"), "set_io_value", ("UD1", "DO3", 1)),
    ],
)
def test_set_value_routes_by_target_type(
    comexio_api: ComexioAPI, client: MagicMock, args: tuple[Any, ...], method: str, expected_call: tuple[Any, ...]
) -> None:
    setattr(client, method, AsyncMock())
    assert asyncio.run(comexio_api.set_value(*args)) is True
    getattr(client, method).assert_awaited_once_with(*expected_call)


@pytest.mark.parametrize(
    "args",
    [("io", "x", 1), ("io", "x", 1, "UD1"), ("marker", "M5", 1)],
)
def test_set_value_rejects_incomplete_targets(
    comexio_api: ComexioAPI, client: MagicMock, args: tuple[Any, ...]
) -> None:
    assert asyncio.run(comexio_api.set_value(*args)) is False


def test_set_value_failure_is_false(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "set_marker_value", ComexioAuthenticationError("API credentials rejected"))
    assert asyncio.run(comexio_api.set_value("marker", 5, 1)) is False


@pytest.mark.parametrize(
    ("call", "method", "expected"),
    [
        (lambda api: api.get_bus_workload(), "get_bus_workload", {}),
        (lambda api: api.check_extension_firmware(), "check_extension_firmware", []),
        (lambda api: api.system_emergency_reboot(), "system_emergency_reboot", False),
    ],
)
def test_misc_reads_fall_back_on_failure(
    comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str, expected: Any
) -> None:
    _fail(client, method, _connection_error())
    assert asyncio.run(call(comexio_api)) == expected


# --- Web-IO ---


@pytest.mark.parametrize("err", [ComexioAuthenticationError("login form"), _connection_error(), HTTP_ERROR])
@pytest.mark.parametrize(
    ("call", "method"),
    [
        (lambda api: api.get_webio_base_info("HA [M]"), "get_webio_base_info"),
        (lambda api: api.get_webio_device_info("HA [M]"), "get_webio_device_id"),
    ],
)
def test_webio_lookup_failure_raises_instead_of_reporting_absent(
    comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str, err: Exception
) -> None:
    # None means "absent" to every caller, which then uploads or recreates the class — a failed
    # check (a lapsed session included) must not read as that.
    _fail(client, method, err)
    with pytest.raises(RuntimeError):
        asyncio.run(call(comexio_api))


def test_webio_base_info_keeps_the_tuple_contract(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_webio_base_info = AsyncMock(side_effect=[WebioBaseInfo(base_id="7", deletable=False), None])
    assert asyncio.run(comexio_api.get_webio_base_info("HA [M]")) == ("7", False)
    assert asyncio.run(comexio_api.get_webio_base_info("HA [M]")) is None


def test_webio_device_info_passes_the_id_through(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_webio_device_id = AsyncMock(return_value="12")
    assert asyncio.run(comexio_api.get_webio_device_info("HA [M]")) == "12"


_WEBIO_WRITES = [
    (lambda api: api.delete_webio_device(3), "delete_webio_device"),
    (lambda api: api.delete_webio_base(3), "delete_webio_base"),
    (lambda api: api.update_webio_device_ip(3, "10.0.0.2:8123", "HA [M]"), "update_webio_device_address"),
    (lambda api: api.delete_single_command(5, 3), "delete_webio_command"),
    (lambda api: api.create_webio_device("HA [M]", 7, "10.0.0.2:8123"), "create_webio_device"),
]


@pytest.mark.parametrize(("call", "method"), _WEBIO_WRITES)
def test_webio_write_failure_is_false(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    _fail(client, method, ComexioRequestRejectedError("not confirmed"))
    assert asyncio.run(call(comexio_api)) is False


@pytest.mark.parametrize(("call", "method"), _WEBIO_WRITES)
def test_webio_write_success_is_true(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    setattr(client, method, AsyncMock(return_value=True if method == "delete_webio_device" else None))
    assert asyncio.run(call(comexio_api)) is True


def test_webio_device_in_use_is_false(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.delete_webio_device = AsyncMock(return_value=False)
    assert asyncio.run(comexio_api.delete_webio_device(3)) is False


def test_upload_web_io_returns_the_base_id(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.upload_webio_class = AsyncMock(return_value="42")
    assert asyncio.run(comexio_api.upload_web_io("iosrv1", "HA [M]", "{}")) == (True, "42")
    client.upload_webio_class.assert_awaited_once_with("{}", class_name="HA [M]", filename="ha_iosrv1.json")


def test_upload_web_io_failure_carries_the_reason(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "upload_webio_class", ComexioRequestRejectedError("refused: no ok"))
    ok, reason = asyncio.run(comexio_api.upload_web_io("iosrv1", "HA [M]", "{}"))
    assert ok is False
    assert "refused" in reason


_COMMAND = {"Name": "M1 Test", "Parameter": "/api/webhook/x", "Data": "{}", "TypeId": 1, "Min": 0, "Max": 1}


@pytest.mark.parametrize(("existing", "expected"), [(None, None), ("", None), ("17", "17")])
def test_save_single_command_treats_a_falsy_id_as_new(
    comexio_api: ComexioAPI, client: MagicMock, existing: Any, expected: Any
) -> None:
    client.save_webio_command = AsyncMock()
    assert asyncio.run(comexio_api.save_single_command(9, 3, _COMMAND, existing_cmd_id=existing)) is True
    client.save_webio_command.assert_awaited_once_with(3, _COMMAND, base_id=9, command_id=expected)


@pytest.mark.parametrize("err", [ComexioConnectionError("gone"), ValueError("id is no number")])
def test_save_single_command_failure_is_false(comexio_api: ComexioAPI, client: MagicMock, err: Exception) -> None:
    _fail(client, "save_webio_command", err)
    assert asyncio.run(comexio_api.save_single_command(9, 3, _COMMAND, existing_cmd_id="x")) is False


@pytest.mark.parametrize("err", [ComexioDataError("no min/max fields"), _connection_error()])
def test_webio_command_range_failure_is_none(comexio_api: ComexioAPI, client: MagicMock, err: Exception) -> None:
    _fail(client, "get_webio_command_range", err)
    assert asyncio.run(comexio_api.get_webio_command_range(5, 3)) == (None, None)
