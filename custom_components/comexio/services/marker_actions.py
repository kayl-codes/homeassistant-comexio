# Version: 0.10.0
"""marker_delete — deletes Markers directly from the Comexio server (not a function plan action).

Kept as its own module rather than folded into misc.py's grab-bag: unlike the other
handlers there, this one is destructive (permanently removes Markers from Comexio) and
needs its own id-list parsing + confirm-gate + per-id result bookkeeping.

Scope is deliberately narrow: only markers this integration itself created via the API
(CategoryId==1, e.g. create_knx_bridge_marker) are deletable — see
api.get_marker_delete_eligibility. Factory-provisioned and Studio-created markers
(CategoryId==0) are never reachable through this service, confirmed marker cleanup use
case being KNX bridge test blocks, not general-purpose marker housekeeping.
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
    for token, parsed in parse_ignored_marker_tokens(raw_input):
        if parsed is None:
            invalid_tokens.append(token)
        elif isinstance(parsed, tuple):
            marker_ids.update(range(parsed[0], parsed[1] + 1))
        else:
            marker_ids.add(parsed)
    return sorted(marker_ids), invalid_tokens


async def handle_marker_delete(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Permanently delete one or more Markers from the Comexio server.

    'confirm' must be enabled — checked before anything else (including login), so an
    unconfirmed call never even opens a Comexio session. Requires literal `True` (not just
    any truthy value) since call.data bypasses services.yaml's declared selector/required
    when invoked outside the UI — an irreversible action shouldn't accept a stray non-empty
    string as "confirmed".

    After login, api.get_marker_delete_eligibility gates the whole batch against a fresh
    CategoryId lookup: any requested id that isn't a marker this integration created itself
    aborts the entire call with nothing deleted, same as the invalid-token/max-count checks
    above — a partial delete on a batch that mixes protected and eligible ids would be more
    confusing than useful here.

    api.delete_marker returns a tri-state per id: True (deleted), False (confirmed absent —
    not treated as an error, per explicit requirement: deleting an already-gone id in a
    batch/range is routine), or None (the request itself failed — HTTP/session/parse error).
    Only the False bucket is reported as harmless; None is surfaced as a real failure so a
    session drop mid-batch is never misreported as "marker already gone".
    """
    raw_input = str(call.data.get("marker_id", "")).strip()
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

    ctx = await _async_get_service_context(hass, call, _TITLE_MARKER_DELETE_ERR, resolve_plan=False, do_login=True)
    if ctx is None:
        return None
    coordinator, api, _fub_id = ctx

    marker_ids, protected = await api.get_marker_delete_eligibility(marker_ids)
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
        elif result is False:
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
        msg += f"\nFAILED (request error, marker may still exist — see log): {', '.join(str(mid) for mid in failed)}."
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
