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
from typing import Any, NamedTuple

from aiocomexio.function_plan import plan_hash
from aiocomexio.reference_catalog import KEY_SEPARATOR
from homeassistant.util import dt as dt_util

from .const import one_line
from .function_plan_block_settings import SNAPSHOT_BLOCK_SETTINGS, snapshot_block_settings

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
# The reasons omit_unresolved_blocks may leave a block out for (the block is gone or cannot be told apart).
OMITTABLE_REASONS = frozenset({REASON_MISSING, REASON_AMBIGUOUS})
# Set on the copy omit_unresolved_blocks returns: what it left out.
OMITTED_BLOCKS = "omitted_blocks"
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
# Keys of the plan dicts and of the entries this module builds, which repeat too often to stay literals.
_ELEMENTS = "elements"
_CONNECTIONS = "connections"
_OUTPUT = "output"
_INPUT = "input"
_HASH = "hash"
_FUB_ELEMENT_ID = "FubElementId"
_ELEMENT = "element"
_REASON = "reason"
_NAME = "name"
_REF_ID = "ref_id"
_KEY = "key"
_UNFED = "unfed"
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
    elem_id, pos = port.get(_FUB_ELEMENT_ID), _as_int(port.get("IOPos"))
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
            problems.append(f"element {elem_id}: input {pos} beyond the {one_line(block.get('name'))} block's inputs")
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
        sinks = conn.get(_OUTPUT)
        for port in [conn.get(_INPUT), *(sinks if isinstance(sinks, list) else [])]:
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
        source = _endpoint(conn.get(_INPUT))
        source_block = _block_def(elements, fub_base, source)
        if source and source_block and source[1] >= (_as_int(source_block.get("n_out")) or 0):
            problems.append(
                f"element {source[0]}: output {source[1]} beyond the "
                f"{one_line(source_block.get('name'))} block's outputs"
            )
            source_block = None
        problems.extend(_sink_problems(elements, fub_base, source, source_block, conn.get(_OUTPUT)))
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
    elements = snapshot.get(_ELEMENTS) or {}
    if _captured_since(snapshot, block_ids_changed_at):
        source = SOURCE_BACKFILLED
    elif implausible_block_wiring(elements, snapshot.get(_CONNECTIONS) or {}, fub_base):
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


def _unresolved_label(key: str | None, old_id: Any) -> str:
    return f"⚠ {key or f'block id {old_id}'}"


def _unresolved_element(elem: Mapping[str, Any], reference: dict[str, Any], key: str | None) -> dict[str, Any]:
    """The element marked as unknown: a ref_id no catalog has and a name saying which block it was."""
    old_id = reference.get("ref_id")
    name = elem.get("name") or ""
    label = _unresolved_label(key, old_id)
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
    stored = snapshot.get(_ELEMENTS) or {}
    if not isinstance(keys, Mapping) or not fub_base:
        if not _used_block_ids(stored):
            return snapshot
        return {**snapshot, BLOCK_IDS_UNCHECKED: UNCHECKED_NO_KEYS if fub_base else UNCHECKED_NO_CATALOG}
    elements, unresolved = _resolve_elements(stored, keys, block_ids_by_key(fub_base))
    extra: dict[str, Any] = {}
    if snapshot.get(SNAPSHOT_KEYS_SOURCE) == SOURCE_BACKFILLED_UNVERIFIED:
        extra[UNVERIFIED_BLOCK_PROBLEMS] = implausible_block_wiring(
            elements, snapshot.get(_CONNECTIONS) or {}, fub_base
        )
    if not unresolved and elements == stored:
        return snapshot | extra if extra else snapshot
    resolved = snapshot | extra | {_ELEMENTS: elements, UNRESOLVED_BLOCKS: unresolved}
    # The stored hash is over the stored ids; a restore verifies against the translated plan.
    resolved[_HASH] = plan_hash(resolved)
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
            unresolved.append({_ELEMENT: str(elem_id), _REF_ID: old_id, _KEY: key, _REASON: reason})
    return elements, unresolved


def unresolved_block_lines(blocks: list[dict[str, Any]]) -> list[str]:
    """One line per UNRESOLVED_BLOCKS entry: which element, which block, and why."""
    return [
        f"element {one_line(block[_ELEMENT])}: block id {one_line(block[_REF_ID])} "
        f"({one_line(block[_KEY]) if block[_KEY] else 'no key'}): "
        f"{_REASON_TEXT.get(block[_REASON], block[_REASON])}"
        for block in blocks
    ]


# ---------------------------------------------------------------------------
# Restore without the blocks Comexio no longer has
# ---------------------------------------------------------------------------


def _endpoint_element(port: Any) -> str | None:
    """The element id a wire end names, None for anything that is no end."""
    if isinstance(port, Mapping) and (element := port.get(_FUB_ELEMENT_ID)) is not None:
        return str(element)
    return None


class _Cut(NamedTuple):
    """What cutting the omitted elements out of the connections left and cost."""

    connections: dict[str, Any]
    affected: dict[str, int]  # {omitted element: connections it made drop or shrink}
    unfed: dict[str, set[str]]  # {omitted source: elements that lost their input with its wire}


def _cut_connection(conn: Mapping[str, Any], omitted: set[str]) -> tuple[Mapping[str, Any] | None, set[str], set[str]]:
    """(the connection without its omitted ends, or None when nothing of it is left; the omitted
    elements it touched; the elements that lost their input because the source of the wire is omitted).

    A wire whose source is omitted has nothing left to carry and goes whole. Of a fan-out only the
    ends at an omitted element go; the wire stays while any sink does. A connection that names no
    omitted element comes back as it is. Comexio keeps connections without an "input", and writes
    "output" as a list or as an object keyed by position ({"0": {...}}), as plan_hash reads both; a
    shortened wire comes back as a list. A connection with a malformed end would already have
    failed plan_hash in resolve_block_ids.
    """
    sinks = conn.get(_OUTPUT) or []
    sinks = list(sinks.values()) if isinstance(sinks, Mapping) else sinks
    source = _endpoint_element(conn.get(_INPUT))
    ends = (source, *(_endpoint_element(sink) for sink in sinks))
    touched = {element for element in ends if element is not None and element in omitted}
    if not touched:
        return conn, touched, set()
    if source in omitted:
        unfed = {element for sink in sinks if (element := _endpoint_element(sink)) and element not in omitted}
        return None, touched, unfed
    remaining = [sink for sink in sinks if _endpoint_element(sink) not in omitted]
    return ({**conn, _OUTPUT: remaining} if remaining else None), touched, set()


def _cut_connections(connections: Mapping[str, Any], omitted: set[str]) -> _Cut:
    """The connections without the omitted ends, and what that cost (see _Cut)."""
    kept: dict[str, Any] = {}
    affected = dict.fromkeys(omitted, 0)
    unfed: dict[str, set[str]] = {}
    for conn_id, conn in connections.items():
        cut, touched, fed_no_more = _cut_connection(conn, omitted)
        for element in touched:
            affected[element] += 1
        if fed_no_more:
            unfed.setdefault(str(_endpoint_element(conn.get(_INPUT))), set()).update(fed_no_more)
        if cut is not None:
            kept[conn_id] = cut
    return _Cut(kept, affected, unfed)


def _omitted_report(block: Mapping[str, Any], elements: Mapping[str, Any], cut: _Cut) -> dict[str, Any]:
    """One OMITTED_BLOCKS entry; the name is the element's own again, without the marker of _unresolved_element.

    elements are the ones resolve_block_ids returned (the marked element), cut what _cut_connections left.
    """
    name = str((elements.get(str(block[_ELEMENT])) or {}).get(_NAME) or "")
    label = _unresolved_label(block[_KEY], block[_REF_ID])
    if name == label:
        name = ""
    elif name.startswith(f"{label}: "):
        name = name[len(label) + 2 :]
    return {
        _ELEMENT: block[_ELEMENT],
        _NAME: name,
        _REF_ID: block[_REF_ID],
        _KEY: block[_KEY],
        _REASON: block[_REASON],
        _CONNECTIONS: cut.affected[str(block[_ELEMENT])],
        _UNFED: sorted(cut.unfed.get(str(block[_ELEMENT]), ()), key=lambda element: (len(element), element)),
    }


def omit_unresolved_blocks(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The snapshot without the blocks Comexio no longer has, as resolve_block_ids returned it.

    Only a block with no live counterpart at all (REASON_MISSING) or no unique one
    (REASON_AMBIGUOUS) is left out, with the connections that end at it (see _cut_connection) and
    its block settings; a block that is merely unknown to the catalog (REASON_NO_KEY) stays and
    keeps needing accept_unverified_blocks. The result carries what was left out under
    OMITTED_BLOCKS ({element, name, ref_id, key, reason, connections, unfed}: unfed are the elements
    that lose their input with the wire of an omitted source) and lists only the remaining blocks
    under UNRESOLVED_BLOCKS, so block_check judges what a restore would build. The hash is recomputed
    like resolve_block_ids does. The same snapshot comes back when nothing is to be omitted (so a
    second call on the result changes nothing); the stored one is never changed. Everything else
    stays as it was: the labels belong to marker, Web-IO and IO elements, fub_base_keys to block ids
    and module_configs to scene and Quantum elements, none of which is a logic block.
    """
    unresolved = snapshot.get(UNRESOLVED_BLOCKS) or []
    omit = [block for block in unresolved if block[_REASON] in OMITTABLE_REASONS]
    if not omit:
        return snapshot
    omitted = {str(block[_ELEMENT]) for block in omit}
    stored = snapshot.get(_ELEMENTS) or {}
    cut = _cut_connections(snapshot.get(_CONNECTIONS) or {}, omitted)
    result = {
        **snapshot,
        _ELEMENTS: _without_elements(stored, omitted),
        _CONNECTIONS: cut.connections,
        UNRESOLVED_BLOCKS: [block for block in unresolved if block not in omit],
        OMITTED_BLOCKS: [_omitted_report(block, stored, cut) for block in omit],
    }
    if (settings := snapshot_block_settings(snapshot)) is not None:
        result[SNAPSHOT_BLOCK_SETTINGS] = _without_elements(settings, omitted)
    if UNVERIFIED_BLOCK_PROBLEMS in snapshot:
        result[UNVERIFIED_BLOCK_PROBLEMS] = _problems_of_the_rest(snapshot[UNVERIFIED_BLOCK_PROBLEMS], omitted)
    result[_HASH] = plan_hash(result)
    return result


def _problems_of_the_rest(problems: list[str], omitted: set[str]) -> list[str]:
    """The implausible-wiring lines (implausible_block_wiring) that do not name an omitted element.

    Lines about the elements that lost their input with a cut wire stay as they were: the list is
    a plausibility hint that errs towards asking for the opt-in, never towards skipping it.
    """
    return [line for line in problems if not any(line.startswith(f"element {elem_id}:") for elem_id in omitted)]


def _without_elements(by_element: Mapping[str, Any], omitted: set[str]) -> dict[str, Any]:
    """by_element ({element id: ...}) without the omitted elements."""
    return {elem_id: value for elem_id, value in by_element.items() if str(elem_id) not in omitted}


def omitted_block_lines(blocks: list[dict[str, Any]]) -> list[str]:
    """One line per OMITTED_BLOCKS entry: which element, which block, why, and what its wires cost."""
    return [_omitted_line(block) for block in blocks]


def _omitted_line(block: Mapping[str, Any]) -> str:
    title = f" ({one_line(block[_NAME])})" if block[_NAME] else ""
    reason = _REASON_TEXT.get(block[_REASON], block[_REASON])
    unfed = (
        f"; input lost at element {', '.join(one_line(element) for element in block[_UNFED])}" if block[_UNFED] else ""
    )
    key = one_line(block[_KEY]) if block[_KEY] else "no key"
    return (
        f"element {one_line(block[_ELEMENT])}{title}: block id {one_line(block[_REF_ID])} ({key}): {reason}; "
        f"{block[_CONNECTIONS]} connection(s) dropped or shortened{unfed}"
    )


def block_check(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """{status, problems} of a snapshot as resolve_block_ids returned it; None when its block ids are exact.

    status is CHECK_UNRESOLVED, CHECK_UNVERIFIED or CHECK_PLAUSIBLE; problems are lines naming the
    affected elements (empty for CHECK_PLAUSIBLE). Read by the restore service and the plan card.
    """
    unresolved = snapshot.get(UNRESOLVED_BLOCKS) or []
    if hard := [block for block in unresolved if block[_REASON] != REASON_NO_KEY]:
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


CARD_OMIT_LINES = 30  # omitted blocks the card's check names; the restore's result and log have them all


def block_check_for_card(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """block_check, plus what a copy without the blocks Comexio no longer has would be like.

    For the plan card's restore dialog. A CHECK_UNRESOLVED check gets an "omit" entry:
    "blocks" are the omitted_block_lines (which blocks go, what their wires cost) and "then"
    is the block_check of the snapshot cut that way (None when it is exact, CHECK_UNVERIFIED
    when a block unknown to the catalog or a backfill verdict remains, which the copy's restore
    still needs accept_unverified_blocks for). Every other check comes back as block_check
    returns it, as the dialog has nothing to offer there.
    """
    check = block_check(snapshot)
    if check is None or check["status"] != CHECK_UNRESOLVED:
        return check
    cut = omit_unresolved_blocks(snapshot)
    if not (omitted := cut.get(OMITTED_BLOCKS)):
        return check
    lines = omitted_block_lines(omitted)
    if len(lines) > CARD_OMIT_LINES:  # the check is a state attribute, which the recorder caps at 16 KiB
        lines = [*lines[:CARD_OMIT_LINES], f"... and {len(lines) - CARD_OMIT_LINES} more"]
    return {**check, "omit": {"blocks": lines, "then": block_check(cut)}}
