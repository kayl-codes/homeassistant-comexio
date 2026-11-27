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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.comexio import certificate_auto_accept as auto_module
from custom_components.comexio.certificate import (
    ACCEPT_ENTRY_CHANGED,
    ACCEPT_PINNED,
    ACCEPT_REPAIR_OPEN,
    ACCEPT_RESTORED,
    CERTIFICATE_ACCEPT_LOCKS,
    async_accept_certificate,
    async_learn_pin_authority,
    async_report_certificate_changed,
    certificate_issue_id,
)
from custom_components.comexio.certificate_auto_accept import (
    CERTIFICATE_AUTO_ACCEPTS,
    async_certificate_changed,
    certificate_in_check,
)
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
    certificate_check_signal,
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
THIRD_SHOWN = ":".join(["CC"] * 32)


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
    assert told[0].data["data"]["tag"] == _acceptance_tag(entry)
    assert "pinned by the rule for the pinned issuer, replacing AA:AA" in caplog.text
    assert "no confirmation from" not in caplog.text  # a push that reached its phone is not reported as failed


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


async def test_an_acceptance_push_no_phone_gets_is_summed_up_in_the_log(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry.add_to_hass(hass)

    async def broken(_call: Any) -> None:
        raise HomeAssistantError("companion app down")

    hass.services.async_register("notify", PHONE, broken)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER  # pinned although the push failed
    assert f"confirmed by 0 of 1 notify targets; no confirmation from: ['notify.{PHONE}']" in caplog.text


async def test_a_partly_failed_acceptance_push_names_only_the_targets_without_confirmation(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}", "notify.mobile_app_broken"]}
    )
    pushes = async_mock_service(hass, "notify", PHONE)

    async def broken(_call: Any) -> None:
        raise HomeAssistantError("companion app down")

    hass.services.async_register("notify", "mobile_app_broken", broken)
    reads.return_value = _info(OTHER)
    accept_read.return_value = OTHER

    await _changed(hass, entry, OTHER)

    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    told = [push for push in pushes if push.data["message"] != "clear_notification"]
    assert len(told) == 1  # the working phone got the push
    assert "confirmed by 1 of 2 notify targets; no confirmation from: ['notify.mobile_app_broken']" in caplog.text


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

    assert (outcome, current) == (ACCEPT_REPAIR_OPEN, OTHER)  # what the read found: the newest the rule knows
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry).data["actual"] == THIRD  # the user's repair is not cleared or replaced


async def test_a_repair_opened_during_the_acceptance_read_shows_what_that_read_found(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock
) -> None:
    # The wait settled on OTHER; while the acceptance read again, a repair came up for THIRD and Comexio moved on
    # to a fourth certificate, which that read found.
    entry.add_to_hass(hass)
    reads.return_value = _info(OTHER)
    fourth = "d" * 64

    async def moved_on(_host: str) -> str:
        async_report_certificate_changed(hass, entry, THIRD)
        return fourth

    accept_read.side_effect = moved_on

    await _changed(hass, entry, OTHER)

    accept_read.assert_awaited_once()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    # Brought up to the acceptance's read: neither OTHER, which Comexio no longer presents, nor the repair's THIRD.
    assert _issue(hass, entry).data["actual"] == fourth


async def test_a_repair_opened_during_the_acceptance_read_is_cleared_when_that_read_finds_the_pin(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock
) -> None:
    # A poll met THIRD while the acceptance read, which found Comexio back on the pin: the repair would ask about a
    # certificate Comexio no longer presents.
    entry.add_to_hass(hass)

    async def back_on_the_pin(_host: str) -> str:
        async_report_certificate_changed(hass, entry, THIRD)
        return PINNED

    accept_read.side_effect = back_on_the_pin

    outcome, current = await async_accept_certificate(hass, entry, OTHER, by_rule=True)

    assert (outcome, current) == (ACCEPT_RESTORED, PINNED)
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == PINNED
    assert _issue(hass, entry) is None


@pytest.mark.parametrize("read", [OTHER, PINNED], ids=["another-certificate", "the-pin-again"])
async def test_the_acceptance_of_the_rule_pins_nothing_on_an_entry_disabled_while_it_read(
    hass: HomeAssistant, entry: MockConfigEntry, accept_read: AsyncMock, read: str
) -> None:
    # The pin found again restores nothing either: a disabled entry is not ours to clear or reload.
    entry.add_to_hass(hass)

    async def disabled(_host: str) -> str:
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        return read

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
            auto_module,
            "asyncio",
            SimpleNamespace(
                sleep=recording_sleep, Task=asyncio.Task, Lock=asyncio.Lock, current_task=asyncio.current_task
            ),
        ),  # this module only
    ):
        await _changed(hass, entry, OTHER)

    assert reads.await_count == 2  # whichever one the read shows, it did not stay the same for the interval
    # The settle interval is waited before every read: the second read after the report is not made at once.
    assert slept.count(settle_sec) == 2
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == shown


async def _move_host(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})


async def _disable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)


async def _move_pin(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_FINGERPRINT: THIRD})


@pytest.mark.parametrize("change", [_move_host, _disable, _move_pin], ids=["host", "disabled", "pin"])
async def test_an_entry_changed_during_the_settle_wait_makes_no_read_of_the_old_host(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock, accept_read: AsyncMock, change: Any
) -> None:
    entry.add_to_hass(hass)
    slept: list[float] = []

    async def changed_while_sleeping(delay: float, *_args: Any) -> None:
        slept.append(delay)
        await change(hass, entry)

    with patch.object(
        auto_module,
        "asyncio",
        SimpleNamespace(
            sleep=changed_while_sleeping, Task=asyncio.Task, Lock=asyncio.Lock, current_task=asyncio.current_task
        ),
    ):  # this module only
        await _changed(hass, entry, OTHER)

    assert len(slept) == 1  # the rule started and reached the wait; it ended there
    reads.assert_not_awaited()  # the old endpoint gets no handshake once the entry moved on
    accept_read.assert_not_awaited()
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] != OTHER  # nothing pinned by the rule
    assert _issue(hass, entry) is None  # the new connection decides on its own
    assert certificate_in_check(hass, entry) is None  # the check ended with the wait


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


async def test_the_end_of_a_replaced_wait_does_not_end_the_check_of_the_new_one(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    # The connection sensor shows certificate_changed while a wait runs (certificate_in_check); the cancelled wait of
    # the connection before a reconfigure ends after the new one started and must not clear it.
    entry.add_to_hass(hass)
    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        await asyncio.sleep(0)  # the first wait runs (its cleanup only runs once it started)
        state = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id]
        first = state.task
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})
        async_certificate_changed(hass, entry, THIRD, FIRMWARE)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert first.cancelled()
        assert certificate_in_check(hass, entry) == THIRD
        state.task.cancel()
        await hass.async_block_till_done()

    assert certificate_in_check(hass, entry) is None  # its own end does


async def test_a_replaced_wait_is_kept_until_it_ended(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A wait a newer report replaced is recorded while it runs, so a removal can wait for it too."""
    entry.add_to_hass(hass)
    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC", 3600):
        async_certificate_changed(hass, entry, OTHER, FIRMWARE)
        await asyncio.sleep(0)
        state = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id]
        first = state.task
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})
        async_certificate_changed(hass, entry, THIRD, FIRMWARE)

        assert state.superseded == {first}
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)  # the done callback runs one turn after the end

        assert first.cancelled()
        assert state.superseded == set()  # its end takes it out again
        state.task.cancel()
        await hass.async_block_till_done()


async def test_the_clear_waits_for_a_replaced_wait_still_sending_its_push(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    order: list[str] = []
    tag = _acceptance_tag(entry)

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            if call.data["data"]["tag"] == tag:
                order.append("clear")
            return
        try:
            await asyncio.Event().wait()
        finally:
            for _ in range(50):  # the cancelled send takes many turns to end: a clear that did not wait is first
                await asyncio.sleep(0)
            order.append("send ended")

    hass.services.async_register("notify", PHONE, phone)
    sending = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The send belongs to a wait a newer one replaced; the newer one has ended.
    hass.data.setdefault(CERTIFICATE_AUTO_ACCEPTS, {})[entry.entry_id] = auto_module._AutoAccepts(superseded={sending})

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sending.cancelled()
    assert order == ["send ended", "clear"]


async def test_the_clear_waits_for_the_send_in_flight_while_a_newer_push_waits_behind_it(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The clear comes after the send still in flight. A newer push queued behind it (one push at a time per entry)
    never reaches the phone: it is cancelled while it waits."""
    entry.add_to_hass(hass)
    order: list[str] = []
    sends: list[str] = []
    started = asyncio.Event()
    tag = _acceptance_tag(entry)

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            if call.data["data"]["tag"] == tag:
                order.append("clear")
            return
        sends.append(call.data["message"])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            for _ in range(50):  # the send in flight is the slow one
                await asyncio.sleep(0)
            order.append("send ended")

    hass.services.async_register("notify", PHONE, phone)
    replaced = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER)))
    await asyncio.wait_for(started.wait(), timeout=5)  # the older send holds the lock
    current = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(THIRD)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    accepts = auto_module._AutoAccepts(task=current, superseded={replaced})
    hass.data.setdefault(CERTIFICATE_AUTO_ACCEPTS, {})[entry.entry_id] = accepts

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert replaced.cancelled()
    assert current.cancelled()
    assert len(sends) == 1  # the newer push waited behind the send in flight and never got its turn
    assert order == ["send ended", "clear"]
    assert entry.entry_id not in hass.data.get(auto_module.CERTIFICATE_AUTO_ACCEPT_SENDS, {})  # the lock went with it


async def test_a_newer_push_does_not_reach_the_phone_before_the_older_one_was_sent(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    # Both pushes carry the tag of the entry: were the newer one sent while the older one is still in its notify call,
    # the older text could replace it on the phone. The newer one goes out only after the older one has been sent.
    entry.add_to_hass(hass)
    sent: list[str] = []
    first_sending = asyncio.Event()
    release = asyncio.Event()

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            return
        sent.append(call.data["message"])
        if len(sent) == 1:
            first_sending.set()
            await release.wait()

    hass.services.async_register("notify", PHONE, phone)
    older = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER)))
    await asyncio.wait_for(first_sending.wait(), timeout=5)
    newer = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(THIRD)))
    for _ in range(50):  # late enough for the newer push to reach the phone if it were not held back
        await asyncio.sleep(0)

    assert len(sent) == 1  # the newer push waits its turn
    release.set()
    await asyncio.wait_for(asyncio.gather(older, newer), timeout=5)
    assert len(sent) == 2
    assert OTHER_SHOWN in sent[0]
    assert THIRD_SHOWN in sent[1]  # the order the pushes were made in


async def test_a_wait_replaced_while_it_sends_its_push_keeps_the_push(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    reads: AsyncMock,
    accept_read: AsyncMock,
    reload: MagicMock,
    notification: MagicMock,
) -> None:
    # The pin is made and its push is on the way when a second change comes in. Cancelling that wait would cut the push
    # of a certificate that is accepted already; the push finishes, the new change's wait runs on its own.
    entry.add_to_hass(hass)
    sending = asyncio.Event()
    release = asyncio.Event()
    outcome: list[str] = []
    reads_made = iter([_info(OTHER)])
    hang = asyncio.Event()  # never set: the wait of the second change reads Comexio only after this

    async def read(_host: str) -> CertificateInfo:
        if (info := next(reads_made, None)) is not None:
            return info
        await hang.wait()
        return _info(THIRD)

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            return
        sending.set()
        try:
            await release.wait()
            outcome.append("push finished")
        except asyncio.CancelledError:
            outcome.append("push cancelled")
            raise

    reads.side_effect = read
    hass.services.async_register("notify", PHONE, phone)
    accept_read.return_value = OTHER

    async_certificate_changed(hass, entry, OTHER, FIRMWARE)
    await asyncio.wait_for(sending.wait(), timeout=5)  # the pin is made and the push is being sent
    assert entry.data[CONF_CERTIFICATE_FINGERPRINT] == OTHER
    # A setup reads the issuer of the new pin after the acceptance's reload; the rule applies to a second change then.
    authority = {"fingerprint": OTHER, "issuer": ISSUER, "authority_key_id": AKI}
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: authority})
    state = hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id]
    first = state.superseded.copy() or {state.task}
    assert state.telling == first  # the pinned wait is marked while it sends

    async_certificate_changed(hass, entry, THIRD, FIRMWARE)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not outcome  # the push is still going: not cancelled by the replacement
    assert first <= state.superseded  # a removal still waits for it
    second = state.task
    assert second is not None
    assert second not in first
    assert not second.done()  # the second change got a wait of its own
    release.set()
    await asyncio.wait_for(asyncio.gather(*first, return_exceptions=True), timeout=5)
    assert outcome == ["push finished"]
    assert state.telling == set()  # the end of the push takes it out again
    second.cancel()  # the wait of the second change, still reading
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_read_of_the_wait_that_shows_another_certificate_is_signalled(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    # The sensor shows what certificate_in_check returns, so a change of it by the wait's own read needs a signal.
    entry.add_to_hass(hass)
    shown: list[str | None] = []
    unsub = async_dispatcher_connect(
        hass, certificate_check_signal(entry.entry_id), lambda: shown.append(certificate_in_check(hass, entry))
    )
    second_read = asyncio.Event()

    async def _read(_host: str) -> CertificateInfo:
        if reads.await_count == 1:
            return _info(THIRD)  # Comexio changed its certificate again during the wait
        await second_read.wait()
        return _info(THIRD)

    reads.side_effect = _read
    async_certificate_changed(hass, entry, OTHER, FIRMWARE)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert shown == [OTHER, THIRD]  # the start, then the read that saw another one
    hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id].task.cancel()
    await hass.async_block_till_done()
    unsub()
    assert shown == [OTHER, THIRD, None]  # the end


async def test_a_wait_for_another_connection_is_no_check_of_the_current_one(
    hass: HomeAssistant, entry: MockConfigEntry, reads: AsyncMock
) -> None:
    # A reconfigure to another host or a new pin leaves the old wait running until it ends (_still_current).
    entry.add_to_hass(hass)
    never = asyncio.Event()

    async def _read(_host: str) -> CertificateInfo:
        await never.wait()
        return _info(OTHER)

    reads.side_effect = _read
    async_certificate_changed(hass, entry, OTHER, FIRMWARE)
    await asyncio.sleep(0)
    assert certificate_in_check(hass, entry) == OTHER

    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "192.0.2.99"})
    assert certificate_in_check(hass, entry) is None
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: HOST})
    assert certificate_in_check(hass, entry) == OTHER
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_FINGERPRINT: OTHER})
    assert certificate_in_check(hass, entry) is None

    hass.data[CERTIFICATE_AUTO_ACCEPTS][entry.entry_id].task.cancel()
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


def _acceptance_tag(entry: MockConfigEntry) -> str:
    """The tag acceptance pushes already on phones carry: written out, not derived, so a changed tag fails here."""
    return f"comexio_certificate_auto_{entry.entry_id}"


def _clears_of_the_acceptance_push(pushes: list[Any], entry: MockConfigEntry) -> list[Any]:
    tag = _acceptance_tag(entry)
    return [push for push in pushes if push.data["message"] == "clear_notification" and push.data["data"]["tag"] == tag]


async def test_removing_the_entry_takes_the_acceptance_push_off_the_phones(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Its own tag: the clear of the repair's push (another tag) does not take this one off the phone.
    assert len(_clears_of_the_acceptance_push(pushes, entry)) == 1


async def test_removing_the_entry_clears_companion_apps_only(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Any other notify service would deliver the command as the text "clear_notification"."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}", "notify.family_group"]}
    )
    phone = async_mock_service(hass, "notify", PHONE)
    group = async_mock_service(hass, "notify", "family_group")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    assert group == []


async def test_removing_an_entry_without_a_pin_sends_no_clear(hass: HomeAssistant) -> None:
    """No pin, no rule, no push: the phones are not bothered."""
    plain = MockConfigEntry(
        domain=DOMAIN,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
        data={CONF_HOST: HOST, CONF_USERNAME: "admin", CONF_PASSWORD: "admin", CONF_SERVER_ID: SERVER_ID},
        options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}"]},
    )
    plain.add_to_hass(hass)
    pushes = async_mock_service(hass, "notify", PHONE)

    await hass.config_entries.async_remove(plain.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert pushes == []


async def test_removing_an_entry_with_group_targets_only_sends_nothing_and_says_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=auto_module.__name__)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: ["notify.family_group"]})
    group = async_mock_service(hass, "notify", "family_group")

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert group == []
    assert "acceptance push clear" not in caplog.text


async def test_a_phone_not_registered_does_not_keep_the_registered_one_from_the_clear(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=auto_module.__name__)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}", "notify.mobile_app_gone"]}
    )
    phone = async_mock_service(hass, "notify", PHONE)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    messages = [record.getMessage() for record in caplog.records]
    warned = [message for message in messages if "acceptance push clear skipped" in message]
    assert len(warned) == 1
    assert "mobile_app_gone" in warned[0]
    assert PHONE not in warned[0]


async def test_a_failing_phone_does_not_keep_the_next_one_from_the_clear(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: ["notify.mobile_app_broken", f"notify.{PHONE}"]}
    )

    async def broken(_call: Any) -> None:
        raise HomeAssistantError("companion app down")

    hass.services.async_register("notify", "mobile_app_broken", broken)
    phone = async_mock_service(hass, "notify", PHONE)

    await hass.config_entries.async_remove(entry.entry_id)  # does not raise
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    assert "notify.mobile_app_broken failed" in caplog.text
    assert "acceptance push clear failed for ['notify.mobile_app_broken']" in caplog.text


async def test_the_removal_does_not_wait_for_a_slow_notify_service(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    release = asyncio.Event()
    arrived: list[Any] = []

    async def slow(call: Any) -> None:
        await release.wait()
        arrived.append(call)

    hass.services.async_register("notify", PHONE, slow)

    await asyncio.wait_for(hass.config_entries.async_remove(entry.entry_id), timeout=5)  # done before the push is
    assert not arrived

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(_clears_of_the_acceptance_push(arrived, entry)) == 1


async def test_a_phone_taken_from_the_notify_option_after_the_push_still_gets_the_clear(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}", "notify.mobile_app_second"]}
    )
    phone = async_mock_service(hass, "notify", PHONE)
    second = async_mock_service(hass, "notify", "mobile_app_second")
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    hass.config_entries.async_update_entry(entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}"]})
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    assert len(_clears_of_the_acceptance_push(second, entry)) == 1  # it showed the push, the option forgot it
    assert entry.entry_id not in hass.data[auto_module.CERTIFICATE_AUTO_ACCEPT_PUSHED]


async def test_a_pin_dropped_after_the_push_does_not_keep_the_clear_from_the_phone(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    phone = async_mock_service(hass, "notify", PHONE)
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    data = {key: value for key, value in entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT}
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1


async def test_a_pin_dropped_after_the_push_clears_only_the_phones_it_went_to(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Without a pin the notify option is no guess any more: only the recorded phones are cleared."""
    entry.add_to_hass(hass)
    phone = async_mock_service(hass, "notify", PHONE)
    later = async_mock_service(hass, "notify", "mobile_app_later")
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    data = {key: value for key, value in entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT}
    hass.config_entries.async_update_entry(
        entry, data=data, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: [f"notify.{PHONE}", "notify.mobile_app_later"]}
    )
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    assert later == []


async def test_the_clear_waits_for_the_push_being_sent_when_the_entry_goes(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    order: list[str] = []
    tag = _acceptance_tag(entry)

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            if call.data["data"]["tag"] == tag:  # not the clear of the repair's push, which has its own tag
                order.append("clear")
            return
        try:
            await asyncio.Event().wait()
        finally:
            for _ in range(50):  # the cancelled send takes many turns to end: a clear that did not wait is first
                await asyncio.sleep(0)
            order.append("send ended")

    hass.services.async_register("notify", PHONE, phone)
    sending = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    hass.data.setdefault(CERTIFICATE_AUTO_ACCEPTS, {})[entry.entry_id] = auto_module._AutoAccepts(task=sending)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sending.cancelled()
    assert order == ["send ended", "clear"]


async def test_a_push_that_never_ends_keeps_the_clear_waiting_only_so_long(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    entry.add_to_hass(hass)
    cleared: list[Any] = []
    got_the_clear = asyncio.Event()
    stuck = asyncio.Event()  # never set: the cancelled send does not end until the test lets it

    async def phone(call: Any) -> None:
        if call.data["message"] == "clear_notification":
            cleared.append(call)
            if call.data["data"]["tag"] == _acceptance_tag(entry):  # not the repair push's clear
                got_the_clear.set()
            return
        try:
            await asyncio.Event().wait()
        finally:
            await stuck.wait()  # swallows the cancellation's end: the send does not finish

    hass.services.async_register("notify", PHONE, phone)
    sending = hass.async_create_task(auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    hass.data.setdefault(CERTIFICATE_AUTO_ACCEPTS, {})[entry.entry_id] = auto_module._AutoAccepts(task=sending)

    with patch.object(auto_module, "CERTIFICATE_AUTO_ACCEPT_CLEAR_WAIT_SEC", 0.05):
        await hass.config_entries.async_remove(entry.entry_id)
        await asyncio.wait_for(got_the_clear.wait(), timeout=5)

    assert len(_clears_of_the_acceptance_push(cleared, entry)) == 1
    assert f"acceptance push still being sent 0.05 s after entry {entry.entry_id} was removed (1 waits)" in caplog.text
    assert not sending.done()  # the clear did not wait for it any longer
    stuck.set()
    await asyncio.gather(sending, return_exceptions=True)


async def test_a_notify_group_next_to_a_phone_gets_no_clear_when_the_pin_is_gone(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A group may hold apps that are no companion apps: they would show the text of the clear."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: ["notify.family_group", f"notify.{PHONE}"]}
    )
    group = async_mock_service(hass, "notify", "family_group")
    phone = async_mock_service(hass, "notify", PHONE)
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    data = {key: value for key, value in entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT}
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(phone, entry)) == 1
    assert all(call.data["message"] != "clear_notification" for call in group)


async def test_a_notify_group_alone_leaves_no_record_and_no_clear(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: ["notify.family_group"]})
    group = async_mock_service(hass, "notify", "family_group")
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    assert entry.entry_id not in hass.data.get(auto_module.CERTIFICATE_AUTO_ACCEPT_PUSHED, {})

    data = {key: value for key, value in entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT}
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert all(call.data["message"] != "clear_notification" for call in group)


async def test_the_phones_of_two_pushes_are_all_cleared(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    first = async_mock_service(hass, "notify", PHONE)
    second = async_mock_service(hass, "notify", "mobile_app_second")
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))
    hass.config_entries.async_update_entry(
        entry, options={CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY: ["notify.mobile_app_second"]}
    )
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))

    data = {key: value for key, value in entry.data.items() if key != CONF_CERTIFICATE_FINGERPRINT}
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(_clears_of_the_acceptance_push(first, entry)) == 1
    assert len(_clears_of_the_acceptance_push(second, entry)) == 1


async def test_a_phone_a_push_went_to_that_is_gone_at_the_removal_is_a_warning(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    entry.add_to_hass(hass)
    async_mock_service(hass, "notify", PHONE)
    await auto_module._async_tell_user(hass, entry, PINNED, _info(OTHER))
    hass.services.async_remove("notify", PHONE)  # the companion app went away after the push

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert f"acceptance push may stay on ['notify.{PHONE}']" in caplog.text


async def test_a_companion_app_not_registered_at_the_removal_is_named_in_the_log(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=auto_module.__name__)
    entry.add_to_hass(hass)  # no notify service registered: the companion app is gone or not up yet

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert f"acceptance push clear skipped for ['notify.{PHONE}']" in caplog.text


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


@pytest.mark.logged_exception  # the traceback of the unexpected error is the point: it is a bug to be seen
async def test_an_unexpected_read_error_leaves_the_issuer_unknown_and_is_logged(
    hass: HomeAssistant, entry: MockConfigEntry, authority_read: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CERTIFICATE_AUTHORITY: None})
    authority_read.side_effect = RuntimeError("bug in the read")

    await async_learn_pin_authority(hass, entry)  # a best-effort step: it must not fail the setup

    assert entry.data[CONF_CERTIFICATE_AUTHORITY] is None
    [record] = [r for r in caplog.records if "failed unexpectedly" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


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
