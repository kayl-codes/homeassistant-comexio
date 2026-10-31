"""HTTPS with a pinned certificate: ComexioAPI's fingerprint, mismatch reporting and the fresh-handshake check."""

import asyncio
import json
import logging
from pathlib import Path
import string
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioResponseError,
    RawConfig,
)
import aiohttp
import pytest

from custom_components.comexio import SERVER_LEVEL_UNIQUE_ID_SUFFIXES, api as api_module, certificate
from custom_components.comexio.api import SYSTEM_INFO_CERTIFICATE_CHANGED, ComexioAPI, SystemInfoReading
from custom_components.comexio.button import _sync_login_error
from custom_components.comexio.const import (
    CERTIFICATE_CHANGED_MSG,
    LOGIN_ERROR_CERTIFICATE,
    LOGIN_ERROR_CONNECTION,
    LOGIN_ERROR_REJECTED,
)
from custom_components.comexio.sensor import ComexioConnectionSensor
from custom_components.comexio.services._context import login_failed_message

PINNED = "a" * 64
OTHER = "b" * 64


def _mismatch() -> ComexioCertificateMismatchError:
    return ComexioCertificateMismatchError("foreign certificate", expected=PINNED, actual=OTHER)


@pytest.fixture
def client(comexio_api: ComexioAPI) -> MagicMock:
    """Replace the main-session client with a mock (kept as long as the settings stay)."""
    fake = MagicMock()
    comexio_api._client = fake
    comexio_api._client_credentials = comexio_api._credentials()
    return fake


@pytest.fixture
def handshake(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The TLS handshake reading Comexio's certificate (pinned value unless a test says otherwise)."""
    fetch = AsyncMock(return_value=PINNED)
    monkeypatch.setattr(api_module, "fetch_certificate_fingerprint", fetch)
    return fetch


def test_without_fingerprint_the_client_uses_plain_http(comexio_api: ComexioAPI) -> None:
    assert comexio_api.client.fingerprint is None


def test_the_client_pins_the_fingerprint(comexio_api: ComexioAPI) -> None:
    comexio_api.fingerprint = PINNED
    assert comexio_api.client.fingerprint == PINNED


def test_a_new_fingerprint_rebuilds_the_client(comexio_api: ComexioAPI) -> None:
    comexio_api.fingerprint = PINNED
    first = comexio_api.client
    assert comexio_api.client is first
    comexio_api.fingerprint = OTHER
    rebuilt = comexio_api.client
    assert rebuilt is not first
    assert rebuilt.fingerprint == OTHER


def test_a_login_meeting_another_certificate_reports_it(comexio_api: ComexioAPI, client: MagicMock) -> None:
    mismatch = _mismatch()
    client.login = AsyncMock(side_effect=mismatch)
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == LOGIN_ERROR_CERTIFICATE
    assert comexio_api.certificate_mismatch is mismatch


def test_a_successful_login_clears_the_mismatch(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api.certificate_mismatch = _mismatch()
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    assert comexio_api.certificate_mismatch is None
    assert comexio_api.last_login_error is None


def test_a_certificate_mismatch_at_login_is_no_credential_refusal(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Else every later login would be cut short as "rejected" and the user sent to reauth, not the repair.
    client.login = AsyncMock(side_effect=_mismatch())
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == LOGIN_ERROR_CERTIFICATE
    assert comexio_api.credentials_refused is False
    client.login = AsyncMock()
    assert asyncio.run(comexio_api.login()) is True
    client.login.assert_awaited_once()
    assert comexio_api.certificate_mismatch is None


def _replace_pin_then(comexio_api: ComexioAPI, outcome: BaseException | None) -> AsyncMock:
    """A client login during which a reconfigure or the repair replaces the pinned certificate."""

    async def login() -> None:
        comexio_api.fingerprint = OTHER
        if outcome is not None:
            raise outcome

    return AsyncMock(side_effect=login)


def test_a_mismatch_met_with_a_replaced_pin_raises_no_repair(comexio_api: ComexioAPI) -> None:
    comexio_api.fingerprint = PINNED
    client = comexio_api._client = MagicMock()
    comexio_api._client_credentials = comexio_api._credentials()
    client.login = _replace_pin_then(comexio_api, _mismatch())
    reported = MagicMock()
    comexio_api.on_certificate_mismatch = reported

    assert asyncio.run(comexio_api.login()) is False

    assert comexio_api.last_login_error == LOGIN_ERROR_CONNECTION
    assert comexio_api.certificate_mismatch is None
    reported.assert_not_called()


def test_a_login_with_a_replaced_pin_proves_nothing_for_the_new_one(comexio_api: ComexioAPI) -> None:
    comexio_api.fingerprint = PINNED
    client = comexio_api._client = MagicMock()
    comexio_api._client_credentials = comexio_api._credentials()
    client.login = _replace_pin_then(comexio_api, None)
    mismatch = comexio_api.certificate_mismatch = _mismatch()
    comexio_api._mismatch_logged = OTHER

    assert asyncio.run(comexio_api.login()) is False

    assert comexio_api.last_login_error == LOGIN_ERROR_CONNECTION
    assert comexio_api.certificate_mismatch is mismatch
    assert comexio_api._mismatch_logged == OTHER


def test_a_refusal_with_a_replaced_pin_still_counts_for_the_credentials(comexio_api: ComexioAPI) -> None:
    # The pin is the transport, not the credentials: Comexio refused them whichever certificate it showed.
    comexio_api.fingerprint = PINNED
    client = comexio_api._client = MagicMock()
    comexio_api._client_credentials = comexio_api._credentials()
    client.login = _replace_pin_then(comexio_api, ComexioAuthenticationError("no"))

    assert asyncio.run(comexio_api.login()) is False

    assert comexio_api.last_login_error == LOGIN_ERROR_REJECTED
    assert comexio_api.credentials_refused is True


@pytest.mark.parametrize(
    ("attribute", "before", "after"),
    [("fingerprint", PINNED, OTHER), ("fingerprint", None, PINNED), ("password", "admin", "new")],
    ids=["pin replaced", "pin set", "password changed"],
)
def test_a_login_waiting_through_a_settings_change_is_not_sent(
    comexio_api: ComexioAPI, attribute: str, before: str | None, after: str
) -> None:
    # The client was built with the old settings; the caller retries with a client built with the new ones.
    setattr(comexio_api, attribute, before)
    stale = MagicMock()
    stale.login = AsyncMock(side_effect=_mismatch())

    async def run() -> None:
        stale_credentials = comexio_api._credentials()
        async with comexio_api._login_request_lock:
            waiting = asyncio.create_task(comexio_api._login(stale, stale_credentials))
            await asyncio.sleep(0)
            setattr(comexio_api, attribute, after)
        assert await waiting is False

    asyncio.run(run())
    stale.login.assert_not_awaited()
    assert comexio_api.last_login_error == LOGIN_ERROR_CONNECTION
    assert comexio_api.certificate_mismatch is None


def test_a_login_outlives_a_change_of_the_api_user(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # The API user (Basic Auth for writes) plays no part in the admin login.
    async def login() -> None:
        comexio_api.api_user = "someone else"

    client.login = AsyncMock(side_effect=login)
    assert asyncio.run(comexio_api.login()) is True
    assert comexio_api.last_login_error is None


def test_another_login_failure_keeps_the_mismatch_of_before(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Only a login that gets through proves the pinned server answers again.
    mismatch = _mismatch()
    comexio_api.certificate_mismatch = mismatch
    client.login = AsyncMock(side_effect=ComexioResponseError("HTTP 500", status=500))
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == LOGIN_ERROR_CONNECTION
    assert comexio_api.certificate_mismatch is mismatch


def test_a_config_fetch_meeting_another_certificate_returns_nothing(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # Callers keep their {} contract; the coordinator reads certificate_mismatch to raise the repair.
    mismatch = _mismatch()
    client.get_raw_config = AsyncMock(side_effect=mismatch)
    assert asyncio.run(comexio_api.get_raw_config()) == {}
    assert comexio_api.certificate_mismatch is mismatch


def test_a_config_fetch_failing_otherwise_names_no_old_mismatch(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # A mismatch met earlier (e.g. by the preview login) must not pass for this fetch's cause.
    comexio_api.certificate_mismatch = _mismatch()
    client.get_raw_config = AsyncMock(side_effect=ComexioResponseError("HTTP 500", status=500))
    assert asyncio.run(comexio_api.get_raw_config()) == {}
    assert comexio_api.certificate_mismatch is None


def test_a_config_fetch_that_gets_through_clears_the_mismatch(comexio_api: ComexioAPI, client: MagicMock) -> None:
    comexio_api.certificate_mismatch = _mismatch()
    client.get_raw_config = AsyncMock(return_value=RawConfig({"FubModules": {}}, {}, {}, None))
    assert asyncio.run(comexio_api.get_raw_config()) == {"FubModules": {}}
    assert comexio_api.certificate_mismatch is None


def test_no_handshake_without_a_pin(comexio_api: ComexioAPI, handshake: AsyncMock) -> None:
    assert asyncio.run(comexio_api.fetch_replaced_fingerprint()) is None
    handshake.assert_not_awaited()


@pytest.mark.parametrize(
    ("presented", "expected"),
    [(PINNED, None), (OTHER, OTHER), (ComexioConnectionError("down"), None), (ValueError("bad host"), None)],
    ids=["pinned certificate", "another certificate", "unreachable", "invalid host"],
)
def test_a_fresh_handshake_names_only_another_certificate(
    comexio_api: ComexioAPI, handshake: AsyncMock, presented: str | Exception, expected: str | None
) -> None:
    comexio_api.fingerprint = PINNED
    if isinstance(presented, Exception):
        handshake.side_effect = presented
    else:
        handshake.return_value = presented
    assert asyncio.run(comexio_api.fetch_replaced_fingerprint()) == expected
    handshake.assert_awaited_once_with(comexio_api.host)


def test_a_refused_login_names_the_certificate_without_a_handshake(
    comexio_api: ComexioAPI, handshake: AsyncMock
) -> None:
    comexio_api.fingerprint = PINNED
    comexio_api.certificate_mismatch = _mismatch()
    assert asyncio.run(certificate.async_replaced_fingerprint(comexio_api)) == OTHER
    handshake.assert_not_awaited()


def test_a_connection_error_is_checked_with_a_fresh_handshake(comexio_api: ComexioAPI, handshake: AsyncMock) -> None:
    comexio_api.fingerprint = PINNED
    handshake.return_value = OTHER
    assert asyncio.run(certificate.async_replaced_fingerprint(comexio_api)) == OTHER
    handshake.assert_awaited_once()


@pytest.mark.parametrize(
    ("result", "expected"),
    [(PINNED, PINNED), (ComexioConnectionError("no TLS"), None), (ValueError("bad host"), None)],
    ids=["serves HTTPS", "no HTTPS", "invalid host"],
)
def test_read_fingerprint_is_none_without_https(
    monkeypatch: pytest.MonkeyPatch, result: str | Exception, expected: str | None
) -> None:
    fetch = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(certificate, "fetch_certificate_fingerprint", fetch)
    assert asyncio.run(certificate.async_read_fingerprint("comexio.local")) == expected
    fetch.assert_awaited_once_with("comexio.local")


def test_placeholders_show_both_fingerprints_the_way_comexio_does() -> None:
    entry = MagicMock(
        data={"server_id": "iosrv1", "host": "192.168.0.250", "certificate_fingerprint": PINNED},
    )
    placeholders = certificate.certificate_placeholders(entry, OTHER)
    assert placeholders == {
        "server_id": "iosrv1",
        "host": "192.168.0.250",
        "pinned": ":".join(["AA"] * 32),
        "actual": ":".join(["BB"] * 32),
    }


@pytest.mark.parametrize(
    ("method", "call", "failed"),
    [
        ("login", lambda api: api.login(), False),
        ("get_raw_config", lambda api: api.get_raw_config(), {}),
        ("set_marker_value", lambda api: api.set_value("marker", 7, 1), False),
        ("get_bus_workload", lambda api: api.get_bus_workload(), {}),
        ("get_live_states", lambda api: api.get_live_states(4), (None, None)),
        ("rename_knx_object", lambda api: api.rename_knx_object(5, "Light"), False),
        ("create_marker", lambda api: api.create_marker(binary=True), None),
        ("get_knx_dpt_catalog", lambda api: api.get_knx_dpt_catalog(), {}),
    ],
    ids=[
        "login",
        "config fetch",
        "API write",
        "bus load poll",
        "live states poll",
        "admin write",
        "admin write with a result",
        "KNX catalog",
    ],
)
def test_every_mismatch_reaches_the_hook_right_away(
    comexio_api: ComexioAPI, client: MagicMock, method: str, call, failed: object
) -> None:
    # The repair comes up with the first request that meets the certificate, not only with the next full poll.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    setattr(client, method, AsyncMock(side_effect=_mismatch()))
    result = asyncio.run(call(comexio_api))
    assert result == failed
    assert type(result) is type(failed)
    hook.assert_called_once_with(OTHER)
    assert comexio_api.certificate_mismatch is not None


def test_a_system_info_poll_meeting_another_certificate_raises_the_repair(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    # The slow system info poll may be the first request to meet the change; the uptime fetch is not sent then.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_system_info = AsyncMock(side_effect=_mismatch())
    client.get_uptime = AsyncMock()
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading == SystemInfoReading(None, None, SYSTEM_INFO_CERTIFICATE_CHANGED, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)
    client.get_uptime.assert_not_awaited()


def test_a_mismatch_on_an_admin_write_whose_transport_failures_raise_is_no_transport_failure(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    # The restore paths abort on a transport failure; a changed certificate fails the write and raises the repair.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.save_function_plan_block_settings = AsyncMock(side_effect=_mismatch())
    assert asyncio.run(comexio_api.function_plan_save_block_settings(3, {"Delay": "5"})) is False
    hook.assert_called_once_with(OTHER)


def test_a_mismatch_at_the_uptime_fetch_drops_the_whole_system_info_reading(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_system_info = AsyncMock(return_value=MagicMock())
    client.get_uptime = AsyncMock(side_effect=_mismatch())
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading == SystemInfoReading(None, None, SYSTEM_INFO_CERTIFICATE_CHANGED, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)


def test_a_mismatch_at_the_retry_after_a_re_login_drops_the_whole_system_info_reading(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_system_info = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), _mismatch()])
    client.get_uptime = AsyncMock()
    client.login = AsyncMock()
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading == SystemInfoReading(None, None, SYSTEM_INFO_CERTIFICATE_CHANGED, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)
    client.get_uptime.assert_not_awaited()


def test_a_re_login_meeting_another_certificate_names_the_certificate_change(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    # The re-login raises the repair; the uptime fetch sends nothing after it, like after any failed re-login.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_system_info = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.get_uptime = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.login = AsyncMock(side_effect=_mismatch())
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading == SystemInfoReading(None, None, SYSTEM_INFO_CERTIFICATE_CHANGED, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)
    assert client.get_system_info.await_count == client.get_uptime.await_count == 1


def test_a_system_info_re_login_meeting_a_pin_replaced_meanwhile_raises_no_repair(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    # The certificate met belongs to a pin no longer in force: a connection failure, no certificate change.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    comexio_api.fingerprint = PINNED
    comexio_api._client_credentials = comexio_api._credentials()
    client.get_system_info = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.get_uptime = AsyncMock(side_effect=ComexioAuthenticationError("login form"))

    async def login_while_the_pin_is_replaced() -> None:
        comexio_api.fingerprint = OTHER
        comexio_api._client_credentials = comexio_api._credentials()  # the uptime fetch keeps the mock client
        raise _mismatch()

    client.login = AsyncMock(side_effect=login_while_the_pin_is_replaced)
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading.info_error == "re-login failed (connection)"
    assert reading.uptime_error == "re-login failed (connection)"
    hook.assert_not_called()
    assert comexio_api.certificate_mismatch is None


def test_the_uptime_fetch_keeps_the_cause_of_the_polls_re_login(comexio_api: ComexioAPI, client: MagicMock) -> None:
    # A login of another task between the two fetches does not rewrite why this poll's re-login failed.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_system_info = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    client.login = AsyncMock(side_effect=_mismatch())

    async def uptime_after_another_login() -> None:
        comexio_api.last_login_error = None
        raise ComexioAuthenticationError("login form")

    client.get_uptime = AsyncMock(side_effect=uptime_after_another_login)
    reading = asyncio.run(comexio_api.get_system_info())
    assert reading == SystemInfoReading(None, None, SYSTEM_INFO_CERTIFICATE_CHANGED, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)


@pytest.mark.parametrize(
    ("helper", "expected"),
    [
        (lambda call: api_module._attempt("Writing", call, "failed"), "failed"),
        (lambda call: api_module._succeeded("Writing", call), False),
    ],
    ids=["attempt", "succeeded"],
)
def test_a_mismatch_without_a_mismatch_hook_is_a_warning(helper, expected, caplog: pytest.LogCaptureFixture) -> None:
    result = asyncio.run(helper(AsyncMock(side_effect=_mismatch())))
    assert result is expected
    assert [record.levelno for record in caplog.records if "Writing failed" in record.getMessage()] == [logging.WARNING]


def test_a_mismatch_without_a_hook_is_kept_for_the_caller(comexio_api: ComexioAPI, client: MagicMock) -> None:
    err = _mismatch()
    client.set_marker_value = AsyncMock(side_effect=err)
    assert asyncio.run(comexio_api.set_value("marker", 7, 1)) is False
    assert comexio_api.certificate_mismatch is err


def _fingerprint_mismatch() -> aiohttp.ServerFingerprintMismatch:
    return aiohttp.ServerFingerprintMismatch(bytes.fromhex(PINNED), bytes.fromhex(OTHER), "192.168.1.10", 443)


def test_a_direct_client_request_reports_another_certificate(comexio_api: ComexioAPI) -> None:
    # delete_single_command turns any ComexioError into False; the client reports the mismatch before that.
    comexio_api.fingerprint = PINNED
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    comexio_api.session.request.side_effect = _fingerprint_mismatch()
    assert asyncio.run(comexio_api.delete_single_command(1, 2)) is False
    assert comexio_api.certificate_mismatch is not None
    assert comexio_api.certificate_mismatch.actual == OTHER
    assert comexio_api.certificate_mismatch_count == 1
    hook.assert_called_once_with(OTHER)


def test_a_client_on_replaced_settings_reports_no_mismatch(comexio_api: ComexioAPI) -> None:
    # It checked a pin no longer in force; a repair would name a certificate the user did not compare with it.
    comexio_api.fingerprint = PINNED
    old = comexio_api.client
    comexio_api.fingerprint = OTHER
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    error = old._transport_error("Deleting Web-IO command", _fingerprint_mismatch())
    assert isinstance(error, ComexioCertificateMismatchError)
    assert comexio_api.certificate_mismatch is None
    assert comexio_api.certificate_mismatch_count == 0
    hook.assert_not_called()


def test_a_connection_error_reports_no_mismatch(comexio_api: ComexioAPI) -> None:
    comexio_api.fingerprint = PINNED
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    error = comexio_api.client._transport_error("Deleting Web-IO command", aiohttp.ClientConnectionError("down"))
    assert isinstance(error, ComexioConnectionError)
    assert comexio_api.certificate_mismatch is None
    assert comexio_api.certificate_mismatch_count == 0
    hook.assert_not_called()


def test_a_login_meeting_another_certificate_through_the_client_logs_once(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    # The client and the login both keep the mismatch; the repair report is idempotent, the ERROR comes once.
    comexio_api.fingerprint = PINNED
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    comexio_api.session.request.side_effect = _fingerprint_mismatch()
    assert asyncio.run(comexio_api.login()) is False
    assert comexio_api.last_login_error == LOGIN_ERROR_CERTIFICATE
    assert comexio_api.certificate_mismatch is not None
    assert comexio_api.certificate_mismatch.actual == OTHER
    assert {call.args for call in hook.call_args_list} == {(OTHER,)}
    assert [record.levelno for record in caplog.records if "certificate changed" in record.getMessage()] == [
        logging.ERROR
    ]


@pytest.mark.parametrize(
    "pin",
    [{}, {"certificate_fingerprint": OTHER}],
    ids=["entry on plain HTTP", "late report of the certificate just pinned"],
)
def test_no_repair_without_another_pin(monkeypatch: pytest.MonkeyPatch, pin: dict) -> None:
    # Nothing pinned to compare with; or a client still on the old pin reports the one the user just confirmed.
    create = MagicMock()
    monkeypatch.setattr(certificate.ir, "async_create_issue", create)
    entry = MagicMock(data={"server_id": "iosrv1", "host": "192.168.0.250", **pin}, entry_id="e1")
    certificate.async_report_certificate_changed(MagicMock(), entry, OTHER)
    create.assert_not_called()


def test_the_repair_carries_the_presented_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    create = MagicMock()
    monkeypatch.setattr(certificate.ir, "async_create_issue", create)
    entry = MagicMock(
        data={"server_id": "iosrv1", "host": "192.168.0.250", "certificate_fingerprint": PINNED}, entry_id="e1"
    )
    certificate.async_report_certificate_changed(MagicMock(), entry, OTHER)
    kwargs = create.call_args.kwargs
    assert create.call_args.args[2] == "certificate_changed_e1"
    assert kwargs["data"] == {"entry_id": "e1", "actual": OTHER}
    assert kwargs["is_fixable"] is True


def _levels(caplog: pytest.LogCaptureFixture, text: str) -> list[int]:
    return [record.levelno for record in caplog.records if text in record.getMessage()]


def test_a_persisting_mismatch_is_logged_at_error_once(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Every poll and plan-card login meets it again; the repair is up after the first.
    caplog.set_level(logging.DEBUG, logger=api_module.__name__)
    client.login = AsyncMock(side_effect=_mismatch())
    client.get_raw_config = AsyncMock(side_effect=_mismatch())
    asyncio.run(comexio_api.login())
    asyncio.run(comexio_api.get_raw_config())
    asyncio.run(comexio_api.login())
    assert _levels(caplog, "the certificate changed") == [logging.ERROR, logging.DEBUG, logging.DEBUG]

    caplog.clear()
    client.login = AsyncMock()
    asyncio.run(comexio_api.login())  # got through: the next mismatch is news again
    client.login = AsyncMock(side_effect=_mismatch())
    comexio_api._logged_in_client = None  # the session lapsed: a full login again
    asyncio.run(comexio_api.login())
    assert _levels(caplog, "the certificate changed") == [logging.ERROR]


def test_the_bus_load_poll_logs_a_mismatch_at_debug_only(
    comexio_api: ComexioAPI, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Quiet, and it does not use up the one ERROR line of the next login or poll.
    caplog.set_level(logging.DEBUG, logger=api_module.__name__)
    client.get_bus_workload = AsyncMock(side_effect=_mismatch())
    client.login = AsyncMock(side_effect=_mismatch())
    asyncio.run(comexio_api.get_bus_workload())
    asyncio.run(comexio_api.login())
    assert _levels(caplog, "the certificate changed") == [logging.DEBUG, logging.ERROR]


def test_a_failing_handshake_is_logged_at_info_once_per_outage(
    comexio_api: ComexioAPI, handshake: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=api_module.__name__)
    comexio_api.fingerprint = PINNED
    handshake.side_effect = ComexioConnectionError("down")
    asyncio.run(comexio_api.fetch_replaced_fingerprint())
    asyncio.run(comexio_api.fetch_replaced_fingerprint())
    handshake.side_effect = None
    asyncio.run(comexio_api.fetch_replaced_fingerprint())  # back: the next outage is news again
    handshake.side_effect = ComexioConnectionError("down")
    asyncio.run(comexio_api.fetch_replaced_fingerprint())
    assert _levels(caplog, "could not be read") == [logging.INFO, logging.DEBUG, logging.INFO]


@pytest.mark.parametrize(
    ("reason", "certificate_named"),
    [(LOGIN_ERROR_CERTIFICATE, True), (LOGIN_ERROR_CONNECTION, False), ("rejected", False)],
)
def test_a_user_action_learns_that_the_certificate_changed(reason: str, certificate_named: bool) -> None:
    # Not "try again later": nothing reaches Comexio until the repair is decided.
    assert (str(_sync_login_error(reason)) == CERTIFICATE_CHANGED_MSG) is certificate_named
    assert (login_failed_message(MagicMock(last_login_error=reason)) == CERTIFICATE_CHANGED_MSG) is certificate_named


@pytest.mark.parametrize("language", ["strings", "en", "de", "fr", "es"])
def test_every_language_shows_the_connection(language: str) -> None:
    # HA reads translations with string.Formatter and logs an error for any it cannot parse (an ICU select
    # with nested braces, say); the placeholders must be the ones connection_placeholders fills.
    path = Path(certificate.__file__).parent / "translations" / f"{language}.json"
    text = json.loads(path.read_text(encoding="utf-8"))["config"]["step"]["reconfigure"]["description"]
    placeholders = {field for _, field, _, _ in string.Formatter().parse(text) if field is not None}
    for data in ({}, {"certificate_fingerprint": PINNED}):
        assert placeholders == set(certificate.connection_placeholders(data))


def test_the_orphan_cleanup_keeps_the_connection_sensor() -> None:
    unique_id = ComexioConnectionSensor(MagicMock(), "srv").unique_id

    assert unique_id.removeprefix("comexio_srv_") in SERVER_LEVEL_UNIQUE_ID_SUFFIXES


@pytest.mark.parametrize("entry_id", ["01JBX5K8Q2ZP4W7N3M6R9T0V1Y", "test_entry_1"])
def test_the_accept_button_names_its_entry_and_certificate(entry_id: str) -> None:
    # An underscore in the entry id must not move the split: the fingerprint is always the last 64 hex digits.
    action = certificate.accept_action_id(entry_id, PINNED)

    assert certificate.parse_accept_action(action) == (entry_id, PINNED)


@pytest.mark.parametrize(
    "action",
    [
        None,
        7,
        "",
        "COMEXIO_ACCEPT_CERTIFICATE_",
        f"COMEXIO_ACCEPT_CERTIFICATE__{PINNED}",
        f"COMEXIO_START_PLAN_srv_{PINNED}",
    ],
)
def test_no_other_action_passes_for_the_accept_button(action: object) -> None:
    assert certificate.parse_accept_action(action) is None
