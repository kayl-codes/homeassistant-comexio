# Version: 0.10.0
"""marker_delete — deletes Markers directly from the Comexio server (not a function plan action).

Kept as its own module rather than folded into misc.py's grab-bag: unlike the other
handlers there, this one is destructive (permanently removes Markers from Comexio) and
needs its own id-list parsing + confirm-gate + per-id result bookkeeping.

Scope is deliberately narrow: by default only markers this integration itself created via
the API (CategoryId==1) are deletable — see api.get_marker_delete_eligibility. Factory and
Studio-created markers (CategoryId==0) are protected; 'force' only opens that for markers
without a title that are not placed in any function plan. Protected ids are skipped and
reported, they no longer abort the whole call.
"""

import logging
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall

from ..const import MARKER_DELETE_MAX_COUNT, parse_ignored_marker_tokens
from ._context import _async_get_service_context

_LOGGER = logging.getLogger(__name__)

_TITLE_MARKER_DELETE_ERR = "Marker Delete — Error"


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
    deleted/skipped/protected/failed, and why ids were protected is in "protected_reason".
    """
    return {
        "requested": requested or [],
        "deleted": [],
        "skipped": [],
        "protected": [],
        "protected_reason": None,
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


def _protected_reason(force: bool, gate_error: str | None) -> str:
    """Why ids were protected (CategoryId gate, force's title/placement check, or a load failure)."""
    if gate_error:
        return gate_error
    if force:
        return (
            "not created by this integration and either titled or placed in a function plan "
            "(force only deletes untitled, unplaced markers)"
        )
    return "not created by this integration (Studio-created/factory markers; enable 'force' for untitled ones)"


def _protected_message(protected: list[int], reason: str) -> str:
    """Notification line listing the protected ids and why they were not deleted."""
    ids = ", ".join(f"M{mid}" for mid in protected)
    return f"Protected, not deleted: {ids} — {reason}"


def _result_message(
    requested: list[int], deleted: list[int], skipped: list[int], failed: list[int], protected_msg: str | None
) -> str:
    """Notification body summarizing one marker_delete run."""
    msg = f"Deleted {len(deleted)} of {len(requested)} requested marker(s)."
    if skipped:
        msg += f"\nNot deleted (already absent): {', '.join(str(mid) for mid in skipped)}."
    if protected_msg:
        msg += f"\n{protected_msg}."
    if failed:
        msg += (
            "\nFAILED (request error or server rejected the delete despite accepting the marker as "
            f"eligible — marker may still exist, see log): {', '.join(str(mid) for mid in failed)}."
        )
    return msg


def _result_title(deleted: list[int], requested: list[int], protected: list[int], failed: list[int]) -> str:
    """Notification title — flags protected and failed ids so a partial run is visible at a glance."""
    title = f"Marker Delete — {len(deleted)}/{len(requested)} OK"
    if protected:
        title += f", {len(protected)} protected"
    if failed:
        title += " (errors)"
    return title


async def handle_marker_delete(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Permanently delete one or more Markers from the Comexio server.

    'confirm' must be enabled — checked (via _validate_marker_delete_request) before login is
    attempted, so an unconfirmed call never opens a Comexio session. Requires literal `True`
    (not just any truthy value) since call.data bypasses services.yaml's declared
    selector/required when invoked outside the UI — an irreversible action shouldn't accept a
    stray non-empty string as "confirmed". 'force' is checked the same strict way.

    After login, api.get_marker_delete_eligibility classifies every id against a fresh config
    lookup: protected ids are skipped and reported (response key "protected"), the rest is
    deleted. Its third return value, known_ids, is the set of ids that lookup actually found on
    the server — used below to sanity-check delete_marker's False results.

    api.delete_marker returns a tri-state per id: True (deleted), False (server reported
    nothing changed), or None (the request itself failed — always a real failure). A False is
    only reported as the harmless "already absent" case when the id wasn't in known_ids either;
    a False for an id that WAS present is treated as a failure.

    Always returns a dict (never None): the service supports responses and Developer Tools /
    MCP callers always request one — see _error_result. "error" carries gate_error when the
    config couldn't be read or force was ignored, so a caller can tell that from real protection;
    "protected_reason" carries the reason for the ids in "protected" in both cases.
    """
    raw_input = str(call.data.get("marker_id", "")).strip()
    marker_ids, error = _validate_marker_delete_request(call, raw_input)
    if error is not None:
        return _fail(hass, error, requested=marker_ids)

    ctx = await _async_get_service_context(hass, call, _TITLE_MARKER_DELETE_ERR, resolve_plan=False, do_login=True)
    if ctx is None:
        # _async_get_service_context posts its own notification with the specific reason.
        return _error_result(
            "Comexio instance not found or admin login failed — see notification. Nothing deleted.",
            requested=marker_ids,
        )
    coordinator, api, _fub_id = ctx

    force = call.data.get("force") is True
    requested = marker_ids
    marker_ids, protected, known_ids, gate_error = await api.get_marker_delete_eligibility(marker_ids, force=force)
    protected_reason = _protected_reason(force, gate_error) if protected else None
    protected_msg = _protected_message(protected, protected_reason) if protected_reason else None
    if not marker_ids:
        result = _fail(hass, f"{protected_msg}. Nothing deleted.", requested=requested)
        # Protection alone is not a failure — keep "error" to gate_error here too, so "5" and
        # "5,6" (M5 protected, M6 deletable) report M5 the same way.
        result["protected"] = protected
        result["protected_reason"] = protected_reason
        result["error"] = gate_error
        return result

    _LOGGER.info(
        "Marker Delete: marker_id='%s' force=%s -> deleting %d marker(s): %s (protected: %s)",
        raw_input,
        force,
        len(marker_ids),
        marker_ids,
        protected,
    )
    t_start = time.monotonic()
    deleted: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    for marker_id in marker_ids:
        result = await api.delete_marker(marker_id)
        if result is True:
            deleted.append(marker_id)
        elif result is False and marker_id not in known_ids:
            skipped.append(marker_id)
        else:
            failed.append(marker_id)
    duration = time.monotonic() - t_start

    if deleted:
        await coordinator.async_request_refresh()

    msg = _result_message(requested, deleted, skipped, failed, protected_msg)
    msg += f"\nDuration: {duration:.1f}s"
    persistent_notification.async_create(hass, msg, title=_result_title(deleted, requested, protected, failed))
    return {
        "requested": requested,
        "deleted": deleted,
        "skipped": skipped,
        "protected": protected,
        "protected_reason": protected_reason,
        "failed": failed,
        "duration": round(duration, 1),
        "error": gate_error,
    }
