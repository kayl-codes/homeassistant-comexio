"""ComexioAPI's adapters over aiocomexio.ComexioClient: each ComexioError onto the old return contract."""

import asyncio
import contextlib
from datetime import timedelta
import logging
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
    ComexioDataError,
    ComexioRequestRejectedError,
    ComexioResponseError,
    CreatedFunctionPlan,
    LiveStates,
    RawConfig,
    SystemInfo,
    TimeServerSettings,
    WebioBaseInfo,
    WebioCommandForm,
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
    client.is_logged_in = AsyncMock(return_value=True)
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_not_awaited()


def test_login_logs_in_again_when_the_session_has_expired(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._logged_in_client = client
    client.is_logged_in = AsyncMock(return_value=False)
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_awaited_once()


@pytest.mark.parametrize("err", [ComexioDataError("an error page"), ComexioAuthenticationError("lapsed")])
def test_login_logs_in_again_when_the_session_state_is_unknown(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # Neither the session's answer nor the login form: only a full login leads back to a known state.
    comexio_api._logged_in_client = client
    _fail(client, "is_logged_in", err)
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_awaited_once()


def test_unknown_session_state_reports_connection_when_the_login_fails(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    comexio_api._logged_in_client = client
    _fail(client, "is_logged_in", ComexioDataError("an error page"))
    _fail(client, "login", HTTP_ERROR)
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == "connection"


@pytest.mark.parametrize("err", [HTTP_ERROR, _connection_error()])
def test_login_keeps_the_session_when_the_probe_cannot_reach_comexio(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # Review 8b-2a: a busy or unreachable server is no lapsed login — no jar-clearing full login.
    comexio_api._logged_in_client = client
    _fail(client, "is_logged_in", err)
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


async def _slow_refusal() -> None:
    await asyncio.sleep(0)  # the other caller is on its way before Comexio answers
    raise ComexioAuthenticationError("no")


def test_refused_credentials_are_not_sent_again(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Review (Sourcery, #212/#213): with reauth pending, no poll may retry them — a lockout risk."""
    client.login = AsyncMock(side_effect=_slow_refusal)

    async def both() -> list[bool]:
        # The second caller waits for the refused login under the lock and must not repeat it.
        return list(await asyncio.gather(comexio_api._full_login(), comexio_api._full_login()))

    assert asyncio.run(both()) == [False, False]
    assert asyncio.run(comexio_api._full_login()) is False
    client.login.assert_awaited_once()
    assert comexio_api.last_login_error == "rejected"
    assert comexio_api.credentials_refused is True


def test_new_credentials_lift_the_refusal(comexio_api: ComexioAPI, client: MagicMock) -> None:
    refused_password = comexio_api.password
    _fail(client, "login", ComexioAuthenticationError("no"))
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    comexio_api.password = "changed"
    assert comexio_api.credentials_refused is False
    client.login = AsyncMock()
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is True
    client.login.assert_awaited_once()
    comexio_api.password = refused_password
    assert comexio_api.credentials_refused is True  # accepting other credentials says nothing about the refused ones


def test_preview_and_main_login_do_not_both_send_refused_credentials(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """Review (Sourcery, #213): the preview logs in outside _login_lock, on its own client."""
    preview = MagicMock()
    client.login = AsyncMock(side_effect=_slow_refusal)
    preview.login = AsyncMock(side_effect=_slow_refusal)

    async def both() -> list[bool]:
        return list(
            await asyncio.gather(comexio_api._full_login(), comexio_api._login(preview, comexio_api._credentials()))
        )

    assert asyncio.run(both()) == [False, False]
    assert client.login.await_count + preview.login.await_count == 1


def test_a_login_with_replaced_credentials_leaves_the_refusal_alone(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Review (Sourcery, #213): an old login finishing late must not set or lift the current refusal."""

    async def refused_after_reconfigure() -> None:
        comexio_api.password = "changed"
        raise ComexioAuthenticationError("no")

    client.login = AsyncMock(side_effect=refused_after_reconfigure)
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    assert comexio_api.credentials_refused is False  # the refusal was for the old password
    assert comexio_api.last_login_error == "connection"  # no reauth: the new password was never tried

    comexio_api.password = "old"
    refused = (comexio_api.host, comexio_api.username, "changed again")
    comexio_api._refused_credentials = {comexio_api._refusal_key(refused)}

    async def accepted_after_reconfigure() -> None:
        comexio_api.password = "changed again"

    client.login = AsyncMock(side_effect=accepted_after_reconfigure)
    # Not a success either: the caller would mark the old client logged in and go on with a new, logged-out one.
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    assert comexio_api.last_login_error == "connection"
    assert comexio_api._refused_credentials == {
        comexio_api._refusal_key(refused)
    }  # the accepted old password does not lift it
    assert comexio_api.credentials_refused is True


def test_a_login_waiting_through_a_reconfigure_is_not_sent(comexio_api: ComexioAPI) -> None:
    """Review (Sourcery, #213): a client built before a reconfigure waits for the login lock meanwhile."""
    stale = MagicMock()
    stale.login = AsyncMock(side_effect=ComexioAuthenticationError("no"))
    fresh = MagicMock()
    fresh.login = AsyncMock()

    async def run() -> None:
        stale_credentials = comexio_api._credentials()
        async with comexio_api._login_request_lock:
            waiting = asyncio.create_task(comexio_api._login(stale, stale_credentials))
            await asyncio.sleep(0)
            comexio_api.password = "new and valid"
        assert await waiting is False
        assert comexio_api.last_login_error == "connection"  # not "rejected": no reauth for untried credentials
        assert comexio_api.credentials_refused is False
        assert await comexio_api._login(fresh, comexio_api._credentials()) is True

    asyncio.run(run())
    stale.login.assert_not_awaited()
    fresh.login.assert_awaited_once()


def test_every_refused_credential_set_stays_refused(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Review (Sourcery, #213): switching back to credentials refused before must not send them again."""
    first_password = comexio_api.password
    _fail(client, "login", ComexioAuthenticationError("no"))
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    comexio_api.password = "second, also wrong"
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    comexio_api.password = first_password
    assert comexio_api.credentials_refused is True
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    assert client.login.await_count == 2


def test_refused_credentials_keep_no_password(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Review (Sourcery, #213): refused passwords must not pile up in memory as plain text."""
    comexio_api.password = "s3cret-but-wrong"
    _fail(client, "login", ComexioAuthenticationError("no"))
    assert asyncio.run(comexio_api._login(client, comexio_api._credentials())) is False
    assert comexio_api.credentials_refused is True
    assert not any("s3cret-but-wrong" in key for key in comexio_api._refused_credentials)


def test_queued_preview_tick_does_not_log_in_after_a_refusal(comexio_api: ComexioAPI) -> None:
    """Review (Sourcery, #213): ticks that passed the first check wait for the preview lock meanwhile."""
    comexio_api._login = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def run() -> aiohttp.ClientSession | None:
        async with comexio_api._preview_session_lock:
            queued = asyncio.create_task(comexio_api.ensure_preview_session())
            await asyncio.sleep(0)
            comexio_api._refused_credentials = {comexio_api._refusal_key(comexio_api._login_credentials())}
        return await queued

    assert asyncio.run(run()) is None
    comexio_api._login.assert_not_awaited()


def test_api_user_change_does_not_lift_the_refusal(comexio_api: ComexioAPI) -> None:
    """Review (Sourcery, #213): the API user (Basic Auth for writes) is not part of the admin login."""
    comexio_api._refused_credentials = {comexio_api._refusal_key(comexio_api._login_credentials())}
    comexio_api.api_user = "someone-else"
    comexio_api.api_pass = "other"
    assert comexio_api.credentials_refused is True


def test_connection_failure_does_not_count_as_a_refusal(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "login", _connection_error())
    assert asyncio.run(comexio_api._full_login()) is False
    assert asyncio.run(comexio_api._full_login()) is False
    assert client.login.await_count == 2
    assert comexio_api.credentials_refused is False


def test_preview_session_does_not_log_in_with_refused_credentials(comexio_api: ComexioAPI) -> None:
    comexio_api._refused_credentials = {comexio_api._refusal_key(comexio_api._login_credentials())}
    comexio_api._login = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert asyncio.run(comexio_api.ensure_preview_session()) is None
    comexio_api._login.assert_not_awaited()


def test_system_info_does_not_log_in_with_refused_credentials(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Review (Sourcery, #212): the 5-minute system info poll kept retrying them while reauth was pending."""
    client.get_system_info = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.get_uptime = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.login = AsyncMock()
    comexio_api._refused_credentials = {comexio_api._refusal_key(comexio_api._login_credentials())}

    reading = asyncio.run(comexio_api.get_system_info())

    assert reading.info_error == reading.uptime_error == "re-login failed (rejected)"
    client.login.assert_not_awaited()


def test_failed_login_forgets_the_logged_in_client(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._logged_in_client = client
    client.is_logged_in = AsyncMock(return_value=False)
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
    async def login_ok(_client: object, _credentials: object) -> bool:
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
    async def login_while_reconfigured(_client: object, _credentials: object) -> bool:
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
    assert comexio_api.config_relogin_unreachable is False  # Comexio answered
    assert comexio_api.config_relogin_rejected is True  # the poll asks for reauth


def test_get_raw_config_rejected_flag_is_for_this_fetch_only(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """A refused re-login flags its own fetch; the next good fetch clears it although last_login_error stays."""
    _fail(client, "get_raw_config", ComexioAuthenticationError("lapsed"))
    _fail(client, "login", ComexioAuthenticationError("rejected"))
    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.config_relogin_rejected is True

    client.get_raw_config = AsyncMock(return_value=RawConfig({"FubModules": {"2": {}}}, {}, {}, None))
    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.config_relogin_rejected is False
    assert comexio_api.last_login_error == "rejected"


def test_get_raw_config_unanswered_re_login_is_flagged_for_this_fetch_only(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """A lapsed session whose re-login gets no reply (e.g. Comexio still rebooting) marks the fetch unreachable."""
    _fail(client, "get_raw_config", ComexioAuthenticationError("lapsed"))
    _fail(client, "login", _connection_error())
    assert asyncio.run(comexio_api.get_raw_config()) == {}
    assert comexio_api.config_relogin_unreachable is True
    assert comexio_api.config_relogin_rejected is False  # no answer is no refusal: no reauth

    client.get_raw_config = AsyncMock(return_value=RawConfig({"FubModules": {"2": {}}}, {}, {}, None))
    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.config_relogin_unreachable is False
    assert comexio_api.last_login_error == "connection"  # left stale, hence the per-fetch flag


@pytest.mark.parametrize(
    "failure", [_connection_error(), ComexioDataError("an error page")], ids=["transport", "scrape"]
)
def test_a_failed_fetch_after_an_unanswered_re_login_clears_the_flag(
    comexio_api: ComexioAPI, client: MagicMock, failure: Exception
) -> None:
    """The flag describes only the last fetch: one failing on its own (not on the re-login) leaves it False."""
    comexio_api.config_relogin_unreachable = True
    _fail(client, "get_raw_config", failure)

    with contextlib.suppress(aiohttp.ClientError):
        asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.config_relogin_unreachable is False


def test_get_raw_config_keeps_io_types_and_version(comexio_api: ComexioAPI, client: MagicMock) -> None:
    variables = {"FubModules": {"2": {}}}
    client.get_raw_config = AsyncMock(return_value=RawConfig(variables, {"1": {}}, {"2": {}}, "11.0.2"))
    assert asyncio.run(comexio_api.get_raw_config()) is variables
    assert (comexio_api.io_types, comexio_api.io_input_types, comexio_api.comexio_version) == (
        {"1": {}},
        {"2": {}},
        "11.0.2",
    )


def test_get_raw_config_keeps_block_settings_only_as_fresh_as_the_last_fetch(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    row = {"FubElementId": 4, "Name": "autohide", "Value": "1"}
    fetches = [{"FubBaseConfig": {"1": row}}, {"FubBaseConfig": []}, {"FubBaseConfig": {"1": row}}, {}]
    client.get_raw_config = AsyncMock(side_effect=[RawConfig(v, {}, {}, None) for v in fetches])

    seen = []
    for _ in fetches:
        asyncio.run(comexio_api.get_raw_config())
        seen.append(comexio_api.block_settings)

    # An empty table is "no settings"; a page without it must not leave an ever older copy behind.
    assert seen == [{"4": {"autohide": "1"}}, {}, {"4": {"autohide": "1"}}, None]
    assert "no longer carries $FubBaseConfig" in caplog.text


@pytest.mark.parametrize("err", [ComexioDataError("an error page"), _connection_error()])
def test_a_failed_config_fetch_drops_the_block_settings(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # A change backup after a failed poll must not store the table of an ever older fetch.
    comexio_api.block_settings = {"4": {"autohide": "1"}}
    _fail(client, "get_raw_config", err)

    with contextlib.suppress(aiohttp.ClientError, TimeoutError):
        asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.block_settings is None


def _block_page(value: str, version: str, active: int = 1) -> RawConfig:
    """A config page whose element 104 has in_0 = value, read on Comexio firmware version; plan 7 Active = active."""
    variables = {
        "FubBaseConfig": {"1": {"FubElementId": 104, "Name": "in_0", "Value": value}},
        "Fubs": {"7": {"Id": 7, "Name": f"Plan {value}", "Active": active}},
        "Paper": {"1": {"Id": 1, "Name": f"Paper {value}"}},
        "CurrentSceneConfig": {"31": {"name": f"Scene {value}", "common": [], "scene": []}},
    }
    return RawConfig(variables, {"1": {"unit": version}}, {"2": {"input": version}}, version)


def _page_state(page: RawConfig) -> tuple[Any, ...]:
    """What a fetch that applied page leaves in block_settings, io_types, io_input_types, comexio_version,
    module_configs."""
    value = page.variables["FubBaseConfig"]["1"]["Value"]
    scenes = {"13": {"31": {"name": f"Scene {value}", "common": [], "scene": []}}}
    return {"104": {"in_0": value}}, page.io_types, page.io_input_types, page.comexio_version, scenes


def _api_state(comexio_api: ComexioAPI) -> tuple[Any, ...]:
    return (
        comexio_api.block_settings,
        comexio_api.io_types,
        comexio_api.io_input_types,
        comexio_api.comexio_version,
        comexio_api.module_configs,
    )


OLDER_PAGE = _block_page("10", "11.0.2")
NEWER_PAGE = _block_page("26", "11.0.3")


class _Overlap(NamedTuple):
    """Outcome of two overlapping config fetches: each one's result and the state after the first finished.

    unreachable holds config_relogin_unreachable as each fetch's caller reads it on resuming.
    """

    older: Any
    newer: Any
    between: tuple[Any, ...]
    unreachable: dict[str, bool]


def _run_overlapping_fetches(
    comexio_api: ComexioAPI,
    client: MagicMock,
    older_outcome: RawConfig | Exception,
    newer_outcome: RawConfig | Exception = NEWER_PAGE,
    older_finishes_first: bool = False,
) -> _Overlap:
    """Two config fetches overlap: both have started before either finishes, then they finish in the given order.

    A failed re-login (ComexioAuthenticationError outcome) gets no reply from Comexio. A transport
    error comes back as the fetch's result, so the test can assert it.
    """

    async def failed_login() -> bool:
        comexio_api.last_login_error = api_module.LOGIN_ERROR_CONNECTION
        return False

    comexio_api._full_login = AsyncMock(side_effect=failed_login)  # type: ignore[method-assign]

    async def run() -> _Overlap:
        entered = {"older": asyncio.Event(), "newer": asyncio.Event()}
        may_finish = {"older": asyncio.Event(), "newer": asyncio.Event()}
        outcomes = {"older": older_outcome, "newer": newer_outcome}
        order = iter(["older", "newer"])

        async def fetch() -> RawConfig:
            name = next(order, "re-login")
            if name == "re-login":
                raise AssertionError("a failed re-login must not fetch again")
            entered[name].set()
            await may_finish[name].wait()
            if isinstance(outcome := outcomes[name], Exception):
                raise outcome
            return outcome

        unreachable: dict[str, bool] = {}

        async def settle(name: str) -> Any:
            try:
                return await tasks[name]
            except aiohttp.ClientError as err:
                return err
            finally:
                unreachable[name] = comexio_api.config_relogin_unreachable

        client.get_raw_config = AsyncMock(side_effect=fetch)
        tasks = {"older": asyncio.create_task(comexio_api.get_raw_config())}
        await entered["older"].wait()
        tasks["newer"] = asyncio.create_task(comexio_api.get_raw_config())
        await entered["newer"].wait()
        first, second = ("older", "newer") if older_finishes_first else ("newer", "older")
        may_finish[first].set()
        results = {first: await settle(first)}
        between = _api_state(comexio_api)
        may_finish[second].set()
        results[second] = await settle(second)
        return _Overlap(results["older"], results["newer"], between, unreachable)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "older_outcome",
    [OLDER_PAGE, ComexioDataError("an error page"), _connection_error(), ComexioAuthenticationError("lapsed")],
    ids=["older-page", "older-scrape-error", "older-transport-error", "older-failed-re-login"],
)
def test_an_older_config_fetch_finishing_last_keeps_the_newer_tables(
    comexio_api: ComexioAPI, client: MagicMock, older_outcome: RawConfig | Exception
) -> None:
    """(bw): an older fetch finishing after a newer one must not put its page back or drop the newer table."""
    overlap = _run_overlapping_fetches(comexio_api, client, older_outcome)

    assert overlap.newer is NEWER_PAGE.variables
    assert _api_state(comexio_api) == _page_state(NEWER_PAGE)
    if isinstance(older_outcome, ComexioConnectionError):
        assert isinstance(overlap.older, aiohttp.ClientError)


def test_a_discarded_config_fetch_returns_the_newer_page(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """The caller of the older fetch parses the newer page, never its older plan list over the cache."""
    overlap = _run_overlapping_fetches(comexio_api, client, OLDER_PAGE)

    assert overlap.older is NEWER_PAGE.variables
    marked = [fubs for fubs, _mark, _generation in comexio_api._fetch_marks]
    assert any(fubs is NEWER_PAGE.variables["Fubs"] for fubs in marked)
    assert not any(fubs is OLDER_PAGE.variables["Fubs"] for fubs in marked)


def _run_three_fetches(
    comexio_api: ComexioAPI, client: MagicMock, outcomes: list[RawConfig | Exception], finish_order: tuple[int, ...]
) -> list[Any]:
    """Three config fetches start in turn (outcomes in start order), then finish in finish_order; their results."""

    async def run() -> list[Any]:
        may_finish = [asyncio.Event() for _ in outcomes]
        entered = [asyncio.Event() for _ in outcomes]
        order = iter(range(len(outcomes)))

        async def fetch() -> RawConfig:
            index = next(order)
            entered[index].set()
            await may_finish[index].wait()
            if isinstance(outcome := outcomes[index], Exception):
                raise outcome
            return outcome

        client.get_raw_config = AsyncMock(side_effect=fetch)
        tasks = []
        for index in range(len(outcomes)):
            tasks.append(asyncio.create_task(comexio_api.get_raw_config()))
            await entered[index].wait()
        results: list[Any] = [None] * len(outcomes)
        for index in finish_order:
            may_finish[index].set()
            results[index] = await tasks[index]
        return results

    return asyncio.run(run())


NEWEST_PAGE = _block_page("30", "11.0.4")


def test_a_stale_failure_does_not_hide_the_newest_page_from_an_overtaken_fetch(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """Three fetches finish newest first: the middle one's failure must not make the oldest return its own page."""
    outcomes: list[RawConfig | Exception] = [OLDER_PAGE, ComexioDataError("an error page"), NEWEST_PAGE]

    oldest, middle, newest = _run_three_fetches(comexio_api, client, outcomes, (2, 1, 0))

    assert (middle, newest) == ({}, NEWEST_PAGE.variables)
    assert oldest is NEWEST_PAGE.variables
    assert _api_state(comexio_api) == _page_state(NEWEST_PAGE)


def test_a_newest_failure_keeps_the_newer_parsed_page_for_an_overtaken_fetch(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """The middle page is applied and parsed, then the newest fetch fails: the oldest still gets the middle page."""
    outcomes: list[RawConfig | Exception] = [OLDER_PAGE, NEWER_PAGE, ComexioDataError("an error page")]

    oldest, middle, newest = _run_three_fetches(comexio_api, client, outcomes, (1, 2, 0))

    assert (middle, newest) == (NEWER_PAGE.variables, {})
    assert oldest is NEWER_PAGE.variables
    # The failure drops both caches; io_types and the version stay those of the middle page.
    assert _api_state(comexio_api) == (None, *_page_state(NEWER_PAGE)[1:4], None)


@pytest.mark.parametrize("older_finishes_first", [False, True], ids=["older-last", "older-first"])
@pytest.mark.parametrize("failing", ["older", "newer"])
def test_each_overlapping_config_fetch_reports_only_its_own_unreachable_re_login(
    comexio_api: ComexioAPI, client: MagicMock, failing: str, older_finishes_first: bool
) -> None:
    """A re-login without reply flags only its own caller's poll, never the other fetch's successful one."""
    outcomes = {"older": OLDER_PAGE, "newer": NEWER_PAGE, failing: ComexioAuthenticationError("lapsed")}

    overlap = _run_overlapping_fetches(
        comexio_api, client, outcomes["older"], outcomes["newer"], older_finishes_first=older_finishes_first
    )

    assert overlap.unreachable == {"older": failing == "older", "newer": failing == "newer"}


def test_an_older_config_fetch_after_a_failed_newer_one_changes_nothing(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """The newest outcome wins also when it is a failure: no table, io_types and version as before both."""
    comexio_api.block_settings = {"104": {"in_0": "1"}}
    comexio_api.io_types, comexio_api.io_input_types = {"1": {"unit": "before"}}, {"2": {"input": "before"}}
    comexio_api.comexio_version = "11.0.1"

    overlap = _run_overlapping_fetches(comexio_api, client, OLDER_PAGE, newer_outcome=ComexioDataError("an error page"))

    assert overlap.newer == {}
    assert overlap.older is OLDER_PAGE.variables
    assert _api_state(comexio_api) == (None, {"1": {"unit": "before"}}, {"2": {"input": "before"}}, "11.0.1", None)


def test_an_older_config_fetch_finishing_first_applies_until_the_newer_one_does(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """The older page is fresher than the cache, so it applies while the newer fetch still runs."""
    overlap = _run_overlapping_fetches(comexio_api, client, OLDER_PAGE, older_finishes_first=True)

    assert overlap.between == _page_state(OLDER_PAGE)
    assert _api_state(comexio_api) == _page_state(NEWER_PAGE)


def test_an_older_page_parsed_after_a_newer_one_keeps_the_newer_plan_cache(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """A caller parses only after further awaits: the older fetch's page, parsed last, leaves the newer caches."""
    overlap = _run_overlapping_fetches(comexio_api, client, OLDER_PAGE, older_finishes_first=True)
    assert overlap.older is OLDER_PAGE.variables

    comexio_api.parse_config(overlap.newer)
    comexio_api.parse_config(overlap.older)

    assert comexio_api.fub_data is NEWER_PAGE.variables["Fubs"]
    assert comexio_api._paper_data is NEWER_PAGE.variables["Paper"]


def test_a_page_parsed_with_an_older_poll_mark_keeps_the_run_states_it_already_shows(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """An overtaken poll parses the newer page with its older mark: HA's run states before that page stay as read."""
    older_poll_mark = comexio_api.run_state_mark()
    comexio_api.set_fub_active(7, True)  # before the newer fetch began, which reads plan 7 stopped again
    client.get_raw_config = AsyncMock(return_value=_block_page("26", "11.0.3", active=0))
    page = asyncio.run(comexio_api.get_raw_config())

    comexio_api.parse_config(page, run_state_mark=older_poll_mark)

    assert comexio_api.fub_data["7"]["Active"] == 0


def test_a_later_caller_mark_keeps_the_run_states_ha_caused_after_the_fetch(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """A mark the caller took after the fetch began wins over the earlier one get_raw_config remembered."""
    client.get_raw_config = AsyncMock(return_value=_block_page("26", "11.0.3", active=0))
    page = asyncio.run(comexio_api.get_raw_config())
    comexio_api.set_fub_active(7, True)  # after the fetch began: newer than the page's Active flag

    comexio_api.parse_config(page, run_state_mark=comexio_api.run_state_mark())
    assert comexio_api.fub_data["7"]["Active"] == 0
    comexio_api.parse_config(page)
    assert comexio_api.fub_data["7"]["Active"] == 1


def test_a_plan_list_no_fetch_remembers_still_replaces_the_cache(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """Without a remembered fetch generation (e.g. evicted marks) the plan list replaces the cache as before."""
    _run_overlapping_fetches(comexio_api, client, OLDER_PAGE, older_finishes_first=True)
    comexio_api.parse_config(NEWER_PAGE.variables)
    unknown = {"9": {"Id": 9, "Name": "Plan 9", "Active": 0}}

    comexio_api.parse_config({"Fubs": unknown})

    assert comexio_api.fub_data is unknown
    comexio_api.parse_config(OLDER_PAGE.variables)
    assert comexio_api.fub_data is unknown


PLACEMENT_PLAN = {"elements": [], "connections": []}


def _older_plan_list_than_the_cached_one(comexio_api: ComexioAPI, client: MagicMock) -> dict[str, Any]:
    """$Fubs of a fetch older than the cached plan list, which adds plan 9; the bulk load returns only plan 7."""
    client.get_raw_config = AsyncMock(return_value=_block_page("26", "11.0.3"))
    older = asyncio.run(comexio_api.get_raw_config())
    newer_page = _block_page("27", "11.0.3")
    newer_page.variables["Fubs"]["9"] = {"Id": 9, "Name": "Plan 9", "Active": 0}
    client.get_raw_config = AsyncMock(return_value=newer_page)
    comexio_api.parse_config(asyncio.run(comexio_api.get_raw_config()))
    comexio_api.function_plan_load_all_plans = AsyncMock(return_value={7: PLACEMENT_PLAN})  # type: ignore[method-assign]
    return older["Fubs"]


def test_a_placement_check_on_an_older_plan_list_also_loads_the_newer_cached_plans(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    # Regression: the older list was rejected for the cache, yet only its plans were checked —
    # a plan the newer cached list adds, omitted by the bulk load, counted as having no placements.
    fubs = _older_plan_list_than_the_cached_one(comexio_api, client)
    comexio_api.function_plan_load_elements = AsyncMock(return_value=PLACEMENT_PLAN)  # type: ignore[method-assign]

    plans = asyncio.run(comexio_api._load_all_plans_verified(fubs, strict=True))

    assert plans == {7: PLACEMENT_PLAN, 9: PLACEMENT_PLAN}
    comexio_api.function_plan_load_elements.assert_awaited_once_with(9, strict=True)


def test_a_placement_check_fails_closed_on_an_unloadable_cached_plan(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    fubs = _older_plan_list_than_the_cached_one(comexio_api, client)
    comexio_api.function_plan_load_elements = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert asyncio.run(comexio_api._load_all_plans_verified(fubs, strict=True)) is None


def test_a_placement_check_fails_closed_on_a_malformed_cached_plan_id(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    fubs = _older_plan_list_than_the_cached_one(comexio_api, client)
    comexio_api._fub_data["x"] = {"Id": "x"}

    assert asyncio.run(comexio_api._load_all_plans_verified(fubs, strict=True)) is None


def test_the_next_config_fetch_after_an_overlap_takes_its_page_again(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    _run_overlapping_fetches(comexio_api, client, _block_page("10", "11.0.2"))

    client.get_raw_config = AsyncMock(return_value=_block_page("30", "11.0.4"))
    asyncio.run(comexio_api.get_raw_config())
    assert (comexio_api.block_settings, comexio_api.comexio_version) == ({"104": {"in_0": "30"}}, "11.0.4")


@pytest.mark.parametrize(
    ("answer", "saved"), [({"saved": 1}, True), ({"saved": "1"}, True), ({"saved": 0}, False), ({}, False)]
)
def test_save_block_settings_needs_the_saved_confirmation(
    comexio_api: ComexioAPI, client: MagicMock, answer: dict[str, Any], saved: bool
) -> None:
    client._plan_json = AsyncMock(return_value=answer)

    assert asyncio.run(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})) is saved
    path, form = client._plan_json.await_args.args
    assert path == "/admin/function_function_module/savefubbaseconfig/"
    assert (form["id"], form["element_in_0"]) == ("104", "26")
    assert form["timestamp"]


def test_save_block_settings_maps_a_refusal_but_raises_a_transport_error(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    _fail(client, "_plan_json", ComexioRequestRejectedError("no"))
    assert asyncio.run(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})) is False

    # The restores catch transport errors to replace their "in progress" notification.
    _fail(client, "_plan_json", _connection_error())
    save = comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})
    with pytest.raises(aiohttp.ClientError):
        asyncio.run(save)


@pytest.mark.parametrize(("answer", "cached"), [({"saved": 1}, "26"), ({"saved": 0}, "10")])
def test_save_block_settings_updates_the_cached_table_once_confirmed(
    comexio_api: ComexioAPI, client: MagicMock, answer: dict[str, Any], cached: str
) -> None:
    # A backup cycle before the next config fetch must see the restored values, not the old ones.
    comexio_api.block_settings = {"104": {"in_0": "10", "autohide": "1"}}
    client._plan_json = AsyncMock(return_value=answer)

    asyncio.run(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"}))
    asyncio.run(comexio_api.function_plan_save_block_settings(200, {"in_1": "5"}))

    assert comexio_api.block_settings["104"] == {"in_0": cached, "autohide": "1"}
    assert ("200" in comexio_api.block_settings) is (cached == "26")


def test_save_block_settings_leaves_an_uncaptured_table_uncaptured(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api.block_settings = None
    client._plan_json = AsyncMock(return_value={"saved": 1})

    assert asyncio.run(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})) is True
    assert comexio_api.block_settings is None


def _old_table_page() -> RawConfig:
    """A config page read before the save: element 104 still has in_0 = 10."""
    return RawConfig({"FubBaseConfig": {"1": {"FubElementId": 104, "Name": "in_0", "Value": "10"}}}, {}, {}, None)


def test_a_config_fetch_overlapping_a_save_keeps_the_saved_values(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """(bn)(4): a fetch that read the page before the save must not put the old value back into the cache."""
    comexio_api.block_settings = {"104": {"in_0": "10"}}
    client._plan_json = AsyncMock(return_value={"saved": 1})

    async def fetch_during_save() -> RawConfig:
        await comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})
        return _old_table_page()

    client.get_raw_config = AsyncMock(side_effect=fetch_during_save)
    asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.block_settings == {"104": {"in_0": "26"}}
    # The next fetch without a save in between takes the page's table again.
    client.get_raw_config = AsyncMock(return_value=_old_table_page())
    asyncio.run(comexio_api.get_raw_config())
    assert comexio_api.block_settings == {"104": {"in_0": "10"}}


def test_a_config_fetch_during_a_running_save_keeps_the_cache(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """A save already running when the fetch starts may land after the page was read."""
    comexio_api.block_settings = {"104": {"in_0": "10"}}

    async def run() -> None:
        save_done, page_read = asyncio.Event(), asyncio.Event()

        async def slow_save(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            await page_read.wait()
            return {"saved": 1}

        async def slow_fetch() -> RawConfig:
            page_read.set()  # the page is read while the save is still running ...
            await save_done.wait()  # ... and the fetch only finishes after the save
            return _old_table_page()

        client._plan_json = AsyncMock(side_effect=slow_save)
        client.get_raw_config = AsyncMock(side_effect=slow_fetch)
        save = asyncio.create_task(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"}))
        await asyncio.sleep(0)
        fetch = asyncio.create_task(comexio_api.get_raw_config())
        await save
        save_done.set()
        await fetch

    asyncio.run(run())

    assert comexio_api.block_settings == {"104": {"in_0": "26"}}


def test_a_save_confirmed_after_the_fetch_lands_on_the_fetched_table(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """A save still running when the fetch returns: the page is taken, the confirmed values go on top."""
    comexio_api.block_settings = None
    page = RawConfig(
        {
            "FubBaseConfig": {
                "1": {"FubElementId": 104, "Name": "in_0", "Value": "10"},
                "2": {"FubElementId": 200, "Name": "in_1", "Value": "5"},
            }
        },
        {},
        {},
        None,
    )

    async def run() -> None:
        fetch_done = asyncio.Event()

        async def slow_save(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            await fetch_done.wait()
            return {"saved": 1}

        client._plan_json = AsyncMock(side_effect=slow_save)
        client.get_raw_config = AsyncMock(return_value=page)
        save = asyncio.create_task(comexio_api.function_plan_save_block_settings(104, {"in_0": "26"}))
        await asyncio.sleep(0)
        await comexio_api.get_raw_config()
        fetch_done.set()
        await save

    asyncio.run(run())

    assert comexio_api.block_settings == {"104": {"in_0": "26"}, "200": {"in_1": "5"}}


def test_a_config_fetch_overlapping_a_save_leaves_an_unread_cache_unread(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """With no table read yet, an overlapping fetch must not capture the pre-save page either."""
    comexio_api.block_settings = None
    client._plan_json = AsyncMock(return_value={"saved": 1})

    async def fetch_during_save() -> RawConfig:
        await comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})
        return _old_table_page()

    client.get_raw_config = AsyncMock(side_effect=fetch_during_save)
    asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.block_settings is None  # "not read" — no backup stores the pre-save values


@pytest.mark.parametrize(
    "fail_save",
    [
        lambda client: setattr(client, "_plan_json", AsyncMock(return_value={"saved": 0})),
        lambda client: _fail(client, "_plan_json", _connection_error()),
    ],
    ids=["refused", "transport error"],
)
def test_a_config_fetch_overlapping_an_unconfirmed_save_takes_the_page(
    comexio_api: ComexioAPI, client: MagicMock, fail_save: Any
) -> None:
    """A save Comexio did not confirm changed nothing — the overlapping fetch's table is current."""
    comexio_api.block_settings = None
    fail_save(client)

    async def fetch_during_save() -> RawConfig:
        with contextlib.suppress(aiohttp.ClientError):
            await comexio_api.function_plan_save_block_settings(104, {"in_0": "26"})
        return _old_table_page()

    client.get_raw_config = AsyncMock(side_effect=fetch_during_save)
    asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.block_settings == {"104": {"in_0": "10"}}


def test_a_failed_save_does_not_freeze_the_cache(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """A save that raises is not counted as confirmed, so later fetches take the page again."""
    comexio_api.block_settings = {"104": {"in_0": "26"}}
    _fail(client, "_plan_json", _connection_error())
    save = comexio_api.function_plan_save_block_settings(104, {"in_0": "30"})
    with pytest.raises(aiohttp.ClientError):
        asyncio.run(save)

    client.get_raw_config = AsyncMock(return_value=_old_table_page())
    asyncio.run(comexio_api.get_raw_config())

    assert comexio_api.block_settings == {"104": {"in_0": "10"}}


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


def test_knx_catalog_meeting_another_certificate_keeps_last_known_good(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    catalog = {"KnxPoints": {"1": {}}}
    comexio_api.seed_knx_dpt_catalog(catalog, "11.0.1")
    comexio_api.comexio_version = "11.0.2"
    err = ComexioCertificateMismatchError("foreign", expected="a" * 64, actual="b" * 64)
    _fail(client, "get_knx_dpt_catalog", err)
    assert asyncio.run(comexio_api.get_knx_dpt_catalog()) is catalog
    assert comexio_api.get_knx_dpt_catalog_snapshot() == (catalog, "11.0.1")
    assert comexio_api.certificate_mismatch is err  # reported, not swallowed as a plain fetch failure


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


def test_load_all_plans_skips_a_key_that_is_no_plain_number(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed $Fubs key must not abort the bulk load — the backup cycle of every valid plan rides on it."""
    comexio_api._fub_data = {"1": {}, "x": {}, "²": {}, "04": {}, "4": {}}
    client.load_all_function_plans = AsyncMock(return_value={})
    asyncio.run(comexio_api.function_plan_load_all_plans(strict=True))
    client.load_all_function_plans.assert_awaited_once_with({1, 4}, strict=True)
    # A plan under such a key gets no backup: say which keys were left out.
    assert "['x', '²', '04']" in caplog.text


def test_load_all_plans_failure_is_empty(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api._fub_data = {"1": {}}
    _fail(client, "load_all_function_plans", HTTP_ERROR)
    assert asyncio.run(comexio_api.function_plan_load_all_plans()) == {}


def test_load_all_plans_failure_raises_on_request(comexio_api: ComexioAPI, client: MagicMock) -> None:
    """The backup cycle must tell a failed load from "no plans" — it asks for the error."""
    comexio_api._fub_data = {"1": {}}
    _fail(client, "load_all_function_plans", HTTP_ERROR)
    call = comexio_api.function_plan_load_all_plans(raise_errors=True)
    with pytest.raises(type(HTTP_ERROR)):
        asyncio.run(call)


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


SYSTEM_INFO = SystemInfo(free_storage_percent=42.5, time_server=TimeServerSettings(True, "pool.ntp.org", 3600))


def test_system_info_returns_both_parts(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(return_value=SYSTEM_INFO)
    client.get_uptime = AsyncMock(return_value=timedelta(days=3))
    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)


@pytest.mark.parametrize("err", [_connection_error(), HTTP_ERROR, ComexioDataError("no duration")])
def test_system_info_failure_drops_only_its_part(comexio_api: ComexioAPI, client: MagicMock, err: Exception) -> None:
    # One failed fetch must not cost the other reading: both are tried, each failure is its own None + cause.
    client.get_uptime = AsyncMock(return_value=timedelta(days=3))
    _fail(client, "get_system_info", err)
    assert asyncio.run(comexio_api.get_system_info()) == (None, timedelta(days=3), str(err), None)
    client.get_system_info = AsyncMock(return_value=SYSTEM_INFO)
    _fail(client, "get_uptime", err)
    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, None, None, str(err))


def test_system_info_logs_in_again_on_a_lapsed_session(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), SYSTEM_INFO])
    client.get_uptime = AsyncMock(return_value=timedelta(days=3))
    client.login = AsyncMock()

    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)
    client.login.assert_awaited_once()
    assert client.get_system_info.await_count == 2


def test_system_info_uptime_fetch_retries_after_the_polls_re_login(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), SYSTEM_INFO])
    client.get_uptime = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), timedelta(days=3)])
    client.login = AsyncMock()

    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)
    # The uptime fetch reuses the poll's one re-login.
    client.login.assert_awaited_once()
    assert client.get_uptime.await_count == 2


def test_system_info_failed_relogin_is_the_cause(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.get_uptime = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.login = AsyncMock(side_effect=_connection_error())

    reading = asyncio.run(comexio_api.get_system_info())

    assert (reading.info, reading.uptime) == (None, None)
    assert reading.info_error == reading.uptime_error == "re-login failed (connection)"
    # Without a session the retry would only see the login form again.
    assert client.get_system_info.await_count == 1
    # Review (Sourcery, #204): one re-login per poll — the uptime fetch does not try again.
    client.login.assert_awaited_once()
    assert client.get_uptime.await_count == 1


def test_system_info_lapse_on_the_uptime_fetch_only_logs_in_there(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(return_value=SYSTEM_INFO)
    client.get_uptime = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), timedelta(days=3)])
    client.login = AsyncMock()

    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)
    client.login.assert_awaited_once()


def test_system_info_both_parts_share_one_relogin(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_system_info = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), SYSTEM_INFO])
    client.get_uptime = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), timedelta(days=3)])
    client.login = AsyncMock()

    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)
    client.login.assert_awaited_once()


def test_system_info_takes_over_a_login_that_finished_after_its_request(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """Review (Sourcery, #204): a second login would clear the jar of the session another caller just logged in."""
    comexio_api._logged_in_client = client

    answers: list[SystemInfo | None] = [None, SYSTEM_INFO]

    async def lapsed_then_relogged_elsewhere() -> SystemInfo:
        answer = answers.pop(0)
        if answer is None:
            # The config poll logs in again while this request is still on its way back with the login form.
            comexio_api._login_generation += 1
            raise ComexioAuthenticationError("login form")
        return answer

    client.get_system_info = AsyncMock(side_effect=lapsed_then_relogged_elsewhere)
    client.get_uptime = AsyncMock(return_value=timedelta(days=3))
    client.login = AsyncMock()

    assert asyncio.run(comexio_api.get_system_info()) == (SYSTEM_INFO, timedelta(days=3), None, None)
    client.login.assert_not_awaited()


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
    lookup = call(comexio_api)
    with pytest.raises(RuntimeError):
        asyncio.run(lookup)


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


def test_webio_delete_errors_name_the_reason(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.delete_webio_device = AsyncMock(return_value=False)
    assert asyncio.run(comexio_api.webio_device_delete_error(3)) == "still used in a function plan"
    _fail(client, "delete_webio_device", HTTP_ERROR)
    assert asyncio.run(comexio_api.webio_device_delete_error(3)) == "request failed: HTTP 500"
    _fail(client, "delete_webio_base", HTTP_ERROR)
    assert asyncio.run(comexio_api.webio_base_delete_error(3)) == "request failed: HTTP 500"


def test_upload_web_io_returns_the_base_id(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.upload_webio_class = AsyncMock(return_value="42")
    assert asyncio.run(comexio_api.upload_web_io("iosrv1", "HA [M]", "{}")) == (True, "42")
    client.upload_webio_class.assert_awaited_once_with("{}", class_name="HA [M]", filename="ha_iosrv1.json")


def test_upload_web_io_failure_carries_the_reason(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "upload_webio_class", ComexioRequestRejectedError("refused: no ok"))
    ok, reason = asyncio.run(comexio_api.upload_web_io("iosrv1", "HA [M]", "{}"))
    assert ok is False
    assert "refused" in reason


def test_upload_web_io_without_base_id_is_a_failure(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # A device must never be created on a missing class id, whatever the client hands back.
    client.upload_webio_class = AsyncMock(return_value="")
    ok, reason = asyncio.run(comexio_api.upload_web_io("iosrv1", "HA [M]", "{}"))
    assert ok is False
    assert "no base_id" in reason


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
def test_webio_command_form_failure_is_none(comexio_api: ComexioAPI, client: MagicMock, err: Exception) -> None:
    _fail(client, "get_webio_command_form", err)
    assert asyncio.run(comexio_api.get_webio_command_form(5, 3)) is None


def test_webio_command_form_is_passed_through(comexio_api: ComexioAPI, client: MagicMock) -> None:
    form = WebioCommandForm(min=-32768.0, max=32767.0, protocol=1)
    client.get_webio_command_form = AsyncMock(return_value=form)
    assert asyncio.run(comexio_api.get_webio_command_form(5, 3)) is form
    client.get_webio_command_form.assert_awaited_once_with(5, 3)


_PLAN_WRITES_BOOL = [
    (lambda api: api.delete_fup(7), "delete_function_plan"),
    (lambda api: api.function_plan_stop_fup(7), "stop_function_plan"),
    (lambda api: api.function_plan_run_fup(7), "run_function_plan"),
    (lambda api: api.function_plan_delete_elements([1, 2]), "delete_function_plan_elements"),
    (lambda api: api._function_plan_set_comment_width(4, "Note"), "save_function_plan_comment"),
    (lambda api: api.rename_marker(12, "M12 Test", True, default=0, store=False), "rename_marker"),
    (lambda api: api.rename_knx_object(3, "K3 Test [RO]"), "rename_knx_object"),
]


@pytest.mark.parametrize(("call", "method"), _PLAN_WRITES_BOOL)
@pytest.mark.parametrize(
    "err", [ComexioRequestRejectedError("not confirmed"), _connection_error(), TypeError("Expected integers")]
)
def test_plan_write_failure_is_false(
    comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str, err: Exception
) -> None:
    _fail(client, method, err)
    assert asyncio.run(call(comexio_api)) is False


@pytest.mark.parametrize(("call", "method"), _PLAN_WRITES_BOOL)
def test_plan_write_success_is_true(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    setattr(client, method, AsyncMock(return_value=None))
    assert asyncio.run(call(comexio_api)) is True


def test_stopping_a_plan_that_is_not_running_is_success(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression (r): routine on every restore/connect of a stopped plan — no warning, no failure.
    refusal = (
        "Stopping function plan 33 was refused: {'error': 'stop_error', 'id': 33, 'state': 0, 'return': '0:not_found'}"
    )
    _fail(client, "stop_function_plan", ComexioRequestRejectedError(refusal))
    with caplog.at_level(logging.INFO):
        assert asyncio.run(comexio_api.function_plan_stop_fup(33)) is True
    assert "the plan was not running" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


_NOT_RUNNING = "Stopping function plan 33 was refused: {'error': 'stop_error', 'state': 0, 'return': '0:not_found'}"


def test_cleanup_of_a_plan_that_is_not_running_leaves_it_stopped(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # (am): a stopped plan was skipped and reported as not cleaned up, although it needs no
    # stop to be cleaned up; a restart would start a plan the user had stopped.
    _fail(client, "stop_function_plan", ComexioRequestRejectedError(_NOT_RUNNING))
    client.delete_function_plan_elements = AsyncMock()
    client.run_function_plan = AsyncMock()
    result = asyncio.run(comexio_api._delete_plan_elements_and_restart(33, [1, 2], [40], "TestPlan"))
    assert result == {
        "deleted_elem_count": 2,
        "webio_cmd_ids": [40],
        "fub_id": 33,
        "plan_stopped": False,
        "plan_name": "TestPlan",
        "was_running": False,
    }
    client.delete_function_plan_elements.assert_awaited_once()
    client.run_function_plan.assert_not_awaited()


def test_failed_cleanup_of_a_plan_that_is_not_running_does_not_start_it(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    _fail(client, "stop_function_plan", ComexioRequestRejectedError(_NOT_RUNNING))
    _fail(client, "delete_function_plan_elements", ComexioRequestRejectedError("not confirmed"))
    client.run_function_plan = AsyncMock()
    result = asyncio.run(comexio_api._delete_plan_elements_and_restart(33, [1, 2], [40], "TestPlan"))
    assert result["delete_failed"] is True
    assert result["plan_stopped"] is False
    client.run_function_plan.assert_not_awaited()


def test_cleanup_is_skipped_when_a_running_plan_refuses_to_stop(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "stop_function_plan", ComexioRequestRejectedError("not confirmed"))
    client.delete_function_plan_elements = AsyncMock()
    client.run_function_plan = AsyncMock()
    result = asyncio.run(comexio_api._delete_plan_elements_and_restart(33, [1, 2], [40], "TestPlan"))
    assert result["stop_failed"] is True
    assert result["webio_cmd_ids"] == []
    client.delete_function_plan_elements.assert_not_awaited()
    client.run_function_plan.assert_not_awaited()


_PLAN_WRITES_ID = [
    (lambda api: api.function_plan_add_element(7, 5, 2, x=15, y=30), "add_function_plan_element"),
    (lambda api: api.function_plan_save_connection(7, 1, [(2, 0, False)]), "save_function_plan_connection"),
    (lambda api: api.function_plan_add_constant_element(7, "1"), "add_function_plan_constant"),
    (lambda api: api.create_marker(True), "create_marker"),
]


@pytest.mark.parametrize(("call", "method"), _PLAN_WRITES_ID)
def test_plan_write_failure_is_none(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    _fail(client, method, ComexioDataError("answer carries no id"))
    assert asyncio.run(call(comexio_api)) is None


@pytest.mark.parametrize(("call", "method"), _PLAN_WRITES_ID)
def test_plan_write_returns_the_id(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    setattr(client, method, AsyncMock(return_value=42))
    assert asyncio.run(call(comexio_api)) == 42


def test_plan_ids_from_payloads_are_passed_as_ints(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Element ids read from plan payloads can be strings; the old requests sent them as text,
    # aiocomexio insists on ints — the adapter casts instead of failing the wiring.
    client.save_function_plan_connection = AsyncMock(return_value=9)
    client.delete_function_plan_elements = AsyncMock()
    asyncio.run(comexio_api.function_plan_save_connection(7, "1", [("2", "0", False)], "analog", existing_conn_id="5"))
    asyncio.run(comexio_api.function_plan_delete_elements(["3", 4]))
    client.save_function_plan_connection.assert_awaited_once_with(
        7, 1, [(2, 0, False)], value_type="analog", source_pos=0, source_inverted=False, connection_id=5
    )
    client.delete_function_plan_elements.assert_awaited_once_with([3, 4])


def test_knx_rename_passes_the_id_as_int(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression (Sourcery, 8b-2c): the coordinator and the repair flow pass registry ids, which can
    # be strings; aiocomexio insists on an int and would refuse the [RO]/[TRIG] rename.
    client.rename_knx_object = AsyncMock()
    assert asyncio.run(comexio_api.rename_knx_object("3", "K3 Test [RO]")) is True
    client.rename_knx_object.assert_awaited_once_with(3, "K3 Test [RO]")


@pytest.mark.parametrize(
    ("call", "method"),
    [
        (lambda api: api.function_plan_delete_elements([]), "delete_function_plan_elements"),
        (lambda api: api.function_plan_save_elements_pos([]), "move_function_plan_elements"),
    ],
)
def test_empty_plan_batch_sends_nothing(comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str) -> None:
    setattr(client, method, AsyncMock())
    assert asyncio.run(call(comexio_api)) is True
    getattr(client, method).assert_not_awaited()


@pytest.mark.parametrize("result", [True, False])
def test_delete_marker_passes_the_verdict_through(comexio_api: ComexioAPI, client: MagicMock, result: bool) -> None:
    client.delete_marker = AsyncMock(return_value=result)
    assert asyncio.run(comexio_api.delete_marker(12)) is result


@pytest.mark.parametrize("err", [ComexioDataError("not an object"), _connection_error(), HTTP_ERROR])
def test_delete_marker_request_failure_is_none_not_false(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception
) -> None:
    # None ("request failed") must stay apart from False ("not deleted") for an irreversible action.
    _fail(client, "delete_marker", err)
    assert asyncio.run(comexio_api.delete_marker(12)) is None


def test_run_fup_unconfirmed_is_no_warning(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Comexio routinely answers result=false after a restore that did apply (services/backup.py).
    _fail(client, "run_function_plan", ComexioRequestRejectedError("refused: result false"))
    with caplog.at_level(logging.INFO, logger=api_module.__name__):
        assert asyncio.run(comexio_api.function_plan_run_fup(7, {"elements": {}, "connections": {}})) is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (ComexioRequestRejectedError("refused: output used twice"), False),
        (_connection_error(), None),
        (HTTP_ERROR, None),
        (ComexioAuthenticationError("lapsed"), None),
        (ComexioDataError("no result field"), None),
        (TypeError("Expected integers"), None),
        (ValueError("not a number"), None),
    ],
    ids=["refused", "connection", "http", "session", "malformed", "type", "value"],
)
def test_run_fup_outcome_tells_a_refusal_apart_from_no_answer(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception, expected: bool | None
) -> None:
    # Review (Copilot, #132): only a refusal may count as a failed start of the plan watchdog —
    # None (no usable answer) must not use up its two attempts before it gives up on a plan.
    _fail(client, "run_function_plan", err)
    assert asyncio.run(comexio_api.function_plan_run_fup_outcome(7)) is expected
    assert "7" not in comexio_api._ha_run_states


def test_run_fup_outcome_confirmed_is_true_and_records_the_run(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.run_function_plan = AsyncMock(return_value=None)
    assert asyncio.run(comexio_api.function_plan_run_fup_outcome(7)) is True
    client.run_function_plan.assert_awaited_once_with(7, None)
    assert comexio_api._ha_run_states["7"][0] is True


def test_comment_gets_its_width_after_placing(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.add_function_plan_comment = AsyncMock(return_value=11)
    client.save_function_plan_comment = AsyncMock(side_effect=ComexioRequestRejectedError("not confirmed"))
    # A failed width update keeps the placed comment.
    assert asyncio.run(comexio_api.function_plan_add_comment_element(7, "Note", x=15, y=7.5)) == 11
    client.save_function_plan_comment.assert_awaited_once_with(11, "Note", width=5)


def test_failed_comment_placing_skips_the_width(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "add_function_plan_comment", _connection_error())
    client.save_function_plan_comment = AsyncMock()
    assert asyncio.run(comexio_api.function_plan_add_comment_element(7, "Note")) is None
    client.save_function_plan_comment.assert_not_awaited()


NEW_PLAN = {"Id": 8, "Name": "HA - IO", "Paper": 2, "Resolution": 90, "Orientation": 0, "Active": 0}


def _created_without_id() -> ComexioCreatedWithoutIdError:
    return ComexioCreatedWithoutIdError("Creating function plan 'HA - IO': reading back its id failed")


def test_create_fup_caches_the_new_plan(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.create_function_plan = AsyncMock(return_value=CreatedFunctionPlan(8, NEW_PLAN))
    client.get_raw_config = AsyncMock()
    assert asyncio.run(comexio_api.create_fup("HA - IO", paper_format="A3")) == 8
    assert comexio_api.fub_data["8"] == NEW_PLAN
    client.get_raw_config.assert_not_awaited()  # the lib already read the entry back


def test_create_fup_tells_the_listener(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression: without the call the new plan got its run-state sensor only with the next coordinator update.
    comexio_api.run_state_listener = listener = MagicMock()
    client.create_function_plan = AsyncMock(return_value=CreatedFunctionPlan(8, NEW_PLAN))
    asyncio.run(comexio_api.create_fup("HA - IO"))
    listener.assert_called_once_with()
    _fail(client, "create_function_plan", ComexioRequestRejectedError("name already in use"))
    asyncio.run(comexio_api.create_fup("HA - IO"))
    listener.assert_called_once_with()  # a failed create changes no plan


def _plan_list_from_before_the_create(comexio_api: ComexioAPI, client: MagicMock) -> dict[str, Any]:
    """A config page whose fetch began before create_fup created plan 8, so it does not list it."""
    client.get_raw_config = AsyncMock(return_value=_block_page("26", "11.0.3"))
    page = asyncio.run(comexio_api.get_raw_config())
    client.create_function_plan = AsyncMock(return_value=CreatedFunctionPlan(8, NEW_PLAN))
    asyncio.run(comexio_api.create_fup("HA - IO"))
    assert "8" not in page["Fubs"]
    return page


def test_a_plan_list_older_than_create_fup_keeps_the_new_plan(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression: the older page replaced the cache, and the new plan was gone until the next poll.
    page = _plan_list_from_before_the_create(comexio_api, client)

    comexio_api.parse_config(page)

    assert comexio_api.fub_data["8"] == NEW_PLAN
    assert set(page["Fubs"]) <= set(comexio_api.fub_data)


def test_a_plan_list_fetched_after_create_fup_is_the_word_on_the_new_plan(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """A fetch begun after the create that does not list the plan (deleted in Studio) drops it for good."""
    older = _plan_list_from_before_the_create(comexio_api, client)
    client.get_raw_config = AsyncMock(return_value=_block_page("27", "11.0.3"))
    newer = asyncio.run(comexio_api.get_raw_config())

    comexio_api.parse_config(newer)
    assert "8" not in comexio_api.fub_data
    comexio_api.parse_config(older)
    assert "8" not in comexio_api.fub_data


def test_a_plan_deleted_after_create_fup_is_not_kept_listed(comexio_api: ComexioAPI, client: MagicMock) -> None:
    page = _plan_list_from_before_the_create(comexio_api, client)
    client.delete_function_plan = AsyncMock()
    assert asyncio.run(comexio_api.delete_fup(8))

    comexio_api.parse_config(page)

    assert "8" not in comexio_api.fub_data


def test_a_plan_list_older_than_create_fup_keeps_the_refreshed_entry(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    page = _plan_list_from_before_the_create(comexio_api, client)
    renamed = {**NEW_PLAN, "Name": "HA - IO renamed"}
    comexio_api.update_fub_cache_entry(8, renamed)

    comexio_api.parse_config(page)

    assert comexio_api.fub_data["8"] == renamed


def test_a_plan_list_older_than_create_fup_that_lists_the_plan_keeps_its_entry(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    """Comexio answered the older fetch after the create: the page's own entry wins."""
    page = _plan_list_from_before_the_create(comexio_api, client)
    listed = {**NEW_PLAN, "Name": "HA - IO as listed"}
    page["Fubs"]["8"] = listed

    comexio_api.parse_config(page)

    assert comexio_api.fub_data["8"] is listed
    assert comexio_api.fub_data is page["Fubs"]


def test_a_failed_delete_keeps_the_created_plan_listed(comexio_api: ComexioAPI, client: MagicMock) -> None:
    page = _plan_list_from_before_the_create(comexio_api, client)
    _fail(client, "delete_function_plan", ComexioRequestRejectedError("plan is in use"))
    assert not asyncio.run(comexio_api.delete_fup(8))

    comexio_api.parse_config(page)

    assert comexio_api.fub_data["8"] == NEW_PLAN


def test_create_fup_finds_a_plan_created_without_id(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression (p): the plan exists once Comexio confirmed it — None read as "name in use" before.
    comexio_api.update_fub_cache_entry(3, {"Id": 3, "Name": "HA - IO"})  # an older namesake
    _fail(client, "create_function_plan", _created_without_id())
    fubs = {"3": {"Id": 3, "Name": "HA - IO"}, "8": NEW_PLAN}
    client.get_raw_config = AsyncMock(return_value=RawConfig({"Fubs": fubs}, {}, {}, None))
    assert asyncio.run(comexio_api.create_fup("HA - IO")) == 8
    assert comexio_api.fub_data["8"] == NEW_PLAN


@pytest.mark.parametrize(
    "fubs",
    [{}, {"8": NEW_PLAN, "9": {"Id": 9, "Name": "HA - IO"}}, {"8": {"Id": 8, "Name": "Other"}}],
    ids=["missing", "twice", "renamed"],
)
def test_create_fup_does_not_guess_the_id(
    comexio_api: ComexioAPI, client: MagicMock, fubs: dict, caplog: pytest.LogCaptureFixture
) -> None:
    _fail(client, "create_function_plan", _created_without_id())
    client.get_raw_config = AsyncMock(return_value=RawConfig({"Fubs": fubs}, {}, {}, None))
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(comexio_api.create_fup("HA - IO")) is None
    assert "do not create it again" in caplog.text


@pytest.mark.parametrize("err", [_connection_error(), ComexioDataError("no $FubModules")])
def test_create_fup_logs_a_failed_second_read_back(
    comexio_api: ComexioAPI, client: MagicMock, err: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    # A failed scrape makes get_raw_config() return {} or raise a transport error — neither may escape.
    _fail(client, "create_function_plan", _created_without_id())
    _fail(client, "get_raw_config", err)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(comexio_api.create_fup("HA - IO")) is None
    assert "do not create it again" in caplog.text
    assert "reading the config failed" in caplog.text


def test_create_fup_failure_is_none(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "create_function_plan", ComexioRequestRejectedError("name already in use"))
    assert asyncio.run(comexio_api.create_fup("HA - IO")) is None


def test_update_paper_passes_the_live_settings_and_updates_the_cache(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    comexio_api.update_fub_cache_entry(7, {"Name": "Old", "Comment": None, "Position": 3, "Active": 1, "Paper": 3})
    client.update_function_plan = AsyncMock()
    assert asyncio.run(comexio_api.function_plan_update_paper(7, "A3", 120, "portrait", name="New")) is True
    client.update_function_plan.assert_awaited_once_with(
        7, name="New", comment="", position=3, active=True, paper_format="A3", orientation="portrait", dpi=120
    )
    assert comexio_api.fub_data["7"] == {
        "Name": "New",
        "Comment": None,
        "Position": 3,
        "Active": 1,
        "Paper": "2",
        "Resolution": 120,
        "Orientation": 1,
    }


def test_update_paper_failure_leaves_the_cache(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api.update_fub_cache_entry(7, {"Name": "Old", "Paper": 3})
    _fail(client, "update_function_plan", ValueError("dpi must be 45-120, not 300"))
    assert asyncio.run(comexio_api.function_plan_update_paper(7, "A3", 300, "landscape")) is False
    assert comexio_api.fub_data["7"] == {"Name": "Old", "Paper": 3}


_TRANSPORT_RAISING = [
    (lambda api: api.function_plan_add_element(7, 5, 2), "add_function_plan_element"),
    (lambda api: api.function_plan_save_connection(7, 1, [(2, 0, False)]), "save_function_plan_connection"),
    (lambda api: api.function_plan_save_elements_pos([(1, 15, 30.0)]), "move_function_plan_elements"),
    (lambda api: api.function_plan_add_constant_element(7, "1"), "add_function_plan_constant"),
]


@pytest.mark.parametrize(("call", "method"), _TRANSPORT_RAISING)
def test_plan_build_transport_failure_raises(
    comexio_api: ComexioAPI, client: MagicMock, call: Any, method: str
) -> None:
    # Regression (review 8b-2c): the restore paths in services/backup.py abort on these instead
    # of trying every further element of the snapshot against an unreachable server.
    _fail(client, method, _connection_error())
    coro = call(comexio_api)
    with pytest.raises(aiohttp.ServerDisconnectedError):
        asyncio.run(coro)


def test_save_elements_pos_rejection_is_false(comexio_api: ComexioAPI, client: MagicMock) -> None:
    _fail(client, "move_function_plan_elements", ComexioRequestRejectedError("not confirmed"))
    assert asyncio.run(comexio_api.function_plan_save_elements_pos([(1, 15, 30.0)])) is False


def test_run_fup_reactivation_refused_is_a_warning(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Without a restore payload an unconfirmed run leaves the plan stopped — that must show.
    _fail(client, "run_function_plan", ComexioRequestRejectedError("refused: output used twice"))
    assert asyncio.run(comexio_api.function_plan_run_fup(7)) is False
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_unsupported_paper_falls_back_to_a4(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Regression (review 8b-2c): an unknown paper value in a backup used to restore on A4;
    # aiocomexio only takes A3/A4/A5, so without the fallback the whole restore would fail.
    comexio_api.update_fub_cache_entry(7, {"Name": "Old", "Paper": 1})
    client.update_function_plan = AsyncMock()
    assert asyncio.run(comexio_api.function_plan_update_paper(7, "Letter", 90, "Landscape")) is True
    kwargs = client.update_function_plan.await_args.kwargs
    assert (kwargs["paper_format"], kwargs["orientation"]) == ("A4", "landscape")
    assert comexio_api.fub_data["7"]["Paper"] == "3"
