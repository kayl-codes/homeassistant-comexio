# Version: 0.7.6
"""Sort/grid-placement logic for function plan elements.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
pure layout math plus the managed-IO-cluster header/positioning helpers, shared by both
the connect (function_plan_connect) and plan_actions (function_plan_sort) handler modules.
Deliberately independent of _context.py (no service-call/notification concerns here).
"""

import contextlib
import logging

from ..const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH as _LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_COMMENT_Y as _LAYOUT_COMMENT_Y,
    FUNCTION_PLAN_LAYOUT_X_MARKER as _LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_X_WEBIO as _LAYOUT_X_WEBIO,
    FUNCTION_PLAN_LAYOUT_Y_START as _LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP as _LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    io_column_rows,
    io_group_headers,
    snap_to_grid,
)
from ..coordinator import ComexioCoordinator

_LOGGER = logging.getLogger(__name__)

# Comment-element (type=14) reference type, and the pinned managed-plan marker comment.
_COMMENT_REF_TYPE = "14"
_MANAGED_COMMENT_TEXT = FUNCTION_PLAN_MANAGED_PLAN_COMMENT


def _is_comment_ref_type(ref_type: str | int | None) -> bool:
    """Whether a plan element's reference type is a comment/text block (_COMMENT_REF_TYPE).

    Shared by _build_sorted_pairs and _assign_io_grid_positions so both sort paths stay
    aligned if the comment-type check ever changes.
    """
    return str(ref_type) == _COMMENT_REF_TYPE


def _connection_outputs(conn: dict) -> list[dict]:
    """A connection's "output" sinks, normalized to a list.

    Comexio may serialize "output" as a dict ({"0": {...}}) instead of a list — same
    server quirk normalized in api.py's _connection_output_ids/_rebuild_one_connection.
    """
    raw_outputs = conn.get("output", [])
    return list(raw_outputs.values()) if isinstance(raw_outputs, dict) else raw_outputs


def _build_sorted_pairs(
    elements: dict,
    connections: dict,
) -> tuple[list[tuple[int, int, int]], list[int]]:
    """Return marker→WebIO pairs sorted by marker ref_id and a list of orphan element IDs.

    Comment/text blocks (type 14) are excluded from the orphans — they keep their
    position and are never moved by the sort (the managed-plan comment is separately
    re-pinned by _pinned_template_positions; any other comment on the plan is left as-is).
    """
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
        for out in _connection_outputs(conn):
            out_eid_raw = out.get("FubElementId")
            if out_eid_raw is None:
                continue
            out_eid = int(out_eid_raw)
            if (inp_eid, out_eid) not in seen:
                seen.add((inp_eid, out_eid))
                pairs.append((marker_ref_id, inp_eid, out_eid))
    pairs.sort(key=lambda p: p[0])
    paired: set[int] = {eid for _, m, w in pairs for eid in (m, w)}
    orphans = [eid for eid, ref in elem_ref.items() if eid not in paired and not _is_comment_ref_type(ref["type"])]
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
    row_step: float = _LAYOUT_Y_STEP,
) -> list[tuple[int, float, float]]:
    """Calculate exact grid positions for sorted pairs and orphan elements.

    row_step overrides the row pitch (default: the generic single-row-tall marker/WebIO
    pitch) — pass the caller's own step when the pair's second element renders taller
    (e.g. the trigger plan's Flanke block), or consecutive rows would visually overlap.
    """
    positions: dict[int, tuple[float, float]] = {}
    pairs_placed = 0
    for row_idx, (_, m_eid, w_eid) in enumerate(pairs):
        col = row_idx // rows_per_col
        if col >= max_cols:
            break
        row_in_col = row_idx % rows_per_col
        y = _LAYOUT_Y_START + row_in_col * row_step
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
        y = _LAYOUT_Y_START + row_in_col * row_step
        positions[eid] = (_LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH, y)
    return [(eid, x, y) for eid, (x, y) in positions.items()]


# --- MANAGED IO CLUSTER PLAN GRID ---


def _io_ref_slots(
    coordinator: ComexioCoordinator,
    members: list[str],
    rows_per_col: int,
) -> dict[int, tuple[float, float]]:
    """Canonical (x_offset, y) slot per IO ref_id across the plan's extension columns.

    Same math as the wire path (api._function_plan_add_single_io_pair): column index =
    membership order, row slots from io_column_rows over the extension's FULL
    identifier list, overlong columns wrap into a sub-column right next to them.
    """
    ios = (coordinator.data or {}).get("io", [])
    slots: dict[int, tuple[float, float]] = {}
    for ext_col, ext in enumerate(members):
        by_ident = {io["identifier"]: io for io in ios if io["ext_name"] == ext}
        for ident, row in io_column_rows(list(by_ident)).items():
            sub_col, row_in_col = divmod(row, rows_per_col)
            try:
                ref_id = int(by_ident[ident]["id"])
            except (TypeError, ValueError):
                continue
            slots[ref_id] = (
                (ext_col + sub_col) * _LAYOUT_COLUMN_WIDTH,
                _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP,
            )
    return slots


def _io_header_slots(
    coordinator: ComexioCoordinator,
    members: list[str],
    rows_per_col: int,
) -> list[tuple[float, float, str]]:
    """Canonical (x, y, text) header-comment slot per IO type-group block, one per column.

    Same column math as _io_ref_slots, keyed by io_group_headers' reserved rows instead of
    per-IO ref_ids — a header comment has no ref_id of its own, so callers place it by
    position rather than matching it against an existing element.
    """
    ios = (coordinator.data or {}).get("io", [])
    slots: list[tuple[float, float, str]] = []
    for ext_col, ext in enumerate(members):
        idents = [io["identifier"] for io in ios if io["ext_name"] == ext]
        for row, text in io_group_headers(idents).items():
            sub_col, row_in_col = divmod(row, rows_per_col)
            slots.append(
                (
                    _LAYOUT_X_MARKER + (ext_col + sub_col) * _LAYOUT_COLUMN_WIDTH,
                    _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP,
                    text,
                )
            )
    return slots


def _stale_io_header_ids(plan_data: dict) -> list[int]:
    """Element ids of a managed IO plan's existing type-group header comments.

    Any comment element that isn't the pinned 'Administrated by HomeAssistant' marker —
    managed plans aren't hand-edited (see FUNCTION_PLAN_MANAGED_PLAN_COMMENT), so this is
    always our own, previously placed set of headers.
    """
    return [
        int(eid)
        for eid, elem in (plan_data.get("elements") or {}).items()
        if _is_comment_ref_type((elem.get("reference") or {}).get("type"))
        and (elem.get("name") or "").strip() != _MANAGED_COMMENT_TEXT
    ]


async def _resync_io_group_headers(
    api,
    fub_id: int,
    plan_data: dict,
    header_slots: list[tuple[float, float, str]],
) -> int:
    """Replace a managed IO plan's type-group header comments with a freshly placed set.

    Comments carry no wiring, so delete-then-recreate is always safe and sidesteps having
    to match stale headers back to a (possibly moved/renamed) slot. Returns the number of
    headers placed.
    """
    if stale_ids := _stale_io_header_ids(plan_data):
        await api.function_plan_delete_elements(stale_ids)
    for x, y, text in header_slots:
        await api.function_plan_add_comment_element(fub_id, text, x=x, y=y)
    return len(header_slots)


async def async_resync_io_group_headers(coordinator: ComexioCoordinator, api, fub_id: int) -> int:
    """Recompute and place an IO cluster plan's type-group header comments.

    The IO wiring path (api.function_plan_add_io_pairs) drops each pair straight into its
    deterministic grid slot without a sort pass, so it never touches header comments —
    call this right after wiring to keep a freshly created or extended IO cluster plan
    labeled, without paying for a full sort run. Returns the number of headers placed,
    or 0 if fub_id isn't a managed IO cluster plan.
    """
    members = coordinator.managed_io_plan_members(fub_id)
    if not members:
        return 0
    _x_max, y_max = api.get_fub_canvas_bounds(fub_id)
    rows_per_col = max(1, int((y_max - _LAYOUT_Y_START) / _LAYOUT_Y_STEP))
    plan_data = await api.function_plan_load_elements(fub_id)
    if not plan_data:
        return 0
    header_slots = _io_header_slots(coordinator, members, rows_per_col)
    return await _resync_io_group_headers(api, fub_id, plan_data, header_slots)


def _io_ref_positions(
    elements: dict, ref_slots: dict[int, tuple[float, float]]
) -> tuple[dict[int, tuple[float, float]], dict[int, tuple[float, float]]]:
    """Grid slot for every IO element (type 1) referencing a known ref_slot; see _assign_io_grid_positions."""
    positions: dict[int, tuple[float, float]] = {}
    elem_slot: dict[int, tuple[float, float]] = {}
    for eid, elem in elements.items():
        ref = elem.get("reference") or {}
        if str(ref.get("type")) != "1":
            continue
        try:
            slot = ref_slots.get(int(ref.get("ref_id")))
        except (TypeError, ValueError):
            slot = None
        if slot:
            x_off, y = slot
            positions[int(eid)] = (_LAYOUT_X_MARKER + x_off, y)
            elem_slot[int(eid)] = slot
    return positions, elem_slot


def _assign_webio_partner_positions(
    connections: dict, elem_slot: dict[int, tuple[float, float]], positions: dict[int, tuple[float, float]]
) -> int:
    """Place each connected Web-IO element on its partner IO's grid row; returns the pair count."""
    pair_count = 0
    for conn in connections.values():
        # FubElementId comes straight out of the raw plan JSON and may be a string, while
        # elem_slot is int-keyed — without the cast every Web-IO element would miss its
        # partner slot and get parked outside the grid (same cast as _build_sorted_pairs).
        try:
            in_eid = int(conn.get("input", {}).get("FubElementId"))
        except (TypeError, ValueError):
            continue
        if not (slot := elem_slot.get(in_eid)):
            continue
        x_off, y = slot
        for out in conn.get("output", []):
            # Same cast + guard as the input side above: a non-numeric endpoint id (partial
            # write, format change) must skip that one endpoint, not abort the whole sort.
            try:
                out_eid = int(out.get("FubElementId"))
            except (TypeError, ValueError):
                continue
            if out_eid not in positions:
                positions[out_eid] = (_LAYOUT_X_WEBIO + x_off, y)
                pair_count += 1
    return pair_count


def _assign_io_grid_positions(
    coordinator: ComexioCoordinator,
    plan_data: dict,
    members: list[str],
    rows_per_col: int,
) -> tuple[list[tuple[int, float, float]], int, list[int]]:
    """Restore the deterministic IO-cluster grid of a managed IO plan.

    IO elements (type 1) go to their reserved slot, each connected Web-IO element to the
    partner position on the same row. Returns (positions, pair_count, leftover element
    ids) — leftovers are elements the grid has no slot for (stale refs, foreign
    elements); comment blocks are excluded, pinning is the caller's job.
    """
    ref_slots = _io_ref_slots(coordinator, members, rows_per_col)
    elements = plan_data.get("elements", {})
    positions, elem_slot = _io_ref_positions(elements, ref_slots)
    pair_count = _assign_webio_partner_positions(plan_data.get("connections", {}), elem_slot, positions)
    leftovers = [
        int(eid)
        for eid, e in elements.items()
        if int(eid) not in positions and not _is_comment_ref_type((e.get("reference") or {}).get("type"))
    ]
    return [(eid, x, y) for eid, (x, y) in positions.items()], pair_count, leftovers


def _park_leftover_positions(
    placed: list[tuple[int, float, float]],
    leftovers: list[int],
    rows_per_col: int,
) -> list[tuple[int, float, float]]:
    """Park unplaceable elements in the first column right of the used IO grid —
    never inside it, so reserved slots stay free for retrofitted pairs."""
    if not leftovers:
        return []
    used_cols = {round((x - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH) for _eid, x, _y in placed}
    first_col = (max(used_cols) + 1) if used_cols else 0
    parked: list[tuple[int, float, float]] = []
    for i, eid in enumerate(leftovers):
        col, row = divmod(i, rows_per_col)
        x = _LAYOUT_X_MARKER + (first_col + col) * _LAYOUT_COLUMN_WIDTH
        parked.append((eid, x, _LAYOUT_Y_START + row * _LAYOUT_Y_STEP))
    return parked


def _pinned_template_positions(plan_data: dict, x_max: float) -> list[tuple[int, float, float]]:
    """Canonical positions for the managed-plan template elements (layout normalizer).

    Every sort run re-rights the managed comment (top center) instead of scrambling it
    into the marker grid — this is what keeps a hand-moved comment permanently in place.
    """
    return [
        (int(eid), snap_to_grid(x_max / 2), _LAYOUT_COMMENT_Y)
        for eid, elem in (plan_data.get("elements") or {}).items()
        if _is_comment_ref_type((elem.get("reference") or {}).get("type"))
        and (elem.get("name") or "").strip() == _MANAGED_COMMENT_TEXT
    ]


def _is_managed_cluster_plan(coordinator: ComexioCoordinator, fub_id: int) -> bool:
    """Whether fub_id is one of this coordinator's HA-managed cluster plans.

    Sorting rewrites every element's position — safe for HA's own marker/IO cluster grids,
    destructive for a user's hand-built Comexio plan. Backstops the services.yaml dropdown
    (which already restricts the picker) against scripted/YAML calls with a raw fub_id.

    Falls back to "not managed" for a malformed/unparseable plan_map: this gate only ever
    removes capability, so refusing to sort is the safe direction — the alternative is an
    unhandled exception in the service handler.
    """
    plan_map = coordinator.config_entry.options.get(CONF_FUNCTION_PLAN_PLAN_MAP, {})
    if not isinstance(plan_map, dict):
        _LOGGER.warning(
            "Ignoring malformed %s option (%s) — refusing to sort",
            CONF_FUNCTION_PLAN_PLAN_MAP,
            type(plan_map).__name__,
        )
        return False
    managed: set[int] = set()
    for value in plan_map.values():
        with contextlib.suppress(TypeError, ValueError):
            managed.add(int(value))
    return int(fub_id) in managed
