# Version: 0.7.6
import contextlib
from datetime import datetime, timedelta
import functools
import json
import logging
import pathlib
import re
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.util import dt as dt_util
import yaml

from .const import (
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    CONF_IGNORED_MARKERS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DOMAIN,
    TIMESTAMP_DISPLAY_FORMAT,
    WEBIO_CLASSES,
    webio_class_name,
)
from .coordinator import ComexioCoordinator
from .function_plan_backup import diff_snapshots, retention_cutoff, snapshot_label_maps

_LOGGER = logging.getLogger(__name__)

# Canvas layout constants for Logikplan POC
_LAYOUT_X_MARKER = 7.5
_LAYOUT_X_WEBIO = 202.5
_LAYOUT_Y_START = 7.5
_LAYOUT_Y_STEP = 22.5
_LAYOUT_COLUMN_WIDTH = 450.0  # x-Abstand zwischen Spaltengruppen


_PLAN_LABEL_ID_RE = re.compile(r"\(ID (\d+)\)\s*$")


def format_plan_label(name: str, fub_id) -> str:
    """Format a plan's select-option label as "<name> (ID <fub_id>)", parseable by _resolve_fub_id()."""
    return f"{name} (ID {fub_id})"


def _resolve_fub_id(fub_id_input: str, fub_data: dict, hass=None) -> int | None:
    """Resolve fub_id from a numeric string, plan name, "<name> (ID <n>)" select label, or select entity_id."""
    stripped = str(fub_id_input).strip()
    # If it looks like an entity_id, read the select entity's current state
    if hass and "." in stripped:
        state = hass.states.get(stripped)
        if state and state.state not in ("unknown", "unavailable", ""):
            stripped = state.state
    # Select options carry "(ID <n>)" — parse it directly so duplicate plan names
    # (Comexio only guarantees fub_id is unique) can't resolve to the wrong plan.
    if match := _PLAN_LABEL_ID_RE.search(stripped):
        return int(match.group(1))
    try:
        return int(stripped)
    except ValueError:
        name_lower = stripped.lower()
        for fid, fub in fub_data.items():
            if fub.get("Name", "").lower() == name_lower:
                return int(fid)
    return None


def _available_plans_str(fub_data: dict) -> str:
    """Return human-readable list of available plans from fub_data."""
    if not fub_data:
        return "keine Pläne geladen — Integration neu laden"
    return ", ".join(
        f"'{fub.get('Name', '?')}' (ID {fid})" for fid, fub in sorted(fub_data.items(), key=lambda x: int(x[0]))
    )


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
        return captured is not None and captured >= cutoff
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


# --- Pulled forward from function_plan_render.py (Cluster 4, not yet merged) — pure label
# resolution only, no HA/Comexio-API dependencies. Re-import from there instead once that
# module lands.
def _named_ref_label(by_id: dict, ref_id: str, fallback: str) -> str:
    """Label of a marker/IO/WebIO reference: the entity's HA display name or a typed fallback."""
    entry = by_id.get(ref_id)
    return entry["name"] if entry else fallback


_TIME_MODULE_KIND_LABELS = {
    "on_delayed": "Einschaltverzögert",
    "off_delayed": "Ausschaltverzögert",
    "on_pulse": "Einschaltwischend",
}
_TIME_MODULE_KINDS_WITHOUT_T2 = frozenset(_TIME_MODULE_KIND_LABELS)


def _fmt_value(value: Any) -> str:
    """Live value display like Studio: compact number, German decimal comma."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _time_module_tooltip(catalog: dict[str, Any], ref_id: str) -> str:
    entry = catalog.get("time_modules", {}).get(ref_id) or {}
    name = entry.get("name") or "Zeitglied"
    kind = entry.get("kind")
    lines = [f"T{ref_id} {name}"]
    if kind:
        lines.append(f"Typ: {_TIME_MODULE_KIND_LABELS.get(kind, kind)}")
    if (t1 := entry.get("t1")) is not None:
        lines.append(f"t1: {_fmt_value(t1 / 10)} Sek.")
    if kind not in _TIME_MODULE_KINDS_WITHOUT_T2 and (t2 := entry.get("t2")) is not None:
        lines.append(f"t2: {_fmt_value(t2 / 10)} Sek.")
    return "\n".join(lines)


def _block_label(catalog: dict[str, Any], ref_id: str, elem_name: str) -> str:
    fub_base = catalog.get("fub_base", {}).get(ref_id) or {}
    block = fub_base.get("short_name") or fub_base.get("display_name") or fub_base.get("name")
    if block and elem_name:
        return f"{block}: {elem_name}"
    return block or elem_name or f"Baustein ref={ref_id}"


def resolve_element_label(
    elem: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> str:
    """Human-readable label for one plan element, matching the Function Plan preview's naming.

    Trimmed copy (no calendar-function/sun-times branch — that needs coordinator sun-time
    data not available to the backup-diff caller) of the full resolver that will live in
    function_plan_render.py (Cluster 4).
    """
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    elem_name = (elem.get("name") or "").strip()
    named_by_type: dict[Any, tuple[dict, str]] = {
        2: (markers_by_id, f"M{ref_id} (unknown)"),
        10: (webio_by_id, f"WebIO ref={ref_id}"),
        1: (ios_by_id, f"IO ref={ref_id}"),
    }
    if named := named_by_type.get(etype):
        return _named_ref_label(named[0], ref_id, named[1])
    if etype == 4:
        return _time_module_tooltip(catalog, ref_id)
    if etype == 16:
        return elem_name or "?"
    if etype == 14:
        return elem_name or "(Kommentar)"
    if etype == 5:
        return _block_label(catalog, ref_id, elem_name)
    type_name = catalog.get("fub_types", {}).get(str(etype)) or f"Typ{etype}"
    return f"{type_name} {elem_name}".strip() if elem_name else f"{type_name} ref={ref_id}"


def _label_backup_identity(
    identity: tuple, catalog: dict, markers_by_id: dict, webio_by_id: dict, ios_by_id: dict
) -> str:
    """Human-readable label for one diff_snapshots() element/endpoint identity tuple.

    identity is (type, ref_id) for types with a globally stable reference (marker/IO/WebIO/
    time module — see function_plan_backup._STABLE_REF_TYPES), or (type, x, y, ref_id, name) as a
    position-based fallback for everything else (blocks/constants/comments have no stable
    cross-snapshot ID).
    """
    etype = identity[0]
    ref_id, name = (identity[1], "") if len(identity) == 2 else (identity[3], identity[4])
    elem = {"reference": {"type": etype, "ref_id": ref_id}, "name": name}
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


_MULTI_INSTANCE_MSG = "Multiple Comexio instances — please specify `config_entry`."
_LOGIN_FAILED_MSG = "Comexio admin login failed."
_INSTANCE_NOT_FOUND_LOG = "Comexio instance %s not found in hass.data"

_TITLE_RESTORE_ERR = "Function Plan Restore — Error"
_TITLE_LIST_BACKUPS_ERR = "Function Plan Backups — Error"
_TITLE_DELETE_BACKUPS_ERR = "Function Plan Delete Backups — Error"
_TITLE_PURGE_ORPHANED_BACKUPS_ERR = "Function Plan Purge Orphaned Backups — Error"


async def _async_get_service_context(
    hass: HomeAssistant,
    call: ServiceCall,
    error_title: str,
    *,
    resolve_plan: bool = True,
    do_login: bool = True,
) -> tuple[ComexioCoordinator, Any, int | None] | None:
    """Resolve coordinator, api and (optionally) target fub_id for a function plan backup
    service call. Posts an English error notification and returns None on any failure.

    Sibling of _resolve_logikplan_plan (kept separate rather than extended in place) since the
    backup services need to resolve WITHOUT requiring a live plan/admin login — a deleted
    plan's stored backups are still listable/restorable, and pure local-storage operations
    (delete/purge) never need a Comexio session at all.
    """
    domain_data = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("config_entry")
    if not entry_id:
        entries = [k for k, v in domain_data.items() if isinstance(v, ComexioCoordinator)]
        if len(entries) != 1:
            persistent_notification.async_create(hass, _MULTI_INSTANCE_MSG, title=error_title)
            return None
        entry_id = entries[0]
    coordinator = domain_data.get(entry_id)
    if not isinstance(coordinator, ComexioCoordinator):
        _LOGGER.error(_INSTANCE_NOT_FOUND_LOG, entry_id)
        return None
    api = coordinator.api

    fub_id: int | None = None
    if resolve_plan:
        explicit_fub_id = call.data.get("fub_id")
        fub_id_raw = explicit_fub_id or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api.fub_data, hass)
        if fub_id is None:
            reason = (
                f"Plan '{fub_id_raw}' not found."
                if explicit_fub_id
                else "No plan selected — the 'Function Plans' selector is empty. "
                "Please specify the plan (fub_id) explicitly."
            )
            persistent_notification.async_create(
                hass,
                f"{reason}\nAvailable: {_available_plans_str(api.fub_data)}",
                title=error_title,
            )
            return None

    if do_login and not await api.login():
        persistent_notification.async_create(hass, _LOGIN_FAILED_MSG, title=error_title)
        return None

    return coordinator, api, fub_id


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


async def _resolve_backup_identity(
    coordinator: ComexioCoordinator, fub_id: int, plan_name_hint: str | None
) -> tuple[str | None, str | None]:
    """Resolve the (fub_id, plan_name) identity for a backup action. Returns (plan_name, error).

    fub_id alone is not a stable identity — Comexio reuses IDs after deletion, so two
    different plans can share one fub_id in the backup store. plan_name_hint short-circuits
    the lookup when already known precisely (composite dropdown value, or a currently-live
    plan's own name). Otherwise looks up stored identities for that fub_id: exactly one
    resolves automatically; none is an error; two or more (a reused ID with more than one
    identity still on record) is ambiguous and asks the caller to reselect from the dropdown.
    """
    if plan_name_hint is not None:
        return plan_name_hint, None
    identities = [
        (name, count)
        for fid, name, count in await coordinator.function_plan_backup.async_backed_up_plans()
        if fid == fub_id
    ]
    if len(identities) == 1:
        return identities[0][0], None
    if not identities:
        return None, f"No stored backups for plan {fub_id}."
    names = ", ".join(f"'{name}'" for name, _ in identities)
    return None, (
        f"fub_id {fub_id} is ambiguous — {len(identities)} different plan identities share this reused ID in "
        f"the backup store ({names}). Please reselect from the Plan/Snapshot dropdown."
    )


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
    raw = str(call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector")
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


def _parse_snapshot_field(raw: str) -> tuple[int, str, int, str | None] | None:
    """Parse the combined snapshot picker field value, or None if malformed.

    Accepts the current 'fub_id:kind:slot:plan_name' value as well as the legacy 3-part
    'fub_id:kind:slot' (plan_name unknown — caller must disambiguate via
    _resolve_backup_identity).
    """
    parts = raw.split(":", 3)
    if len(parts) not in (3, 4):
        return None
    fub_id_str, kind, slot_str = parts[0], parts[1], parts[2]
    plan_name = parts[3] if len(parts) == 4 else None
    try:
        return int(fub_id_str), kind, int(slot_str), plan_name
    except ValueError:
        return None


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

    # Safety net: snapshot the CURRENT state before restoring, so the restore itself is undoable
    await coordinator.async_function_plan_change_backup(fub_id, f"pre_restore {kind}[{slot}]")

    apply = await _restore_apply_snapshot(api, fub_id, snapshot, live_name, plan_name, was_active)
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

    msg = _restore_build_message(
        plan_name, fub_id, kind, slot, snapshot, status, apply, verify, identity_was_mismatched, was_active, duration
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
    persistent_notification.async_create(hass, msg, title=f"Function Plan Restore — {status}")


async def _restore_apply_snapshot(
    api, fub_id: int, snapshot: dict, live_name: str, plan_name: str, was_active: bool
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

    await api.logikplan_stop_fup(fub_id)

    # Restore element positions via saveelementspos (known-good mechanism from sort)
    positions = [
        (int(elem_id), elem.get("position_x", 0.0), elem.get("position_y", 0.0))
        for elem_id, elem in snapshot.get("elements", {}).items()
    ]
    pos_ok = await api.logikplan_save_elements_pos(positions) if positions else True

    # Restore structure + activate via run_fup with the snapshot payload
    run_ok = await api.logikplan_run_fup(fub_id, plan_data=snapshot)

    # Preserve previous inactive state
    if run_ok and not was_active:
        await api.logikplan_stop_fup(fub_id)

    return {
        "paper": paper,
        "dpi": dpi,
        "orientation": orientation,
        "properties_changed": properties_changed,
        "paper_ok": paper_ok,
        "pos_ok": pos_ok,
        "run_ok": run_ok,
    }


async def _restore_verify(api, fub_id: int, snapshot: dict, plan_hash) -> dict:
    """Reload the plan after a restore and compare it against the snapshot (hash + counts)."""
    fresh = await api.logikplan_load_elements(fub_id)
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
    }


def _restore_status(content_ok: bool, apply: dict, verify: dict) -> str:
    """Overall OK/PARTIAL/FAILED verdict for a restore (see _restore_plan_in_place)."""
    if content_ok and apply["paper_ok"]:
        return "OK"
    if apply["run_ok"] or apply["pos_ok"] or apply["paper_ok"] or verify["counts_match"]:
        return "PARTIAL"
    return "FAILED"


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
) -> str:
    """Human-readable persistent_notification body for a restore result."""
    reactivation_note = ""
    if apply["run_ok"]:
        run_label = "✅"
    elif was_active:
        run_label = "❌"
        reactivation_note = "\n⚠️ Plan could not be re-activated — please check/activate it in Comexio."
    else:
        # Data payload was applied; the server merely skipped activating an inactive plan.
        run_label = "➖ (plan inactive — activation skipped)"
    paper_line = ""
    if apply["properties_changed"]:
        paper_line = (
            f"Aligned with snapshot: name '{plan_name}', paper {apply['paper']} @ {apply['dpi']} DPI, "
            f"{apply['orientation']} — {'✅' if apply['paper_ok'] else '❌ failed, restore may look wrong'}\n"
        )
    if identity_was_mismatched:
        content_line = (
            f"Content match: {'✅' if verify['counts_match'] else '❌'} "
            "(element IDs are always reassigned when overriding a different plan — "
            "matching counts is what matters here, not a byte-for-byte hash)\n"
        )
    else:
        content_line = f"Hash match: {'✅' if verify['hash_match'] else '❌'} | "
    return (
        f"Restore of plan '{plan_name}' (ID {fub_id}) from {kind}[{slot}] "
        f"({snapshot.get('captured_at')}): **{status}**\n"
        f"Snapshot: {verify['elem_count']} elements / {verify['conn_count']} connections → "
        f"now: {verify['fresh_elem']} / {verify['fresh_conn']}\n"
        f"{paper_line}"
        f"{content_line}positions: {'✅' if apply['pos_ok'] else '❌'} | "
        f"run_fup: {run_label}"
        f"{reactivation_note}\n"
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
        )
        return

    elements_created, connections_created, warnings = await api.function_plan_rebuild_plan_from_snapshot(
        new_fub_id, snapshot
    )
    # create_fup always creates plans inactive (fub_active="0"); run_fup's very first call
    # therefore routinely reports result=False even though the data payload IS applied — the
    # same Comexio quirk documented for the in-place restore path. run_ok is NOT a success
    # criterion here; the recreated/expected element+connection counts are.
    run_ok = await api.logikplan_run_fup(new_fub_id)
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
        "Activation: ✅ active"
        if run_ok
        else "Activation: ➖ not active yet (Comexio always creates new plans inactive — "
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
        "Comment text (if any) was reset to the managed-plan default — original wording isn't recoverable.\n"
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
    persistent_notification.async_create(hass, msg, title=f"Function Plan Restore — {status}")


def _build_sorted_pairs(
    elements: dict,
    connections: dict,
) -> tuple[list[tuple[int, int, int]], list[int]]:
    """Return marker→WebIO pairs sorted by marker ref_id and a list of orphan element IDs."""
    elem_ref: dict[int, dict] = {
        int(eid): {
            "type": e.get("reference", {}).get("type"),
            "ref_id": e.get("reference", {}).get("ref_id"),
        }
        for eid, e in elements.items()
    }
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int, int]] = []  # (marker_ref_id, marker_elem_id, webio_elem_id)
    for conn in connections.values():
        inp_eid_raw = conn.get("input", {}).get("FubElementId")
        if inp_eid_raw is None:
            continue
        inp_eid = int(inp_eid_raw)
        if elem_ref.get(inp_eid, {}).get("type") != 2:
            continue
        marker_ref_id = int(elem_ref[inp_eid].get("ref_id", 0))
        for out in conn.get("output", []):
            out_eid_raw = out.get("FubElementId")
            if out_eid_raw is None:
                continue
            out_eid = int(out_eid_raw)
            if (inp_eid, out_eid) not in seen:
                seen.add((inp_eid, out_eid))
                pairs.append((marker_ref_id, inp_eid, out_eid))
    pairs.sort(key=lambda p: p[0])
    paired: set[int] = {eid for _, m, w in pairs for eid in (m, w)}
    orphans = [int(eid) for eid in elements if int(eid) not in paired]
    return pairs, orphans


def _get_occupied_grid_slots(
    elements: dict,
    rows_per_col: int,
    max_cols: int,
) -> set[tuple[int, int]]:
    """Collect all occupied (col, row) grid slots from existing elements."""
    occupied: set[tuple[int, int]] = set()
    for elem in elements.values():
        y = elem.get("position_y", 0.0)
        if y >= _LAYOUT_Y_START:
            row_in_col = round((y - _LAYOUT_Y_START) / _LAYOUT_Y_STEP)
            x = elem.get("position_x", 0.0)
            col = round((x - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH)
            if 0 <= col < max_cols and 0 <= row_in_col < rows_per_col:
                occupied.add((col, row_in_col))
    return occupied


def _find_first_free_grid_position(
    occupied: set[tuple[int, int]],
    rows_per_col: int,
    max_cols: int,
) -> tuple[int, int] | None:
    """Find the first free (col, row) position, scanning left-to-right, top-to-bottom."""
    for col in range(max_cols):
        for row in range(rows_per_col):
            if (col, row) not in occupied:
                return (col, row)
    return None


def _assign_grid_positions(
    pairs: list[tuple[int, int, int]],
    orphans: list[int],
    rows_per_col: int,
    max_cols: int,
) -> list[tuple[int, float, float]]:
    """Calculate exact grid positions for sorted pairs and orphan elements."""
    positions: dict[int, tuple[float, float]] = {}
    pairs_placed = 0
    for row_idx, (_, m_eid, w_eid) in enumerate(pairs):
        col = row_idx // rows_per_col
        if col >= max_cols:
            break
        row_in_col = row_idx % rows_per_col
        y = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
        if m_eid not in positions:
            positions[m_eid] = (_LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH, y)
        if w_eid not in positions:
            positions[w_eid] = (_LAYOUT_X_WEBIO + col * _LAYOUT_COLUMN_WIDTH, y)
        pairs_placed += 1
    for i, eid in enumerate(orphans):
        row_idx = pairs_placed + i
        col = row_idx // rows_per_col
        if col >= max_cols:
            _LOGGER.warning(
                "Grid layout full (%d cols × %d rows): %d orphan element(s) left unsorted",
                max_cols,
                rows_per_col,
                len(orphans) - i,
            )
            break
        row_in_col = row_idx % rows_per_col
        y = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
        positions[eid] = (_LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH, y)
    return [(eid, x, y) for eid, (x, y) in positions.items()]


_LOGIKPLAN_SERVICES = (
    "logikplan_connect_poc",
    "logikplan_sort",
    "logikplan_stop",
    "logikplan_activate",
    "logikplan_visualize",
)
_SERVICES_YAML_PATH = pathlib.Path(__file__).parent / "services.yaml"


def _rewrite_services_yaml_plans(plan_options: list[str]) -> None:
    """Blocking read/modify/write of services.yaml; run via executor job only.

    services.yaml is rewritten (rather than deriving fub_id options purely at runtime) because HA's
    service-call schema is static: the Developer Tools "Call Service" UI resolves a field's selector
    from the YAML at service-registration time, and selectors don't support a per-call dynamic option
    list bound to live coordinator data. The select entity (select.py) is the live source of truth;
    this rewrite just keeps the YAML-declared dropdown in sync with it whenever the plan set changes.
    """
    try:
        content = yaml.safe_load(_SERVICES_YAML_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOGGER.warning("services.yaml missing or invalid, skipping plan option rewrite: %s", exc)
        return
    for svc in _LOGIKPLAN_SERVICES:
        fub_field = content.get(svc, {}).get("fields", {}).get("fub_id")
        if fub_field:
            fub_field["selector"] = {"select": {"options": plan_options, "custom_value": True}}
    _SERVICES_YAML_PATH.write_text(
        yaml.dump(content, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


async def _update_services_yaml_plans(hass: HomeAssistant) -> None:
    """Rewrite fub_id select options in services.yaml with current plan labels from all active coordinators.

    Labels use the same "<name> (ID <fid>)" format as the select entity, so duplicate
    plan names across coordinators still resolve unambiguously via _resolve_fub_id().
    """
    from .coordinator import ComexioCoordinator

    plan_labels: set[str] = set()
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, ComexioCoordinator):
            for fub_id, fub in coordinator.api.fub_data.items():
                name = fub.get("Name", "")
                if name:
                    plan_labels.add(format_plan_label(name, fub_id))

    if not plan_labels:
        _LOGGER.debug("_update_services_yaml_plans: no plans available, skipping")
        return

    plan_options = sorted(plan_labels, key=str.lower)
    try:
        await hass.async_add_executor_job(_rewrite_services_yaml_plans, plan_options)
        _LOGGER.debug("Updated services.yaml: %d Logikplan plan options (labels) written", len(plan_options))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not update services.yaml with plan labels: %s", exc)


def _resolve_logikplan_plan(hass: HomeAssistant, call: ServiceCall, error_title: str) -> tuple[Any, Any, int] | None:
    """Resolve (coordinator, api, fub_id) for a Logikplan service call — without logging in.

    Handles config_entry auto-resolution (when only one Comexio instance exists) and fub_id
    resolution via `_resolve_fub_id`, notifying the user and returning None on any failure.
    """
    domain_data = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("config_entry")
    if not entry_id:
        entries = list(domain_data.keys())
        if len(entries) != 1:
            _LOGGER.error("config_entry required when multiple Comexio instances exist: %s", entries)
            persistent_notification.async_create(
                hass, "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.", title=error_title
            )
            return None
        entry_id = entries[0]
    if entry_id not in domain_data:
        _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
        return None

    coordinator = domain_data[entry_id]
    api = coordinator.api
    fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
    fub_id = _resolve_fub_id(str(fub_id_raw), api.fub_data, hass)
    if fub_id is None:
        persistent_notification.async_create(
            hass,
            f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api.fub_data)}",
            title=error_title,
        )
        return None

    return coordinator, api, fub_id


async def _ensure_comexio_login(hass: HomeAssistant, api, error_title: str) -> bool:
    """Log in to the Comexio admin session, notifying the user on failure."""
    if await api.login():
        return True
    persistent_notification.async_create(hass, "Comexio Admin-Login fehlgeschlagen.", title=error_title)
    return False


async def _resolve_logikplan_context(
    hass: HomeAssistant, call: ServiceCall, error_title: str
) -> tuple[Any, Any, int] | None:
    """Resolve (coordinator, api, fub_id) for a Logikplan service call and ensure the admin session is logged in."""
    ctx = _resolve_logikplan_plan(hass, call, error_title)
    if ctx is None:
        return None
    _coordinator, api, _fub_id = ctx
    if not await _ensure_comexio_login(hass, api, error_title):
        return None
    return ctx


def _plan_activation_note(was_active: bool, activated: bool, has_changes: bool, fub_id: int) -> str:
    """Build the German status note describing what happened to plan activation after a sync/sort action."""
    if not was_active:
        if has_changes:
            return "Plan war inaktiv — Änderungen gespeichert, Plan bleibt inaktiv."
        return f"Plan fub_id={fub_id} — keine neuen Verbindungen, Plan unverändert."
    if activated:
        return "Plan gespeichert und aktiviert." if has_changes else "Plan unverändert, weiterhin aktiv."
    return "Plan-Aktivierung fehlgeschlagen — bitte manuell im Comexio-UI speichern."


def _get_canvas_grid_dims(api, fub_id: int, canvas_format_raw: str) -> tuple[str, float, float, int, int]:
    """Resolve canvas paper format + pixel bounds, then derive the (rows_per_col, max_cols) layout grid."""
    if canvas_format_raw and canvas_format_raw != "AUTO":
        canvas_label = canvas_format_raw
        x_max, y_max = api.get_fub_canvas_bounds(fub_id, paper_name=canvas_format_raw)
    else:
        canvas_label = api.get_fub_paper_format(fub_id).upper()
        x_max, y_max = api.get_fub_canvas_bounds(fub_id)

    rows_per_col = max(1, int((y_max - _LAYOUT_Y_START) / _LAYOUT_Y_STEP))
    max_cols = max(1, round((x_max - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH))
    return canvas_label, x_max, y_max, rows_per_col, max_cols


def _parse_ignored_marker_ids(conf: dict) -> set[int]:
    """Parse the ignored_markers option string into a set of marker IDs (invalid tokens are skipped)."""
    ignored_raw = conf.get(CONF_IGNORED_MARKERS, "").strip()
    ignored_ids: set[int] = set()
    if ignored_raw:
        for token in ignored_raw.replace(";", ",").split(","):
            stripped = token.strip()
            if stripped:
                with contextlib.suppress(ValueError):
                    ignored_ids.add(int(stripped))
    return ignored_ids


def _resolve_requested_marker_ids(
    raw_input: str, all_markers: bool, markers_by_id: dict, ignored_ids: set[int]
) -> tuple[list[int], list[str]]:
    """Resolve the requested marker IDs from the service call's `marker_id`/`all_markers` input.

    Returns (marker_ids, invalid_tokens); invalid_tokens is only ever non-empty for the explicit-list path.
    """
    if all_markers or raw_input == "*":
        return [mid for mid in markers_by_id if mid not in ignored_ids], []

    raw_ids = [tok.strip().lstrip("Mm") for tok in raw_input.split(",") if tok.strip()]
    marker_ids: list[int] = []
    invalid_tokens: list[str] = []
    for raw_id in raw_ids:
        try:
            mid = int(raw_id)
        except ValueError:
            invalid_tokens.append(raw_id)
            continue
        if mid not in ignored_ids:
            marker_ids.append(mid)
    return marker_ids, invalid_tokens


async def _load_connect_poc_topology(
    api, fub_id: int, rows_per_col: int, max_cols: int
) -> tuple[dict[tuple[int, int], int], set[tuple[int, int]], set[tuple[int, int]]]:
    """Load current plan elements/connections and derive existing-element refs, wired pairs, occupied grid slots."""
    plan_data = await api.logikplan_load_elements(fub_id)
    existing_by_ref: dict[tuple[int, int], int] = {}
    connected_pairs: set[tuple[int, int]] = set()
    occupied_slots: set[tuple[int, int]] = set()

    if not plan_data:
        _LOGGER.warning("Logikplan POC: loadelements fehlgeschlagen, fahre ohne Plan-Zustand fort")
        return existing_by_ref, connected_pairs, occupied_slots

    for elem_id_str, elem in plan_data.get("elements", {}).items():
        ref = elem.get("reference", {})
        ref_type, ref_id = ref.get("type"), ref.get("ref_id")
        if ref_type is not None and ref_id is not None:
            existing_by_ref[(int(ref_type), int(ref_id))] = int(elem_id_str)
    for conn in plan_data.get("connections", {}).values():
        inp = conn.get("input", {})
        for out in conn.get("output", []):
            inp_id, out_id = inp.get("FubElementId"), out.get("FubElementId")
            if inp_id is not None and out_id is not None:
                connected_pairs.add((int(inp_id), int(out_id)))
    occupied_slots = _get_occupied_grid_slots(plan_data.get("elements", {}), rows_per_col, max_cols)
    _LOGGER.info("Logikplan POC: Plan fub=%s — %d belegte Grid-Slots gefunden", fub_id, len(occupied_slots))
    return existing_by_ref, connected_pairs, occupied_slots


async def _get_or_create_marker_element(
    api, fub_id: int, marker_id: int, existing_marker_elem: int | None, x: float, y: float
) -> tuple[int | None, str | None]:
    """Reuse an existing canvas marker element, or create a new one. Returns (elem_id, error)."""
    if existing_marker_elem:
        _LOGGER.info(
            "Logikplan POC: M%s — Merker-Element bereits vorhanden: elem_id=%s", marker_id, existing_marker_elem
        )
        return existing_marker_elem, None

    _LOGGER.info("Logikplan POC: M%s → add_element (Merker, x=%.1f, y=%.1f)", marker_id, x, y)
    elem_marker = await api.logikplan_add_element(fub_id=fub_id, ref_id=int(marker_id), element_type=2, x=x, y=y)
    if elem_marker is None:
        return None, f"M{marker_id}: add_element (Merker) fehlgeschlagen"
    _LOGGER.info("Logikplan POC: M%s — Merker-Element angelegt: elem_id=%s", marker_id, elem_marker)
    return elem_marker, None


async def _get_or_create_webio_element(
    api,
    fub_id: int,
    marker_id: int,
    elem_marker: int,
    web_ref_id,
    conn_type: str,
    existing_webio_elem: int | None,
    x: float,
    y: float,
) -> tuple[int | None, str | None]:
    """Reuse an existing canvas WebIO element (wiring it up if needed), or create + connect a new one."""
    if existing_webio_elem:
        _LOGGER.info("Logikplan POC: M%s — WebIO-Element bereits vorhanden: elem_id=%s", marker_id, existing_webio_elem)
        # Reused elements aren't connected yet (already_connected would have skipped this
        # marker otherwise), so the wire has to be drawn explicitly here.
        conn_id = await api.logikplan_save_connection(fub_id, elem_marker, existing_webio_elem, conn_type)
        if conn_id is None:
            return None, f"M{marker_id}: save_connection (elem {elem_marker}→{existing_webio_elem}) fehlgeschlagen"
        _LOGGER.info(
            "Logikplan POC: M%s — Verbindung nachgezogen: elem %s→%s (conn_id=%s)",
            marker_id,
            elem_marker,
            existing_webio_elem,
            conn_id,
        )
        return existing_webio_elem, None

    _LOGGER.info("Logikplan POC: M%s → add_element+connect (WebIO, x=%.1f, y=%.1f)", marker_id, x, y)
    conn_payload = {
        "0": {
            "id": "new",
            "fub_id": fub_id,
            "type": conn_type,
            "input": {"element": str(elem_marker), "pos": "0", "inverted": False},
            "output": {"0": {"element": "new", "pos": "0", "inverted": False}},
        }
    }
    elem_webio = await api.logikplan_add_element(
        fub_id=fub_id,
        ref_id=int(web_ref_id),
        element_type=10,
        x=x,
        y=y,
        connection=conn_payload,
    )
    if elem_webio is None:
        return None, f"M{marker_id}: add_element (WebIO, webIoId={web_ref_id}) fehlgeschlagen"
    _LOGGER.info("Logikplan POC: M%s — WebIO+Verbindung angelegt: elem_id=%s", marker_id, elem_webio)
    return elem_webio, None


async def _connect_marker_to_webio(
    api,
    fub_id: int,
    marker_id: int,
    marker: dict | None,
    webio_commands: dict,
    existing_by_ref: dict[tuple[int, int], int],
    connected_pairs: set[tuple[int, int]],
    occupied_slots: set[tuple[int, int]],
    rows_per_col: int,
    max_cols: int,
    canvas_format: str,
) -> tuple[str | None, str | None, str | None]:
    """Connect a single marker to its WebIO command on the canvas.

    Returns (result, skip, error) — exactly one is set, the other two are None.
    Mutates `occupied_slots` in place when a new grid slot is claimed.
    """
    if not marker:
        _LOGGER.warning("Logikplan POC: M%s nicht gefunden", marker_id)
        return None, None, f"M{marker_id}: nicht in Koordinator-Daten"

    expected_cmd_name = f"HA {marker['name']}"
    webio_cmd = webio_commands.get(expected_cmd_name)
    if not webio_cmd:
        _LOGGER.warning("Logikplan POC: M%s — WebIO '%s' nicht gefunden", marker_id, expected_cmd_name)
        return None, None, f"M{marker_id}: WebIO '{expected_cmd_name}' nicht gefunden"

    # ref_id for type=10 (WebIO) is the local FubModules dict-key (webIoId), not cmdId
    web_ref_id = webio_cmd.get("webIoId")
    if web_ref_id is None:
        return None, None, f"M{marker_id}: WebIO '{expected_cmd_name}' hat keine webIoId"

    conn_type = "binary" if marker["type"] == "digital" else "analog"

    # Reuse existing elements on canvas (avoid duplicates)
    existing_marker_elem = existing_by_ref.get((2, int(marker_id)))
    existing_webio_elem = existing_by_ref.get((10, int(web_ref_id)))

    # Skip if already connected in this specific plan
    already_connected = (
        existing_marker_elem and existing_webio_elem and (existing_marker_elem, existing_webio_elem) in connected_pairs
    )
    if already_connected:
        _LOGGER.info("Logikplan POC: M%s — bereits in Plan verbunden, übersprungen", marker_id)
        return (
            None,
            f"M{marker_id} ({marker['name']}): bereits in Plan {fub_id} verbunden"
            f" (elem {existing_marker_elem}→{existing_webio_elem})",
            None,
        )

    # Find first free grid slot, update occupied_slots
    free_pos = _find_first_free_grid_position(occupied_slots, rows_per_col, max_cols)
    if free_pos is None:
        _LOGGER.warning("Logikplan POC: Canvas %s voll bei M%s", canvas_format, marker_id)
        return None, None, f"M{marker_id}: Canvas {canvas_format} voll ({max_cols} Spalten × {rows_per_col} Zeilen)"

    col, row_in_col = free_pos
    occupied_slots.add((col, row_in_col))
    y_new = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
    x_marker_cur = _LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH
    x_webio_cur = _LAYOUT_X_WEBIO + col * _LAYOUT_COLUMN_WIDTH
    if row_in_col == 0 and col > 0:
        _LOGGER.info("Logikplan POC: Spalte %d beginnt bei x=%.1f", col, x_marker_cur)

    elem_marker, err = await _get_or_create_marker_element(
        api, fub_id, marker_id, existing_marker_elem, x_marker_cur, y_new
    )
    if err:
        return None, None, err

    elem_webio, err = await _get_or_create_webio_element(
        api, fub_id, marker_id, elem_marker, web_ref_id, conn_type, existing_webio_elem, x_webio_cur, y_new
    )
    if err:
        return None, None, err

    result = (
        f"M{marker_id} ({marker['name']}) → elem={elem_marker} | "
        f"WebIO webIoId={web_ref_id} → elem={elem_webio} ({conn_type})"
    )
    return result, None, None


def _build_connect_poc_summary(
    fub_id: int,
    results: list[str],
    skipped: list[str],
    errors: list[str],
    duration: float,
    was_active: bool,
    activated: bool,
) -> tuple[str, str]:
    """Build the final notification message + title for a logikplan_connect_poc run."""
    lines: list[str] = []
    if results:
        lines += [f"**{len(results)} verbunden:**"] + [f"- {r}" for r in results]
    if skipped:
        lines += [f"\n**{len(skipped)} bereits verbunden (übersprungen):**"] + [f"- {s}" for s in skipped]
    if errors:
        lines += [f"\n**{len(errors)} Fehler:**"] + [f"- {e}" for e in errors]

    act_note = _plan_activation_note(was_active, activated, has_changes=bool(results), fub_id=fub_id)
    lines.append(f"\n{act_note}")
    lines.append(f"Dauer: {duration:.1f}s")

    title = f"Logikplan POC — {len(results)} OK / {len(skipped)} Skip / {len(errors)} Fehler"
    return "\n".join(lines), title


def _element_label(elem_id: str | int, elements: dict, markers_by_id: dict, webio_by_id: dict) -> str:
    """Human-readable label for a Logikplan element (marker name, WebIO command name, or type/ref fallback)."""
    ref = elements.get(str(elem_id), {}).get("reference", {})
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    if etype == 2:
        marker = markers_by_id.get(ref_id)
        return f"M{ref_id} {marker['name']}" if marker else f"M{ref_id} (unbekannt)"
    if etype == 10:
        return webio_by_id.get(ref_id, f"WebIO ref={ref_id}")
    return f"Typ{etype} ref={ref_id}"


def _build_visualize_lines(
    elements: dict, connections: dict, markers_by_id: dict, webio_by_id: dict
) -> tuple[list[str], list[str]]:
    """Build the connection lines and orphan-element lines for the Logikplan visualize text diagram."""
    connected_elem_ids: set[str] = set()
    conn_lines: list[str] = []
    for conn in sorted(connections.values(), key=lambda c: c.get("input", {}).get("FubElementId", 0)):
        inp = conn.get("input", {})
        inp_id = str(inp.get("FubElementId", "?"))
        inv_in = " ¬" if inp.get("Inverted") else ""
        conn_type = conn.get("type", "?")
        connected_elem_ids.add(inp_id)
        out_parts: list[str] = []
        for out in conn.get("output", []):
            out_id = str(out.get("FubElementId", "?"))
            inv_out = " ¬" if out.get("Inverted") else ""
            out_parts.append(f"{_element_label(out_id, elements, markers_by_id, webio_by_id)}{inv_out}")
            connected_elem_ids.add(out_id)
        conn_lines.append(
            f"  {_element_label(inp_id, elements, markers_by_id, webio_by_id)}{inv_in} "
            f"→[{conn_type}]→ {', '.join(out_parts)}"
        )

    orphan_lines: list[str] = []
    for elem_id, elem in sorted(
        elements.items(), key=lambda kv: (kv[1].get("position_x", 0), kv[1].get("position_y", 0))
    ):
        if elem_id not in connected_elem_ids:
            x, y = elem.get("position_x", 0), elem.get("position_y", 0)
            label = _element_label(elem_id, elements, markers_by_id, webio_by_id)
            orphan_lines.append(f"  {label} (@ {x:.0f},{y:.0f})")

    return conn_lines, orphan_lines


async def handle_generate_web_io(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service to preview or upload the Web-IO configuration."""
    entry_id = call.data.get("config_entry")

    if entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
        return

    coordinator = hass.data[DOMAIN][entry_id]
    api = coordinator.api
    server_id = coordinator.server_id
    do_upload = call.data.get("upload", False)

    try:
        conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
        webio_name = conf.get("webio_name", "HomeAssistant")

        if not do_upload:
            preview_parts = []
            for cls in WEBIO_CLASSES:
                class_name = webio_class_name(webio_name, cls)
                web_io_json = api.generate_webio_json(server_id, class_name, coordinator.data, webio_class=cls)
                preview_parts.append(f"**{class_name}**\n```json\n{web_io_json}\n```")
            persistent_notification.async_create(
                hass, "\n\n".join(preview_parts), title=f"Comexio Preview ({server_id})"
            )
            return

        results: list[str] = []
        for cls in WEBIO_CLASSES:
            class_name = webio_class_name(webio_name, cls)
            web_io_json = api.generate_webio_json(server_id, class_name, coordinator.data, webio_class=cls)

            base_info = await api.get_webio_base_info(class_name)
            if base_info:
                base_id, deletable = base_info
                if deletable:
                    _LOGGER.info("Base class '%s' is deletable, performing clean reinstall.", class_name)
                    await api.delete_webio_base(base_id)
                else:
                    results.append(
                        f"{class_name}: skipped — in use by Comexio logic, use the Smart-Sync button instead"
                    )
                    continue

            success, result_val = await api.upload_web_io(server_id, class_name, web_io_json)
            results.append(
                f"{class_name}: Base-ID {result_val}" if success else f"{class_name}: Upload failed: {result_val}"
            )

        persistent_notification.async_create(
            hass, "\n".join(results) or "Nothing to do.", title=f"Comexio Sync ({server_id})"
        )

    except Exception as e:
        _LOGGER.exception("Error in Comexio service: %s", e)


async def handle_logikplan_connect_poc(hass: HomeAssistant, call: ServiceCall) -> None:
    """POC: Connect markers (comma-separated list or all) to their WebIO commands."""
    error_title = "Logikplan POC — Fehler"
    ctx = _resolve_logikplan_plan(hass, call, error_title)
    if ctx is None:
        return
    coordinator, api, fub_id = ctx

    all_markers = call.data.get("all_markers", False)
    raw_input = str(call.data.get("marker_id", "2")).strip()
    markers_by_id = {int(m["id"]): m for m in coordinator.data.get("markers", [])}
    webio_commands = coordinator.data.get("webio_commands", {})

    conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
    ignored_ids = _parse_ignored_marker_ids(conf)
    marker_ids, invalid_tokens = _resolve_requested_marker_ids(raw_input, all_markers, markers_by_id, ignored_ids)
    if invalid_tokens:
        persistent_notification.async_create(
            hass, f"Ungültige Merker-IDs (keine Ganzzahlen): {', '.join(invalid_tokens)}.", title=error_title
        )
        return
    if not marker_ids:
        persistent_notification.async_create(hass, "Keine gültigen Merker-IDs angegeben.", title=error_title)
        return

    if not await _ensure_comexio_login(hass, api, error_title):
        return

    plan_info = api.fub_data.get(str(fub_id), {})
    was_active = bool(plan_info.get("Active", True))
    if was_active:
        await api.logikplan_stop_fup(fub_id)

    # Canvas-Grenzen und Spalten-Layout — DPI+Ausrichtung immer aus Plan-Daten
    canvas_format_raw = str(call.data.get("canvas_format", "")).strip().upper()
    canvas_format, x_max, y_max, rows_per_col, max_cols = _get_canvas_grid_dims(api, fub_id, canvas_format_raw)
    _LOGGER.info(
        "Logikplan POC: Canvas %s %.0f×%.0f (Plan fub_id=%s)",
        canvas_format,
        x_max,
        y_max,
        fub_id,
    )
    _LOGGER.info(
        "Logikplan POC: Canvas %s (%.0f×%.0f) → %d Zeilen/Spalte, %d Spalten",
        canvas_format,
        x_max,
        y_max,
        rows_per_col,
        max_cols,
    )

    # Load current plan state: existing elements + connections
    existing_by_ref, connected_pairs, occupied_slots = await _load_connect_poc_topology(
        api, fub_id, rows_per_col, max_cols
    )

    _LOGGER.info("Logikplan POC: fub_id=%s, %d Merker zu verarbeiten: %s", fub_id, len(marker_ids), marker_ids)
    t_start = time.monotonic()
    results: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []

    for marker_id in marker_ids:
        marker = markers_by_id.get(marker_id)
        result, skip, error = await _connect_marker_to_webio(
            api,
            fub_id,
            marker_id,
            marker,
            webio_commands,
            existing_by_ref,
            connected_pairs,
            occupied_slots,
            rows_per_col,
            max_cols,
            canvas_format,
        )
        if result:
            results.append(result)
        if skip:
            skipped.append(skip)
        if error:
            errors.append(error)

    duration = time.monotonic() - t_start
    # Plan was stopped above whenever it was active, so it must always be resumed
    # here regardless of `results` — otherwise a no-op run (e.g. all markers
    # already connected) leaves a previously active plan stopped permanently.
    activated = await api.logikplan_run_fup(fub_id) if was_active else False
    msg, title = _build_connect_poc_summary(fub_id, results, skipped, errors, duration, was_active, activated)
    persistent_notification.async_create(hass, msg, title=title)


async def handle_logikplan_visualize(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service to visualize current state of a Logikplan plan as a text diagram."""
    error_title = "Logikplan Visualize — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    coordinator, api, fub_id = ctx

    plan_data = await api.logikplan_load_elements(fub_id)
    if not plan_data:
        persistent_notification.async_create(hass, f"Plan {fub_id} konnte nicht geladen werden.", title=error_title)
        return

    markers_by_id = {str(m["id"]): m for m in coordinator.data.get("markers", [])}
    webio_commands = coordinator.data.get("webio_commands", {})
    webio_by_id = {
        str(cmd.get("webIoId")): name for name, cmd in webio_commands.items() if cmd.get("webIoId") is not None
    }
    elements = plan_data.get("elements", {})
    connections = plan_data.get("connections", {})

    conn_lines, orphan_lines = _build_visualize_lines(elements, connections, markers_by_id, webio_by_id)

    paper_fmt = api.get_fub_paper_format(fub_id)
    x_max, y_max = api.get_fub_canvas_bounds(fub_id)
    lines = [
        f"**Plan {fub_id}** — {paper_fmt}, Canvas {x_max:.0f}×{y_max:.0f}",
        f"{len(elements)} Elemente, {len(connections)} Verbindungen",
        "",
        f"**Verbindungen ({len(connections)}):**",
    ]
    lines += conn_lines or ["  (keine)"]
    if orphan_lines:
        lines += ["", f"**Nicht verbundene Elemente ({len(orphan_lines)}):**"]
        lines += orphan_lines

    persistent_notification.async_create(
        hass, "\n".join(lines), title=f"Logikplan Plan {fub_id} — {len(connections)} Verbindungen"
    )


async def handle_logikplan_sort(hass: HomeAssistant, call: ServiceCall) -> None:
    """Sort all Logikplan elements by marker ID, snapping every element to exact grid."""
    error_title = "Logikplan Sort — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    _coordinator, api, fub_id = ctx

    t_start = time.monotonic()
    plan_info = api.fub_data.get(str(fub_id), {})
    was_active = bool(plan_info.get("Active", True))

    canvas_format_raw = str(call.data.get("canvas_format", "")).strip().upper()
    canvas_label, _x_max, _y_max, rows_per_col, max_cols = _get_canvas_grid_dims(api, fub_id, canvas_format_raw)

    plan_data = await api.logikplan_load_elements(fub_id)
    if not plan_data:
        persistent_notification.async_create(hass, f"Plan {fub_id} konnte nicht geladen werden.", title=error_title)
        return

    pairs, orphans = _build_sorted_pairs(plan_data.get("elements", {}), plan_data.get("connections", {}))
    new_positions = _assign_grid_positions(pairs, orphans, rows_per_col, max_cols)

    if not new_positions:
        persistent_notification.async_create(hass, "Keine Elemente im Plan.", title=f"Logikplan Sort Plan {fub_id}")
        return

    _LOGGER.info(
        "Logikplan Sort: Plan %s — %d Paare, %d Waisen, %d Positionen (aktiv=%s)",
        fub_id,
        len(pairs),
        len(orphans),
        len(new_positions),
        was_active,
    )
    if was_active:
        await api.logikplan_stop_fup(fub_id)
    success = await api.logikplan_save_elements_pos(new_positions)
    activated = await api.logikplan_run_fup(fub_id) if (success and was_active) else False
    duration = time.monotonic() - t_start
    status = "erfolgreich" if success else "fehlgeschlagen"
    act_note = _plan_activation_note(was_active, activated, has_changes=True, fub_id=fub_id)
    msg = (
        f"Sortierung {status}: {len(pairs)} Paare nach Merker-ID geordnet"
        f" + {len(orphans)} Einzelelemente.\n"
        f"Canvas {canvas_label}: {max_cols} Spalten × {rows_per_col} Zeilen.\n"
        f"{act_note}\n"
        f"Dauer: {duration:.1f}s"
    )
    persistent_notification.async_create(
        hass, msg, title=f"Logikplan Sort Plan {fub_id} — {'OK' if success else 'Fehler'}"
    )


async def handle_logikplan_stop(hass: HomeAssistant, call: ServiceCall) -> None:
    """Stop/pause a Logikplan plan."""
    error_title = "Logikplan Stop — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    _coordinator, api, fub_id = ctx

    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    _LOGGER.info("Logikplan Stop: fub_id=%s name='%s'", fub_id, plan_name)
    t_start = time.monotonic()
    success = await api.logikplan_stop_fup(fub_id)
    duration = time.monotonic() - t_start
    msg = (
        f"Plan '{plan_name}' (ID {fub_id}) gestoppt.\nDauer: {duration:.1f}s"
        if success
        else f"Stop fehlgeschlagen (Plan '{plan_name}', ID {fub_id}).\nDauer: {duration:.1f}s"
    )
    persistent_notification.async_create(hass, msg, title=f"Logikplan Stop — {'OK' if success else 'Fehler'}")


async def handle_logikplan_activate(hass: HomeAssistant, call: ServiceCall) -> None:
    """Save and activate a Logikplan plan (run_fup)."""
    error_title = "Logikplan Aktivieren — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    _coordinator, api, fub_id = ctx

    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    _LOGGER.info("Logikplan Aktivieren: fub_id=%s name='%s'", fub_id, plan_name)
    t_start = time.monotonic()
    success = await api.logikplan_run_fup(fub_id)
    duration = time.monotonic() - t_start
    msg = (
        f"Plan '{plan_name}' (ID {fub_id}) gespeichert und aktiviert.\nDauer: {duration:.1f}s"
        if success
        else f"Aktivierung fehlgeschlagen (Plan '{plan_name}', ID {fub_id}).\nDauer: {duration:.1f}s"
    )
    persistent_notification.async_create(hass, msg, title=f"Logikplan Aktivieren — {'OK' if success else 'Fehler'}")


def _build_restore_options(restore_plans: list[tuple[int, str, bool, int]]) -> list[dict]:
    """{label, value} dropdown options for the fub_id field of the restore-related services."""
    return [
        {
            "label": (
                f"{name} — fub {fub_id} ({'live' if is_live else 'deleted'}, "
                f"{count} snapshot{'s' if count != 1 else ''})"
            ),
            "value": f"{fub_id}:{name}",
        }
        for fub_id, name, is_live, count in sorted(restore_plans, key=lambda p: (p[1].lower(), p[0]))
    ]


_SNAPSHOT_OPTION_MAX_OP_LEN = 30


def _snapshot_option_row(kind: str, entry: dict) -> tuple[str, float, dict]:
    """One (sort_key, -epoch, option) row for _build_snapshot_options."""
    ts = dt_util.parse_datetime(str(entry.get("captured_at", "")))
    epoch = ts.timestamp() if ts else 0.0
    ts_label = dt_util.as_local(ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if ts else "?"
    operation = str(entry.get("operation", ""))
    # Some operations embed a full marker-ID list (e.g. add_marker_pairs) — that's
    # useful in the log but far too long for a dropdown label, so truncate it here.
    if len(operation) > _SNAPSHOT_OPTION_MAX_OP_LEN:
        operation = operation[: _SNAPSHOT_OPTION_MAX_OP_LEN - 1] + "…"
    op_suffix = f" ({operation})" if kind == "change" and operation else ""
    label = (
        f"{entry.get('plan_name')} — fub {entry.get('fub_id')} — {kind}[{entry.get('slot')}] — {ts_label}{op_suffix}"
    )
    value = f"{entry.get('fub_id')}:{kind}:{entry.get('slot')}:{entry.get('plan_name')}"
    return str(entry.get("plan_name", "")).lower(), -epoch, {"label": label, "value": value}


def _build_snapshot_options(backups: dict[str, list[dict]]) -> list[dict]:
    """Build one dropdown option per stored snapshot for the combined 'snapshot' picker.

    value: 'fub_id:kind:slot:plan_name' (parsed back by _parse_snapshot_field) — plan_name is
    included because fub_id alone is not a stable identity (a reused ID can carry two
    different plans' snapshots at once).
    """
    rows = [_snapshot_option_row(kind, entry) for kind in ("auto", "change") for entry in backups.get(kind, [])]
    rows.sort(key=lambda r: (r[0], r[1]))
    return [option for _, _, option in rows]


def _set_yaml_field_options(
    content: dict, service: str, field: str, options: list, *, custom_value: bool = True
) -> None:
    # custom_value=False for the {label, value} dict-option fields (fub_id/snapshot on the
    # restore-related services): with custom_value=True, HA's frontend shows the raw
    # submitted value instead of the matching label once an option is picked — acceptable
    # for the plain-string options elsewhere, but defeats the point of a readable label here.
    target_field = content.get(service, {}).get("fields", {}).get(field)
    if target_field:
        target_field["selector"] = {"select": {"options": options, "custom_value": custom_value}}


_BACKUP_DYNAMIC_SERVICES = ("function_plan_restore", "function_plan_list_backups", "function_plan_delete_backups")


def _update_services_yaml_backup_options(
    restore_plans: list[tuple[int, str, bool, int]], snapshot_options: list[dict]
) -> dict | None:
    """Blocking read/modify/write of services.yaml's backup-related dropdowns; executor job only.

    Trimmed sibling of _rewrite_services_yaml_plans (which independently owns the
    logikplan_* services' fub_id dropdown) — only touches the fub_id/snapshot fields of the
    three backup services, so that function is left completely untouched.
    """
    try:
        content = yaml.safe_load(_SERVICES_YAML_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOGGER.warning("services.yaml missing or invalid, skipping backup option rewrite: %s", exc)
        return None
    restore_options = _build_restore_options(restore_plans)
    for svc in _BACKUP_DYNAMIC_SERVICES:
        _set_yaml_field_options(content, svc, "fub_id", restore_options, custom_value=False)
    _set_yaml_field_options(content, "function_plan_restore", "snapshot", snapshot_options, custom_value=False)
    _set_yaml_field_options(content, "function_plan_delete_backups", "snapshot", snapshot_options, custom_value=False)
    try:
        _SERVICES_YAML_PATH.write_text(
            yaml.dump(content, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOGGER.warning("Could not write services.yaml backup option rewrite: %s", exc)
        return None
    return content


async def _refresh_service_descriptions(hass: HomeAssistant) -> None:
    """Rewrite services.yaml's backup dropdowns and push them into HA's live schema cache.

    Re-registering an already-registered service does NOT make Home Assistant reload its
    cached description — only async_set_service_schema writes directly into that cache and
    is guaranteed to take effect immediately. Called by coordinator.py right after a new
    auto-/change-backup is captured, and by this module's own backup service handlers.
    """
    restore_plans: list[tuple[int, str, bool, int]] = []
    snapshot_options: list[dict] = []
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, ComexioCoordinator):
            live_fub_ids = {int(k) for k in coordinator.api.fub_data}
            for fub_id, name, count in await coordinator.function_plan_backup.async_backed_up_plans():
                restore_plans.append((fub_id, name, fub_id in live_fub_ids, count))
            backups = await coordinator.function_plan_backup.async_list_backups()
            snapshot_options.extend(_build_snapshot_options(backups))
    if not restore_plans:
        return
    content = await hass.async_add_executor_job(_update_services_yaml_backup_options, restore_plans, snapshot_options)
    if not content:
        return
    for svc in _BACKUP_DYNAMIC_SERVICES:
        if schema := content.get(svc):
            async_set_service_schema(hass, DOMAIN, svc, schema)


async def _handle_function_plan_restore(hass: HomeAssistant, call: ServiceCall):
    """Restore a function plan from a stored backup snapshot.

    Branches on the CURRENT live state of the snapshot's fub_id:
    - free (plan deleted) → always rebuilt as a new plan (see _restore_plan_as_new).
    - live under the SAME name → normal in-place restore via run_fup (unchanged, safe path).
    - live under a DIFFERENT name (ID reused by an unrelated plan) → identity mismatch;
      requires explicit `confirm: true` plus `on_conflict` ('new_id' rebuilds separately,
      'force_override' overwrites the live plan anyway).
    """
    from .function_plan_backup import plan_hash

    ctx = await _async_get_service_context(hass, call, _TITLE_RESTORE_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return
    coordinator, api, _unused_fub_id = ctx

    snapshot_raw = call.data.get("snapshot")
    if snapshot_raw:
        parsed = _parse_snapshot_field(str(snapshot_raw))
        if parsed is None:
            persistent_notification.async_create(
                hass, f"Invalid 'snapshot' value: '{snapshot_raw}'.", title=_TITLE_RESTORE_ERR
            )
            return
        fub_id, kind, slot, plan_name_hint = parsed
    else:
        fub_id, plan_name_hint, resolve_err = await _resolve_restore_target(hass, call, coordinator, api)
        if resolve_err:
            persistent_notification.async_create(hass, resolve_err, title=_TITLE_RESTORE_ERR)
            return
        kind = str(call.data.get("kind", "auto")).strip().lower()
        slot = int(call.data.get("slot", 0))

    plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
    if identity_err:
        persistent_notification.async_create(hass, identity_err, title=_TITLE_RESTORE_ERR)
        return

    on_conflict = str(call.data.get("on_conflict", "new_id")).strip().lower()
    confirm = bool(call.data.get("confirm", False))
    _LOGGER.info(
        "Function Plan Restore: fub_id=%s plan_name=%s kind=%s slot=%s on_conflict=%s confirm=%s",
        fub_id,
        plan_name,
        kind,
        slot,
        on_conflict,
        confirm,
    )

    snapshot = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
    if snapshot is None:
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
        return

    if not await api.login():
        persistent_notification.async_create(hass, _LOGIN_FAILED_MSG, title=_TITLE_RESTORE_ERR)
        return

    # Fresh live lookup — api.fub_data may be up to one poll interval stale, and this
    # decision (does the plan still exist, under what name?) must not be made on stale data.
    raw_config = await api.get_raw_config()
    live_fub = raw_config.get("Fubs", {}).get(str(fub_id))
    if live_fub is not None:
        api._fub_data[str(fub_id)] = live_fub  # keep the cache fresh for _restore_plan_in_place's reads
    snapshot_name = snapshot.get("plan_name", str(fub_id))
    live_name = live_fub.get("Name") if live_fub else None
    conflict = live_fub is None or live_name != snapshot_name

    if conflict:
        if not confirm:
            persistent_notification.async_create(
                hass,
                _restore_conflict_message(fub_id, snapshot_name, live_name, on_conflict),
                title=_TITLE_RESTORE_ERR,
            )
            return
        if live_fub is None or on_conflict != "force_override":
            await _restore_plan_as_new(
                hass, coordinator, api, fub_id, snapshot, kind, slot, old_id_still_live=live_fub is not None
            )
            await _refresh_service_descriptions(hass)
            return
        # force_override on an identity mismatch: fall through and deliberately
        # overwrite the plan that now occupies this fub_id.

    await _restore_plan_in_place(
        hass, coordinator, api, fub_id, snapshot, kind, slot, plan_hash, identity_was_mismatched=conflict
    )
    await _refresh_service_descriptions(hass)


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
        parsed = _parse_snapshot_field(str(snapshot_raw))
        if parsed is None:
            persistent_notification.async_create(
                hass, f"Invalid 'snapshot' value: '{snapshot_raw}'.", title=_TITLE_DELETE_BACKUPS_ERR
            )
            return
        fub_id, kind, slot, plan_name_hint = parsed
        plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
        if identity_err:
            persistent_notification.async_create(hass, identity_err, title=_TITLE_DELETE_BACKUPS_ERR)
            return
        deleted = await coordinator.function_plan_backup.async_delete_snapshot(kind, fub_id, plan_name, slot)
        msg = (
            f"Deleted snapshot {kind}[{slot}] for plan '{plan_name}' (fub {fub_id})."
            if deleted
            else f"No snapshot found at {kind}[{slot}] for plan '{plan_name}' (fub {fub_id}) — nothing deleted."
        )
    elif fub_id_raw not in (None, ""):
        split = _split_plan_field(str(fub_id_raw))
        if split is None:
            persistent_notification.async_create(
                hass, f"Invalid 'fub_id' value: '{fub_id_raw}'.", title=_TITLE_DELETE_BACKUPS_ERR
            )
            return
        fub_id, plan_name_hint = split
        plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
        if identity_err:
            persistent_notification.async_create(hass, identity_err, title=_TITLE_DELETE_BACKUPS_ERR)
            return
        count = await coordinator.function_plan_backup.async_delete_plan_backups(fub_id, plan_name)
        msg = (
            f"Deleted all {count} snapshot(s) for plan '{plan_name}' (fub {fub_id})."
            if count
            else f"No stored snapshots for plan '{plan_name}' (fub {fub_id}) — nothing deleted."
        )
    else:
        count = await coordinator.function_plan_backup.async_delete_all_backups()
        msg = (
            f"Deleted ALL {count} stored snapshot(s) across ALL plans on this instance.\n"
            "Fresh backups will be created automatically starting with the next backup cycle / next change."
        )

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


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register additional services for the Comexio integration."""
    if not hass.services.has_service(DOMAIN, "generate_web_io"):
        hass.services.async_register(DOMAIN, "generate_web_io", functools.partial(handle_generate_web_io, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_connect_poc"):
        hass.services.async_register(
            DOMAIN, "logikplan_connect_poc", functools.partial(handle_logikplan_connect_poc, hass)
        )
    if not hass.services.has_service(DOMAIN, "logikplan_visualize"):
        hass.services.async_register(DOMAIN, "logikplan_visualize", functools.partial(handle_logikplan_visualize, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_sort"):
        hass.services.async_register(DOMAIN, "logikplan_sort", functools.partial(handle_logikplan_sort, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_stop"):
        hass.services.async_register(DOMAIN, "logikplan_stop", functools.partial(handle_logikplan_stop, hass))
    if not hass.services.has_service(DOMAIN, "logikplan_activate"):
        hass.services.async_register(DOMAIN, "logikplan_activate", functools.partial(handle_logikplan_activate, hass))
    if not hass.services.has_service(DOMAIN, "function_plan_restore"):
        hass.services.async_register(
            DOMAIN, "function_plan_restore", functools.partial(_handle_function_plan_restore, hass)
        )
    if not hass.services.has_service(DOMAIN, "function_plan_delete_backups"):
        hass.services.async_register(
            DOMAIN, "function_plan_delete_backups", functools.partial(_handle_function_plan_delete_backups, hass)
        )
    if not hass.services.has_service(DOMAIN, "function_plan_purge_orphaned_backups"):
        hass.services.async_register(
            DOMAIN,
            "function_plan_purge_orphaned_backups",
            functools.partial(_handle_function_plan_purge_orphaned_backups, hass),
        )
    if not hass.services.has_service(DOMAIN, "function_plan_list_backups"):
        hass.services.async_register(
            DOMAIN,
            "function_plan_list_backups",
            functools.partial(_handle_function_plan_list_backups, hass),
            supports_response=SupportsResponse.ONLY,
        )

    await _update_services_yaml_plans(hass)

    # Exposed so coordinator.py can trigger the same refresh right after it writes a new auto-
    # or change-backup — those happen on the polling cycle and before plan-mutating actions,
    # neither of which routes through this module's own service handlers.
    hass.data[DOMAIN]["_refresh_service_descriptions"] = functools.partial(_refresh_service_descriptions, hass)
    await _refresh_service_descriptions(hass)
