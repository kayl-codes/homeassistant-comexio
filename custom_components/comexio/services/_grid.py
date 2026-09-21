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
    FUNCTION_PLAN_LAYOUT_ROW_HEIGHT as _LAYOUT_ROW_HEIGHT,
    FUNCTION_PLAN_LAYOUT_X_KNX_WEBIO as _LAYOUT_X_KNX_WEBIO,
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

# reference type codes for element kinds this module's sort logic distinguishes
_REF_TYPE_MARKER = 2
_REF_TYPE_KNX = 11
_REF_TYPE_WEBIO = 10

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


def _outputs_by_input(connections: dict) -> dict[int, list[int]]:
    """elem_id -> the FubElementIds it connects to (input -> every output), across all
    connections regardless of element type — shared by _build_sorted_pairs to walk both
    hops of a Marker->K->WebIO KNX bridge chain without a second connections scan.
    """
    outputs: dict[int, list[int]] = {}
    for conn in connections.values():
        inp_eid_raw = conn.get("input", {}).get("FubElementId")
        if inp_eid_raw is None:
            continue
        inp_eid = int(inp_eid_raw)
        for out in _connection_outputs(conn):
            out_eid_raw = out.get("FubElementId")
            if out_eid_raw is None:
                continue
            outputs.setdefault(inp_eid, []).append(int(out_eid_raw))
    return outputs


def _resolve_bridge_hop(
    elem_ref: dict[int, dict], outputs_by_input: dict[int, list[int]], out_eid: int
) -> tuple[list[int], int | None]:
    """Follow a Marker connection's direct output one more hop if it lands on a KNX
    (type 11) element — Entwurf A "Merker-Brücke" wires Marker->K->WebIO as two chained
    connections, not one, so the K-object itself is never the pair's true WebIO partner.

    Returns (webio_elem_ids, knx_elem_id): the direct output unchanged as a single-item list
    with knx_elem_id=None for a plain Marker->WebIO pair, or (the K-object's downstream
    WebIOs, the K-object) once such a second hop exists. A K-object can fan out to MORE THAN
    ONE downstream WebIO (Phase 7 "API-Loopback": wire_knx_bridge_loopback unions a second
    WebIO onto the same connection's existing output list, alongside the original read-path
    WebIO) — every downstream WebIO is returned, sorted by elem_id for a stable, deterministic
    column assignment across repeated sort runs. Bug found + fixed 2026-09-19: this used to
    pick only the FIRST downstream WebIO via next(...), silently dropping any further fan-out
    sink from the pair entirely — _build_sorted_pairs then treated it as an unrelated orphan
    and parked it far away from its K/Marker pair, looking un-wired even though the underlying
    connection was intact.

    Falls back to the K-object itself (no downstream WebIO found at all — e.g. not wired yet)
    so it's still placed somewhere on the grid instead of vanishing; that fallback is logged
    (debug) since it's expected only briefly, right after a bridge is freshly created and
    before its own read-path pair is wired — a review (2026-09-16) found it would otherwise
    "repair" a stuck/broken read-path wire into a plausible-looking position with zero trace
    of the underlying gap.
    """
    if elem_ref.get(out_eid, {}).get("type") != _REF_TYPE_KNX:
        return [out_eid], None
    webio_eids = sorted(
        eid for eid in outputs_by_input.get(out_eid, []) if elem_ref.get(eid, {}).get("type") == _REF_TYPE_WEBIO
    )
    if not webio_eids:
        _LOGGER.debug(
            "KNX bridge K-object %d has no downstream Web-IO wired yet — placing it without a "
            "dedicated Web-IO column this run; investigate if this persists across sort runs",
            out_eid,
        )
        return [out_eid], out_eid
    return webio_eids, out_eid


def _build_sorted_pairs(
    elements: dict,
    connections: dict,
) -> tuple[list[tuple[int, int, list[int], int | None]], list[int]]:
    """Return marker→WebIO pairs sorted for display and a list of orphan element IDs.

    Each pair is (sort_key, marker_elem_id, webio_elem_ids, knx_elem_id) — knx_elem_id
    is None for the common direct Marker->WebIO case, and the intermediate K-object's elem_id
    for a KNX write-bridge chain (see _resolve_bridge_hop); webio_elem_ids can hold more than
    one entry for such a chain (Phase 7 API-Loopback fan-out); _assign_grid_positions gives
    the K-object and each of its WebIOs their own dedicated column so none of them gets
    mistaken for — or lost behind — the pair's other elements.

    sort_key is the Marker's own ref_id (M-number) for a plain pair, but the KNX object's
    ref_id (K-number) for a bridge pair — a bridge Marker's M-number reflects only the
    historical order its marker happened to be created/reused in (see
    function_plan_add_knx_bridge_pairs' free-marker reuse), which can drift arbitrarily far
    from K order (e.g. K5 created after K6/K7 ends up with a HIGHER-numbered marker than
    both — K5 -> M306 while K6/K7 got M304/M305 — so it sorts after them). Sorting those
    rows by M-number instead of K-number then visibly scrambles the K column even though
    the actual K objects are numbered sequentially (user report, 2026-09-20: K5 rendered
    between K7 and K8 on a "HA - KNX [1-100]" plan).

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
    outputs_by_input = _outputs_by_input(connections)
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int, list[int], int | None]] = []
    for inp_eid, out_eids in outputs_by_input.items():
        if elem_ref.get(inp_eid, {}).get("type") != _REF_TYPE_MARKER:
            continue
        marker_ref_id = int(elem_ref[inp_eid].get("ref_id", 0))
        for out_eid in out_eids:
            if (inp_eid, out_eid) in seen:
                continue
            seen.add((inp_eid, out_eid))
            webio_eids, knx_eid = _resolve_bridge_hop(elem_ref, outputs_by_input, out_eid)
            sort_key = int(elem_ref[knx_eid].get("ref_id", 0)) if knx_eid is not None else marker_ref_id
            pairs.append((sort_key, inp_eid, webio_eids, knx_eid))
    pairs.sort(key=lambda p: p[0])
    paired: set[int] = {eid for _, m, ws, k in pairs for eid in ([m, k, *ws]) if eid is not None}
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


# Every downstream WebIO of a K-object — read-path (hop 0) and Phase 7 API-Loopback fan-out
# (hop 1+, see _resolve_bridge_hop) — shares this SAME column; only the row (see
# _KNX_LOOPBACK_Y_OFFSET below) tells hops apart. Kept as its own name (not just reusing
# _LAYOUT_X_KNX_WEBIO inline) so _place_pair_row and _KNX_COLUMN_WIDTH read as "the one KNX
# WebIO column" rather than duplicating the const.py import path. Originally hop 1 got its
# own further-right column (X_KNX_LOOPBACK) instead — user feedback, 2026-09-19, after the
# Y-offset fix below had already resolved the renderer's collinearity bug: wanted the Loopback
# pill directly under the read-path pill, in the same column, purely a layout preference (the
# render-correctness issue was already fully fixed by the Y-offset alone — see
# _KNX_LOOPBACK_Y_OFFSET's docstring).
_KNX_WEBIO_X = _LAYOUT_X_KNX_WEBIO
_KNX_WEBIO_STEP = _LAYOUT_X_WEBIO - _LAYOUT_X_MARKER

# A KNX bridge row spans from X_MARKER to one WebIO-column's-worth past X_KNX_WEBIO — wider
# than the standard _LAYOUT_COLUMN_WIDTH (450) used for plain marker/WebIO pairs, since a KNX
# row additionally reserves the K-object's own column between marker and WebIO. Packing KNX
# columns at the standard pitch would let one column's WebIO collide with the next column
# (dual-review finding, 2026-09-19). Plans containing at least one KNX bridge pair use this
# wider pitch for every column instead — see _assign_grid_positions.
#
# _KNX_WEBIO_X + _KNX_WEBIO_STEP - _LAYOUT_X_MARKER (585) is the exact minimum span with NO
# margin — the next column's marker pillar would land only 15 units past this column's WebIO
# pillar (vs. 75 units in the standard non-KNX layout, per _PILL_WIDTH=180 in
# function_plan_render_constants.py), so two adjacent KNX columns visually read as barely
# separated (user-reported, 2026-09-21 screenshots: "der rechte Block ist immer noch nicht
# verschoben"). A first fix (585->600, +15 margin) was still not visually distinct enough
# (same user, same day, re-tested). _KNX_COLUMN_MARGIN=215 (total width 800) matches the gap
# the user manually tested live in Comexio Studio and confirmed renders correctly (screenshot
# + element-position JSON, 2026-09-21: shifted an entire second-column block by +195 units,
# landing its rightmost WebIO pill's right edge at x=1380 — clearly past this integration's
# own get_fub_canvas_bounds-derived x_max~=1230 for A3/90dpi — and reported "und gut ist").
# That result disproves the assumption the previous (585..607) ceiling was built on: x_max is
# apparently NOT a hard clipping/rendering bound on Comexio's live editable canvas (it is
# derived from paper-format mm size and DPI, and may only matter for print/export sizing, not
# the on-screen SVG canvas) — see _KNX_MAX_COLS below for the column-count consequence.
_KNX_COLUMN_MARGIN = 215.0
_KNX_COLUMN_WIDTH = _KNX_WEBIO_X + _KNX_WEBIO_STEP - _LAYOUT_X_MARKER + _KNX_COLUMN_MARGIN

# Fixed column count for any plan containing at least one KNX bridge pair — no longer derived
# from x_max/column_width (see _KNX_COLUMN_WIDTH's docstring for why that derivation was
# dropped: the user's live test showed x_max is not an actual canvas limit, so a formula built
# on it was both wrong in practice and, for a wide pitch like 800, wrongly floored to 1 column,
# silently halving capacity). 2 is not a heuristic guess — it is the fixed design every KNX
# cluster plan is built against: FUNCTION_PLAN_KNX_CLUSTER_SIZE=50 in const.py buckets markers
# into cluster plans specifically sized for "2 columns x ~26 two-slot pairs" (see that
# constant's comment), and every KNX cluster plan is created at the fixed _MANAGED_PLAN_PAPER
# ("A3", coordinator.py) — so there is no dynamic paper-format case to size this against here.
_KNX_MAX_COLS = 2

# hop_index>=1 (Phase 7 loopback and any further hop) is pushed this many units BELOW the
# K-object's own row, one sub-row per extra hop. Without ANY offset, hop 0 and hop 1 sit on
# the exact same row/y as their shared source K — verified against the Studio-clone renderer
# (function_plan_render_wiring.py): _edge_route treats two same-row sinks as "stays on row"
# for BOTH branches (no lane-change column, no vertical run), and _junction_points then never
# emits a T-junction dot for a same-row-only fan-out. The result is two flat, fully
# overlapping straight lines from the K-object's output pin to each sink, rendered with no
# visible branch point — indistinguishable on screen from a serial chain K -> hop0 -> hop1,
# since hop0's pill sits geometrically between K and hop1 on that same line (bug reported
# 2026-09-19: user's Comexio Studio screenshot showed exactly this "all in series" look right
# after the sort-pass fan-out fix, even though function_plan_analyze/function_plan_flow_diagram
# already confirmed the underlying connection data was correctly parallel).
#
# Originally set to the full _LAYOUT_Y_STEP (22.5, one pair-to-pair row pitch) to fix that —
# any offset clears the renderer's 0.75-unit same-row threshold, so that full pitch was never
# required for correctness, only convenient to reuse. It visibly left a blank row's worth of
# gap between the two WebIO pills, which the user asked to close (2026-09-20, "Abstand ... auf
# 0 ... so dass sie aneinander liegen"). Now uses _LAYOUT_ROW_HEIGHT (15, Studio's own
# port-row pitch — the same value used for two elements stacked with zero gap elsewhere) so
# the pills sit directly adjacent instead of one full row apart, while still comfortably
# clearing the 0.75-unit threshold. Deliberately independent of _assign_grid_positions' own
# row_step (the pair-to-pair slot pitch, e.g. 22.5, or the trigger plan's 90.0 — never
# actually exercised here since trigger and KNX-bridge plans never share a fub_id): that
# pitch still reserves a FULL row-slot per hop (pair_slots = len(w_eids) in
# _assign_grid_positions), so tightening this pixel offset only shrinks the gap WITHIN that
# already-reserved footprint and can never collide with the next pair's own row.
_KNX_LOOPBACK_Y_OFFSET = _LAYOUT_ROW_HEIGHT


def _place_pair_row(
    positions: dict[int, tuple[float, float]],
    m_eid: int,
    w_eids: list[int],
    k_eid: int | None,
    col: int,
    y: float,
    column_width: float = _LAYOUT_COLUMN_WIDTH,
    hop_step: float = _KNX_LOOPBACK_Y_OFFSET,
) -> None:
    """Place one pair's marker/(K-object)/WebIO elements on their row (see _assign_grid_positions).

    column_width overrides the column pitch (default: the standard marker/WebIO pitch) —
    _assign_grid_positions passes the wider _KNX_COLUMN_WIDTH for a plan containing KNX
    bridge pairs, since their WebIO fan-out is wider than the standard pitch (see
    _KNX_COLUMN_WIDTH's docstring).

    hop_step is the Y distance between consecutive WebIO hops of ONE pair (default:
    _KNX_LOOPBACK_Y_OFFSET, the Studio port-row height — hops sit directly adjacent, no
    visual gap). Deliberately NOT tied to _assign_grid_positions' own row_step (the
    pair-to-pair slot pitch, which stays whatever it was) — that pitch still reserves a full
    row-slot per hop (pair_slots = len(w_eids) there), so this only tightens the pixel gap
    WITHIN that already-reserved footprint; it can never grow past what _assign_grid_positions
    reserved without also growing row_step itself, so a caller-supplied hop_step this small
    can never collide with the next pair's own row (see _KNX_LOOPBACK_Y_OFFSET's docstring for
    the full history — this used to be the row_step itself, until 2026-09-20).

    A pair's K-object (knx_elem_id, KNX write-bridge chain only — see _resolve_bridge_hop)
    goes to its own column at X_WEBIO, matching where a freshly wired bridge triad already
    lands (api.function_plan_add_knx_bridge_pairs); the pair's real WebIO partner(s) all
    share ONE further column at X_KNX_WEBIO (see _KNX_WEBIO_X) — a K-object can have more
    than one downstream WebIO (Phase 7 API-Loopback fan-out, see _resolve_bridge_hop), and
    every one of them is placed in that same column, one row each (see _KNX_LOOPBACK_Y_OFFSET),
    so none of them collide or get dropped (bug found + fixed 2026-09-19: the single-w_eid
    version of this function could only ever place the one WebIO its caller already picked,
    silently losing any further fan-out sink to the general orphan area — see
    _resolve_bridge_hop's docstring). Each hop beyond the first is pushed one hop_step below
    the row's y — see _KNX_LOOPBACK_Y_OFFSET's docstring for why a same-row fan-out renders as
    a false serial chain. _assign_grid_positions reserves one extra row-slot per extra hop so
    this sub-row never collides with the next pair's own row.

    Each K-object should belong to exactly one bridge Marker (Entwurf A design) — it should
    never legitimately show up in more than one pair, so k_eid already being placed always
    means a second marker references the same K-object; a review (2026-09-16) found the
    "already placed" guard below would otherwise silently drop the second marker's row into
    an unexplained gap. Logged as a warning instead of silently orphaning it, since this
    points at a wiring bug upstream. (A narrower `k_eid not in w_eids` variant of this guard
    was tried during the 2026-09-19 fan-out fix but dual-review found it could itself
    silently suppress the warning for the one duplicate-marker case it exists to catch —
    dropped again in favor of the unconditional check.)
    """
    x_off = col * column_width
    if m_eid not in positions:
        positions[m_eid] = (_LAYOUT_X_MARKER + x_off, y)
    if k_eid is not None:
        if k_eid in positions:
            _LOGGER.warning(
                "KNX bridge K-object %d is already placed by another marker pair — marker %d "
                "also references it; check for a duplicate bridge Marker",
                k_eid,
                m_eid,
            )
        else:
            positions[k_eid] = (_LAYOUT_X_WEBIO + x_off, y)
    webio_x = _KNX_WEBIO_X if k_eid is not None else _LAYOUT_X_WEBIO
    for hop_index, w_eid in enumerate(w_eids):
        if w_eid in positions:
            continue
        webio_y = y + hop_index * hop_step
        positions[w_eid] = (webio_x + x_off, webio_y)


def _balanced_rows_per_col(total_slots: int, max_rows_per_col: int, max_cols: int) -> int:
    """Rows per column that spreads total_slots evenly across the columns actually needed.

    _get_canvas_grid_dims derives rows_per_col purely from canvas HEIGHT (how many rows
    physically fit), independent of how many pairs/orphans are actually being sorted. Filling
    column 0 to that full physical capacity before spilling into column 1 (plain greedy
    col/slot fill below) produces an uneven split for an exact multiple — e.g. 50 KNX pairs
    in a 26-row-capacity column becomes 26/24 instead of the expected 25/25 (user-reported
    live, 2026-09-21, sorting a fresh 'HA - KNX [1-50]' cluster plan — api.py's own
    _balanced_rows_per_col, applied to the *fresh-plan direct-placement* branch of
    function_plan_add_knx_bridge_pairs, never actually ran here: that leg always calls with
    fresh_plan=False, see button.py's _wire_knx_leg_bridge docstring, so every KNX cluster
    plan is placed via this sort path instead).

    Unlike api.py's variant, max_cols here is a hard physical cap (how many columns fit on
    this canvas at the current column pitch), not unbounded — so the ideal column count is
    additionally capped to it. When placing total_slots would genuinely need more columns
    than fit, this falls back to max_rows_per_col unchanged and lets the existing per-item
    overflow warnings in the loops below do their job exactly as before.
    """
    if total_slots <= 0 or max_cols <= 0:
        return max(1, max_rows_per_col)
    ideal_cols = min(max_cols, -(-total_slots // max_rows_per_col) or 1)
    return min(max_rows_per_col, -(-total_slots // ideal_cols))


def _assign_grid_positions(
    pairs: list[tuple[int, int, list[int], int | None]],
    orphans: list[int],
    rows_per_col: int,
    max_cols: int,
    row_step: float = _LAYOUT_Y_STEP,
) -> tuple[list[tuple[int, float, float]], int, int]:
    """Calculate exact grid positions for sorted pairs and orphan elements.

    row_step overrides the row pitch (default: the generic single-row-tall marker/WebIO
    pitch) — pass the caller's own step when the pair's second element renders taller
    (e.g. the trigger plan's Flanke block), or consecutive rows would visually overlap.

    A plan containing KNX bridge pairs overrides max_cols to the fixed _KNX_MAX_COLS instead
    of the caller's canvas-derived value, since it also switches to the wider _KNX_COLUMN_WIDTH
    pitch (see both constants' docstrings for why this is a fixed design constant rather than
    something derived from canvas x_max).

    Each pair consumes as many row-slots as it has WebIOs (normally 1; a KNX bridge pair
    with a Phase 7 loopback fan-out consumes 2 — see _KNX_LOOPBACK_Y_OFFSET) instead of a
    fixed one, so a multi-hop pair's extra sub-row is reserved space, not an overlap with the
    next pair's own row. A running (col, slot) cursor replaces the old uniform row_idx
    indexing for this reason; orphans continue from wherever that cursor left off, one slot
    each, on the same cursor.

    A variable pair_slots also means the pairs loop can hit column overflow sooner/more
    unpredictably than the old fixed-1-slot scheme (a multi-slot pair wastes the last
    partial slot of a column rather than splitting across it — a plan of all-2-slot KNX
    pairs fits only half as many per column as the old fixed-1-slot scheme did) — both
    overflow cases (a single pair too tall for rows_per_col regardless of column, and the
    grid running out of columns generally) now warn with how many pairs are dropped,
    mirroring the orphans loop's existing warning (silent-failure-hunter finding,
    2026-09-19: the old code silently dropped every remaining pair with zero log output
    once either happened).

    Returns (positions, dropped_pairs, dropped_orphans) — the two counts are how many
    trailing pairs/orphans this call could NOT place (0 unless one of the two overflow
    warnings above fired), so the caller can report an accurate "N sorted" count instead of
    the pre-drop len(pairs)/len(orphans) (code-review finding, 2026-09-19: the halved KNX
    capacity above makes the caller's stale count reachable at a realistic ~16 KNX bridges
    on a single-column canvas format, not just a theoretical edge case).
    """
    column_width = _LAYOUT_COLUMN_WIDTH
    if any(k_eid is not None for _, _, _, k_eid in pairs):
        column_width = _KNX_COLUMN_WIDTH
        max_cols = _KNX_MAX_COLS
    total_slots = sum(len(w_eids) for _, _, w_eids, _ in pairs) + len(orphans)
    rows_per_col = _balanced_rows_per_col(total_slots, rows_per_col, max_cols)
    positions: dict[int, tuple[float, float]] = {}
    col = 0
    slot = 0
    dropped_pairs = 0
    for i, (_, m_eid, w_eids, k_eid) in enumerate(pairs):
        pair_slots = len(w_eids)
        if pair_slots > rows_per_col:
            # Can never fit regardless of column (every column has the same rows_per_col) —
            # a distinct, clearer warning than the generic "grid full" one below, since this
            # is a canvas/row-height mismatch, not the grid actually running out of columns
            # (silent-failure-hunter finding, 2026-09-19: without this, a KNX bridge pair on
            # a small canvas format — rows_per_col floors to 1 — vanished with zero trace,
            # even on an otherwise-empty grid).
            _LOGGER.warning(
                "KNX bridge pair (marker %d) needs %d row-slots but only %d fit per column "
                "(canvas too small) — dropping it and %d remaining pair(s)",
                m_eid,
                pair_slots,
                rows_per_col,
                len(pairs) - i - 1,
            )
            dropped_pairs = len(pairs) - i
            break
        if slot + pair_slots > rows_per_col:
            col += 1
            slot = 0
        if col >= max_cols:
            _LOGGER.warning(
                "Grid layout full (%d cols × %d rows): %d pair(s) left unsorted",
                max_cols,
                rows_per_col,
                len(pairs) - i,
            )
            dropped_pairs = len(pairs) - i
            break
        y = _LAYOUT_Y_START + slot * row_step
        _place_pair_row(positions, m_eid, w_eids, k_eid, col, y, column_width)
        slot += pair_slots
    dropped_orphans = 0
    for i, eid in enumerate(orphans):
        if slot >= rows_per_col:
            col += 1
            slot = 0
        if col >= max_cols:
            _LOGGER.warning(
                "Grid layout full (%d cols × %d rows): %d orphan element(s) left unsorted",
                max_cols,
                rows_per_col,
                len(orphans) - i,
            )
            dropped_orphans = len(orphans) - i
            break
        y = _LAYOUT_Y_START + slot * row_step
        positions[eid] = (_LAYOUT_X_MARKER + col * column_width, y)
        slot += 1
    return [(eid, x, y) for eid, (x, y) in positions.items()], dropped_pairs, dropped_orphans


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
