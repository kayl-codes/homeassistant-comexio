# Version: 0.8.2
"""Rotating persistent snapshots of Comexio Function Plan wiring (backup for rollback).

Two independent stores per server:
- auto:   written by the poll cycle whenever a plan's content hash changes
          (FUNCTION_PLAN_AUTO_BACKUP_SLOTS per plan)
- change: written immediately BEFORE any HA-side plan mutation, tagged with an
          operation label (FUNCTION_PLAN_CHANGE_BACKUP_SLOTS per plan)

Comexio provides no modification timestamps for plans, so every snapshot carries
the capture timestamp instead. Snapshots live under .storage/ and are therefore
included in Home Assistant's own backups.
"""

from datetime import datetime, timedelta
import logging
import re
from typing import Any

from aiocomexio.function_plan import plan_hash, referenced_label_metadata
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, FUNCTION_PLAN_AUTO_BACKUP_SLOTS, FUNCTION_PLAN_CHANGE_BACKUP_SLOTS, TIMESTAMP_DISPLAY_FORMAT

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Fallback used when backfilling snapshots captured before paper/DPI tracking existed and
# the plan can no longer be matched live (deleted or reassigned) — a spacious default so a
# rebuild from such a snapshot cannot come out smaller/clipped versus the (unknown) original.
_FALLBACK_PAPER = "A3"
_FALLBACK_DPI = 90
_FALLBACK_ORIENTATION = "landscape"


def _backfill_identity_group(
    identities: dict[str, list[dict[str, Any]]],
    live_name: str | None,
    live_format: tuple[str, int, str] | None,
) -> int:
    """Fill in missing paper/dpi/orientation on every snapshot under one fub_id key.

    Paper format is the same for every snapshot of a given plan_name (see
    async_backfill_paper_metadata), so it's resolved once per plan_name bucket rather than
    once per snapshot. Returns the number of snapshots filled in.
    """
    filled = 0
    for plan_name, history in identities.items():
        is_live_identity = live_format is not None and live_name is not None and plan_name == live_name
        fallback = (_FALLBACK_PAPER, _FALLBACK_DPI, _FALLBACK_ORIENTATION)
        paper, dpi, orientation = live_format if is_live_identity else fallback
        for snap in history:
            if "paper" in snap:
                continue
            snap["paper"] = paper
            snap["dpi"] = dpi
            snap["orientation"] = orientation
            filled += 1
    return filled


def _backup_entry(key: str, plan_name: str, slot: int, snap: dict[str, Any]) -> dict[str, Any]:
    """One async_list_backups() metadata entry for a single stored snapshot."""
    entry = {
        "fub_id": int(key),
        "plan_name": snap.get("plan_name", plan_name),
        "captured_at": snap.get("captured_at"),
        "hash": snap.get("hash"),
        "slot": slot,
    }
    if "operation" in snap:
        entry["operation"] = snap["operation"]
    if "restored_at" in snap:
        entry["restored_at"] = snap["restored_at"]
    for opt_key in ("paper", "dpi", "orientation"):
        if opt_key in snap:
            entry[opt_key] = snap[opt_key]
    return entry


def _list_backup_entries(data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Flatten one store's {fub_id: {plan_name: [snap, ...]}} into sorted metadata entries."""
    entries = []
    for key, identities in data.items():
        for plan_name, history in identities.items():
            for slot, snap in enumerate(history):
                entries.append(_backup_entry(key, plan_name, slot, snap))
    entries.sort(key=lambda e: e.get("captured_at") or "", reverse=True)
    return entries


def _newer_timestamp(a: str | None, b: str | None) -> str | None:
    """The later of two ISO-8601 captured_at timestamps (either side may be missing)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def is_orphaned_identity(fub_data: dict[str, Any], fub_id: int, plan_name: str) -> bool:
    """Whether a backed-up (fub_id, plan_name) identity no longer exists live in Comexio.

    False without a live $Fubs snapshot: a failed plan fetch must never make every plan look
    deleted. A fub_id that now carries a different name counts as orphaned (ID reused).
    """
    return bool(fub_data) and fub_data.get(str(fub_id), {}).get("Name") != plan_name


def _newest_orphaned_in_store(
    data: dict[str, dict[str, list[dict[str, Any]]]] | None, fub_data: dict[str, Any]
) -> dict[tuple[int, str], str | None]:
    """Newest captured_at per orphaned (no-longer-live) (fub_id, plan_name) identity in one store."""
    newest: dict[tuple[int, str], str | None] = {}
    for key, identities in (data or {}).items():
        fub_id = int(key)
        for plan_name, history in identities.items():
            if not is_orphaned_identity(fub_data, fub_id, plan_name):
                continue  # still live under this fub_id
            captured_at = history[0].get("captured_at") if history else None
            newest[(fub_id, plan_name)] = _newer_timestamp(newest.get((fub_id, plan_name)), captured_at)
    return newest


def _iter_snapshots(*stores: dict[str, dict[str, list[dict[str, Any]]]] | None):
    """Yield every stored snapshot dict across any number of {fub_id: {plan_name: [snap, ...]}} stores."""
    for data in stores:
        for identities in (data or {}).values():
            for history in identities.values():
                yield from history


def _snapshot_count(data: dict[str, dict[str, list]] | None) -> int:
    return sum(len(history) for identities in (data or {}).values() for history in identities.values())


def _identity_count(data: dict[str, dict[str, list]] | None) -> int:
    return sum(len(identities) for identities in (data or {}).values())


def _latest_capture(*stores: dict[str, dict[str, list[dict[str, Any]]]] | None) -> str | None:
    """Newest captured_at timestamp across any number of {fub_id: {plan_name: [snap, ...]}} stores."""
    timestamps = (snap.get("captured_at") for snap in _iter_snapshots(*stores) if snap.get("captured_at"))
    return max(timestamps, default=None)


def retention_cutoff(months: int) -> datetime:
    """UTC cutoff timestamp for a retention window expressed in whole months (~30 days each).

    Shared by the coordinator's periodic orphaned-backup purge and the manual
    function_plan_purge_orphaned_backups service, so both apply the exact same "how old is
    too old" math for the configured CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS option.
    """
    return dt_util.utcnow() - timedelta(days=months * 30)


_OP_LIST_MAX_ITEMS = 3
_OP_LIST_RE = re.compile(r"\[([^\[\]]*)]")


def _truncate_operation_lists(operation: str) -> str:
    """Cap any bracketed id list embedded in an operation string (e.g. add_marker_pairs).

    HA's native select dropdown CSS-ellipsizes the whole label once it overflows the box,
    which can swallow the operation name itself for a long marker list. Truncating the list
    here — not the label as a whole — keeps the meaningful part ("add_marker_pairs", the
    first few ids) visible regardless of box width.
    """

    def _replace(match: re.Match[str]) -> str:
        items = [item.strip() for item in match.group(1).split(",") if item.strip()]
        if len(items) <= _OP_LIST_MAX_ITEMS:
            return match.group(0)
        return "[" + ", ".join(items[:_OP_LIST_MAX_ITEMS]) + ", …]"

    return _OP_LIST_RE.sub(_replace, operation)


def format_backup_label(entry: dict[str, Any]) -> str:
    """Human-readable label for one plan_backups_for_identity_sync() entry.

    Shared by select.py (building the backup-selector's dropdown options) and button.py
    (matching the selector's current state back to a concrete kind/slot) so both sides stay
    in sync without a hidden value encoding — the visible label IS the lookup key, same
    approach the existing 'Function Plans' plan selector already uses (plan name as state).
    """
    ts = dt_util.parse_datetime(str(entry.get("captured_at", "")))
    ts_label = dt_util.as_local(ts).strftime(TIMESTAMP_DISPLAY_FORMAT) if ts else "?"
    # "*" marks a snapshot promoted to slot 0 by a restore (see async_mark_restored) — the
    # captured_at timestamp stays the ORIGINAL capture time, not the restore time, so the
    # marker is the only visible sign this isn't a fresh auto/change snapshot.
    if entry.get("restored_at"):
        ts_label += "*"
    operation = entry.get("operation")
    kind = entry.get("kind")
    op_suffix = f" ({_truncate_operation_lists(operation)})" if kind == "change" and operation else ""
    return f"{kind}[{entry.get('slot')}] — {ts_label}{op_suffix}"


# Indents a snapshot row under its plan row in the backup selector's orphaned-plans view.
# Non-breaking spaces: the frontend collapses leading regular whitespace.
_ORPHAN_SNAPSHOT_INDENT = "\u00a0" * 4


def _orphan_plan_label(orphan: dict[str, Any]) -> str:
    count = len(orphan["backups"])
    return f"{orphan['plan_name']} (ID {orphan['fub_id']}) — {count} backup{'' if count == 1 else 's'}"


def build_orphaned_backup_options(orphans: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(label, choice) per row of the backup selector's orphaned-plans view.

    One plan row per orphaned identity (see FunctionPlanBackupManager.orphaned_plans_sync),
    followed by its indented snapshot rows. choice carries the identity plus the kind/slot to
    show; a plan row shows the plan's newest snapshot. Shared by select.py (options) and
    button.py (resolving the chosen label back), like format_backup_label. Labels must be
    unique select options, but two plans changed in the same minute share a snapshot row
    label — a repeat gets its plan appended.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for orphan in orphans:
        identity = {"fub_id": orphan["fub_id"], "plan_name": orphan["plan_name"], "kept": orphan["kept"]}
        backups = orphan["backups"]
        newest = max(backups, key=lambda entry: entry.get("captured_at") or "")
        plan_choice = {**identity, "kind": newest["kind"], "slot": newest["slot"], "plan_row": True}
        rows.append((_orphan_plan_label(orphan), plan_choice))
        for entry in backups:
            label = f"{_ORPHAN_SNAPSHOT_INDENT}{format_backup_label(entry)}"
            if label in seen:
                label += f" · {orphan['plan_name']} (ID {orphan['fub_id']})"
            seen.add(label)
            rows.append((label, {**identity, "kind": entry["kind"], "slot": entry["slot"], "plan_row": False}))
    return rows


class FunctionPlanBackupManager:
    """Manage rotating auto and pre-change snapshots of function plans."""

    def __init__(self, hass: HomeAssistant, server_id: str) -> None:
        self._server_id = server_id
        # Storage keys keep the legacy "logikplan" spelling — renaming them would orphan
        # every snapshot already persisted under .storage/.
        self._auto_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_logikplan_auto_{server_id}")
        self._change_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_logikplan_changes_{server_id}")
        # Orphaned plan identities the user chose to keep via the orphaned_plan_backups repair or
        # the plan card's orphaned-plans view: never raised again and never purged, only deleted
        # by hand (or un-kept in the plan card).
        self._kept_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_function_plan_backup_kept_{server_id}")
        self._kept: set[tuple[int, str]] = set()
        # Lazy-loaded caches: {fub_id_str: {plan_name: [snapshot, ...]}} — newest first per identity.
        # fub_id alone is not a stable identity (Comexio reuses IDs after deletion), so rotation
        # and lookups are always scoped to the (fub_id, plan_name) pair, never to fub_id alone.
        # Kept non-Optional (empty dict until loaded, guarded by _loaded) so callers never need
        # to narrow away a None — avoids sprinkling `assert ... is not None` through this class.
        self._loaded = False
        self._auto_data: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._change_data: dict[str, dict[str, list[dict[str, Any]]]] = {}

    @staticmethod
    def _migrate_legacy_shape(data: dict[str, Any]) -> bool:
        """Regroup a pre-hardening flat {fub_id: [snapshot,...]} entry into {fub_id: {name: [...]}}.

        Snapshots always carried their own plan_name, so this is a lossless, idempotent
        reshape — safe to run on every load (a no-op once migrated).
        """
        changed = False
        for key, value in list(data.items()):
            if not isinstance(value, list):
                continue
            grouped: dict[str, list[dict[str, Any]]] = {}
            for snap in value:
                grouped.setdefault(snap.get("plan_name", key), []).append(snap)
            data[key] = grouped
            changed = True
        return changed

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._auto_data = await self._auto_store.async_load() or {}
        if self._migrate_legacy_shape(self._auto_data):
            await self._auto_store.async_save(self._auto_data)
            _LOGGER.info(
                "[%s] Function Plan backup: migrated auto-backup storage to identity-scoped shape", self._server_id
            )
        self._change_data = await self._change_store.async_load() or {}
        if self._migrate_legacy_shape(self._change_data):
            await self._change_store.async_save(self._change_data)
            _LOGGER.info(
                "[%s] Function Plan backup: migrated change-backup storage to identity-scoped shape",
                self._server_id,
            )
        kept = await self._kept_store.async_load() or {}
        items = kept.get("identities") if isinstance(kept, dict) else None
        if not isinstance(items, list):
            if kept:
                _LOGGER.warning("[%s] Function Plan backup: ignoring invalid kept store %r", self._server_id, kept)
            items = []
        self._kept = set()
        for item in items:
            try:
                self._kept.add((int(item["fub_id"]), str(item["plan_name"])))
            except (KeyError, TypeError, ValueError):
                # A damaged side file must not block every backup; the plan is simply asked about again.
                _LOGGER.warning("[%s] Function Plan backup: ignoring invalid kept entry %r", self._server_id, item)
        self._loaded = True

    async def _async_save_kept(self) -> None:
        identities = [{"fub_id": fub_id, "plan_name": name} for fub_id, name in sorted(self._kept)]
        await self._kept_store.async_save({"identities": identities})

    async def _async_forget_kept_if_gone(self, fub_id: int, plan_name: str) -> None:
        """Drop a keep decision once its identity has no snapshots left.

        Done right at deletion, not only in the next audit: a plan recreated under the same
        identity and deleted again before that audit would otherwise stay silenced for good.
        """
        if (fub_id, plan_name) in self._kept and not self._snapshot_total(fub_id, plan_name):
            self._kept.discard((fub_id, plan_name))
            await self._async_save_kept()

    @staticmethod
    def _build_snapshot(
        plan_data: dict[str, Any],
        plan_name: str,
        operation: str | None = None,
        paper: str | None = None,
        dpi: int | None = None,
        orientation: str | None = None,
        comexio_version: str | None = None,
        label_metadata: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "hash": plan_hash(plan_data),
            "captured_at": dt_util.utcnow().isoformat(),
            "plan_name": plan_name,
            "elements": plan_data.get("elements", {}),
            "connections": plan_data.get("connections", {}),
        }
        if operation is not None:
            snapshot["operation"] = operation
        if paper is not None:
            snapshot["paper"] = paper
        if dpi is not None:
            snapshot["dpi"] = dpi
        if orientation is not None:
            snapshot["orientation"] = orientation
        if label_metadata:
            snapshot["labels"] = label_metadata
        if comexio_version is not None:
            # Comexio's own firmware/frontend version at capture time (ComexioAPI.comexio_version,
            # e.g. "11.0.2") — lets a future restore detect "this snapshot predates a firmware
            # update", same stamp as function_plan_catalog.py's cached element/block-type catalog.
            snapshot["comexio_version"] = comexio_version
        return snapshot

    async def async_auto_backup(
        self,
        plans: dict[int, dict[str, Any]],
        fub_data: dict[str, Any],
        plan_format: dict[str, tuple[str, int, str]] | None = None,
        comexio_version: str | None = None,
        markers_by_id: dict[str, Any] | None = None,
        webio_by_id: dict[str, Any] | None = None,
        ios_by_id: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Rotate in snapshots for all plans whose wiring changed since the last auto backup.

        plan_format: optional {fub_id_str: (paper, dpi, orientation)} so the snapshot can
        record the canvas settings needed to recreate the plan identically on restore.
        markers_by_id/webio_by_id/ios_by_id: live label maps (coordinator.function_plan_label_maps())
        at capture time, used to freeze each snapshot's element names — see
        referenced_label_metadata.
        Returns one {fub_id, plan_name, captured_at} entry per plan that produced a new
        snapshot this call — feeds the "changed plans" diagnostic sensor, whose whole point
        is to let a user confirm that an intentional edit changed exactly the plan(s) they
        expected and no others.
        """
        await self._async_ensure_loaded()
        changed_identities: list[dict[str, Any]] = []
        for fub_id, plan_data in plans.items():
            key = str(fub_id)
            new_hash = plan_hash(plan_data)
            plan_name = fub_data.get(key, {}).get("Name", key)
            history = self._auto_data.setdefault(key, {}).setdefault(plan_name, [])
            if history and history[0].get("hash") == new_hash:
                continue
            paper, dpi, orientation = (plan_format or {}).get(key, (None, None, None))
            label_metadata = referenced_label_metadata(
                plan_data, markers_by_id or {}, webio_by_id or {}, ios_by_id or {}
            )
            snapshot = self._build_snapshot(
                plan_data,
                plan_name,
                paper=paper,
                dpi=dpi,
                orientation=orientation,
                comexio_version=comexio_version,
                label_metadata=label_metadata,
            )
            history.insert(0, snapshot)
            del history[FUNCTION_PLAN_AUTO_BACKUP_SLOTS:]
            changed_identities.append(
                {"fub_id": fub_id, "plan_name": plan_name, "captured_at": snapshot["captured_at"]}
            )
            _LOGGER.info(
                "[%s] Function Plan auto backup: plan '%s' (fub=%s) changed — %d/%d slot(s) used",
                self._server_id,
                plan_name,
                fub_id,
                len(history),
                FUNCTION_PLAN_AUTO_BACKUP_SLOTS,
            )
        if changed_identities:
            await self._auto_store.async_save(self._auto_data)
        return changed_identities

    async def async_change_backup(
        self,
        fub_id: int,
        plan_data: dict[str, Any],
        plan_name: str,
        operation: str,
        paper: str | None = None,
        dpi: int | None = None,
        orientation: str | None = None,
        comexio_version: str | None = None,
        markers_by_id: dict[str, Any] | None = None,
        webio_by_id: dict[str, Any] | None = None,
        ios_by_id: dict[str, Any] | None = None,
    ) -> None:
        """Rotate in a pre-mutation snapshot for one plan (call BEFORE HA modifies it).

        markers_by_id/webio_by_id/ios_by_id: live label maps at capture time — see
        async_auto_backup / referenced_label_metadata.
        """
        await self._async_ensure_loaded()
        history = self._change_data.setdefault(str(fub_id), {}).setdefault(plan_name, [])
        label_metadata = referenced_label_metadata(plan_data, markers_by_id or {}, webio_by_id or {}, ios_by_id or {})
        snapshot = self._build_snapshot(
            plan_data, plan_name, operation, paper, dpi, orientation, comexio_version, label_metadata
        )
        history.insert(0, snapshot)
        del history[FUNCTION_PLAN_CHANGE_BACKUP_SLOTS:]
        await self._change_store.async_save(self._change_data)
        _LOGGER.info(
            "[%s] Function Plan pre-change backup: plan '%s' (fub=%s), operation '%s' — %d/%d slot(s) used",
            self._server_id,
            plan_name,
            fub_id,
            operation,
            len(history),
            FUNCTION_PLAN_CHANGE_BACKUP_SLOTS,
        )

    async def async_backfill_paper_metadata(
        self,
        fub_data: dict[str, Any],
        plan_format: dict[str, tuple[str, int, str]],
    ) -> int:
        """Add paper/dpi/orientation to snapshots stored before this was tracked (idempotent).

        Matches a snapshot to its live plan by fub_id + plan_name (Comexio has never been
        observed to let a plan's paper/DPI/orientation change after creation, so the current
        live value is authoritative for still-existing plans). No match (plan deleted or its
        ID reused by a different plan) falls back to A3 @ 90 DPI landscape. Snapshots that
        already carry a "paper" key are left untouched, so this is safe to call on every
        backup cycle. Returns the number of snapshots filled in.
        """
        await self._async_ensure_loaded()
        filled = 0
        for data, store in ((self._auto_data, self._auto_store), (self._change_data, self._change_store)):
            group_filled = 0
            for key, identities in data.items():
                live_name = fub_data.get(key, {}).get("Name")
                live_format = plan_format.get(key)
                group_filled += _backfill_identity_group(identities, live_name, live_format)
            if group_filled:
                await store.async_save(data)
            filled += group_filled
        if filled:
            _LOGGER.info(
                "[%s] Function Plan backup: backfilled paper/DPI metadata on %d snapshot(s)", self._server_id, filled
            )
        return filled

    async def async_rekey_fub_id(self, old_fub_id: int, new_fub_id: int, plan_name: str) -> int:
        """Move one identity's stored backup history from old_fub_id to new_fub_id.

        Call this right after a restore-as-new gives a deleted/reassigned plan a fresh
        fub_id, so its backup lineage keeps going under the new ID instead of being
        orphaned under the stale one. Only the plan_name bucket being restored is moved —
        an unrelated identity that may also live under old_fub_id is untouched (that case
        should go through async_purge_identity instead, see old_id_still_live). Combines
        with any history already captured under new_fub_id (e.g. the plan's own first
        auto-backup), re-sorts newest first, and truncates back to the normal slot limits.
        A no-op if old_fub_id has no history for plan_name. Returns the number moved.
        """
        await self._async_ensure_loaded()
        old_key, new_key = str(old_fub_id), str(new_fub_id)
        moved = 0
        for data, store, limit in (
            (self._auto_data, self._auto_store, FUNCTION_PLAN_AUTO_BACKUP_SLOTS),
            (self._change_data, self._change_store, FUNCTION_PLAN_CHANGE_BACKUP_SLOTS),
        ):
            old_identities = data.get(old_key)
            old_history = old_identities.pop(plan_name, None) if old_identities else None
            if old_identities is not None and not old_identities:
                data.pop(old_key, None)
            if not old_history:
                continue
            new_identities = data.setdefault(new_key, {})
            combined = new_identities.get(plan_name, []) + old_history
            combined.sort(key=lambda s: s.get("captured_at") or "", reverse=True)
            new_identities[plan_name] = combined[:limit]
            moved += len(old_history)
            await store.async_save(data)
        # The restored plan is live again under new_fub_id, so the keep decision is not carried over.
        await self._async_forget_kept_if_gone(old_fub_id, plan_name)
        if moved:
            _LOGGER.info(
                "[%s] Function Plan backup: rekeyed %d snapshot(s) of '%s' from fub=%s to fub=%s",
                self._server_id,
                moved,
                plan_name,
                old_fub_id,
                new_fub_id,
            )
        return moved

    async def async_purge_identity(self, fub_id: int, plan_name: str) -> int:
        """Remove only the plan_name bucket at fub_id (auto + change).

        Used for a restore-as-new whose old fub_id is still occupied by an unrelated live
        plan: rather than rekeying (which would also need to handle merging with the
        occupant's change-backups), the now-superseded old identity's own snapshots are
        simply deleted, since the plan has its own fresh history at the new fub_id going
        forward. An unrelated identity sharing the same fub_id is left completely untouched.
        Returns the number of snapshots removed.
        """
        await self._async_ensure_loaded()
        key = str(fub_id)
        removed = 0
        for data, store in ((self._auto_data, self._auto_store), (self._change_data, self._change_store)):
            identities = data.get(key)
            if not identities or plan_name not in identities:
                continue
            removed += len(identities.pop(plan_name))
            if not identities:
                data.pop(key, None)
            await store.async_save(data)
        await self._async_forget_kept_if_gone(fub_id, plan_name)
        if removed:
            _LOGGER.info(
                "[%s] Function Plan backup: purged %d superseded snapshot(s) of '%s' at old fub=%s",
                self._server_id,
                removed,
                plan_name,
                fub_id,
            )
        return removed

    async def async_mark_restored(self, kind: str, fub_id: int, plan_name: str, slot: int) -> bool:
        """Promote a just-restored snapshot to slot 0 of its own kind-array and flag it.

        Called right after a restore that leaves the live plan matching this snapshot (see
        services/backup.py's _restore_plan_in_place) — the restored content IS now the
        current state, so instead of leaving a duplicate for the next auto-backup cycle to
        write, the existing entry is moved to the front (no new storage) and stamped with
        "restored_at". captured_at is deliberately left untouched: it still records when this
        content was ORIGINALLY captured, not when it was restored. format_backup_label() shows
        a trailing "*" for any entry carrying restored_at. Returns False if the slot is gone
        (e.g. deleted or rotated out between resolving the restore target and finishing it).
        """
        await self._async_ensure_loaded()
        data, store = (self._auto_data, self._auto_store) if kind == "auto" else (self._change_data, self._change_store)
        history = data.get(str(fub_id), {}).get(plan_name)
        if not history or not (0 <= slot < len(history)):
            return False
        snap = history.pop(slot)
        snap["restored_at"] = dt_util.utcnow().isoformat()
        history.insert(0, snap)
        await store.async_save(data)
        _LOGGER.info(
            "[%s] Function Plan backup: promoted %s[%d] of '%s' (fub=%s) to slot 0 after restore",
            self._server_id,
            kind,
            slot,
            plan_name,
            fub_id,
        )
        return True

    async def async_delete_snapshot(self, kind: str, fub_id: int, plan_name: str, slot: int) -> bool:
        """Delete a single stored snapshot (one slot of one plan identity/kind). Returns True if removed."""
        await self._async_ensure_loaded()
        data, store = (self._auto_data, self._auto_store) if kind == "auto" else (self._change_data, self._change_store)
        key = str(fub_id)
        history = data.get(key, {}).get(plan_name)
        if not history or not (0 <= slot < len(history)):
            return False
        del history[slot]
        history_emptied = len(history) == 0
        if history_emptied:
            del data[key][plan_name]
            if not data[key]:
                del data[key]
        await store.async_save(data)
        if history_emptied:
            await self._async_forget_kept_if_gone(fub_id, plan_name)
        _LOGGER.info(
            "[%s] Function Plan backup: deleted %s[%d] snapshot for fub=%s ('%s')",
            self._server_id,
            kind,
            slot,
            fub_id,
            plan_name,
        )
        return True

    async def async_delete_plan_backups(self, fub_id: int, plan_name: str) -> int:
        """Delete ALL stored snapshots (auto + change) for one plan identity. Returns the number removed."""
        await self._async_ensure_loaded()
        key = str(fub_id)
        removed = 0
        for data, store in ((self._auto_data, self._auto_store), (self._change_data, self._change_store)):
            identities = data.get(key)
            history = identities.pop(plan_name, None) if identities else None
            if identities is not None and not identities:
                data.pop(key, None)
            if history:
                removed += len(history)
                await store.async_save(data)
        await self._async_forget_kept_if_gone(fub_id, plan_name)
        if removed:
            _LOGGER.info(
                "[%s] Function Plan backup: deleted all %d snapshot(s) for fub=%s ('%s')",
                self._server_id,
                removed,
                fub_id,
                plan_name,
            )
        return removed

    async def async_delete_all_backups(self) -> int:
        """Delete every stored snapshot for every plan (auto + change). Returns the number removed."""
        await self._async_ensure_loaded()

        def _count(data: dict[str, dict[str, list]]) -> int:
            return sum(len(h) for identities in data.values() for h in identities.values())

        removed = _count(self._auto_data) + _count(self._change_data)
        self._auto_data = {}
        self._change_data = {}
        await self._auto_store.async_save(self._auto_data)
        await self._change_store.async_save(self._change_data)
        if self._kept:
            self._kept.clear()
            await self._async_save_kept()
        if removed:
            _LOGGER.info(
                "[%s] Function Plan backup: deleted ALL %d snapshot(s) across all plans", self._server_id, removed
            )
        return removed

    async def async_list_backups(self) -> dict[str, list[dict[str, Any]]]:
        """Return metadata (no element payloads) for all stored snapshots, newest first.

        Shape: {"auto": [...], "change": [...]} with entries
        {fub_id, plan_name, captured_at, hash, operation?, paper?, dpi?, orientation?, slot}.
        slot is scoped per (fub_id, plan_name) identity, not per raw fub_id.
        """
        await self._async_ensure_loaded()
        return {"auto": _list_backup_entries(self._auto_data), "change": _list_backup_entries(self._change_data)}

    async def async_load(self) -> None:
        """Ensure both stores are loaded into the caches (for sensors reading them synchronously)."""
        await self._async_ensure_loaded()

    async def async_backed_up_plans(self) -> list[tuple[int, str, int]]:
        """Return (fub_id, plan_name, snapshot_count) for every distinct (fub_id, plan_name)
        identity present in either store, sorted by fub_id then name (includes plans since
        deleted in Comexio, and — unlike a plain fub_id list — keeps two identities that
        currently share a reused fub_id distinct, since fub_id alone is not stable identity).
        The count (auto + change combined) tells the user how many versions are available.
        """
        await self._async_ensure_loaded()
        counts: dict[tuple[int, str], int] = {}
        for data in (self._auto_data, self._change_data):
            for key, identities in (data or {}).items():
                for plan_name, history in identities.items():
                    counts[(int(key), plan_name)] = counts.get((int(key), plan_name), 0) + len(history)
        return sorted((fub_id, name, count) for (fub_id, name), count in counts.items())

    def _orphaned_identities(self, fub_data: dict[str, Any]) -> list[tuple[int, str, str | None]]:
        """(fub_id, plan_name, newest_captured_at) for every backed-up identity no longer live.

        "Live" uses the same check as coordinator._stale_plan_map_entries — fub_id missing
        from the live $Fubs snapshot, or present but now naming a different plan (ID reused
        after deletion). Unlike that plan_map-scoped check, this covers every plan the backup
        cycle has ever snapshotted (function_plan_load_all_plans loads ALL Comexio function
        plans, not just HA-managed cluster ones), so a plan deleted directly in Comexio Studio
        shows up here even if it was never managed.
        """
        newest_by_identity: dict[tuple[int, str], str | None] = {}
        for data in (self._auto_data, self._change_data):
            for identity, captured_at in _newest_orphaned_in_store(data, fub_data).items():
                newest_by_identity[identity] = _newer_timestamp(newest_by_identity.get(identity), captured_at)
        return [(fid, name, ts) for (fid, name), ts in newest_by_identity.items()]

    def _snapshot_total(self, fub_id: int, plan_name: str) -> int:
        """Number of stored snapshots (auto + change) of one identity."""
        key = str(fub_id)
        return sum(len(data.get(key, {}).get(plan_name, [])) for data in (self._auto_data, self._change_data))

    def _expired_orphans(self, fub_data: dict[str, Any], cutoff: datetime) -> list[tuple[int, str, str | None]]:
        """Orphaned identities whose newest snapshot is older than cutoff, kept ones excluded.

        An identity without a readable timestamp is never treated as expired.
        """
        expired = []
        for fub_id, plan_name, newest in self._orphaned_identities(fub_data):
            ts = dt_util.parse_datetime(newest) if newest else None
            if ts is not None and ts < cutoff and (fub_id, plan_name) not in self._kept:
                expired.append((fub_id, plan_name, newest))
        return expired

    async def async_expired_orphans(self, fub_data: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]] | None:
        """Orphaned identities past the retention period that still need a decision.

        One {fub_id, plan_name, count, captured_at} entry each; kept identities are left out.
        None without a live $Fubs snapshot to compare against: a transient fetch hiccup must
        neither raise nor clear a repair. Also forgets kept identities that are live again or
        have no snapshots left, so a plan deleted a second time gets asked about again.
        """
        await self._async_ensure_loaded()
        if not fub_data:
            return None
        stale = {
            (fub_id, name)
            for fub_id, name in self._kept
            if not is_orphaned_identity(fub_data, fub_id, name) or not self._snapshot_total(fub_id, name)
        }
        if stale:
            self._kept -= stale
            await self._async_save_kept()
        return [
            {"fub_id": fub_id, "plan_name": name, "count": self._snapshot_total(fub_id, name), "captured_at": newest}
            for fub_id, name, newest in self._expired_orphans(fub_data, cutoff)
        ]

    async def async_keep_orphaned(self, fub_id: int, plan_name: str) -> bool:
        """Keep an orphaned identity's snapshots for good: no repair, no purge, only manual deletion.

        False if it was kept already.
        """
        await self._async_ensure_loaded()
        if (fub_id, plan_name) in self._kept:
            return False
        self._kept.add((fub_id, plan_name))
        await self._async_save_kept()
        _LOGGER.info(
            "[%s] Function Plan backup: keeping the backups of deleted plan fub=%s ('%s')",
            self._server_id,
            fub_id,
            plan_name,
        )
        return True

    async def async_unkeep_orphaned(self, fub_id: int, plan_name: str) -> bool:
        """Take back a keep decision (plan card's orphaned-plans view). True if one was stored.

        The identity is then asked about again, and purged by the purge action, once its
        retention has passed.
        """
        await self._async_ensure_loaded()
        if (fub_id, plan_name) not in self._kept:
            return False
        self._kept.discard((fub_id, plan_name))
        await self._async_save_kept()
        _LOGGER.info(
            "[%s] Function Plan backup: no longer keeping the backups of deleted plan fub=%s ('%s')",
            self._server_id,
            fub_id,
            plan_name,
        )
        return True

    def orphaned_plans_sync(self, fub_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Every orphaned identity that still has snapshots, sorted by plan name, then fub_id.

        One {fub_id, plan_name, kept, backups} entry each, backups as in
        plan_backups_for_identity_sync. Cache-only like that method, so it can back the
        selectors' synchronous options. Empty without a live $Fubs snapshot (see
        is_orphaned_identity), so a failed plan fetch never shows every plan as deleted.
        """
        orphans: list[dict[str, Any]] = []
        for fub_id, plan_name, _newest in self._orphaned_identities(fub_data):
            if backups := self.plan_backups_for_identity_sync(fub_id, plan_name):
                kept = (fub_id, plan_name) in self._kept
                orphans.append({"fub_id": fub_id, "plan_name": plan_name, "kept": kept, "backups": backups})
        orphans.sort(key=lambda orphan: (orphan["plan_name"].lower(), orphan["fub_id"]))
        return orphans

    async def async_purge_orphaned(self, fub_data: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
        """Delete all snapshots (auto + change) of every orphaned identity older than cutoff.

        Only the manual function_plan_purge_orphaned_backups service calls this; the periodic
        backup cycle raises a repair per identity instead (see orphaned_backups.py). A live
        plan's backups are never touched, no matter how old, and neither are kept ones.
        Returns one {fub_id, plan_name, removed, captured_at} entry per identity actually purged.
        """
        await self._async_ensure_loaded()
        if not fub_data:
            # No live $Fubs snapshot to compare against — treating that as "everything is
            # orphaned" would wipe every backup on a transient fetch hiccup.
            return []
        purged: list[dict[str, Any]] = []
        for fub_id, plan_name, newest in self._expired_orphans(fub_data, cutoff):
            if removed := await self.async_delete_plan_backups(fub_id, plan_name):
                purged.append({"fub_id": fub_id, "plan_name": plan_name, "removed": removed, "captured_at": newest})
        return purged

    async def async_referenced_marker_ids(self) -> set[str]:
        """Marker IDs (type-2 element refs) in the NEWEST auto snapshot of every plan identity.

        Cold-start seed for coordinator._referenced_marker_ids(): the live bulk plan snapshot
        is loaded asynchronously AFTER the first poll cycle, but platform setup creates
        entities right after that first cycle — so an unlabeled-but-wired marker (see
        aiocomexio parse_config) would miss entity creation on every restart without this
        offline approximation. Slightly stale data (e.g. an identity whose plan was since
        deleted) is corrected by the refresh-on-change trigger once the bulk load lands.
        """
        await self._async_ensure_loaded()
        referenced: set[str] = set()
        for identities in self._auto_data.values():
            for history in identities.values():
                if not history:
                    continue
                for elem in (history[0].get("elements") or {}).values():
                    ref = elem.get("reference") or {}
                    if str(ref.get("type")) == "2":
                        referenced.add(str(ref.get("ref_id")))
        return referenced

    def plan_backups_for_identity_sync(self, fub_id: int, plan_name: str) -> list[dict[str, Any]]:
        """Return auto+change snapshot metadata for one plan identity, newest first per kind.

        Sync + cache-only (like summary()) so it can back a select entity's synchronous
        `options` property without an event-loop round-trip. Empty before the first
        async_load()/async_auto_backup() populates the cache.
        """
        key = str(fub_id)
        entries: list[dict[str, Any]] = []
        for kind, data in (("auto", self._auto_data), ("change", self._change_data)):
            history = (data or {}).get(key, {}).get(plan_name, [])
            for slot, snap in enumerate(history):
                entry = {"kind": kind, "slot": slot, "captured_at": snap.get("captured_at")}
                if "operation" in snap:
                    entry["operation"] = snap["operation"]
                if "restored_at" in snap:
                    entry["restored_at"] = snap["restored_at"]
                entries.append(entry)
        return entries

    def summary(self) -> dict[str, Any]:
        """Return a sync summary from the caches (zeros before the first async_load)."""
        return {
            "auto_snapshots": _snapshot_count(self._auto_data),
            "change_snapshots": _snapshot_count(self._change_data),
            "plans_with_auto_backup": _identity_count(self._auto_data),
            "plans_with_change_backup": _identity_count(self._change_data),
            "last_capture": _latest_capture(self._auto_data, self._change_data),
        }

    async def async_get_snapshot(self, kind: str, fub_id: int, plan_name: str, slot: int = 0) -> dict[str, Any] | None:
        """Return the full snapshot (incl. elements/connections) for a plan identity, or None.

        kind: "auto" or "change"; slot: 0 = newest.
        """
        await self._async_ensure_loaded()
        data = self._auto_data if kind == "auto" else self._change_data
        history = data.get(str(fub_id), {}).get(plan_name, [])
        if 0 <= slot < len(history):
            return history[slot]
        return None
