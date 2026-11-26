"""The certificate rule of #294: a certificate Comexio replaced on its own, taken over where the rule allows it."""

from collections.abc import Generator
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import CertificateInfo, ComexioCertificateMismatchError, ComexioConnectionError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.comexio import certificate_auto_accept as auto_module
from custom_components.comexio.certificate import (
    ACCEPT_ENTRY_CHANGED,
    ACCEPT_PINNED,
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
    assert "BB:BB" in notification.call_args.args[1]
    assert ISSUER in notification.call_args.args[1]
    told = [push for push in pushes if push.data["message"] != "clear_notification"]
    assert len(told) == 1
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

    assert notification.call_args.kwargs["title"] == auto_module.AUTO_ACCEPT_TEXTS["en"]["title"]
    told = [push for push in pushes if push.data["message"] != "clear_notification"]
    assert told[0].data["title"] == auto_module.AUTO_ACCEPT_TEXTS["de"]["title"]


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
    assert "certificate pinned by the rule, but telling the user failed" in caplog.text


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
