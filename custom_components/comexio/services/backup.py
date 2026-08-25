# Version: 0.7.6
"""Restore / Backup handlers — function_plan_restore, function_plan_delete_backups,
function_plan_purge_orphaned_backups, function_plan_list_backups.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
everything that reads/writes the stored backup catalog (function_plan_backup.py) rather
than acting on the live plan directly.
"""

from datetime import datetime, timedelta
import json
import logging
import time
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    ICON_ERROR,
    ICON_INACTIVE,
    ICON_SUCCESS,
    ICON_WARNING,
    TIMESTAMP_DISPLAY_FORMAT,
)
from ..coordinator import ComexioCoordinator
from ..function_plan_backup import diff_snapshots, retention_cutoff, snapshot_label_maps
from ..function_plan_render import resolve_element_label
from ._context import (
    _LOGIN_FAILED_MSG,
    _async_get_service_context,
    _parse_snapshot_field,
    _resolve_backup_identity,
    _resolve_fub_id,
)
from ._yaml_sync import _refresh_service_descriptions

_LOGGER = logging.getLogger(__name__)

_TITLE_RESTORE_ERR = "Function Plan Restore — Error"
_TITLE_RESTORE_PROGRESS = "Function Plan Restore — IN PROGRESS"
_TITLE_LIST_BACKUPS_ERR = "Function Plan Backups — Error"
_TITLE_DELETE_BACKUPS_ERR = "Function Plan Delete Backups — Error"
_TITLE_PURGE_ORPHANED_BACKUPS_ERR = "Function Plan Purge Orphaned Backups — Error"

_AGE_KEYS = ("days", "hours", "minutes", "seconds")


def _coerce_int(value) -> int | None:
    """Best-effort int conversion for service field values (number selectors may deliver floats/strings)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _age_cutoff(age) -> datetime | None:
    """Translate a max_age service field (duration dict or plain hours) into a UTC cutoff timestamp."""
    if not age:
        return None
    try:
        if isinstance(age, dict):
            delta = timedelta(**{k: float(v) for k, v in age.items() if k in _AGE_KEYS and v})
        else:
            delta = timedelta(hours=float(age))
    except (TypeError, ValueError):
        return None
    if delta.total_seconds() <= 0:
        return None
    return dt_util.utcnow() - delta


def _backup_entry_matches(
    entry: dict,
    fub_id: int | None,
    plan_name: str | None,
    slot: int | None,
    cutoff: datetime | None,
) -> bool:
    """Check a backup metadata entry against the optional list_backups filters."""
    if fub_id is not None and _coerce_int(entry.get("fub_id")) != fub_id:
        return False
    if plan_name and plan_name.lower() not in str(entry.get("plan_name", "")).lower():
        return False
    if slot is not None and _coerce_int(entry.get("slot")) != slot:
        return False
    if cutoff is not None:
        captured = dt_util.parse_datetime(str(entry.get("captured_at", "")))
        # max_age only excludes entries known to be older than the cutoff; entries with a
        # missing/unparseable captured_at (legacy or corrupted metadata) are kept rather
        # than silently dropped from the listing.
        return captured is None or captured >= cutoff
    return True


def _sort_backup_entries(entries: list[dict], order_by: str) -> list[dict]:
    """Sort list_backups metadata entries; slot 0 is always the newest snapshot of a plan."""
    if order_by == "oldest":
        return sorted(entries, key=lambda e: e.get("captured_at") or "")
    if order_by == "plan":
        return sorted(entries, key=lambda e: (str(e.get("plan_name", "")).lower(), e.get("slot", 0)))
    if order_by == "fub_id":
        return sorted(entries, key=lambda e: (_coerce_int(e.get("fub_id")) or 0, e.get("slot", 0)))
    if order_by == "slot":
        return sorted(entries, key=lambda e: (e.get("slot", 0), str(e.get("plan_name", "")).lower()))
    return sorted(entries, key=lambda e: e.get("captured_at") or "", reverse=True)  # newest (default)


def _label_backup_identity(
    identity: tuple, catalog: dict, markers_by_id: dict, webio_by_id: dict, ios_by_id: dict
) -> str:
    """Human-readable label for one diff_snapshots() element/endpoint identity tuple.

    identity is a fixed-shape (type, ref_id, x, y, name) tuple — x/y/name are None for types
    with a globally stable reference (marker/IO/WebIO/time module — see
    function_plan_backup._STABLE_REF_TYPES); populated as a position-based fallback for
    everything else (blocks/constants/comments have no stable cross-snapshot ID).
    """
    etype, ref_id, _pos_x, _pos_y, name = identity
    elem = {"reference": {"type": etype, "ref_id": ref_id}, "name": name or ""}
    return resolve_element_label(elem, catalog, markers_by_id, webio_by_id, ios_by_id)


def _label_backup_wire(wire: tuple, catalog: dict, markers_by_id: dict, webio_by_id: dict, ios_by_id: dict) -> str:
    """Human-readable 'source → target' label for one diff_snapshots() connection tuple."""
    (src_identity, _src_port, src_inv), (dst_identity, _dst_port, dst_inv) = wire
    src_label = _label_backup_identity(src_identity, catalog, markers_by_id, webio_by_id, ios_by_id)
    dst_label = _label_backup_identity(dst_identity, catalog, markers_by_id, webio_by_id, ios_by_id)
    inv = " ¬" if src_inv or dst_inv else ""
    return f"{src_label} → {dst_label}{inv}"


def _label_backup_moved_wire(
    pair: tuple[tuple, tuple], catalog: dict, markers_by_id: dict, webio_by_id: dict, ios_by_id: dict
) -> str:
    """Human-readable label for one diff_snapshots() connections['moved'] (old_wire, new_wire) pair."""
    old_wire, new_wire = pair
    label = _label_backup_wire(new_wire, catalog, markers_by_id, webio_by_id, ios_by_id)
    (old_src, _, _), (old_dst, _, _) = old_wire
    (new_src, _, _), (new_dst, _, _) = new_wire
    moves = [
        f"({old_id[1]},{old_id[2]}) → ({new_id[1]},{new_id[2]})"
        for old_id, new_id in ((old_src, new_src), (old_dst, new_dst))
        if len(old_id) == 5 and (old_id[1], old_id[2]) != (new_id[1], new_id[2])
    ]
    suffix = f" [{', '.join(moves)}]" if moves else ""
    return f"{label}{suffix}"


def _label_diff_group(
    group: dict[str, list],
    catalog: dict,
    newer_maps: tuple[dict, dict, dict],
    older_maps: tuple[dict, dict, dict],
) -> dict[str, list[str]]:
    """Label an added/removed identity group.

    Added resolves against the newer snapshot's label maps, removed against the older
    snapshot's (see _attach_backup_diffs), so each side of the diff shows the name as it was
    AT THAT SNAPSHOT rather than today's possibly since-renamed live name.
    """
    return {
        "added": [_label_backup_identity(i, catalog, *newer_maps) for i in group["added"]],
        "removed": [_label_backup_identity(i, catalog, *older_maps) for i in group["removed"]],
    }


def _label_connection_diff(
    group: dict[str, list],
    catalog: dict,
    newer_maps: tuple[dict, dict, dict],
    older_maps: tuple[dict, dict, dict],
) -> dict[str, list[str]]:
    """Like _label_diff_group, but also labels the 'moved' pairs diff_snapshots() reports for
    connections (see _split_moved in function_plan_backup.py): a wire whose endpoint only shifted
    position, with no actual add/remove, shown as one entry instead of a confusing pair.
    """
    n_markers, n_webio, n_ios = newer_maps
    o_markers, o_webio, o_ios = older_maps
    labeled = {
        "added": [_label_backup_wire(w, catalog, n_markers, n_webio, n_ios) for w in group["added"]],
        "removed": [_label_backup_wire(w, catalog, o_markers, o_webio, o_ios) for w in group["removed"]],
    }
    labeled["moved"] = [_label_backup_moved_wire(pair, catalog, n_markers, n_webio, n_ios) for pair in group["moved"]]
    return labeled


async def _attach_backup_diffs(coordinator: ComexioCoordinator, entries: list[dict], kind: str) -> None:
    """Add a 'diff' field to each entry vs. its same-kind predecessor slot (slot+1), in-place.

    Entries without a predecessor (the oldest known snapshot of that plan identity) are left
    without a 'diff' key. Each snapshot's own captured label metadata (if any — see
    function_plan_backup.snapshot_label_maps) is overlaid on the live maps, per side of the diff,
    so a renamed/deleted marker doesn't misrepresent an old backup with today's name.
    """
    live_markers, live_webio, live_ios = coordinator.function_plan_label_maps()
    catalog = await coordinator.function_plan_catalog.async_get_catalog()
    for entry in entries:
        fub_id, plan_name, slot = entry["fub_id"], entry["plan_name"], entry["slot"]
        older = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot + 1)
        if older is None:
            continue
        newer = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
        if newer is None:
            continue
        raw_diff = diff_snapshots(newer, older)
        newer_maps = snapshot_label_maps(newer.get("labels"), live_markers, live_webio, live_ios)
        older_maps = snapshot_label_maps(older.get("labels"), live_markers, live_webio, live_ios)
        entry["diff"] = {
            "markers": _label_diff_group(raw_diff["markers"], catalog, newer_maps, older_maps),
            "ios": _label_diff_group(raw_diff["ios"], catalog, newer_maps, older_maps),
            "connections": _label_connection_diff(raw_diff["connections"], catalog, newer_maps, older_maps),
        }


def _split_plan_field(raw: str) -> tuple[int, str | None] | None:
    """Split a Plan dropdown value into (fub_id, plan_name).

    Accepts the current composite 'fub_id:plan_name' value as well as a bare legacy
    fub_id (pre-hardening dropdown value, or a hand-typed numeric ID in a scripted call) —
    plan_name is then None and the caller must disambiguate via _resolve_backup_identity.
    Returns None if the fub_id part isn't numeric at all (e.g. a plain plan name or entity_id,
    which callers resolve differently).
    """
    fub_id_part, sep, name_part = str(raw).partition(":")
    if not fub_id_part.strip().lstrip("-").isdigit():
        return None
    return int(fub_id_part), (name_part if sep else None)


async def _resolve_restore_target(
    hass: HomeAssistant, call: ServiceCall, coordinator: ComexioCoordinator, api
) -> tuple[int | None, str | None, str | None]:
    """Resolve the restore target as (fub_id, plan_name_hint, error).

    Accepts the composite 'fub_id:plan_name' dropdown value (plan_name_hint is then already
    exact and unambiguous). Otherwise resolves via _resolve_fub_id (bare fub_id, plan name, or
    the 'Function Plans' selector entity when the field is left empty) — if that fub_id is
    currently live, its live name is used as the hint; if not (plan deleted), plan_name_hint
    stays None and the caller must disambiguate via _resolve_backup_identity. Falls back to a
    name lookup in the stored backup metadata for a plan name that no longer exists live.
    """
    raw = call.data.get("fub_id")
    if not raw:
        fub_id = coordinator.get_active_function_plan_fub_id()
        if fub_id is not None:
            return fub_id, api.fub_data.get(str(fub_id), {}).get("Name"), None
        return (
            None,
            None,
            "No plan selected — the 'Function Plans' selector is empty. Please specify the plan (fub_id) explicitly.",
        )

    raw = str(raw)
    split = _split_plan_field(raw)
    if split and split[1] is not None:
        return split[0], split[1], None

    fub_id = _resolve_fub_id(raw, api.fub_data, hass)
    if fub_id is not None:
        return fub_id, api.fub_data.get(str(fub_id), {}).get("Name"), None

    raw_lower = raw.strip().lower()
    backups = await coordinator.function_plan_backup.async_list_backups()
    matches = {
        (entry["fub_id"], entry.get("plan_name"))
        for entries in backups.values()
        for entry in entries
        if str(entry.get("plan_name", "")).lower() == raw_lower
    }
    if len(matches) == 1:
        fub_id, plan_name = next(iter(matches))
        return fub_id, plan_name, None
    if len(matches) > 1:
        ids = sorted({m[0] for m in matches})
        return (
            None,
            None,
            (
                f"Plan name '{raw}' is ambiguous across stored backups (fub_ids {ids}) "
                "— please specify the numeric plan ID instead."
            ),
        )
    return None, None, f"Plan '{raw}' not found — neither live nor in any stored backup."


def _restore_conflict_message(fub_id: int, snapshot_name: str, live_name: str | None, on_conflict: str) -> str:
    """Build the confirmation-required notification text for a plan_missing/identity_mismatch restore."""
    reason = (
        f"Plan '{snapshot_name}' (ID {fub_id}) no longer exists."
        if live_name is None
        else f"ID {fub_id} is now used by a different plan ('{live_name}'), not '{snapshot_name}'."
    )
    action = (
        f"the LIVE plan '{live_name}' (ID {fub_id}) will be OVERWRITTEN"
        if on_conflict == "force_override" and live_name is not None
        else f"a NEW plan named '{snapshot_name}' will be created and rebuilt from the snapshot"
    )
    return (
        f"{reason}\nWith on_conflict='{on_conflict}', {action}.\nRe-run with `confirm: true` to proceed, "
        "or change `on_conflict`."
    )


async def _restore_plan_in_place(
    hass: HomeAssistant,
    coordinator: ComexioCoordinator,
    api,
    fub_id: int,
    snapshot: dict,
    kind: str,
    slot: int,
    plan_hash,
    identity_was_mismatched: bool = False,
    auto_start: bool = True,
) -> None:
    """Restore a snapshot onto the SAME live plan via run_fup (the original, safe-path restore).

    Also used for force_override onto an unrelated live plan (identity_was_mismatched=True) —
    in that case the live plan's name and canvas settings likely differ from the snapshot's,
    so both are aligned first (function_plan_update_paper) so the plan comes back exactly as it
    was, not just its wiring.
    """
    live_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    plan_name = snapshot.get("plan_name", live_name)
    was_active = bool(api.fub_data.get(str(fub_id), {}).get("Active", True))
    t_start = time.monotonic()

    # Same notification_id for the start and final message — the "in progress" notice is
    # replaced in place by the result, mirroring the sync-progress pattern in button.py.
    notif_id = f"comexio_function_plan_restore_{coordinator.server_id}"
    persistent_notification.async_create(
        hass,
        f"Restoring plan '{plan_name}' (ID {fub_id}) from {kind}[{slot}] — this can take up to a minute…",
        title=_TITLE_RESTORE_PROGRESS,
        notification_id=notif_id,
    )

    try:
        # Safety net: snapshot the CURRENT state before restoring, so the restore itself is undoable
        await coordinator.async_function_plan_change_backup(fub_id, f"pre_restore {kind}[{slot}]")

        apply = await _restore_apply_snapshot(api, fub_id, snapshot, live_name, plan_name, was_active, auto_start)
        verify = await _restore_verify(api, fub_id, snapshot, plan_hash)
        duration = time.monotonic() - t_start

        # identity_was_mismatched (force_override onto a DIFFERENT plan): that plan's elements
        # never existed under the snapshot's IDs, so Comexio always assigns fresh ones — a
        # byte-for-byte hash match can never be achieved there, matching counts is the
        # meaningful signal instead. Same-identity restore: elements keep their IDs, so a hash
        # match IS achievable and more precise (run_fup returns result=False on an inactive plan
        # even though the data payload IS applied).
        content_ok = verify["counts_match"] if identity_was_mismatched else verify["hash_match"]
        status = _restore_status(content_ok, apply, verify)

        # Promote the restored snapshot to slot 0 instead of leaving the next auto-backup cycle
        # write a near-duplicate entry for content that's already stored (see async_mark_restored).
        # Gated on content_ok (hash/counts match), NOT apply["run_ok"]: run_fup routinely returns
        # result=False on a plan that needs manual reactivation in Comexio even though the actual
        # element/connection data was applied correctly — using run_ok here would skip promotion
        # for exactly that (common, still-successful) case.
        # Skipped for identity_was_mismatched (force_override onto a DIFFERENT plan): this
        # snapshot's own (fub_id, plan_name) key no longer describes what's now live, so its
        # backup history is left exactly where it was.
        promoted = False
        if content_ok and not identity_was_mismatched:
            promoted = await coordinator.function_plan_backup.async_mark_restored(kind, fub_id, plan_name, slot)

        msg = _restore_build_message(
            plan_name,
            fub_id,
            kind,
            slot,
            snapshot,
            status,
            apply,
            verify,
            identity_was_mismatched,
            was_active,
            duration,
            promoted,
            auto_start,
        )
        _LOGGER.info(
            "Function Plan Restore result: fub=%s status=%s identity_was_mismatched=%s hash_match=%s "
            "counts_match=%s pos_ok=%s run_ok=%s properties_changed=%s paper_ok=%s duration=%.1fs",
            fub_id,
            status,
            identity_was_mismatched,
            verify["hash_match"],
            verify["counts_match"],
            apply["pos_ok"],
            apply["run_ok"],
            apply["properties_changed"],
            apply["paper_ok"],
            duration,
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        # Without this, a connection drop mid-restore leaves the "in progress" notification
        # above stuck forever, same gap as the copy-restore path (see _restore_plan_as_copy).
        _LOGGER.exception("Function Plan in-place restore failed for '%s' (ID %s)", plan_name, fub_id)
        persistent_notification.async_create(
            hass,
            f"Restore of {kind}[{slot}] for plan '{plan_name}' (ID {fub_id}) failed: {exc}. "
            "The plan may be left in a partially restored state — check it in Comexio Studio.",
            title=_TITLE_RESTORE_ERR,
            notification_id=notif_id,
        )
        return
    persistent_notification.async_create(hass, msg, title=f"Function Plan Restore — {status}", notification_id=notif_id)


async def _restore_apply_snapshot(
    api, fub_id: int, snapshot: dict, live_name: str, plan_name: str, was_active: bool, auto_start: bool = True
) -> dict:
    """Apply a snapshot onto the live plan (align paper/name, restore positions, run_fup).

    Align name + canvas settings with the snapshot first — a no-op when they already match
    (the normal same-identity restore), but required when force_override targets a plan
    whose current name/paper/DPI differs from the snapshot's.
    """
    paper = snapshot.get("paper", "A3")
    dpi = snapshot.get("dpi", 90)
    orientation = snapshot.get("orientation", "landscape")
    properties_changed = (
        api.get_fub_paper_format(fub_id) != paper
        or api.get_fub_dpi(fub_id) != dpi
        or api.get_fub_orientation(fub_id) != orientation
        or live_name != plan_name
    )
    paper_ok = True
    if properties_changed:
        paper_ok = await api.function_plan_update_paper(fub_id, paper, dpi, orientation, name=plan_name)

    await api.function_plan_stop_fup(fub_id)

    # Restore element positions via saveelementspos (known-good mechanism from sort)
    positions = [
        (int(elem_id), elem.get("position_x", 0.0), elem.get("position_y", 0.0))
        for elem_id, elem in snapshot.get("elements", {}).items()
    ]
    pos_ok = await api.function_plan_save_elements_pos(positions) if positions else True

    # A comment/header block (type=14) can be deleted independently of the plan's actual
    # wiring (e.g. during a grid re-sort) without touching connections or hash-relevant
    # structure — but its stale ID surviving in the run_fup payload crashes the ENTIRE call
    # (Comexio returns HTTP 500 for any element ID it no longer recognizes), silently failing
    # the whole restore rather than just the comment. Strip any such stale IDs from the
    # payload first — but DON'T recreate the comments yet (see _recreate_missing_comments).
    run_fup_snapshot, missing_comments, load_warning = await _sanitize_missing_comments(api, fub_id, snapshot)

    # run_fup(plan_data=...) is Comexio's ONLY mechanism for applying a structural snapshot
    # onto a live plan — it cannot be skipped just because the user doesn't want the plan
    # left running. The "auto-start disabled" outcome is achieved by stopping it again right
    # after, not by skipping the apply step (which would also drop the wiring restore).
    run_ok = await api.function_plan_run_fup(fub_id, plan_data=run_fup_snapshot)

    # Preserve previous inactive state, or leave it stopped when the user opted out of
    # auto-start. NOT gated on run_ok: run_fup routinely reports result=False even though the
    # payload WAS applied (the same Comexio quirk documented for the as-new restore path
    # above) — skipping the stop on a "failed" run_ok could leave a plan running that either
    # was inactive before the restore or that the user explicitly asked to leave stopped.
    # stop_ok IS a reliable success signal (unlike run_ok) — function_plan_stop_fup has no
    # equivalent "reports False on a technically-applied action" quirk — so it's tracked and
    # surfaced in the result message instead of being discarded.
    stop_ok = True
    if not was_active or not auto_start:
        stop_ok = await api.function_plan_stop_fup(fub_id)

    # Recreate missing comments only AFTER run_fup: run_fup treats its plan_data argument as
    # the plan's complete state, so a comment created before that call — and necessarily
    # absent from run_fup_snapshot, since its stale ID had to be stripped to avoid the crash
    # above — would be wiped out again by the call itself.
    recovered_comments, comment_warnings = await _recreate_missing_comments(api, fub_id, missing_comments)
    if load_warning:
        comment_warnings = [load_warning, *comment_warnings]

    return {
        "paper": paper,
        "dpi": dpi,
        "orientation": orientation,
        "properties_changed": properties_changed,
        "paper_ok": paper_ok,
        "pos_ok": pos_ok,
        "run_ok": run_ok,
        "stop_ok": stop_ok,
        "recovered_comments": recovered_comments,
        "comment_warnings": comment_warnings,
    }


async def _sanitize_missing_comments(api, fub_id: int, snapshot: dict) -> tuple[dict, dict[str, dict], str | None]:
    """Identify snapshot comment blocks (type=14) that no longer exist on the live plan.

    See the call site in _restore_apply_snapshot for why the run_fup payload must have these
    stripped out, and why recreating them has to happen AFTER run_fup rather than here.

    Returns (snapshot with those elements stripped from "elements", the stripped elements
    keyed by their stale snapshot ID, an optional warning if the live plan couldn't be
    reloaded to check for missing elements at all).
    """
    current = await api.function_plan_load_elements(fub_id)
    if current is None:
        return snapshot, {}, "could not reload the live plan to check for missing elements"

    current_ids = set(current.get("elements", {}))
    missing_comments = {
        eid: elem
        for eid, elem in snapshot.get("elements", {}).items()
        if eid not in current_ids and (elem.get("reference") or {}).get("type") == 14
    }
    if not missing_comments:
        return snapshot, {}, None

    sanitized_elements = {
        eid: elem for eid, elem in snapshot.get("elements", {}).items() if eid not in missing_comments
    }
    return {**snapshot, "elements": sanitized_elements}, missing_comments, None


async def _recreate_missing_comments(api, fub_id: int, missing_comments: dict[str, dict]) -> tuple[int, list[str]]:
    """Recreate the comment blocks identified by _sanitize_missing_comments.

    The original text IS recoverable here (unlike restore-as-new): a comment element carries
    its text in its own "name" field, and function_plan_load_elements exposes that field —
    it's the element's positional/ID metadata that's snapshot-local, not its text.

    Returns (recovered count, warnings — e.g. a comment that failed to recreate).
    """
    recovered = 0
    warnings: list[str] = []
    for eid, elem in missing_comments.items():
        text = (elem.get("name") or "").strip() or FUNCTION_PLAN_MANAGED_PLAN_COMMENT
        x, y = elem.get("position_x", 0.0), elem.get("position_y", 0.0)
        if await api.function_plan_add_comment_element(fub_id, text, x=x, y=y) is None:
            warnings.append(f"comment element {eid} ('{text}') failed to recreate")
        else:
            recovered += 1
    return recovered, warnings


async def _restore_verify(api, fub_id: int, snapshot: dict, plan_hash) -> dict:
    """Reload the plan after a restore and compare it against the snapshot (hash + counts)."""
    fresh = await api.function_plan_load_elements(fub_id)
    fresh_hash = plan_hash(fresh) if fresh else None
    hash_match = fresh_hash == snapshot.get("hash")
    elem_count = len(snapshot.get("elements", {}))
    conn_count = len(snapshot.get("connections", {}))
    fresh_elem = len(fresh.get("elements", {})) if fresh else -1
    fresh_conn = len(fresh.get("connections", {})) if fresh else -1
    counts_match = fresh_elem == elem_count and fresh_conn == conn_count
    return {
        "hash_match": hash_match,
        "counts_match": counts_match,
        "elem_count": elem_count,
        "conn_count": conn_count,
        "fresh_elem": fresh_elem,
        "fresh_conn": fresh_conn,
        # False when function_plan_load_elements failed post-restore (fresh is None) — a restore
        # we can't verify must never be reported as OK/PARTIAL.
        "reload_ok": fresh is not None,
    }


def _restore_status(content_ok: bool, apply: dict, verify: dict) -> str:
    """Overall OK/PARTIAL/FAILED verdict for a restore (see _restore_plan_in_place)."""
    if not verify["reload_ok"]:
        return "FAILED"
    if content_ok and apply["paper_ok"]:
        return "OK"
    if apply["run_ok"] or apply["pos_ok"] or apply["paper_ok"] or verify["counts_match"]:
        return "PARTIAL"
    return "FAILED"


def _restore_run_label(apply: dict, was_active: bool, auto_start: bool) -> tuple[str, str]:
    """(run_label, reactivation_note) for the run_fup line of a restore result message."""
    # A stop is attempted whenever the plan needs to end up inactive (see
    # _restore_apply_snapshot) — surface a failed stop regardless of which of those two
    # conditions triggered it, since either way the plan may still be running unexpectedly.
    if (not was_active or not auto_start) and not apply["stop_ok"]:
        note = (
            f"\n{ICON_WARNING} Plan could not be stopped after restore — it may still be running, "
            "please check/stop it manually in Comexio."
        )
        return f"{ICON_ERROR} (stop failed after restore)", note
    if not auto_start:
        # run_fup still ran (it's the only apply mechanism), but the result was deliberately
        # stopped again right after — this is neither a success nor a failure to report.
        return f"{ICON_SUCCESS if apply['run_ok'] else ICON_ERROR} (left stopped — auto-start disabled)", ""
    if apply["run_ok"]:
        return ICON_SUCCESS, ""
    if was_active:
        note = f"\n{ICON_WARNING} Plan could not be re-activated — please check/activate it in Comexio."
        return ICON_ERROR, note
    # Data payload was applied; the server merely skipped activating an inactive plan.
    return f"{ICON_INACTIVE} (plan inactive — activation skipped)", ""


def _restore_build_message(
    plan_name: str,
    fub_id: int,
    kind: str,
    slot: int,
    snapshot: dict,
    status: str,
    apply: dict,
    verify: dict,
    identity_was_mismatched: bool,
    was_active: bool,
    duration: float,
    promoted: bool = False,
    auto_start: bool = True,
) -> str:
    """Human-readable persistent_notification body for a restore result."""
    run_label, reactivation_note = _restore_run_label(apply, was_active, auto_start)
    paper_line = ""
    if apply["properties_changed"]:
        paper_line = (
            f"Aligned with snapshot: name '{plan_name}', paper {apply['paper']} @ {apply['dpi']} DPI, "
            f"{apply['orientation']} — "
            f"{ICON_SUCCESS if apply['paper_ok'] else f'{ICON_ERROR} failed, restore may look wrong'}\n"
        )
    if identity_was_mismatched:
        content_line = (
            f"Content match: {ICON_SUCCESS if verify['counts_match'] else ICON_ERROR} "
            "(element IDs are always reassigned when overriding a different plan — "
            "matching counts is what matters here, not a byte-for-byte hash)\n"
        )
    else:
        content_line = f"Hash match: {ICON_SUCCESS if verify['hash_match'] else ICON_ERROR} | "
    comment_line = ""
    if apply["recovered_comments"]:
        comment_line = (
            f"Comment/header blocks recreated: {apply['recovered_comments']} "
            "(were missing on the live plan — a hash mismatch above can be this alone, not a real problem)\n"
        )
    if apply["comment_warnings"]:
        shown = apply["comment_warnings"][:5]
        comment_line += "\n".join(f"{ICON_WARNING} {w}" for w in shown) + "\n"
    promoted_line = f"Backup slot: promoted {kind}[{slot}] → {kind}[0] (now marked restored *)\n" if promoted else ""
    return (
        f"Restore of plan '{plan_name}' (ID {fub_id}) from {kind}[{slot}] "
        f"({snapshot.get('captured_at')}): **{status}**\n"
        f"Snapshot: {verify['elem_count']} elements / {verify['conn_count']} connections → "
        f"now: {verify['fresh_elem']} / {verify['fresh_conn']}\n"
        f"{paper_line}"
        f"{content_line}positions: {ICON_SUCCESS if apply['pos_ok'] else ICON_ERROR} | "
        f"run_fup: {run_label}"
        f"{reactivation_note}\n"
        f"{comment_line}"
        f"{promoted_line}"
        f"Duration: {duration:.1f}s"
    )


async def _restore_plan_as_new(
    hass: HomeAssistant,
    coordinator: ComexioCoordinator,
    api,
    old_fub_id: int,
    snapshot: dict,
    kind: str,
    slot: int,
    old_id_still_live: bool = False,
) -> None:
    """Recreate a deleted/reassigned plan as a brand-new plan and rebuild it from the snapshot.

    old_id_still_live: True for the identity-mismatch/on_conflict='new_id' case, where
    old_fub_id is occupied by an unrelated live plan rather than genuinely deleted — that
    plan's own backup lineage must stay under its own ID, so the rekey below is skipped.
    """
    plan_name = snapshot.get("plan_name", str(old_fub_id))
    t_start = time.monotonic()

    notif_id = f"comexio_function_plan_restore_{coordinator.server_id}"
    persistent_notification.async_create(
        hass,
        f"Restoring plan '{plan_name}' (former ID {old_fub_id}) from {kind}[{slot}] as a new plan — "
        "this can take up to a minute…",
        title=_TITLE_RESTORE_PROGRESS,
        notification_id=notif_id,
    )

    # Snapshots captured before paper/DPI tracking existed (or never backfilled) fall back to
    # A3 @ 90 DPI landscape — a spacious default so the rebuild cannot come out clipped.
    paper = snapshot.get("paper", "A3")
    dpi = snapshot.get("dpi", 90)
    orientation = snapshot.get("orientation", "landscape")
    new_fub_id = await api.create_fup(plan_name, paper_format=paper, dpi=dpi, orientation=orientation)
    if new_fub_id is None:
        persistent_notification.async_create(
            hass,
            f"Could not create a replacement plan named '{plan_name}' (the name may already be in use live). "
            f"Restore of {kind}[{slot}] for the former plan {old_fub_id} was aborted.",
            title=_TITLE_RESTORE_ERR,
            notification_id=notif_id,
        )
        return

    elements_created, connections_created, warnings = await api.function_plan_rebuild_plan_from_snapshot(
        new_fub_id, snapshot
    )
    # create_fup always creates plans inactive (fub_active="0"); run_fup's very first call
    # therefore routinely reports result=False even though the data payload IS applied — the
    # same Comexio quirk documented for the in-place restore path. run_ok is NOT a success
    # criterion here; the recreated/expected element+connection counts are.
    run_ok = await api.function_plan_run_fup(new_fub_id)
    duration = time.monotonic() - t_start

    if old_id_still_live:
        purged = await coordinator.function_plan_backup.async_purge_identity(old_fub_id, plan_name)
    else:
        await coordinator.function_plan_backup.async_rekey_fub_id(old_fub_id, new_fub_id, plan_name)
        purged = 0
    updated_consumers = await coordinator.async_repoint_function_plan_fub_id(plan_name, old_fub_id, new_fub_id)
    if updated_consumers:
        await hass.config_entries.async_reload(coordinator.config_entry.entry_id)

    elem_count = len(snapshot.get("elements", {}))
    conn_count = len(snapshot.get("connections", {}))
    structurally_complete = elements_created == elem_count and connections_created == conn_count
    status = "OK" if structurally_complete and not warnings else "PARTIAL"

    captured_raw = snapshot.get("captured_at")
    captured_ts = dt_util.parse_datetime(str(captured_raw)) if captured_raw else None
    captured_label = dt_util.as_local(captured_ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if captured_ts else "?"

    activation_line = (
        f"Activation: {ICON_SUCCESS} active"
        if run_ok
        else f"Activation: {ICON_INACTIVE} not active yet (Comexio always creates new plans inactive — "
        "activate manually in Comexio Studio if needed)"
    )
    consumer_line = (
        f"Updated references: {', '.join(updated_consumers)}"
        if updated_consumers
        else "References: nothing pointed at the old ID (cluster plan map / plan selector) — nothing to update."
    )
    lineage_line = (
        f"Backup lineage: {purged} old snapshot(s) for '{plan_name}' removed from old ID {old_fub_id} "
        f"(superseded — the plan now lives at {new_fub_id}; the old ID's OTHER plan is untouched)."
        if old_id_still_live
        else f"Backup lineage: moved from old ID {old_fub_id} to new ID {new_fub_id}."
    )

    msg = (
        f"Plan '{plan_name}' restored as a NEW plan (old ID {old_fub_id} → new ID {new_fub_id})\n\n"
        f"Source: {kind}[{slot}], captured {captured_label}\n"
        f"Elements: {elements_created}/{elem_count} recreated\n"
        f"Connections: {connections_created}/{conn_count} recreated\n"
        f"Paper: {paper} @ {dpi} DPI, {orientation}\n"
        f"{activation_line}\n\n"
        f"{consumer_line}\n"
        f"{lineage_line}\n\n"
        f"Duration: {duration:.1f}s"
    )
    if warnings:
        shown = warnings[:10]
        msg += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in shown)
        if len(warnings) > len(shown):
            msg += f"\n… and {len(warnings) - len(shown)} more (see log)"

    _LOGGER.info(
        "Function Plan Restore (new plan): old_fub=%s new_fub=%s status=%s elements=%d connections=%d/%d "
        "run_ok=%s duration=%.1fs",
        old_fub_id,
        new_fub_id,
        status,
        elements_created,
        connections_created,
        conn_count,
        run_ok,
        duration,
    )
    persistent_notification.async_create(hass, msg, title=f"Function Plan Restore — {status}", notification_id=notif_id)


async def _restore_plan_as_copy(
    hass: HomeAssistant,
    coordinator: ComexioCoordinator,
    api,
    source_fub_id: int,
    snapshot: dict,
    kind: str,
    slot: int,
    new_plan_name: str,
    auto_start: bool,
) -> None:
    """Restore a snapshot as a brand-new, independent plan — a true copy.

    Unlike _restore_plan_as_new (which supersedes a deleted/reassigned plan's identity: it
    rekeys the backup lineage onto the new ID and repoints any consumers), the source plan
    here is untouched and still live: its backup lineage stays exactly where it is, and no
    consumer (cluster plan_map, 'Function Plans' selector) is repointed at the copy. The copy
    is not part of any backup lineage yet — it just starts fresh from here on.
    """
    source_name = snapshot.get("plan_name", str(source_fub_id))
    t_start = time.monotonic()

    notif_id = f"comexio_function_plan_restore_{coordinator.server_id}"
    persistent_notification.async_create(
        hass,
        f"Restoring {kind}[{slot}] of plan '{source_name}' as a new copy '{new_plan_name}' — "
        "this can take up to a minute…",
        title=_TITLE_RESTORE_PROGRESS,
        notification_id=notif_id,
    )

    paper = snapshot.get("paper", "A3")
    dpi = snapshot.get("dpi", 90)
    orientation = snapshot.get("orientation", "landscape")
    try:
        new_fub_id = await api.create_fup(new_plan_name, paper_format=paper, dpi=dpi, orientation=orientation)
        if new_fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Could not create plan '{new_plan_name}' (the name may already be in use live). "
                f"Copy-restore of {kind}[{slot}] for '{source_name}' was aborted.",
                title=_TITLE_RESTORE_ERR,
                notification_id=notif_id,
            )
            return

        elements_created, connections_created, warnings = await api.function_plan_rebuild_plan_from_snapshot(
            new_fub_id, snapshot
        )
        # create_fup always creates plans inactive — auto_start=False just leaves that default
        # alone instead of calling run_fup at all (no plan_data payload here: the structure was
        # already built above via individual element/connection calls, not via run_fup).
        run_ok = await api.function_plan_run_fup(new_fub_id) if auto_start else None
    except (aiohttp.ClientError, TimeoutError) as exc:
        # Without this, a connection drop mid-restore leaves the "in progress" notification
        # above stuck forever — the exception propagates past the persistent_notification calls
        # instead of replacing it, even though the restore lock is released either way.
        _LOGGER.exception("Function Plan copy-restore failed while building '%s'", new_plan_name)
        persistent_notification.async_create(
            hass,
            f"Copy-restore of {kind}[{slot}] for '{source_name}' failed while building '{new_plan_name}': {exc}. "
            "The new plan may be partially created — check Comexio Studio and delete it manually if needed.",
            title=_TITLE_RESTORE_ERR,
            notification_id=notif_id,
        )
        return
    duration = time.monotonic() - t_start

    elem_count = len(snapshot.get("elements", {}))
    conn_count = len(snapshot.get("connections", {}))
    structurally_complete = elements_created == elem_count and connections_created == conn_count
    status = "OK" if structurally_complete and not warnings else "PARTIAL"

    captured_raw = snapshot.get("captured_at")
    captured_ts = dt_util.parse_datetime(str(captured_raw)) if captured_raw else None
    captured_label = dt_util.as_local(captured_ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if captured_ts else "?"

    if not auto_start:
        activation_line = f"Activation: {ICON_INACTIVE} not started (auto-start disabled for this restore)"
    elif run_ok:
        activation_line = f"Activation: {ICON_SUCCESS} active"
    else:
        activation_line = (
            f"Activation: {ICON_INACTIVE} not active yet (Comexio always creates new plans inactive — "
            "activate manually in Comexio Studio if needed)"
        )

    msg = (
        f"Plan '{new_plan_name}' created as a COPY of '{source_name}' "
        f"(source ID {source_fub_id} unchanged, new copy ID {new_fub_id})\n\n"
        f"Source: {kind}[{slot}], captured {captured_label}\n"
        f"Elements: {elements_created}/{elem_count} recreated\n"
        f"Connections: {connections_created}/{conn_count} recreated\n"
        f"Paper: {paper} @ {dpi} DPI, {orientation}\n"
        f"{activation_line}\n\n"
        "Backup lineage: unchanged — snapshots stay with the source plan; the copy starts fresh.\n\n"
        f"Duration: {duration:.1f}s"
    )
    if warnings:
        shown = warnings[:10]
        msg += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in shown)
        if len(warnings) > len(shown):
            msg += f"\n… and {len(warnings) - len(shown)} more (see log)"

    _LOGGER.info(
        "Function Plan Restore (copy): source_fub=%s new_fub=%s new_name=%s status=%s elements=%d "
        "connections=%d/%d run_ok=%s auto_start=%s duration=%.1fs",
        source_fub_id,
        new_fub_id,
        new_plan_name,
        status,
        elements_created,
        connections_created,
        conn_count,
        run_ok,
        auto_start,
        duration,
    )
    persistent_notification.async_create(hass, msg, title=f"Function Plan Restore — {status}", notification_id=notif_id)


async def _resolve_restore_params(
    hass: HomeAssistant, call: ServiceCall, coordinator: ComexioCoordinator, api
) -> tuple[int, str, int, str | None] | None:
    """Resolve (fub_id, kind, slot, plan_name_hint) from the call's 'snapshot' or 'fub_id' field.

    Returns None if resolution failed — the error notification has already been shown.
    """
    snapshot_raw = call.data.get("snapshot")
    if snapshot_raw:
        parsed = _parse_snapshot_field(str(snapshot_raw))
        if parsed is None:
            persistent_notification.async_create(
                hass, f"Invalid 'snapshot' value: '{snapshot_raw}'.", title=_TITLE_RESTORE_ERR
            )
            return None
        return parsed
    fub_id, plan_name_hint, resolve_err = await _resolve_restore_target(hass, call, coordinator, api)
    if resolve_err:
        persistent_notification.async_create(hass, resolve_err, title=_TITLE_RESTORE_ERR)
        return None
    kind = str(call.data.get("kind", "auto")).strip().lower()
    slot = int(call.data.get("slot", 0))
    return fub_id, kind, slot, plan_name_hint


async def _resolve_restore_snapshot(
    hass: HomeAssistant, coordinator: ComexioCoordinator, fub_id: int, plan_name: str, kind: str, slot: int
) -> dict[str, Any] | None:
    """Look up the requested snapshot; notifies with the available alternatives if missing."""
    snapshot = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
    if snapshot is not None:
        return snapshot
    backups = await coordinator.function_plan_backup.async_list_backups()
    available = [
        f"{kind}[{b['slot']}] {b['captured_at']}"
        for b in backups.get(kind, [])
        if b["fub_id"] == fub_id and b.get("plan_name") == plan_name
    ]
    persistent_notification.async_create(
        hass,
        f"No snapshot found for plan '{plan_name}' (fub {fub_id}, kind={kind}, slot={slot}).\n"
        f"Available {kind} snapshots: {', '.join(available) if available else 'none'}",
        title=_TITLE_RESTORE_ERR,
    )
    return None


async def _resolve_restore_conflict(
    hass: HomeAssistant,
    coordinator: ComexioCoordinator,
    api,
    fub_id: int,
    snapshot: dict[str, Any],
    kind: str,
    slot: int,
    live_fub: dict[str, Any] | None,
    live_name: str | None,
    snapshot_name: str,
    on_conflict: str,
    confirm: bool,
) -> bool:
    """Handle an identity-mismatch conflict. Returns True if the caller should stop here."""
    if not confirm:
        persistent_notification.async_create(
            hass,
            _restore_conflict_message(fub_id, snapshot_name, live_name, on_conflict),
            title=_TITLE_RESTORE_ERR,
        )
        return True
    if live_fub is None or on_conflict != "force_override":
        await _restore_plan_as_new(
            hass, coordinator, api, fub_id, snapshot, kind, slot, old_id_still_live=live_fub is not None
        )
        await _refresh_service_descriptions(hass)
        return True
    # force_override on an identity mismatch: fall through and deliberately
    # overwrite the plan that now occupies this fub_id.
    return False


async def _handle_function_plan_restore(hass: HomeAssistant, call: ServiceCall):
    """Restore a function plan from a stored backup snapshot.

    Branches on the CURRENT live state of the snapshot's fub_id:
    - free (plan deleted) → always rebuilt as a new plan (see _restore_plan_as_new).
    - live under the SAME name → normal in-place restore via run_fup (unchanged, safe path).
    - live under a DIFFERENT name (ID reused by an unrelated plan) → identity mismatch;
      requires explicit `confirm: true` plus `on_conflict` ('new_id' rebuilds separately,
      'force_override' overwrites the live plan anyway).
    """
    from ..function_plan_backup import plan_hash

    ctx = await _async_get_service_context(hass, call, _TITLE_RESTORE_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return
    coordinator, api, _unused_fub_id = ctx

    if coordinator._restore_lock.locked():
        _LOGGER.warning("Function Plan Restore already in progress, ignoring concurrent request")
        persistent_notification.async_create(
            hass,
            "A restore is already running — please wait for it to finish before starting another one.",
            title=_TITLE_RESTORE_ERR,
        )
        return

    async with coordinator._restore_lock:
        # Fire an immediate coordinator update so the backup-select entity's
        # 'restore_in_progress' attribute (select.py) reaches every dashboard card instance
        # right away — a card only disables its OWN restore button on click, so a second
        # card instance (or a stale browser tab) needs this push to grey out too, instead
        # of relying solely on the lock rejection above.
        coordinator.async_update_listeners()
        try:
            await _run_function_plan_restore(hass, call, coordinator, api, plan_hash)
        finally:
            coordinator.async_update_listeners()


async def _run_function_plan_restore(
    hass: HomeAssistant, call: ServiceCall, coordinator: ComexioCoordinator, api, plan_hash
) -> None:
    """Body of _handle_function_plan_restore, run while holding coordinator._restore_lock."""
    resolved = await _resolve_restore_params(hass, call, coordinator, api)
    if resolved is None:
        return
    fub_id, kind, slot, plan_name_hint = resolved

    plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
    if identity_err:
        persistent_notification.async_create(hass, identity_err, title=_TITLE_RESTORE_ERR)
        return

    on_conflict = str(call.data.get("on_conflict", "new_id")).strip().lower()
    confirm = bool(call.data.get("confirm", False))
    as_copy = bool(call.data.get("as_copy", False))
    new_plan_name = str(call.data.get("new_plan_name", "")).strip()
    auto_start = bool(call.data.get("auto_start", True))
    _LOGGER.info(
        "Function Plan Restore: fub_id=%s plan_name=%s kind=%s slot=%s on_conflict=%s confirm=%s "
        "as_copy=%s new_plan_name=%s auto_start=%s",
        fub_id,
        plan_name,
        kind,
        slot,
        on_conflict,
        confirm,
        as_copy,
        new_plan_name,
        auto_start,
    )
    if as_copy and not new_plan_name:
        persistent_notification.async_create(
            hass, "A name is required for 'Restore as copy' ('new_plan_name').", title=_TITLE_RESTORE_ERR
        )
        return

    snapshot = await _resolve_restore_snapshot(hass, coordinator, fub_id, plan_name, kind, slot)
    if snapshot is None:
        return

    if not await api.login():
        persistent_notification.async_create(hass, _LOGIN_FAILED_MSG, title=_TITLE_RESTORE_ERR)
        return

    if as_copy:
        # The source plan is never touched here — no conflict/identity check applies.
        await _restore_plan_as_copy(hass, coordinator, api, fub_id, snapshot, kind, slot, new_plan_name, auto_start)
        await _refresh_service_descriptions(hass)
        return

    # Fresh live lookup — api.fub_data may be up to one poll interval stale, and this
    # decision (does the plan still exist, under what name?) must not be made on stale data.
    raw_config = await api.get_raw_config()
    live_fub = raw_config.get("Fubs", {}).get(str(fub_id))
    if live_fub is not None:
        api.update_fub_cache_entry(fub_id, live_fub)  # keep the cache fresh for _restore_plan_in_place's reads
    snapshot_name = snapshot.get("plan_name", str(fub_id))
    live_name = live_fub.get("Name") if live_fub else None
    conflict = live_fub is None or live_name != snapshot_name

    if conflict and await _resolve_restore_conflict(
        hass,
        coordinator,
        api,
        fub_id,
        snapshot,
        kind,
        slot,
        live_fub,
        live_name,
        snapshot_name,
        on_conflict,
        confirm,
    ):
        return

    await _restore_plan_in_place(
        hass, coordinator, api, fub_id, snapshot, kind, slot, plan_hash, identity_was_mismatched=conflict
    )
    await _refresh_service_descriptions(hass)


async def _delete_one_snapshot(hass: HomeAssistant, coordinator: ComexioCoordinator, snapshot_raw: str) -> str | None:
    """Delete the single snapshot named by the call's 'snapshot' field.

    Returns the result message, or None on error (the notification has already been shown).
    """
    parsed = _parse_snapshot_field(str(snapshot_raw))
    if parsed is None:
        persistent_notification.async_create(
            hass, f"Invalid 'snapshot' value: '{snapshot_raw}'.", title=_TITLE_DELETE_BACKUPS_ERR
        )
        return None
    fub_id, kind, slot, plan_name_hint = parsed
    plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
    if identity_err:
        persistent_notification.async_create(hass, identity_err, title=_TITLE_DELETE_BACKUPS_ERR)
        return None
    deleted = await coordinator.function_plan_backup.async_delete_snapshot(kind, fub_id, plan_name, slot)
    if deleted:
        return f"Deleted snapshot {kind}[{slot}] for plan '{plan_name}' (fub {fub_id})."
    return f"No snapshot found at {kind}[{slot}] for plan '{plan_name}' (fub {fub_id}) — nothing deleted."


async def _delete_plan_backups_by_fub_id(
    hass: HomeAssistant, coordinator: ComexioCoordinator, fub_id_raw: str
) -> str | None:
    """Delete all snapshots of the plan named by the call's 'fub_id' field.

    Returns the result message, or None on error (the notification has already been shown).
    """
    split = _split_plan_field(str(fub_id_raw))
    if split is None:
        persistent_notification.async_create(
            hass, f"Invalid 'fub_id' value: '{fub_id_raw}'.", title=_TITLE_DELETE_BACKUPS_ERR
        )
        return None
    fub_id, plan_name_hint = split
    plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
    if identity_err:
        persistent_notification.async_create(hass, identity_err, title=_TITLE_DELETE_BACKUPS_ERR)
        return None
    count = await coordinator.function_plan_backup.async_delete_plan_backups(fub_id, plan_name)
    if count:
        return f"Deleted all {count} snapshot(s) for plan '{plan_name}' (fub {fub_id})."
    return f"No stored snapshots for plan '{plan_name}' (fub {fub_id}) — nothing deleted."


async def _handle_function_plan_delete_backups(hass: HomeAssistant, call: ServiceCall):
    """Delete stored function plan backup snapshots — one snapshot, one plan's, or all of them.

    Pick ONE of "snapshot" (deletes just that one snapshot) or "fub_id" (deletes ALL
    snapshots of that plan, auto + change). Leave both empty to wipe every stored backup
    for every plan on this instance — new ones are rebuilt automatically starting with
    the next backup cycle / next change. Local storage only, no Comexio API call needed.
    """
    ctx = await _async_get_service_context(hass, call, _TITLE_DELETE_BACKUPS_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return
    coordinator, _api, _unused_fub_id = ctx

    if not bool(call.data.get("confirm", False)):
        persistent_notification.async_create(
            hass, "Nothing deleted — 'confirm' must be enabled.", title=_TITLE_DELETE_BACKUPS_ERR
        )
        return

    snapshot_raw = call.data.get("snapshot")
    fub_id_raw = call.data.get("fub_id")

    if snapshot_raw:
        msg = await _delete_one_snapshot(hass, coordinator, snapshot_raw)
    elif fub_id_raw not in (None, ""):
        msg = await _delete_plan_backups_by_fub_id(hass, coordinator, fub_id_raw)
    else:
        count = await coordinator.function_plan_backup.async_delete_all_backups()
        msg = (
            f"Deleted ALL {count} stored snapshot(s) across ALL plans on this instance.\n"
            "Fresh backups will be created automatically starting with the next backup cycle / next change."
        )
    if msg is None:
        return

    _LOGGER.info("Function Plan Delete Backups: %s", msg)
    coordinator.async_update_listeners()  # refresh the backup-summary diagnostic sensor
    await _refresh_service_descriptions(hass)
    persistent_notification.async_create(hass, msg, title="Function Plan Delete Backups")


async def _handle_function_plan_purge_orphaned_backups(hass: HomeAssistant, call: ServiceCall):
    """Delete backup snapshots (auto + change) of plans that no longer exist live in Comexio.

    Only orphaned identities (fub_id/plan_name pairs whose plan was deleted directly in
    Comexio Studio) are ever touched, and only once their newest snapshot is older than
    the configured retention (Options → Function Plan, default 6 months) — a live plan's
    backups are never purged, no matter how old. Runs automatically on the periodic
    backup cycle too; this service is normally only needed to force an out-of-schedule
    cleanup. Local storage only, no Comexio API call needed.
    """
    ctx = await _async_get_service_context(
        hass, call, _TITLE_PURGE_ORPHANED_BACKUPS_ERR, resolve_plan=False, do_login=False
    )
    if ctx is None:
        return
    coordinator, api, _unused_fub_id = ctx

    if not bool(call.data.get("confirm", False)):
        persistent_notification.async_create(
            hass, "Nothing purged — 'confirm' must be enabled.", title=_TITLE_PURGE_ORPHANED_BACKUPS_ERR
        )
        return

    retention_months = coordinator.config_entry.options.get(
        CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS, DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS
    )
    purged = await coordinator.function_plan_backup.async_purge_orphaned(
        api.fub_data, cutoff=retention_cutoff(int(retention_months))
    )
    total = sum(p["removed"] for p in purged)
    msg = (
        f"Purged {total} snapshot(s) across {len(purged)} orphaned plan(s) (older than {retention_months} month(s))."
        if purged
        else f"No orphaned plan backups older than {retention_months} month(s) found — nothing purged."
    )

    _LOGGER.info("Function Plan Purge Orphaned Backups: %s", msg)
    coordinator.async_update_listeners()  # refresh the backup-summary diagnostic sensor
    await _refresh_service_descriptions(hass)
    persistent_notification.async_create(hass, msg, title="Function Plan Purge Orphaned Backups")


async def _handle_function_plan_list_backups(hass: HomeAssistant, call: ServiceCall) -> dict:
    """List stored function plan backup snapshots as a service response, optionally filtered."""
    ctx = await _async_get_service_context(hass, call, _TITLE_LIST_BACKUPS_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return {"error": "Comexio instance could not be resolved — see notification for details."}
    coordinator, api, _fub_id = ctx

    # Optional filters: plan (ID or name via the usual resolver), name substring, slot, age.
    # fub_id alone can be ambiguous (a reused ID may carry two identities in the backup
    # store) — that's harmless for a pure listing filter, but if the dropdown's composite
    # 'fub_id:plan_name' value is used, the name is extracted to pre-fill the separate
    # plan_name filter for free when it wasn't already set explicitly.
    plan_raw = call.data.get("fub_id")
    plan_name = call.data.get("plan_name") or None
    fub_id = None
    if plan_raw not in (None, ""):
        split = _split_plan_field(str(plan_raw))
        if split:
            fub_id, name_hint = split
            plan_name = plan_name or name_hint
        else:
            fub_id = _resolve_fub_id(str(plan_raw), api.fub_data, hass)
            if fub_id is None:
                fub_id = _coerce_int(plan_raw)  # deleted plans: accept the raw numeric ID
    slot = _coerce_int(call.data.get("slot"))
    cutoff = _age_cutoff(call.data.get("max_age"))
    kind = call.data.get("kind", "all")
    order_by = call.data.get("order_by", "newest")
    export_as_json = bool(call.data.get("export_as_json", False))
    diff = bool(call.data.get("diff", False))

    backups = await coordinator.function_plan_backup.async_list_backups()

    def _filtered(entries: list[dict]) -> list[dict]:
        matches = [e for e in entries if _backup_entry_matches(e, fub_id, plan_name, slot, cutoff)]
        return _sort_backup_entries(matches, order_by)

    auto_entries = _filtered(backups.get("auto", [])) if kind != "change" else []
    change_entries = _filtered(backups.get("change", [])) if kind != "auto" else []
    if diff:
        await _attach_backup_diffs(coordinator, auto_entries, "auto")
        await _attach_backup_diffs(coordinator, change_entries, "change")
    _LOGGER.info(
        "Function Plan Backups: %d auto / %d change listed "
        "(plan=%s name=%s slot=%s max_age=%s kind=%s order=%s diff=%s)",
        len(auto_entries),
        len(change_entries),
        plan_raw,
        plan_name,
        slot,
        call.data.get("max_age"),
        kind,
        order_by,
        diff,
    )
    result = {
        "auto_count": len(auto_entries),
        "change_count": len(change_entries),
        "auto_backups": auto_entries,
        "change_backups": change_entries,
    }
    if export_as_json:
        return {"json": json.dumps(result, indent=2, default=str)}
    return result
