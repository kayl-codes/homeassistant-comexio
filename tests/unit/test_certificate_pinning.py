"""HTTPS with a pinned certificate: ComexioAPI's fingerprint, mismatch reporting and the fresh-handshake check."""

import asyncio
import json
import logging
from pathlib import Path
import string
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioResponseError,
    RawConfig,
)
import pytest

from custom_components.comexio import api as api_module, certificate
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import _sync_login_error
from custom_components.comexio.const import CERTIFICATE_CHANGED_MSG, LOGIN_ERROR_CERTIFICATE, LOGIN_ERROR_CONNECTION
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
    ("method", "call"),
    [
        ("login", lambda api: api.login()),
        ("get_raw_config", lambda api: api.get_raw_config()),
        ("set_marker_value", lambda api: api.set_value("marker", 7, 1)),
        ("get_bus_workload", lambda api: api.get_bus_workload()),
    ],
    ids=["login", "config fetch", "API write", "bus load poll"],
)
def test_every_mismatch_reaches_the_hook_right_away(
    comexio_api: ComexioAPI, client: MagicMock, method: str, call
) -> None:
    # The repair comes up with the first request that meets the certificate, not only with the next full poll.
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    setattr(client, method, AsyncMock(side_effect=_mismatch()))
    result = asyncio.run(call(comexio_api))
    assert result in (False, {})
    hook.assert_called_once_with(OTHER)
    assert comexio_api.certificate_mismatch is not None


def test_a_mismatch_without_a_hook_is_kept_for_the_caller(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.set_marker_value = AsyncMock(side_effect=_mismatch())
    assert asyncio.run(comexio_api.set_value("marker", 7, 1)) is False
    assert comexio_api.certificate_mismatch is not None


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
