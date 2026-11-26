"""The certificate rule of #294: a certificate Comexio replaced on its own, taken over where the rule allows it."""

import asyncio
from collections.abc import Generator
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import CertificateInfo, ComexioCertificateMismatchError, ComexioConnectionError
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.comexio import certificate_auto_accept as auto_module
from custom_components.comexio.certificate import (
    ACCEPT_ENTRY_CHANGED,
    ACCEPT_PINNED,
    ACCEPT_REPAIR_OPEN,
    CERTIFICATE_ACCEPT_LOCKS,
    async_accept_certificate,
    async_learn_pin_authority,
    async_report_certificate_changed,
    certificate_issue_id,
)
from custom_components.comexio.certificate_auto_accept import CERTIFICATE_AUTO_ACCEPTS, async_certificate_changed
from custom_components.comexio.const import (
    CERTIFICATE_AUTO_ACCEPT_MAX_READS,
    CONF_CERTIFICATE_AUTHORITY,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_CERTIFICATE_PINNED_AT,
    CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    DOMAIN,
    LOGIN_ERROR_CERTIFICATE,
)

from .conftest import HOST, SERVER_ID

PINNED = "a" * 64
OTHER = "b" * 64
THIRD = "c" * 64
ISSUER = "CN=Comexio LAN CA"
AKI = "0a1b2c3d"
FIRMWARE = "11.1.5"
PHONE = "mobile_app_phone"
# As the user sees them: all 32 bytes, upper case, colon-separated (written out, not built by the code under test).
PINNED_SHOWN = ":".join(["AA"] * 32)
OTHER_SHOWN = ":".join(["BB"] * 32)


def _info(fingerprint: str, issuer: str | None = ISSUER, aki: str | None = AKI) -> CertificateInfo:
    return CertificateInfo(fingerprint, None, None, issuer, aki)


@pytest.fixture
def entry() -> MockConfigEntry:
    """An entry pinned to PINNED whose issuer is known, pushing to PHONE; not set up (the rule needs no setup)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Comexio {SERVER_ID} ({HOST})",
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={
            CONF_HOST: HOST,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "admin",
            CONF_SERVER_ID: SERVER_ID,
            CONF_CERTIFICATE_FINGERPRINT: PINNED,
            CONF_CERTIFICATE_PINNED_AT: "2026-01-01T00:00:00+00:00",
            CONF_CERTIFICATE_AUTHORITY: {"fingerprint": PINNED, "issuer": ISSUER, "authority_key_id": AKI},
        },
        options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}"]},
    )


@pytest.fixture
def reads() -> Generator[AsyncMock]:
    """The rule's handshake (with issuer); the settle time is 0, so the wait is one loop turn."""
    read = AsyncMock(name="fetch_certificate")
    with (
        patch.object(auto_module, "fetch_certificate", read),
        patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 0),
    ):
        yield read


@pytest.fixture
def accept_read() -> Generator[AsyncMock]:
    """The acceptance's own read of the fingerprint (certificate.async_accept_certificate)."""
    read = AsyncMock(name="fetch_certificate_fingerprint")
    with patch("custom_components.comexio.certificate.fetch_certificate_fingerprint", read):
        yield read


@pytest.fixture
def reload() -> Generator[MagicMock]:
    """The reload of an entry that is not loaded: recorded, not run (these tests set up no entry)."""
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload") as scheduled:
        yield scheduled


@pytest.fixture
def notification() -> Generator[MagicMock]:
    with patch.object(auto_module.persistent_notification, "async_create") as create:
        yield create


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, certificate_issue_id(entry.entry_id))


async def _changed(hass: HomeAssistant, entry: MockConfigEntry, actual: str, firmware: str | None = FIRMWARE) -> None:
    async_certificate_changed(hass, entry, actual, firmware)
    await hass.async_block_till_done(wait_background_tasks=True)  # the rule runs as a background task


async def test_a_new_certificate_of_the_pinned_issuer_is_pinned_and_the_user_is_told(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert _issue(hass, entry) is None  # never asked
    reload.assert_called_once_with(entry.entry_id)  # not loaded: the new pin takes effect with a setup
    notification.assert_called_once()
    notice = notification.call_args.args[1]
    assert f"Issuer: {ISSUER}" in notice
    told = [push for push in pushes if push.data["message"] != "clear_notification"]
    assert len(told) == 1
    # The user compares both values with Comexio's admin page: the notification and the push name both, complete.
    for text in (notice, told[0].data["message"]):
        assert f"Previous (SHA-256): {PINNED_SHOWN}\n" in text
        assert f"New (SHA-256): {OTHER_SHOWN}\n" in text
    assert "actions" not in told[0].data["data"]  # nothing left to decide
    assert told[0].data["data"]["push"]["interruption-level"] == "critical"
    assert "pinned by the rule for the pinned issuer, replacing AA:AA" in caplog.text


async def test_the_notification_stays_english_and_the_push_follows_the_system_language(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
) -> None:
    # A persistent notification has no user: the system language need not be the viewer's.
    hass.config.language = "de"
    entry.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER

    await _changed(hass, entry, OTHER)

    # Written out, not taken from the module: a title translated into the wrong language must fail here.
    assert notification.call_args.kwargs["title"] == "Comexio: new certificate accepted"
    assert f"Issuer: {ISSUER}" in notification.call_args.args[1]
    assert "Aussteller: " not in notification.call_args.args[1]
    told = [push for push in pushes if push.data["message"] != "clear_notification"]
    assert told[0].data["title"] == "Comexio: neues Zertifikat übernommen"
    # The body is German too, not only the title (a German title over an English body would pass the line above).
    assert f"Aussteller: {ISSUER}" in told[0].data["message"]
    assert f"Bisher (SHA-256): {PINNED_SHOWN}\n" in told[0].data["message"]
    assert f"Neu (SHA-256): {OTHER_SHOWN}\n" in told[0].data["message"]
    assert "Issuer: " not in told[0].data["message"]


@pytest.mark.logged_exception  # the failed notice is logged with its traceback
async def test_the_push_goes_out_although_the_notification_could_not_be_created(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
) -> None:
    entry.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)
    notification.side_effect = RuntimeError("no notification")
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert [push for push in pushes if push.data["message"] != "clear_notification"]  # the user is still told


async def test_a_failed_acceptance_read_asks_for_the_newest_certificate_reported_meanwhile(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)

    async def poll_meets_another_then_read_fails(_host: str) -> str:
        async_certificate_changed(hass, entry, THIRD, FIRMWARE)  # the running wait is kept
        raise ComexioConnectionError("down")

    accept_read.side_effect = poll_meets_another_then_read_fails

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == THIRD  # not the settled one Comexio may no longer present


async def test_the_new_pin_has_no_issuer_until_a_setup_reads_it_from_the_new_certificate(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock, reload: MagicMock
) -> None:
    # The stored pair belongs to the old pin: a third certificate right after must not be compared against it.
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER
    await _changed(hass, entry, OTHER)

    await _changed(hass, entry, THIRD)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert _issue(hass, entry).data["actual"] == THIRD


async def test_another_issuer_goes_to_the_user_and_is_not_taken_up_again(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, reload: MagicMock
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER, issuer="CN=Someone else")

    await _changed(hass, entry, OTHER)
    ir.async_delete_issue(hass, DOMAIN, certificate_issue_id(entry.entry_id))  # as if dismissed
    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER
    assert reads.await_count == 1  # turned down once: the second report goes straight to the repair


async def test_another_authority_key_id_goes_to_the_user(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, reload: MagicMock
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER, aki="ffff")

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER


@pytest.mark.parametrize("firmware", ["11.1.6", "12.0.0", None, "unknown"])
async def test_a_firmware_beyond_the_known_one_or_an_unknown_one_goes_to_the_user(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, firmware: str | None
) -> None:
    entry.add_to_hass(hass)

    await _changed(hass, entry, OTHER, firmware)

    reads.assert_not_awaited()
    assert _issue(hass, entry).data["actual"] == OTHER


@pytest.mark.parametrize(
    "authority",
    [None, {"fingerprint": THIRD, "issuer": ISSUER, "authority_key_id": AKI}],
    ids=["never learned", "learned for an earlier pin"],
)
async def test_without_the_pins_own_issuer_the_user_decides(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, authority: Any
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: authority})

    await _changed(hass, entry, OTHER)

    reads.assert_not_awaited()
    assert _issue(hass, entry).data["actual"] == OTHER


async def test_an_open_repair_stays_the_users_decision(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    async_report_certificate_changed(hass, entry, THIRD)  # e.g. from before the rule existed

    await _changed(hass, entry, OTHER)

    reads.assert_not_awaited()
    assert _issue(hass, entry).data["actual"] == OTHER


async def test_a_certificate_that_keeps_changing_goes_to_the_user(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = [_info(str(n) * 64) for n in range(1, CERTIFICATE_AUTO_ACCEPT_MAX_READS + 1)]

    await _changed(hass, entry, OTHER)

    assert reads.await_count == CERTIFICATE_AUTO_ACCEPT_MAX_READS
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    newest = str(CERTIFICATE_AUTO_ACCEPT_MAX_READS) * 64
    assert _issue(hass, entry).data["actual"] == newest  # the one read last, not the first report


async def test_a_repair_opened_during_the_wait_stays_the_users_decision(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def repair_opened(_host: str) -> CertificateInfo:
        async_report_certificate_changed(hass, entry, OTHER)  # e.g. a reconfigure's test met it meanwhile
        return _info(OTHER)

    reads.side_effect = repair_opened

    await _changed(hass, entry, OTHER)

    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER


async def test_a_repair_opened_while_the_acceptance_waits_for_its_lock_stays_the_users_decision(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    # The rule looked for a repair before it queued for the acceptance lock (a tap on the repair holds it).
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    lock = hass.data.setdefault(CERTIFICATE_ACCEPT_LOCKS, {}).setdefault(entry.entry_id, asyncio.Lock())

    async with lock:
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        for _ in range(10):
            await asyncio.sleep(0)  # the rule runs up to the lock
        assert _issue(hass, entry) is None
        async_report_certificate_changed(hass, entry, THIRD)  # a repair comes up, raised after the rule's read
    await hass.async_block_till_done(wait_background_tasks=True)

    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER  # still the user's repair, brought up to the newest read


async def test_the_acceptance_of_the_rule_leaves_an_open_repair_alone(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    async_report_certificate_changed(hass, entry, OTHER)

    outcome, current = await async_accept_certificate(hass, entry, OTHER, by_rule=True)

    assert (outcome, current) == (ACCEPT_REPAIR_OPEN, None)
    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry) is not None


async def test_the_acceptance_by_the_user_is_not_held_back_by_an_open_repair(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock, reload: MagicMock
) -> None:
    # The open repair is what the user accepts: only the rule steps back for it.
    entry.add_to_hass(hass)
    async_report_certificate_changed(hass, entry, OTHER)
    accept_read.return_value = OTHER

    outcome, current = await async_accept_certificate(hass, entry, OTHER)

    assert (outcome, current) == (ACCEPT_PINNED, OTHER)
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER


async def test_the_acceptance_of_the_rule_pins_nothing_on_an_entry_disabled_while_it_queued(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)

    outcome, current = await async_accept_certificate(hass, entry, OTHER, by_rule=True)

    assert (outcome, current) == (ACCEPT_ENTRY_CHANGED, None)
    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED


async def test_the_acceptance_of_the_rule_leaves_a_repair_alone_that_came_up_while_it_read(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def repair_opened(_host: str) -> str:
        async_report_certificate_changed(hass, entry, THIRD)  # e.g. a poll met another certificate meanwhile
        return OTHER

    accept_read.side_effect = repair_opened

    outcome, current = await async_accept_certificate(hass, entry, OTHER, by_rule=True)

    assert (outcome, current) == (ACCEPT_REPAIR_OPEN, None)
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == THIRD  # the user's repair is not cleared or replaced


async def test_the_acceptance_of_the_rule_pins_nothing_on_an_entry_disabled_while_it_read(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def disabled(_host: str) -> str:
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        return OTHER

    accept_read.side_effect = disabled

    outcome, current = await async_accept_certificate(hass, entry, OTHER, by_rule=True)

    assert (outcome, current) == (ACCEPT_ENTRY_CHANGED, None)
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED


async def test_a_failing_read_during_the_wait_reports_the_newest_certificate_seen(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = [_info(THIRD), ComexioConnectionError("down")]  # another certificate, then the read fails

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == THIRD  # not the one the first report named


async def test_a_report_during_the_wait_is_what_a_failing_read_reports(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def poll_meets_another_then_read_fails(_host: str) -> CertificateInfo:
        async_certificate_changed(hass, entry, THIRD, FIRMWARE)  # a later poll: the running wait is kept
        raise ComexioConnectionError("down")

    reads.side_effect = poll_meets_another_then_read_fails

    await _changed(hass, entry, OTHER)

    assert _issue(hass, entry).data["actual"] == THIRD


@pytest.mark.parametrize("shown", [OTHER, THIRD], ids=["the_first_again", "the_one_reported_meanwhile"])
async def test_a_certificate_reported_during_the_wait_restarts_the_settle_interval(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock, reload: MagicMock, shown: str
) -> None:
    entry.add_to_hass(hass)
    accept_read.return_value = shown
    answers = [_info(shown), _info(shown)]
    settle_sec = 7  # not 0 like the fixture's: the sleeps of exactly this length are what is counted
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float, *args: Any) -> None:
        slept.append(delay)
        await real_sleep(0, *args)  # no real waiting

    async def poll_meets_another_during_the_first_read(_host: str) -> CertificateInfo:
        if len(answers) == 2:  # the first read only
            async_certificate_changed(hass, entry, THIRD, FIRMWARE)
        return answers.pop()

    reads.side_effect = poll_meets_another_during_the_first_read

    with (
        patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", settle_sec),
        patch.object(
            auto_module, "asyncio", SimpleNamespace(sleep=recording_sleep, Task=asyncio.Task)
        ),  # this module only
    ):
        await _changed(hass, entry, OTHER)

    assert reads.await_count == 2  # whichever one the read shows, it did not stay the same for the interval
    # The settle interval is waited before every read: the second read after the report is not made at once.
    assert slept.count(settle_sec) == 2
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == shown


async def test_an_entry_disabled_during_the_wait_pins_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def disabled(_host: str) -> CertificateInfo:
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        return _info(OTHER)

    reads.side_effect = disabled

    await _changed(hass, entry, OTHER)

    assert entry.disabled_by is ConfigEntryDisabler.USER
    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry) is None  # switched off: no repair for it either


async def test_a_repair_opened_for_an_older_certificate_is_brought_up_to_the_newest(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def stale_repair_opened(_host: str) -> CertificateInfo:
        async_report_certificate_changed(hass, entry, THIRD)  # an older certificate than the one now presented
        return _info(OTHER)

    reads.side_effect = stale_repair_opened

    await _changed(hass, entry, OTHER)

    accept_read.assert_not_awaited()
    assert _issue(hass, entry).data["actual"] == OTHER  # the user compares what Comexio presents now


async def test_why_the_rule_does_not_apply_is_logged_once_per_certificate(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)  # the line is INFO: not left to the harness's default level
    entry.add_to_hass(hass)

    for _ in range(3):  # every poll reports it again
        async_certificate_changed(hass, entry, OTHER, "12.0.0")

    assert caplog.text.count("is left to the user (#294)") == 1
    assert "firmware 12.0.0 is not one known to replace its certificate" in caplog.text
    reads.assert_not_awaited()


@pytest.mark.logged_exception  # the failed notice is logged with its traceback
async def test_a_failed_notice_leaves_the_new_pin_and_says_so_in_the_log(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER
    notification.side_effect = RuntimeError("bug")

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert _issue(hass, entry) is None  # pinned: nothing left to ask
    assert "the notice of the accepted certificate could not be created" in caplog.text


async def test_a_certificate_that_settles_after_one_change_is_taken_over(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock, reload: MagicMock
) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = [_info(THIRD), _info(THIRD)]
    accept_read.return_value = THIRD

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == THIRD
    assert _issue(hass, entry) is None


async def test_comexio_not_answering_the_acceptance_goes_to_the_user(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    notification: MagicMock,
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    accept_read.side_effect = ComexioConnectionError("down")

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER  # never silent: the repair asks
    notification.assert_not_called()


async def test_yet_another_certificate_at_the_acceptance_goes_to_the_user(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    notification: MagicMock,
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    accept_read.return_value = THIRD  # changed again between the rule's read and the acceptance's

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == THIRD  # the newest, not the one the rule checked
    notification.assert_not_called()


async def test_a_failed_read_goes_to_the_user(hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = ComexioConnectionError("down")

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == OTHER


@pytest.mark.logged_exception  # the rule logs the unexpected error with its traceback
async def test_an_unexpected_error_goes_to_the_user_too(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = RuntimeError("bug")

    await _changed(hass, entry, OTHER)

    assert _issue(hass, entry).data["actual"] == OTHER
    assert "the repair asks instead" in caplog.text


async def test_the_pinned_certificate_back_needs_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, reload: MagicMock
) -> None:
    entry.add_to_hass(hass)
    reads.return_value = _info(PINNED)

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry) is None
    reload.assert_called_once_with(entry.entry_id)  # not loaded: a setup gets through with the pin now


async def test_the_pinned_certificate_back_takes_down_a_repair_raised_during_the_wait(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, reload: MagicMock
) -> None:
    entry.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)

    async def repair_raised_then_pin_back(_host: str) -> CertificateInfo:
        # A reconfigure's connection test met another certificate while the rule waited.
        async_report_certificate_changed(hass, entry, THIRD)
        assert _issue(hass, entry) is not None  # the repair is up before the pin comes back
        return _info(PINNED)

    reads.side_effect = repair_raised_then_pin_back

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry) is None  # it asked about a certificate Comexio no longer presents
    # Its push goes off the phones too.
    assert [push.data["message"] for push in pushes][-1] == "clear_notification"
    reload.assert_called_once_with(entry.entry_id)  # not loaded: a setup gets through with the pin now


async def test_reports_during_the_wait_start_no_second_one(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    reads.side_effect = ComexioConnectionError("down")

    async_certificate_changed(hass, entry, OTHER, FIRMWARE)
    async_certificate_changed(hass, entry, OTHER, FIRMWARE)  # the next poll, while the first waits
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 1
    assert _issue(hass, entry).data["actual"] == OTHER


async def test_a_reconfigure_during_the_wait_pins_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    async def reconfigured(_host: str) -> CertificateInfo:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})
        return _info(OTHER)

    reads.side_effect = reconfigured

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    accept_read.assert_not_awaited()
    assert _issue(hass, entry) is None  # the new connection decides on its own


async def test_the_state_of_the_rule_is_not_bound_to_the_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # A setup retry or reload must not end the wait: it lives in hass.data, keyed by the entry.
    entry.add_to_hass(hass)
    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        state = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id]
        assert state.task is not None
        assert not state.task.done()
        state.task.cancel()
    await hass.async_block_till_done()


async def test_an_unload_of_the_entry_during_the_wait_does_not_end_it(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    # The wait is a background task of Home Assistant, not of the entry: the unload of a reload or a setup retry
    # cancels the entry's own tasks but not this one.
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    with (
        patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600),
        patch("custom_components.comexio.async_unload_entry", AsyncMock(return_value=True)),
    ):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        task = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id].task

        assert await hass.config_entries.async_unload(entry.entry_id)
        await asyncio.sleep(0)

        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not task.done()
        task.cancel()
    await hass.async_block_till_done()


async def test_a_wait_for_the_connection_before_a_reconfigure_does_not_swallow_the_next_report(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        state = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id]
        first = state.task
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})

        async_certificate_changed(hass, entry, OTHER, FIRMWARE)

        assert state.task is not first  # a wait of its own for the new host
        assert state.connection == ("192.0.2.99", PINNED)
        for task in (first, state.task):
            task.cancel()
    await hass.async_block_till_done()


async def test_removing_the_entry_ends_its_wait(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        task = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id].task

        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert task.cancelled()
    assert entry.entry_id not in hass.data[CERTIFICATE_AUTO_ACCEPTS]


# Learning the pin's issuer


async def test_the_issuer_is_learned_from_a_handshake_that_presents_the_pin(
    hass: HomeAssistant, entry: MockConfigEntry, authority_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})
    authority_read.side_effect = None
    authority_read.return_value = _info(PINNED)

    await async_learn_pin_authority(hass, entry)

    assert entry.data[CONF_CERTIFICATE_AUTHORITY] == {
        "fingerprint": PINNED,
        "issuer": ISSUER,
        "authority_key_id": AKI,
    }


@pytest.mark.parametrize(
    "presented",
    [_info(OTHER), _info(PINNED, issuer=None), _info(PINNED, aki=None)],
    ids=["another certificate", "no issuer", "no key id"],
)
async def test_nothing_is_learned_from_another_certificate_or_an_incomplete_one(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    authority_read: AsyncMock,
    presented: CertificateInfo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})
    authority_read.side_effect = None
    authority_read.return_value = presented

    await async_learn_pin_authority(hass, entry)

    assert entry.data[CONF_CERTIFICATE_AUTHORITY] is None
    # An incomplete pin is the one reason the rule never applies that the user cannot see in Repairs: say it once.
    assert ("never accepted automatically" in caplog.text) == (presented.fingerprint == PINNED)


async def test_a_certificate_pinned_by_someone_else_meanwhile_is_not_reported_as_the_rule(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_FINGERPRINT: OTHER})
    accept_read.return_value = OTHER

    by_rule, _ = await async_accept_certificate(hass, entry, OTHER, by_rule=True)
    by_user, _ = await async_accept_certificate(hass, entry, OTHER)

    assert by_rule == ACCEPT_ENTRY_CHANGED  # no "accepted automatically" notice
    assert by_user == ACCEPT_PINNED


async def test_a_known_issuer_is_not_read_again(
    hass: HomeAssistant, entry: MockConfigEntry, authority_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)

    await async_learn_pin_authority(hass, entry)

    authority_read.assert_not_awaited()


async def test_a_failed_read_leaves_the_issuer_unknown(
    hass: HomeAssistant, entry: MockConfigEntry, authority_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})

    await async_learn_pin_authority(hass, entry)  # the fixture's default: no HTTPS

    assert entry.data[CONF_CERTIFICATE_AUTHORITY] is None


async def test_a_start_after_the_certificate_changed_takes_it_over_with_the_saved_firmware(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_comexio_api: list,
    api_returns: dict,
    api_attributes: dict,
    authority_read: AsyncMock,
    accept_read: AsyncMock,
) -> None:
    # Home Assistant restarted after Comexio replaced its certificate: no poll has read the firmware in this run,
    # so the rule has only what the last scraped poll saved; the login fails on the new certificate.
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})
    authority_read.side_effect = None
    authority_read.return_value = _info(PINNED)
    api_attributes["comexio_version"] = FIRMWARE
    assert await hass.config_entries.async_setup(entry.entry_id)  # learns the issuer, saves config and firmware
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    api_attributes["comexio_version"] = None
    api_returns["login"] = False
    api_attributes["last_login_error"] = LOGIN_ERROR_CERTIFICATE
    api_attributes["certificate_mismatch"] = ComexioCertificateMismatchError("foreign", expected=PINNED, actual=OTHER)
    authority_read.return_value = _info(OTHER)
    accept_read.return_value = OTHER
    with (
        patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 0),
        patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload"),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.data[DOMAIN][entry.entry_id].started_from_saved_config is True
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    assert _issue(hass, entry) is None


async def test_the_setup_learns_the_issuer_without_a_reload(
    hass: HomeAssistant, entry: MockConfigEntry, mock_comexio_api: list, authority_read: AsyncMock
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})
    authority_read.side_effect = None
    authority_read.return_value = _info(PINNED)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_CERTIFICATE_AUTHORITY]["issuer"] == ISSUER
    assert len(mock_comexio_api) == 1  # one setup: recording the issuer reloaded nothing
