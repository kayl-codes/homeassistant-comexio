"""Protect function plan backups against shifted block ids.

Comexio addresses a logic block (fubBase, plan element type 5) by its $FubModules["5"] id. Those
ids are handed out in installation order, so installing a Comexio app or a firmware update can
shift them: a backup's stored ref_id then names a different block, or none at all.

A snapshot therefore freezes, at capture, the stable key of every block id it uses
(aiocomexio.reference_catalog.fub_base_key: "<Name>/<input port types>/<output port types>").
The plan data itself is never rewritten. Ids are translated only when a snapshot is used
(preview, diff, restore): stored id -> frozen key -> today's id. That holds across any number of
shifts and across servers, without chains, and without touching a stored backup when ids move.

Snapshots taken before keys existed get them backfilled once from today's catalog
(backfill_block_keys). How far that can be trusted is recorded in SNAPSHOT_KEYS_SOURCE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiocomexio.function_plan import plan_hash
from aiocomexio.reference_catalog import KEY_SEPARATOR
from homeassistant.util import dt as dt_util

# Plan element reference type of a logic block (fubBase).
BLOCK_ELEMENT_TYPE = "5"

SNAPSHOT_KEYS = "fub_base_keys"
SNAPSHOT_KEYS_SOURCE = "fub_base_keys_source"
# Keys frozen from the catalog at capture time.
SOURCE_CAPTURED = "captured"
# Backfilled from today's catalog; the snapshot was captured after the block ids last changed.
SOURCE_BACKFILLED = "backfilled"
# Backfilled, captured before the last block-id change, but every wire fits today's blocks.
SOURCE_BACKFILLED_PLAUSIBLE = "backfilled_plausible"
# Backfilled, captured before the last block-id change, and a wire does not fit today's blocks.
SOURCE_BACKFILLED_UNVERIFIED = "backfilled_unverified"

# Set on the copy async_get_snapshot returns: blocks whose id has no unique live counterpart.
UNRESOLVED_BLOCKS = "unresolved_blocks"
REASON_NO_KEY = "no key"  # the id was not in the catalog when the keys were frozen
REASON_MISSING = "missing"  # no live block has this key
REASON_AMBIGUOUS = "ambiguous"  # several live blocks have this key
# Missing/ambiguous blocks get this ref_id prefix, so no renderer or diff mistakes them for a live block.
# A block without a key keeps its stored id: it may be restored with an explicit opt-in.
UNRESOLVED_REF_PREFIX = "unresolved:"

# Set on the copy async_get_snapshot returns when its block ids could not be checked at all.
BLOCK_IDS_UNCHECKED = "block_ids_unchecked"
UNCHECKED_NO_KEYS = "no keys"  # not backfilled yet
UNCHECKED_NO_CATALOG = "no catalog"  # no verified block catalog right now

# Set on the copy async_get_snapshot returns for a backfilled_unverified snapshot: why its wiring
# does not fit today's blocks (implausible_block_wiring).
UNVERIFIED_BLOCK_PROBLEMS = "unverified_block_problems"
# The catalog while none is verified: one shared object (never mutated), so identity memos still hit.
NO_FUB_BASE: dict[str, Any] = {}

# block_check status: how far a snapshot's block ids can be trusted (None = exactly known).
CHECK_UNRESOLVED = "unresolved"  # a block has no unique live counterpart: a restore is refused
CHECK_UNVERIFIED = "unverified"  # blocks cannot be checked: a restore needs accept_unverified_blocks
CHECK_PLAUSIBLE = "plausible"  # derived from today's catalog, every wire fits today's blocks

_REASON_TEXT = {
    REASON_MISSING: "no block of this kind exists on Comexio today (app or firmware removed it?)",
    REASON_AMBIGUOUS: "several blocks on Comexio today match it",
    REASON_NO_KEY: "the block was unknown to the block catalog when the backup was taken",
}
_UNCHECKED_TEXT = {
    UNCHECKED_NO_KEYS: "the block ids of this backup have not been checked yet (wait for the next backup cycle)",
    UNCHECKED_NO_CATALOG: "Comexio's block catalog could not be verified in the last poll",
}
_BACKFILLED_UNVERIFIED_TEXT = "the wiring of this backup did not fit the blocks when its block ids were derived"

_PORT_TYPE_CODES = {0: "d", 1: "a"}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _type_codes(types: Any) -> str:
    return "".join(_PORT_TYPE_CODES.get(port_type, str(port_type)) for port_type in types or [])


def catalog_entry_key(entry: Mapping[str, Any]) -> str | None:
    """Stable key of one cached fub_base entry (function_plan_catalog), None without a Name.

    Uses the key stored at extraction; entries cached before that derive it from name and port
    types, which gives the same key for digital/analog ports.
    """
    if key := entry.get("key"):
        return str(key)
    if not (name := entry.get("name")):
        return None
    return KEY_SEPARATOR.join((str(name), _type_codes(entry.get("in_types")), _type_codes(entry.get("out_types"))))


def block_key_map(fub_base: Mapping[str, Any]) -> dict[str, str]:
    """{block id: key} of a cached fub_base catalog."""
    return {
        str(ref_id): key
        for ref_id, entry in fub_base.items()
        if isinstance(entry, Mapping) and (key := catalog_entry_key(entry))
    }


def block_ids_by_key(fub_base: Mapping[str, Any]) -> dict[str, list[str]]:
    """{key: [block ids]} of a cached fub_base catalog — a list, so duplicates stay visible."""
    ids_by_key: dict[str, list[str]] = {}
    for ref_id, key in block_key_map(fub_base).items():
        ids_by_key.setdefault(key, []).append(ref_id)
    return ids_by_key


def block_ids_changed(old_base: Mapping[str, Any], new_base: Mapping[str, Any]) -> bool:
    """Whether any block id now names a different block (or appeared/disappeared).

    Text-only changes (UI language, descriptions) don't count — they don't move ids.
    """
    return block_key_map(old_base) != block_key_map(new_base)


def _block_reference(elem: Any) -> dict[str, Any] | None:
    reference = elem.get("reference") if isinstance(elem, Mapping) else None
    if isinstance(reference, Mapping) and str(reference.get("type")) == BLOCK_ELEMENT_TYPE:
        return dict(reference)
    return None


def _used_block_ids(elements: Mapping[str, Any]) -> set[str]:
    return {
        str(reference.get("ref_id"))
        for elem in elements.values()
        if (reference := _block_reference(elem)) is not None and reference.get("ref_id") is not None
    }


def capture_block_keys(elements: Mapping[str, Any], fub_base: Mapping[str, Any]) -> dict[str, str]:
    """{block id: key} for every block id the plan uses that the catalog knows."""
    key_map = block_key_map(fub_base)
    return {ref_id: key_map[ref_id] for ref_id in sorted(_used_block_ids(elements)) if ref_id in key_map}


def uses_unknown_block_ids(elements: Mapping[str, Any], fub_base: Mapping[str, Any]) -> bool:
    """True when the plan uses a block id the catalog has no key for (e.g. an app installed since the last poll)."""
    return not _used_block_ids(elements) <= block_key_map(fub_base).keys()


# ---------------------------------------------------------------------------
# Port plausibility: does a snapshot's wiring fit today's blocks?
# ---------------------------------------------------------------------------


def _endpoint(port: Any) -> tuple[str, int] | None:
    if not isinstance(port, Mapping):
        return None
    elem_id, pos = port.get("FubElementId"), _as_int(port.get("IOPos"))
    return (str(elem_id), pos) if elem_id is not None and pos is not None else None


def _block_def(
    elements: Mapping[str, Any], fub_base: Mapping[str, Any], endpoint: tuple[str, int] | None
) -> Mapping[str, Any] | None:
    if endpoint is None or (reference := _block_reference(elements.get(endpoint[0]))) is None:
        return None
    block = fub_base.get(str(reference.get("ref_id")))
    return block if isinstance(block, Mapping) else None


def _port_type(block: Mapping[str, Any] | None, types_field: str, pos: int) -> Any:
    types = block.get(types_field) if block else None
    return types[pos] if isinstance(types, list) and 0 <= pos < len(types) else None


def _input_count(block: Mapping[str, Any]) -> int:
    # Autogrow blocks (e.g. Oder) may have more inputs than the catalog's base variant declares.
    return max(_as_int(block.get("n_in")) or 0, _as_int(block.get("autogrow")) or 0)


def _sink_problems(
    elements: Mapping[str, Any],
    fub_base: Mapping[str, Any],
    source: tuple[str, int] | None,
    source_block: Mapping[str, Any] | None,
    sinks: Any,
) -> list[str]:
    problems: list[str] = []
    source_type = _port_type(source_block, "out_types", source[1]) if source else None
    for sink in sinks if isinstance(sinks, list) else []:
        endpoint = _endpoint(sink)
        if endpoint is None or (block := _block_def(elements, fub_base, endpoint)) is None:
            continue
        elem_id, pos = endpoint
        if pos >= _input_count(block):
            problems.append(f"element {elem_id}: input {pos} beyond the {block.get('name')} block's inputs")
            continue
        sink_type = _port_type(block, "in_types", pos)
        if None not in (source_type, sink_type) and source_type != sink_type:
            problems.append(f"element {elem_id}: input {pos} wired to an output of another data type")
    return problems


def _wired_element_ids(connections: Mapping[str, Any]) -> set[str]:
    wired: set[str] = set()
    for conn in connections.values():
        if not isinstance(conn, Mapping):
            continue
        sinks = conn.get("output")
        for port in [conn.get("input"), *(sinks if isinstance(sinks, list) else [])]:
            if (endpoint := _endpoint(port)) is not None:
                wired.add(endpoint[0])
    return wired


def _uncheckable_blocks(
    elements: Mapping[str, Any], connections: Mapping[str, Any], fub_base: Mapping[str, Any]
) -> list[str]:
    """Blocks the wiring check can say nothing about: unknown to today's catalog, or not wired at all."""
    wired = _wired_element_ids(connections)
    problems: list[str] = []
    for elem_id, elem in elements.items():
        if (reference := _block_reference(elem)) is None:
            continue
        if str(reference.get("ref_id")) not in fub_base:
            problems.append(f"element {elem_id}: block id {reference.get('ref_id')} unknown today")
        elif str(elem_id) not in wired:
            problems.append(f"element {elem_id}: no wire to check the block against")
    return problems


def implausible_block_wiring(
    elements: Mapping[str, Any], connections: Mapping[str, Any], fub_base: Mapping[str, Any]
) -> list[str]:
    """Blocks and wires of a snapshot that don't fit today's blocks, read with today's ids.

    Checks every wire end at a block: the port must exist (inputs up to the autogrow limit), and a
    wire between two blocks must join ports of the same data type (digital/analog). A shifted id
    usually names a block with other ports, which this catches. A block without any wire, or whose
    id today's catalog lacks, cannot be checked and counts as a problem too. An empty result is a
    plausibility hint, not a proof.
    """
    problems = _uncheckable_blocks(elements, connections, fub_base)
    for conn in connections.values():
        if not isinstance(conn, Mapping):
            continue
        source = _endpoint(conn.get("input"))
        source_block = _block_def(elements, fub_base, source)
        if source and source_block and source[1] >= (_as_int(source_block.get("n_out")) or 0):
            problems.append(
                f"element {source[0]}: output {source[1]} beyond the {source_block.get('name')} block's outputs"
            )
            source_block = None
        problems.extend(_sink_problems(elements, fub_base, source, source_block, conn.get("output")))
    return problems


# ---------------------------------------------------------------------------
# Backfill of snapshots captured before keys existed
# ---------------------------------------------------------------------------


def _captured_since(snapshot: Mapping[str, Any], block_ids_changed_at: str | None) -> bool:
    captured = dt_util.parse_datetime(str(snapshot.get("captured_at") or ""))
    changed = dt_util.parse_datetime(str(block_ids_changed_at or ""))
    return captured is not None and changed is not None and captured >= changed


def backfill_block_keys(
    snapshot: dict[str, Any], fub_base: Mapping[str, Any], block_ids_changed_at: str | None
) -> str | None:
    """Add keys from today's catalog to a snapshot that has none; returns the source, None if it had keys.

    Exact when the snapshot was captured after the block ids last changed (today's catalog was
    already valid then). Older ones are checked against today's blocks by their wiring.
    """
    if SNAPSHOT_KEYS in snapshot:
        return None
    elements = snapshot.get("elements") or {}
    if _captured_since(snapshot, block_ids_changed_at):
        source = SOURCE_BACKFILLED
    elif implausible_block_wiring(elements, snapshot.get("connections") or {}, fub_base):
        source = SOURCE_BACKFILLED_UNVERIFIED
    else:
        source = SOURCE_BACKFILLED_PLAUSIBLE
    snapshot[SNAPSHOT_KEYS] = capture_block_keys(elements, fub_base)
    snapshot[SNAPSHOT_KEYS_SOURCE] = source
    return source


# ---------------------------------------------------------------------------
# Use: translate a snapshot's block ids to today's
# ---------------------------------------------------------------------------


def _live_block_id(key: str, old_id: str, ids_by_key: Mapping[str, list[str]]) -> tuple[str | None, str | None]:
    """(today's id, None) or (None, reason) for one frozen key; the stored id wins while it still fits."""
    ids = ids_by_key.get(key) or []
    if old_id in ids:
        return old_id, None
    if not ids:
        return None, REASON_MISSING
    if len(ids) > 1:
        return None, REASON_AMBIGUOUS
    return ids[0], None


def _same_id_type(new_id: str, old_id: Any) -> Any:
    return int(new_id) if isinstance(old_id, int) and new_id.isdigit() else new_id


def _unresolved_element(elem: Mapping[str, Any], reference: dict[str, Any], key: str | None) -> dict[str, Any]:
    """The element marked as unknown: a ref_id no catalog has and a name saying which block it was."""
    old_id = reference.get("ref_id")
    name = elem.get("name") or ""
    label = f"⚠ {key or f'block id {old_id}'}"
    return {
        **elem,
        "name": f"{label}: {name}" if name else label,
        "reference": {**reference, "ref_id": f"{UNRESOLVED_REF_PREFIX}{old_id}"},
    }


def _resolve_element(
    elem: Any, keys: Mapping[str, Any], ids_by_key: Mapping[str, list[str]]
) -> tuple[Any, str | None, str | None]:
    """(element with today's block id, its key, why it could not be resolved or None)."""
    reference = _block_reference(elem)
    if reference is None:
        return elem, None, None
    old_id = reference.get("ref_id")
    key = keys.get(str(old_id))
    if key is None:
        # Unknown to the catalog at capture: kept as stored, a restore needs the opt-in.
        return elem, None, REASON_NO_KEY
    live_id, reason = _live_block_id(str(key), str(old_id), ids_by_key)
    if live_id is None:
        return _unresolved_element(elem, reference, key), key, reason
    if live_id == str(old_id):
        return elem, key, None
    return {**elem, "reference": {**reference, "ref_id": _same_id_type(live_id, old_id)}}, key, None


def resolve_block_ids(snapshot: dict[str, Any], fub_base: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot with today's block ids (a new dict when anything changed, else the same one).

    A block without a unique live counterpart is marked (see _unresolved_element) and listed under
    UNRESOLVED_BLOCKS as {element, ref_id, key, reason}; a restore must refuse such a snapshot.
    Without keys (not backfilled yet) or without a catalog the stored ids are kept, and a snapshot
    that uses blocks says so under BLOCK_IDS_UNCHECKED — a restore then needs the opt-in. A
    backfilled_unverified snapshot lists why under UNVERIFIED_BLOCK_PROBLEMS.
    """
    keys = snapshot.get(SNAPSHOT_KEYS)
    stored = snapshot.get("elements") or {}
    if not isinstance(keys, Mapping) or not fub_base:
        if not _used_block_ids(stored):
            return snapshot
        return {**snapshot, BLOCK_IDS_UNCHECKED: UNCHECKED_NO_KEYS if fub_base else UNCHECKED_NO_CATALOG}
    elements, unresolved = _resolve_elements(stored, keys, block_ids_by_key(fub_base))
    extra: dict[str, Any] = {}
    if snapshot.get(SNAPSHOT_KEYS_SOURCE) == SOURCE_BACKFILLED_UNVERIFIED:
        extra[UNVERIFIED_BLOCK_PROBLEMS] = implausible_block_wiring(
            elements, snapshot.get("connections") or {}, fub_base
        )
    if not unresolved and elements == stored:
        return snapshot | extra if extra else snapshot
    resolved = snapshot | extra | {"elements": elements, UNRESOLVED_BLOCKS: unresolved}
    # The stored hash is over the stored ids; a restore verifies against the translated plan.
    resolved["hash"] = plan_hash(resolved)
    return resolved


def _resolve_elements(
    stored: Mapping[str, Any], keys: Mapping[str, Any], ids_by_key: Mapping[str, list[str]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(elements with today's block ids, UNRESOLVED_BLOCKS entries) of one snapshot."""
    elements: dict[str, Any] = {}
    unresolved: list[dict[str, Any]] = []
    for elem_id, elem in stored.items():
        elements[elem_id], key, reason = _resolve_element(elem, keys, ids_by_key)
        if reason is not None:
            old_id = (_block_reference(elem) or {}).get("ref_id")
            unresolved.append({"element": str(elem_id), "ref_id": old_id, "key": key, "reason": reason})
    return elements, unresolved


def unresolved_block_lines(blocks: list[dict[str, Any]]) -> list[str]:
    """One line per UNRESOLVED_BLOCKS entry: which element, which block, and why."""
    return [
        f"element {block['element']}: block id {block['ref_id']} ({block['key'] or 'no key'}): "
        f"{_REASON_TEXT.get(block['reason'], block['reason'])}"
        for block in blocks
    ]


def block_check(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """{status, problems} of a snapshot as resolve_block_ids returned it; None when its block ids are exact.

    status is CHECK_UNRESOLVED, CHECK_UNVERIFIED or CHECK_PLAUSIBLE; problems are lines naming the
    affected elements (empty for CHECK_PLAUSIBLE). Read by the restore service and the plan card.
    """
    unresolved = snapshot.get(UNRESOLVED_BLOCKS) or []
    if hard := [block for block in unresolved if block["reason"] != REASON_NO_KEY]:
        return {"status": CHECK_UNRESOLVED, "problems": unresolved_block_lines(hard)}
    if unresolved:
        return {"status": CHECK_UNVERIFIED, "problems": unresolved_block_lines(unresolved)}
    if unchecked := snapshot.get(BLOCK_IDS_UNCHECKED):
        return {"status": CHECK_UNVERIFIED, "problems": [_UNCHECKED_TEXT.get(unchecked, str(unchecked))]}
    source = snapshot.get(SNAPSHOT_KEYS_SOURCE)
    if source == SOURCE_BACKFILLED_UNVERIFIED:
        # Empty when today's catalog finds nothing wrong any more; the backfill verdict still stands.
        problems = snapshot.get(UNVERIFIED_BLOCK_PROBLEMS) or [_BACKFILLED_UNVERIFIED_TEXT]
        return {"status": CHECK_UNVERIFIED, "problems": list(problems)}
    if source == SOURCE_BACKFILLED_PLAUSIBLE:
        return {"status": CHECK_PLAUSIBLE, "problems": []}
    return None
