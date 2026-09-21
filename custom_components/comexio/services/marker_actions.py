# Version: 0.10.0
"""marker_delete — deletes Markers directly from the Comexio server (not a function plan action).

Kept as its own module rather than folded into misc.py's grab-bag: unlike the other
handlers there, this one is destructive (permanently removes Markers from Comexio) and
needs its own id-list parsing + confirm-gate + per-id result bookkeeping.

Scope is deliberately narrow: only markers this integration itself created via the API
(CategoryId==1) are deletable — see api.get_marker_delete_eligibility. Factory-provisioned
and Studio-created markers (CategoryId==0) are never reachable through this service — this
is a cleanup tool for integration-created markers, not a general-purpose marker housekeeping
service.
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


def _validate_marker_delete_request(hass: HomeAssistant, call: ServiceCall, raw_input: str) -> list[int] | None:
    """Parse+validate marker_id/confirm, posting the matching error notification on failure.

    Split out of handle_marker_delete to keep that function's cognitive complexity in check —
    these are independent, sequential guard checks (bad tokens / empty / too many / not
    confirmed) with no shared state, a natural helper boundary. Returns the resolved marker_id
    list on success, or None if the call should stop here (notification already posted).
    """
    marker_ids, invalid_tokens = _parse_marker_id_list(raw_input)
    if invalid_tokens:
        persistent_notification.async_create(
            hass,
            f"Invalid marker_id token(s): {', '.join(invalid_tokens)}. Nothing deleted.",
            title=_TITLE_MARKER_DELETE_ERR,
        )
        return None
    if not marker_ids:
        persistent_notification.async_create(
            hass, "No marker_id given — nothing to delete.", title=_TITLE_MARKER_DELETE_ERR
        )
        return None
    if len(marker_ids) > MARKER_DELETE_MAX_COUNT:
        persistent_notification.async_create(
            hass,
            f"marker_id resolves to {len(marker_ids)} markers, more than the safety limit of "
            f"{MARKER_DELETE_MAX_COUNT}. Nothing deleted — narrow the list/range and try again.",
            title=_TITLE_MARKER_DELETE_ERR,
        )
        return None
    if call.data.get("confirm") is not True:
        persistent_notification.async_create(
            hass, "Nothing deleted — 'confirm' must be enabled.", title=_TITLE_MARKER_DELETE_ERR
        )
        return None
    return marker_ids


async def handle_marker_delete(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Permanently delete one or more Markers from the Comexio server.

    'confirm' must be enabled — checked (via _validate_marker_delete_request) before login is
    attempted, so an unconfirmed call never opens a Comexio session. Requires literal `True`
    (not just any truthy value) since call.data bypasses services.yaml's declared
    selector/required when invoked outside the UI — an irreversible action shouldn't accept a
    stray non-empty string as "confirmed".

    After login, api.get_marker_delete_eligibility gates the whole batch against a fresh
    CategoryId lookup: any requested id that isn't a marker this integration created itself
    aborts the entire call with nothing deleted, same as the invalid-token/max-count checks in
    _validate_marker_delete_request — a partial delete on a batch that mixes protected and
    eligible ids would be more confusing than useful here. Its third return value, known_ids,
    is the set of ids that lookup actually found on the server (as opposed to ids already
    absent before this call even started) — used below to sanity-check delete_marker's False
    results.

    api.delete_marker returns a tri-state per id: True (deleted), False (server reported
    nothing changed), or None (the request itself failed — HTTP/session/parse error, always
    a real failure). A False is only reported as the harmless "already absent" case when the
    id wasn't in known_ids either — i.e. it was already gone before this call started. A
    False for an id that WAS in known_ids (present with CategoryId==1 moments ago) is treated
    as a failure instead, since the server accepted it as deletable but the delete itself
    reported no change — that's not the routine "already-gone" case this tri-state exists to
    let through silently.
    """
    raw_input = str(call.data.get("marker_id", "")).strip()
    marker_ids = _validate_marker_delete_request(hass, call, raw_input)
    if marker_ids is None:
        return None

    ctx = await _async_get_service_context(hass, call, _TITLE_MARKER_DELETE_ERR, resolve_plan=False, do_login=True)
    if ctx is None:
        return None
    coordinator, api, _fub_id = ctx

    marker_ids, protected, known_ids = await api.get_marker_delete_eligibility(marker_ids)
    if protected:
        persistent_notification.async_create(
            hass,
            "Refusing to delete: "
            f"{', '.join(f'M{mid}' for mid in protected)} — could not be verified as created by "
            "this integration (marker_delete only removes markers it created itself, e.g. KNX "
            "bridge markers; Studio-created/factory markers are protected, and the whole "
            "current Comexio config is refused wholesale if it couldn't be fetched at all — "
            "see log for details in that case). Nothing deleted.",
            title=_TITLE_MARKER_DELETE_ERR,
        )
        return None

    _LOGGER.info("Marker Delete: marker_id='%s' -> %d marker(s): %s", raw_input, len(marker_ids), marker_ids)
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

    id_list = ", ".join(str(mid) for mid in marker_ids)
    msg = f"Deleted {len(deleted)} of {len(marker_ids)} marker(s) ({id_list})."
    if skipped:
        msg += f"\nNot deleted (already absent): {', '.join(str(mid) for mid in skipped)}."
    if failed:
        msg += (
            "\nFAILED (request error or server rejected the delete despite accepting the marker as "
            f"eligible — marker may still exist, see log): {', '.join(str(mid) for mid in failed)}."
        )
    msg += f"\nDuration: {duration:.1f}s"
    title = f"Marker Delete — {len(deleted)}/{len(marker_ids)} OK" + (" (errors)" if failed else "")
    persistent_notification.async_create(hass, msg, title=title)
    return {
        "requested": marker_ids,
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "duration": round(duration, 1),
    }
