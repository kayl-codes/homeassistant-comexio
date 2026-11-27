"""A certificate Comexio replaced on its own, taken over without asking where a rule allows it (#294).

Comexio firmware up to CERTIFICATE_AUTO_ACCEPT_MAX_FIRMWARE replaces its TLS certificate on its own. Each time
the pin stops matching and the integration would wait for the user. The rule takes a new certificate over when:

- the pin's issuer and authority key identifier are known (certificate.pinned_authority) and the new certificate
  has the same pair (aiocomexio.same_authority),
- the firmware Comexio last reported is at most CERTIFICATE_AUTO_ACCEPT_MAX_FIRMWARE (unknown: no),
- no certificate_changed repair is open for the entry, and the rule has not turned this certificate down before,
- the new certificate stays the same for CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC (read again then).

It is then pinned as the repair would (certificate.async_accept_certificate), and the user learns it from a
persistent notification and a push to the notify targets, without a button. Anything else goes to the repair as
before. Issuer and key identifier are copied from the certificate itself, so the rule tells Comexio's own
certificates apart from a stranger's only as far as nobody copies them: it narrows the trust on first use the pin
already rests on, it does not replace it.
"""

import asyncio
from dataclasses import dataclass, field
import logging
import re
from typing import Any

from aiocomexio import CertificateInfo, ComexioError, fetch_certificate, format_fingerprint, same_authority
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .certificate import (
    ACCEPT_PINNED,
    ACCEPT_REPAIR_OPEN,
    ACCEPT_UNREACHABLE,
    _async_notify,
    _is_mobile_app,
    _notify_targets,
    _phones,
    async_accept_certificate,
    async_clear_certificate_changed,
    async_report_certificate_changed,
    pinned_authority,
    presented_fingerprint,
)
from .const import (
    CERTIFICATE_AUTO_ACCEPT_CLEAR_WAIT_SEC,
    CERTIFICATE_AUTO_ACCEPT_MAX_FIRMWARE,
    CERTIFICATE_AUTO_ACCEPT_MAX_READS,
    CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_HOST,
    CONF_SERVER_ID,
    DOMAIN,
    MOBILE_APP_CLEAR_NOTIFICATION,
    certificate_check_signal,
)
from .plan_watchdog import NOTIFY_DOMAIN

_LOGGER = logging.getLogger(__name__)

# hass.data: entry_id -> _AutoAccepts. Not bound to the entry: a setup retry or a reload must not end the wait.
CERTIFICATE_AUTO_ACCEPTS = f"{DOMAIN}_certificate_auto_accepts"
# hass.data: entry_id -> the companion apps an acceptance push was sent to this run (set before the send, so a send
# cancelled in flight is in it too). The removal of the entry clears these, whatever the notify option or the pin
# is by then.
CERTIFICATE_AUTO_ACCEPT_PUSHED = f"{DOMAIN}_certificate_auto_accept_pushed"
# hass.data: entry_id -> asyncio.Lock. The acceptance pushes of one entry are sent one after the other: a newer
# push must not reach a phone before an older one still in its notify call, or the older text would replace the
# newer one (same tag).
CERTIFICATE_AUTO_ACCEPT_SENDS = f"{DOMAIN}_certificate_auto_accept_sends"
# ASCII: \d alone also matches other scripts' digits. Bounded: int() of a digit string beyond Python's conversion limit
# (4300) raises, and no real firmware component has more than a few digits, nor a version more than a few parts.
_FIRMWARE_RE = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,7}", re.ASCII)

AUTO_ACCEPT_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Comexio: new certificate accepted",
        "message": (
            "Comexio {server_id} ({host}) presented a new TLS certificate of the same issuer as the pinned one. "
            "Its firmware replaces the certificate on its own, so Home Assistant accepted it automatically. "
            "Compare it with the SHA-256 value on Comexio's admin page; if it differs, reconfigure the entry.\n"
            "Previous (SHA-256): {pinned}\n"
            "New (SHA-256): {actual}\n"
            "Issuer: {issuer}"
        ),
    },
    "de": {
        "title": "Comexio: neues Zertifikat übernommen",
        "message": (
            "Comexio {server_id} ({host}) zeigte ein neues TLS-Zertifikat desselben Ausstellers wie das gepinnte. "
            "Ihre Firmware tauscht das Zertifikat selbst aus, daher hat Home Assistant es automatisch übernommen. "
            "Vergleiche es mit dem SHA-256-Wert auf der Admin-Seite der Comexio; weicht er ab, konfiguriere den "
            "Eintrag neu.\n"
            "Bisher (SHA-256): {pinned}\n"
            "Neu (SHA-256): {actual}\n"
            "Aussteller: {issuer}"
        ),
    },
}


@dataclass
class _AutoAccepts:
    """One entry's rule state: the wait running (and the host and pin it started from), and the (pin,
    certificate) pairs the rule turned down."""

    task: asyncio.Task[None] | None = None
    # Waits replaced by a newer one that have not ended yet: one may still be in its notify call.
    superseded: set[asyncio.Task[None]] = field(default_factory=set)
    # Waits that pinned and are sending the acceptance push: a newer report does not cancel them (it cuts the push).
    telling: set[asyncio.Task[None]] = field(default_factory=set)
    connection: tuple[str, str] | None = None
    seen: str | None = None  # the certificate last seen (a report or a read of the wait): what a failure reports
    refused: set[tuple[str, str]] = field(default_factory=set)
    noted: set[tuple[str, str]] = field(default_factory=set)  # pairs whose "left to the user" reason was logged


def parse_firmware(version: str | None) -> tuple[int, ...] | None:
    """Comexio's firmware version ("11.1.5") as numbers, None when unknown or not in that form."""
    if version is None or _FIRMWARE_RE.fullmatch(version.strip()) is None:
        return None
    return tuple(int(part) for part in version.strip().split("."))


def firmware_replaces_certificate(version: str | None) -> bool:
    """Whether the firmware is one that replaces its certificate on its own (at most the known last one)."""
    parsed = parse_firmware(version)
    return parsed is not None and parsed <= CERTIFICATE_AUTO_ACCEPT_MAX_FIRMWARE


def async_certificate_changed(hass: HomeAssistant, entry: ConfigEntry, actual: str, firmware: str | None) -> None:
    """Comexio presents `actual` instead of the pin: the rule takes it over where it applies, else the repair asks.

    firmware: the version Comexio last reported (live or from the saved config), None when unknown.
    """
    if not _async_start(hass, entry, actual, firmware):
        async_report_certificate_changed(hass, entry, actual)


def _async_start(hass: HomeAssistant, entry: ConfigEntry, actual: str, firmware: str | None) -> bool:
    """Start (or keep) the rule's wait for `actual`; False when the rule does not apply and the user decides."""
    host, pinned = entry.data[CONF_HOST], entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
    if pinned is None or actual == pinned:
        return False
    state: _AutoAccepts = hass.data.setdefault(CERTIFICATE_AUTO_ACCEPTS, {}).setdefault(entry.entry_id, _AutoAccepts())
    if (why_not := _why_not(entry, firmware)) is not None:
        if (pinned, actual) not in state.noted:  # every poll reports it again: once is enough
            state.noted.add((pinned, actual))
            _LOGGER.info(
                "Comexio at %s: certificate %s is left to the user (#294): %s",
                host,
                format_fingerprint(actual),
                why_not,
            )
        return False
    if presented_fingerprint(hass, entry.entry_id) is not None:
        return False  # the repair is up already: the user decides
    if (pinned, actual) in state.refused:
        return False
    # A wait started for the connection before a reconfigure ends on its own (_still_current); this one waits anew.
    if state.task is not None and not state.task.done() and state.connection == (host, pinned):
        if state.seen != actual:
            state.seen = actual  # the wait reads the certificate again anyway; a failure of it reports this one
            async_dispatcher_send(hass, certificate_check_signal(entry.entry_id))
        return True
    _LOGGER.warning(
        "Comexio at %s presents certificate %s instead of the pinned %s; checking it against the pinned issuer in %d s",
        host,
        format_fingerprint(actual),
        format_fingerprint(pinned),
        CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC,
    )
    if (replaced := state.task) is not None and not replaced.done():
        if replaced not in state.telling:  # a pin made already keeps its push: it is about the certificate accepted
            replaced.cancel()  # a wait for the connection before a reconfigure ends here
        state.superseded.add(replaced)  # a removal waits for it too, should it be sending its push
        replaced.add_done_callback(state.superseded.discard)
    state.connection = (host, pinned)
    state.seen = actual
    state.task = hass.async_create_background_task(
        _async_run(hass, entry, state, host, pinned, actual), f"{DOMAIN} certificate auto accept {entry.entry_id}"
    )
    async_dispatcher_send(hass, certificate_check_signal(entry.entry_id))  # the connection sensor shows the check
    return True


def certificate_in_check(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The certificate the rule's wait checks for the entry (the one seen last), None when no wait runs.

    Home Assistant sends nothing to Comexio during the wait either, so the connection sensor shows it like the repair.
    A wait for the connection before a reconfigure or a new pin is no check of this one: it ends on its own.
    """
    state: _AutoAccepts | None = hass.data.get(CERTIFICATE_AUTO_ACCEPTS, {}).get(entry.entry_id)
    if state is None or state.task is None or state.task.done():
        return None
    if state.connection != (entry.data[CONF_HOST], entry.data.get(CONF_CERTIFICATE_FINGERPRINT)):
        return None
    return state.seen


def _why_not(entry: ConfigEntry, firmware: str | None) -> str | None:
    """Why the rule does not apply to the entry, None when it does."""
    if pinned_authority(entry.data) is None:
        return "the pinned certificate's issuer is not known"
    if not firmware_replaces_certificate(firmware):
        return f"firmware {firmware or 'unknown'} is not one known to replace its certificate"
    return None


def async_remove_auto_accept(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A removed entry's rule state goes with it; a wait still running is cancelled, its notice dismissed.

    The push of an acceptance is taken off the companion apps as well: the ones it was sent to this run (whatever the
    notify option says by now), and, while the entry is pinned, every companion app of the notify option too, as the
    certificate push's first clear does (a push may predate the restart; a clear for a tag the app does not show does
    nothing). A push still being sent when the entry goes is waited for (bounded) before the clear. Limits: a notify
    group is not cleared (its members need not be companion apps, and any other would show the text of the clear), a
    failed clear is not retried, and the platform may still deliver a push after the clear (one that outlives the
    wait, or is already with the push service, may still show).
    """
    persistent_notification.async_dismiss(hass, _notification_id(entry.entry_id))
    hass.data.get(CERTIFICATE_AUTO_ACCEPT_SENDS, {}).pop(entry.entry_id, None)
    state: _AutoAccepts | None = hass.data.get(CERTIFICATE_AUTO_ACCEPTS, {}).pop(entry.entry_id, None)
    ending: set[asyncio.Task[None]] = set()
    if state is not None:
        ending = {task for task in (state.task, *state.superseded) if task is not None and not task.done()}
    for task in ending:
        task.cancel()
    pushed: set[str] = hass.data.get(CERTIFICATE_AUTO_ACCEPT_PUSHED, {}).pop(entry.entry_id, set())
    # No push sent and no pin, no rule, no push. A background task: the removal does not wait for a slow notify
    # service, and nothing in it can raise into the removal's cleanup behind it. A task cancelled with Home Assistant
    # leaves the push.
    if pushed or CONF_CERTIFICATE_FINGERPRINT in entry.data:
        hass.async_create_background_task(
            _async_clear_push(hass, entry, pushed, ending), f"{DOMAIN} acceptance push clear"
        )


async def _async_clear_push(
    hass: HomeAssistant, entry: ConfigEntry, pushed: set[str], ending: set[asyncio.Task[None]]
) -> None:
    """Take the push of an acceptance off the companion apps it may be on; every failure is logged.

    ending: the waits of the rule still running (the current one and any it replaced; a cancelled one or one that
    already pinned and is sending its push), which may still be in their notify
    call: the clear waits for them to end.
    """
    try:
        await _async_wait_for_send(entry, ending)
        reached = set(pushed)
        if CONF_CERTIFICATE_FINGERPRINT in entry.data:
            reached |= _phones(entry)
        phones = sorted(reached)
        prefix = f"{NOTIFY_DOMAIN}."
        registered = [phone for phone in phones if hass.services.has_service(NOTIFY_DOMAIN, phone.removeprefix(prefix))]
        _log_unreachable(pushed, set(phones).difference(registered))
        if not registered:
            return
        # A companion-app command: any other notify service would deliver it as text.
        service_data = {"message": MOBILE_APP_CLEAR_NOTIFICATION, "data": {"tag": _notification_id(entry.entry_id)}}
        _, failed = await _async_notify(hass, service_data, registered, set())
        if failed:
            _LOGGER.warning("Comexio acceptance push clear failed for %s: a push already sent may stay", sorted(failed))
    except Exception:
        _LOGGER.exception("Comexio acceptance push clear for entry %s failed", entry.entry_id)


async def _async_wait_for_send(entry: ConfigEntry, ending: set[asyncio.Task[None]]) -> None:
    """Wait (bounded) for the cancelled waits to leave their notify call; a send that outlasts the bound is logged."""
    if not ending:
        return
    _, pending = await asyncio.wait(ending, timeout=CERTIFICATE_AUTO_ACCEPT_CLEAR_WAIT_SEC)
    if pending:
        _LOGGER.warning(
            "Comexio acceptance push still being sent %s s after entry %s was removed (%d waits): it may stay on the "
            "phone",
            CERTIFICATE_AUTO_ACCEPT_CLEAR_WAIT_SEC,
            entry.entry_id,
            len(pending),
        )


def _log_unreachable(pushed: set[str], unreachable: set[str]) -> None:
    """A phone a push went to that cannot be cleared is a warning; one only taken from the notify option is debug."""
    if lost := sorted(pushed & unreachable):
        _LOGGER.warning("Comexio acceptance push may stay on %s: not registered when the entry was removed", lost)
    if rest := sorted(unreachable - pushed):
        # Debug: nothing says a push ever went there, and the repair push's removal warns already.
        _LOGGER.debug("Comexio acceptance push clear skipped for %s: not registered", rest)


async def _async_run(
    hass: HomeAssistant, entry: ConfigEntry, state: _AutoAccepts, host: str, pinned: str, seen: str
) -> None:
    """The wait and the decision; whatever goes wrong ends at the repair, never in silence."""
    try:
        await _async_decide(hass, entry, state, host, pinned, seen)
    except Exception:
        _LOGGER.exception("Comexio at %s: the check of the new certificate failed; the repair asks instead", host)
        _report_if_current(hass, entry, host, pinned, state.seen or seen)
    finally:
        # Also when cancelled. Only this run's own task: a wait started anew meanwhile is still checking.
        if state.task is asyncio.current_task():
            state.task = None
            async_dispatcher_send(hass, certificate_check_signal(entry.entry_id))


async def _async_decide(
    hass: HomeAssistant, entry: ConfigEntry, state: _AutoAccepts, host: str, pinned: str, seen: str
) -> None:
    try:
        info, last = await _async_settled_certificate(hass, entry, host, pinned, seen, state)
    except (ComexioError, ValueError) as err:
        _LOGGER.warning("Comexio at %s: the new certificate could not be read again (%s); the repair asks", host, err)
        _report_if_current(hass, entry, host, pinned, state.seen or seen)
        return
    if not _still_current(hass, entry, host, pinned):
        # Removed, disabled or reconfigured meanwhile: the new connection decides on its own.
        _LOGGER.info("Comexio at %s: the entry changed during the wait; nothing pinned, no repair raised", host)
        return
    if info is None:
        _refuse(hass, entry, state, pinned, last, "it kept changing")  # the newest: the one the repair compares
        return
    if info.fingerprint == pinned:
        _LOGGER.info("Comexio at %s presents the pinned certificate again; nothing to accept", host)
        # A repair raised during the wait (a reconfigure's test) asks about a certificate Comexio no longer presents,
        # as the acceptance does when the pin is back.
        async_clear_certificate_changed(hass, entry.entry_id)
        if entry.state is not ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    authority = pinned_authority(entry.data)
    if authority is None or not same_authority(authority, info):
        _refuse(hass, entry, state, pinned, info.fingerprint, f"its issuer differs ({info.issuer})")
        return
    if _left_to_open_repair(hass, entry, host, info.fingerprint):
        return
    outcome, current = await async_accept_certificate(hass, entry, info.fingerprint, by_rule=True)
    await _async_after_acceptance(hass, entry, state, host, pinned, info, outcome, current)


async def _async_after_acceptance(
    hass: HomeAssistant,
    entry: ConfigEntry,
    state: _AutoAccepts,
    host: str,
    pinned: str,
    info: CertificateInfo,
    outcome: str,
    current: str | None,
) -> None:
    """What follows the acceptance by the rule: tell the user, or leave the case to the repair.

    current: the certificate the acceptance read itself, None where it read none."""
    if outcome == ACCEPT_PINNED:
        # Marked before the first await (nothing in between can cancel it): see _async_start.
        sending = asyncio.current_task()
        if sending is not None:
            state.telling.add(sending)
        try:
            await _async_tell_user(hass, entry, pinned, info)
        except Exception:
            # Pinned already: the log line is all that is left to say it was the rule.
            _LOGGER.exception("Comexio at %s: certificate pinned by the rule, but the push to the user failed", host)
        finally:
            if sending is not None:
                state.telling.discard(sending)
    elif outcome == ACCEPT_UNREACHABLE:
        # Not read again: the repair asks, for the newest certificate seen (a report during the read counts).
        _report_if_current(hass, entry, host, pinned, state.seen or info.fingerprint)
    elif outcome == ACCEPT_REPAIR_OPEN:
        # A repair came up while the acceptance queued or read the certificate: the user decides, on the newest read
        # (the acceptance's own if it got that far, else the wait's).
        _left_to_open_repair(hass, entry, host, current or info.fingerprint)
    else:
        # Another certificate again raised the repair itself; the rest ended the case without one.
        _LOGGER.debug(
            "Comexio at %s: acceptance of %s ended with %s", host, format_fingerprint(info.fingerprint), outcome
        )


def _left_to_open_repair(hass: HomeAssistant, entry: ConfigEntry, host: str, actual: str) -> bool:
    """Whether a repair came up during the wait (a reconfigure's test, a tap on an older push): the user decides.

    The repair is brought up to the certificate read last, so the user compares the one Comexio presents now."""
    shown = presented_fingerprint(hass, entry.entry_id)
    if shown is None:
        return False
    _LOGGER.info("Comexio at %s: a certificate repair is open, the new certificate is left to it", host)
    if shown != actual:
        async_report_certificate_changed(hass, entry, actual)
    return True


async def _async_settled_certificate(
    hass: HomeAssistant, entry: ConfigEntry, host: str, pinned: str, seen: str, state: _AutoAccepts
) -> tuple[CertificateInfo | None, str]:
    """The certificate once it stayed the same for the settle time (None when it kept changing), and the
    fingerprint read last.

    Ends without a read once the entry no longer has this host and pin (reconfigured, disabled, removed): the old
    endpoint gets no further handshake, and the caller finds the entry changed."""
    for _ in range(CERTIFICATE_AUTO_ACCEPT_MAX_READS):
        await asyncio.sleep(CERTIFICATE_AUTO_ACCEPT_SETTLE_SEC)
        if not _still_current(hass, entry, host, pinned):
            return None, seen
        info = await fetch_certificate(host)
        # The read must match the last read and the newest seen (a report of the wait included): another certificate
        # reported meanwhile restarts the interval, whichever one the read then shows.
        unchanged = info.fingerprint == seen == state.seen
        changed = info.fingerprint != state.seen
        seen = state.seen = info.fingerprint  # a later failure reports the newest one, not the first
        if changed:
            async_dispatcher_send(hass, certificate_check_signal(entry.entry_id))  # the connection sensor shows it
        if unchanged:
            return info, seen
    return None, seen


def _still_current(hass: HomeAssistant, entry: ConfigEntry, host: str, pinned: str) -> bool:
    """Whether the entry is still there, enabled, with the host and the pin the rule started from.

    A reload or setup retry leaves it current (the wait is not bound to the entry); disabling it does not: the user
    switched the integration off, so nothing is pinned and no repair is raised for it."""
    return (
        hass.config_entries.async_get_entry(entry.entry_id) is entry
        and entry.disabled_by is None
        and entry.data[CONF_HOST] == host
        and entry.data.get(CONF_CERTIFICATE_FINGERPRINT) == pinned
    )


def _report_if_current(hass: HomeAssistant, entry: ConfigEntry, host: str, pinned: str, actual: str) -> None:
    if _still_current(hass, entry, host, pinned):
        async_report_certificate_changed(hass, entry, actual)


def _refuse(
    hass: HomeAssistant, entry: ConfigEntry, state: _AutoAccepts, pinned: str, actual: str, reason: str
) -> None:
    """Leave the certificate to the user: the repair asks, and the rule does not take it up again in this run.

    The pairs are kept in memory: after a restart the rule checks a certificate afresh, and a stable one of the
    pinned issuer is taken over then, as it would have been at the first report."""
    state.refused.add((pinned, actual))
    _LOGGER.warning(
        "Comexio at %s: certificate %s not accepted automatically, %s; see Repairs",
        entry.data[CONF_HOST],
        format_fingerprint(actual),
        reason,
    )
    async_report_certificate_changed(hass, entry, actual)


def _notification_id(entry_id: str) -> str:
    """The id of the persistent notification and the tag of the push: one notice per entry."""
    return f"{DOMAIN}_certificate_auto_{entry_id}"


def _send_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """The lock that orders the acceptance pushes of one entry (created on first use, dropped with the entry)."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(CERTIFICATE_AUTO_ACCEPT_SENDS, {})
    return locks.setdefault(entry_id, asyncio.Lock())


async def _async_tell_user(hass: HomeAssistant, entry: ConfigEntry, pinned: str, info: CertificateInfo) -> None:
    """A persistent notification and a push without a button: the certificate is pinned already.

    The notification is English (it has no user, and hass.config.language is the system's language, not the
    viewer's); the push is in the system language where it is English or German (like the certificate push of
    certificate.py), English for any other.
    """
    values = {
        "server_id": entry.data[CONF_SERVER_ID],
        "host": entry.data[CONF_HOST],
        "pinned": format_fingerprint(pinned),
        "actual": format_fingerprint(info.fingerprint),
        "issuer": info.issuer,
    }
    english = AUTO_ACCEPT_TEXTS["en"]
    tag = _notification_id(entry.entry_id)
    notice = english["message"].format(**values)  # a template bug is the outer handler's, once
    try:
        persistent_notification.async_create(hass, notice, title=english["title"], notification_id=tag)
    except Exception:
        # Pinned already: the push below must still go out.
        _LOGGER.exception("Comexio at %s: the notice of the accepted certificate could not be created", values["host"])
    if not (targets := _notify_targets(entry)):
        return
    # Before the send: one cancelled while in flight may have reached a phone already.
    if phones := {target for target in targets if _is_mobile_app(target)}:
        hass.data.setdefault(CERTIFICATE_AUTO_ACCEPT_PUSHED, {}).setdefault(entry.entry_id, set()).update(phones)
    texts = AUTO_ACCEPT_TEXTS.get(hass.config.language, english)
    message = texts["message"].format(**values)
    # As urgent as the push asking for the decision: a certificate the user did not see is trusted now. Its own
    # tag, so the clear of that push does not take this one off the phones.
    service_data: dict[str, Any] = {
        "title": texts["title"],
        "message": message,
        "data": {
            "tag": tag,
            "group": f"{DOMAIN}_{entry.data[CONF_SERVER_ID]}",
            "ttl": 0,
            "priority": "high",
            "channel": "alarm_stream",
            "push": {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 1.0}},
        },
    }
    # Behind an older push still being sent (a wait's push is not cancelled by a newer change, see _async_start).
    async with _send_lock(hass, entry.entry_id):
        delivered, failed = await _async_notify(hass, service_data, targets, set())
    if failed:
        # The certificate stays pinned either way; the persistent notification still tells the user about it.
        _LOGGER.warning(
            "Comexio acceptance push of entry %s confirmed by %d of %d notify targets; no confirmation from: %s",
            entry.entry_id,
            len(delivered),
            len(set(targets)),
            sorted(failed),
        )
