# Version: 0.10.0
"""marker_delete — deletes Markers directly from the Comexio server (not a function plan action).

Kept as its own module rather than folded into misc.py's grab-bag: unlike the other
handlers there, this one is destructive (permanently removes Markers from Comexio) and
needs its own id-list parsing + confirm-gate + per-id result bookkeeping.

Scope is deliberately narrow — see api.get_marker_delete_eligibility: a marker placed in any
function plan is never deleted, force or not. Of the unplaced ones, markers created via the
admin API or the plan editor (CategoryId==1) are deletable; factory markers and those from
Studio's marker list (CategoryId==0) are protected, 'force' only opens that for markers without
a title. Protected ids are skipped and reported with their reason, they no longer abort the
whole call.
"""

import logging
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall

from ..const import MARKER_DELETE_MAX_COUNT, parse_ignored_marker_tokens
from ._context import _async_get_service_context, _ensure_comexio_login, refuse_while_plans_busy

_LOGGER = logging.getLogger(__name__)

_TITLE_MARKER_DELETE_ERR = "Marker Delete — Error"
_PLANS_BUSY_RESULT = "A function plan restore, sync, cleanup or reload is running — nothing deleted."


def _parse_marker_id_list(raw_input: str) -> tuple[list[int], list[str]]:
    """Parse the marker_id field into a sorted, de-duplicated list of marker IDs.

    Reuses the shared ignored-ids token parser (parse_ignored_marker_tokens) for the same
    comma/semicolon/space/dot separators, optional 'M' prefix and inclusive 'start-end'
    ranges as the ignored_markers option and function_plan_connect's marker_id field
    (e.g. '355', '351,355', '306-355'). Returns (marker_ids, invalid_tokens).
    """
    marker_ids: set[int] = set()
    invalid_tokens: list[str] = []
    over_limit = False
    for token, parsed in parse_ignored_marker_tokens(raw_input):
        if parsed is None:
            # Always collect invalid tokens, even after the safety limit is hit below —
            # otherwise a token after an oversized range (e.g. "1-1000,abc") would never be
            # looked at, and the caller would report only the "too many markers" error
            # while silently dropping the fact that the input also had a bad token.
            invalid_tokens.append(token)
            continue
        if over_limit:
            # Already over the safety limit — keep scanning for invalid tokens (above) but
            # stop growing marker_ids further, so a huge range/token list can't inflate the
            # set beyond what the caller's over-the-limit check already needs to trigger.
            continue
        if isinstance(parsed, tuple):
            start, end = parsed
            # Cap expansion at MARKER_DELETE_MAX_COUNT + 1 per range — enough for the
            # caller's over-the-limit check further down to still fire, without
            # materializing a multi-million-entry set for a typo'd range boundary
            # (e.g. "306-3555000") before that check even runs.
            end = min(end, start + MARKER_DELETE_MAX_COUNT)
            marker_ids.update(range(start, end + 1))
        else:
            marker_ids.add(parsed)
        if len(marker_ids) > MARKER_DELETE_MAX_COUNT:
            over_limit = True
    return sorted(marker_ids), invalid_tokens


def _error_result(error: str, requested: list[int] | None = None) -> dict:
    """Build the response for a call that deleted nothing.

    The service is registered with SupportsResponse.OPTIONAL and Developer Tools (and MCP
    callers) always request a response — returning None there makes HA reject the call with
    "expected a dictionary, but got NoneType", masking the actual reason. So every exit path
    returns a dict. "error" is reserved for failures of the call itself or of the gate (it is
    None when the gate ran as requested); per-id outcomes are always in
    deleted/skipped/protected/failed, and why ids were protected is in "protected_reasons"
    (per id) and "protected_reason" (one line for all).
    """
    return {
        "requested": requested or [],
        "deleted": [],
        "skipped": [],
        "protected": [],
        "protected_reason": None,
        "protected_reasons": {},
        "failed": [],
        "duration": 0.0,
        "error": error,
    }


def _validate_marker_delete_request(call: ServiceCall, raw_input: str) -> tuple[list[int], str | None]:
    """Parse+validate marker_id/confirm.

    Split out of handle_marker_delete to keep that function's cognitive complexity in check —
    these are independent, sequential guard checks (bad tokens / empty / too many / not
    confirmed) with no shared state, a natural helper boundary. Returns (marker_ids, error);
    error is None when the call may proceed.
    """
    marker_ids, invalid_tokens = _parse_marker_id_list(raw_input)
    if invalid_tokens:
        return marker_ids, f"Invalid marker_id token(s): {', '.join(invalid_tokens)}. Nothing deleted."
    if not marker_ids:
        return marker_ids, "No marker_id given — nothing to delete."
    if len(marker_ids) > MARKER_DELETE_MAX_COUNT:
        return marker_ids, (
            f"marker_id resolves to {len(marker_ids)} markers, more than the safety limit of "
            f"{MARKER_DELETE_MAX_COUNT}. Nothing deleted — narrow the list/range and try again."
        )
    if call.data.get("confirm") is not True:
        return marker_ids, "Nothing deleted — 'confirm' must be enabled."
    return marker_ids, None


def _fail(hass: HomeAssistant, error: str, requested: list[int] | None = None) -> dict:
    """Post the error notification and return the matching error response."""
    persistent_notification.async_create(hass, error, title=_TITLE_MARKER_DELETE_ERR)
    return _error_result(error, requested)


def _marker_ids(marker_ids: list[int]) -> str:
    """'M3, M5' — marker ids as the notification lists them."""
    return ", ".join(f"M{mid}" for mid in marker_ids)


def _protected_reason(protected: dict[int, str], gate_error: str | None) -> str | None:
    """One line for the response's "protected_reason": the gate error, or each id with its reason."""
    if not protected:
        return None
    if gate_error:
        return gate_error
    return "; ".join(f"M{mid}: {reason}" for mid, reason in protected.items())


def _protected_message(protected: dict[int, str], gate_error: str | None) -> str | None:
    """Notification lines listing the protected ids and why each was not deleted."""
    if not protected:
        return None
    if gate_error:
        return f"Protected, not deleted: {_marker_ids(list(protected))} — {gate_error}"
    lines = "\n".join(f"- M{mid}: {reason}" for mid, reason in protected.items())
    return f"Protected, not deleted:\n{lines}"


def _result_message(
    requested: list[int], deleted: list[int], skipped: list[int], failed: list[int], protected_msg: str | None
) -> str:
    """Notification body summarizing one marker_delete run."""
    msg = f"Deleted {len(deleted)} of {len(requested)} requested marker(s)."
    if deleted:
        msg += f"\nDeleted: {_marker_ids(deleted)}"
    if skipped:
        msg += f"\nNot deleted (already absent): {_marker_ids(skipped)}"
    if protected_msg:
        msg += f"\n{protected_msg}"
    if failed:
        msg += (
            "\nFAILED (request error or server rejected the delete despite accepting the marker as "
            f"eligible — marker may still exist, see log): {_marker_ids(failed)}"
        )
    return msg


def _result_title(
    deleted: list[int], requested: list[int], protected: dict[int, str], failed: list[int], gate_error: str | None
) -> str:
    """Notification title — flags protected and failed ids and a failed check so a partial run is visible."""
    title = f"Marker Delete — {len(deleted)}/{len(requested)} OK"
    if protected:
        title += f", {len(protected)} protected"
    if failed or gate_error:
        title += " (errors)"
    return title


def _with_categories(marker_ids: list[int], categories: dict[int, Any]) -> str:
    """'M359 (CategoryId 1), M7 (absent)' — for the log line, so a marker's origin is visible there.

    categories is only empty when the marker config could not be read (a parsed one never is):
    the ids were not checked then, not found absent.
    """
    missing = "absent" if categories else "unchecked"
    return (
        ", ".join(
            f"M{mid} (CategoryId {categories[mid]!r})" if mid in categories else f"M{mid} ({missing})"
            for mid in marker_ids
        )
        or "none"
    )


async def handle_marker_delete(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Permanently delete one or more Markers from the Comexio server.

    'confirm' must be enabled — checked (via _validate_marker_delete_request) before login is
    attempted, so an unconfirmed call never opens a Comexio session. Requires literal `True`
    (not just any truthy value) since call.data bypasses services.yaml's declared
    selector/required when invoked outside the UI — an irreversible action shouldn't accept a
    stray non-empty string as "confirmed". 'force' is checked the same strict way.

    After login, api.get_marker_delete_eligibility classifies every id against a fresh config
    lookup: protected ids are skipped and reported with their reason (response keys "protected",
    "protected_reasons"), the rest is deleted. Its third return value, categories, maps the ids
    that lookup actually found on the server to their CategoryId — logged, and used below to
    sanity-check delete_marker's False results.

    api.delete_marker returns a tri-state per id: True (deleted), False (server reported
    nothing changed), or None (the request itself failed — always a real failure). A False is
    only reported as the harmless "already absent" case when the id wasn't in categories either;
    a False for an id that WAS present is treated as a failure.

    Always returns a dict (never None): the service supports responses and Developer Tools /
    MCP callers always request one — see _error_result. "error" carries gate_error when the
    config or a function plan couldn't be read (every id refused), so a caller can tell that from
    real protection; "protected_reason(s)" carry the reason for the ids in "protected" in both cases.
    """
    raw_input = str(call.data.get("marker_id", "")).strip()
    marker_ids, error = _validate_marker_delete_request(call, raw_input)
    if error is not None:
        return _fail(hass, error, requested=marker_ids)

    ctx = await _async_get_service_context(hass, call, _TITLE_MARKER_DELETE_ERR, resolve_plan=False, do_login=False)
    # Checked before the login (no needless round-trip) and again after it (a restore may start meanwhile).
    if ctx is not None and refuse_while_plans_busy(hass, ctx[0], _TITLE_MARKER_DELETE_ERR):
        return _error_result(_PLANS_BUSY_RESULT, requested=marker_ids)
    if ctx is None or not await _ensure_comexio_login(hass, ctx[1], _TITLE_MARKER_DELETE_ERR):
        # Both post their own notification with the specific reason.
        return _error_result(
            "Comexio instance not found or admin login failed — see notification. Nothing deleted.",
            requested=marker_ids,
        )
    coordinator, api, _fub_id = ctx
    if refuse_while_plans_busy(hass, coordinator, _TITLE_MARKER_DELETE_ERR):
        return _error_result(_PLANS_BUSY_RESULT, requested=marker_ids)
    # No await since the check: a restore starting from here on refuses until the deletion ends.
    with coordinator.plan_edit():
        return await _async_delete_eligible_markers(hass, call, coordinator, api, raw_input, marker_ids)


async def _async_delete_eligible_markers(
    hass: HomeAssistant, call: ServiceCall, coordinator, api, raw_input: str, marker_ids: list[int]
) -> dict:
    """Delete the requested markers the eligibility check allows; the service response (see handle_marker_delete)."""
    force = call.data.get("force") is True
    requested = marker_ids
    marker_ids, protected, categories, gate_error = await api.get_marker_delete_eligibility(marker_ids, force=force)
    _LOGGER.info(
        "Marker Delete: marker_id='%s' force=%s -> deleting %d marker(s): %s; protected: %s",
        raw_input,
        force,
        len(marker_ids),
        _with_categories(marker_ids, categories),
        "; ".join(f"{_with_categories([mid], categories)}: {reason}" for mid, reason in protected.items()) or "none",
    )
    t_start = time.monotonic()
    deleted: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    for marker_id in marker_ids:
        result = await api.delete_marker(marker_id)
        if result is True:
            deleted.append(marker_id)
        elif result is False and marker_id not in categories:
            skipped.append(marker_id)
        else:
            failed.append(marker_id)
    duration = time.monotonic() - t_start

    if deleted:
        await coordinator.async_request_refresh()

    msg = _result_message(requested, deleted, skipped, failed, _protected_message(protected, gate_error))
    msg += f"\nDuration: {duration:.1f}s"
    title = _result_title(deleted, requested, protected, failed, gate_error)
    persistent_notification.async_create(hass, msg, title=title)
    return {
        "requested": requested,
        "deleted": deleted,
        "skipped": skipped,
        "protected": list(protected),
        "protected_reason": _protected_reason(protected, gate_error),
        "protected_reasons": protected,
        "failed": failed,
        "duration": round(duration, 1),
        "error": gate_error,
    }
