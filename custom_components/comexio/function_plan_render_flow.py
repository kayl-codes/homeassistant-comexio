# Version: 0.8.3
"""Simplified signal-flow diagram for a Function Plan: labeled boxes + arrows arranged in
topological row order, top to bottom (input -> logic -> output) — deliberately NOT the detailed Comexio
pill/block styling (pins, port rows, ID boxes, obstacle-avoiding wire routing) that
render_plan_svg uses; an earlier version reused that drawing code and just looked like "the
same diagram again" instead of a flow chart (user feedback, 2026-08). Only the topological
layering algorithm is shared with that first attempt — the drawing here is new and
self-contained, with its own minimal visual language.

Boxes use classic flowchart shapes per element role (terminal/decision/process — see
_element_shape), not a single uniform rectangle: a plain box-per-element version still read as
"the same diagram again" rather than a typical flowchart to the user (feedback 2026-08-24).
"""

from html import escape
from typing import Any

from .function_plan_analysis import build_wiring
from .function_plan_render_geometry import _fit_to_bounding_box
from .function_plan_render_labels import resolve_element_label
from .function_plan_render_selfreset import detect_self_reset_cycles

__all__ = ["render_flow_svg"]

# Body text was reported as too small to read at a glance (user feedback, 2026-08-24) — scaled
# ~27% up from the original 11.0/6.2/32.0/... baseline; box dimensions scale with it so labels
# keep the same relative padding instead of suddenly overflowing their shapes.
_FONT_SCALE = 1.27
_BOX_H = 32.0 * _FONT_SCALE
_BOX_MIN_W = 70.0 * _FONT_SCALE
_CHAR_W = 6.2 * _FONT_SCALE  # approx text width per character at _FONT_SIZE — sizes boxes to their label
_FONT_SIZE = 11.0 * _FONT_SCALE
_H_GAP = 90.0  # horizontal gap between boxes within the same row/layer
_V_GAP = 40.0  # vertical gap between stacked rows/layers
_MARGIN = 25.0
_MAX_LABEL_LEN = 22

# Element shapes, classic-flowchart style (see _element_shape for the classification rules).
_SHAPE_TERMINAL = "terminal"  # oval/pill — a data boundary: marker/IO/WebIO read or write, constant
_SHAPE_DECISION = "decision"  # diamond — an actual boolean logic/comparison gate
_SHAPE_PROCESS = "process"  # rectangle — everything else (stateful/multi-output function blocks)
_DECISION_W_FACTOR = 1.9  # a diamond needs much more width than its label to keep text off the points
_DECISION_MIN_W = 110.0 * _FONT_SCALE
_DECISION_H = 56.0 * _FONT_SCALE
_TERMINAL_PAD = 22.0 * _FONT_SCALE  # extra horizontal padding so the pill's rounded ends don't clip the label

_STYLE = """
<style>
  .flow-bg { fill: #ffffff; }
  .flow-title { fill: #111111; }
  .flow-box { fill: #ebf4ff; stroke: #2b6cb0; }
  .flow-box-terminal { fill: #e6f4ea; stroke: #2e7d32; }
  .flow-box-decision { fill: #f3e8fd; stroke: #7e57c2; }
  .flow-box-cycle { fill: #fff3e0; stroke: #e69138; }
  .flow-label { fill: #111111; text-anchor: middle; dominant-baseline: middle; }
  .flow-legend-label { fill: #333333; dominant-baseline: middle; }
  .flow-edge { stroke: #555555; fill: none; }
  .flow-arrow { fill: #555555; }
  .flow-junction { fill: #555555; stroke: none; }
  @media (prefers-color-scheme: dark) {
    .flow-bg { fill: #1e1e1e; }
    .flow-title { fill: #e8e8e8; }
    .flow-box { fill: #16324a; stroke: #5b9bd5; }
    .flow-box-terminal { fill: #1b3a24; stroke: #66bb6a; }
    .flow-box-decision { fill: #2e2440; stroke: #b39ddb; }
    .flow-box-cycle { fill: #3a2e12; stroke: #e6a23c; }
    .flow-label { fill: #e8e8e8; }
    .flow-legend-label { fill: #cccccc; }
    .flow-edge { stroke: #aaaaaa; }
    .flow-arrow { fill: #aaaaaa; }
    .flow-junction { fill: #aaaaaa; }
  }
</style>
""".strip()

_SHAPE_CSS = {
    _SHAPE_TERMINAL: "flow-box-terminal",
    _SHAPE_DECISION: "flow-box-decision",
    _SHAPE_PROCESS: "flow-box",
}
# fub_base "categories" (see function_plan_catalog._extract_fub_base_catalog) that are actual
# boolean logic/comparison gates (and/or/not/xor, equal/gt/lt/ge/le/unequal/threshold/min_max) —
# confirmed against a live catalog dump (X:/.storage/comexio_logikplan_catalog_*), not guessed.
# Everything else in "logic" (e.g. multiklickstein, latch_s_r, rgb_dimmer) is a stateful/
# multi-output function block, not a single decision, so it stays a process rectangle.
_DECISION_CATEGORIES = frozenset({"basic", "comparison"})
# Reference types that name a data boundary (see resolve_element_label) rather than a computed
# gate/function: marker/IO/WebIO (read or write) and constants.
_TERMINAL_REF_TYPES = (1, 2, 10, 16)


def _element_shape(elem: dict[str, Any], fub_base: dict[str, Any]) -> str:
    """Classic-flowchart shape for one element: terminal (oval) for data-boundary elements,
    decision (diamond) for actual logic/comparison gates, process (rectangle) for everything
    else — a uniform box for every element still read as "the same diagram again" rather than a
    typical flowchart (user feedback, 2026-08-24)."""
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    if etype in _TERMINAL_REF_TYPES:
        return _SHAPE_TERMINAL
    if etype == 5:
        block = fub_base.get(str(ref.get("ref_id"))) or {}
        if set(block.get("categories") or ()) & _DECISION_CATEGORIES:
            return _SHAPE_DECISION
    return _SHAPE_PROCESS


def _box_size(shape: str, label: str) -> tuple[float, float]:
    text_w = len(label) * _CHAR_W
    if shape == _SHAPE_DECISION:
        return max(_DECISION_MIN_W, text_w * _DECISION_W_FACTOR), _DECISION_H
    if shape == _SHAPE_TERMINAL:
        return max(_BOX_MIN_W, text_w + _TERMINAL_PAD * 2), _BOX_H
    return max(_BOX_MIN_W, text_w + 20), _BOX_H


_LEGEND_ITEMS = (
    (_SHAPE_TERMINAL, "Ein-/Ausgang"),
    (_SHAPE_DECISION, "Logik/Vergleich"),
    (_SHAPE_PROCESS, "Baustein"),
)
_LEGEND_H = 30.0 * _FONT_SCALE
_LEGEND_ICON = 14.0 * _FONT_SCALE
_LEGEND_ITEM_W = 150.0 * _FONT_SCALE
_LEGEND_FONT_SIZE = 10.0 * _FONT_SCALE
_LEGEND_MIN_WIDTH = _MARGIN + len(_LEGEND_ITEMS) * _LEGEND_ITEM_W


def _render_legend_svg(y: float) -> list[str]:
    """One compact row explaining the three shapes — introducing per-role shapes without a
    legend would just trade "uniform boxes" confusion for "why are some diamonds" confusion."""
    parts = []
    x = _MARGIN
    for shape, text in _LEGEND_ITEMS:
        css = _SHAPE_CSS[shape]
        icon_y = y - _LEGEND_ICON
        if shape == _SHAPE_DECISION:
            cx, cy = x + _LEGEND_ICON / 2, icon_y + _LEGEND_ICON / 2
            points = (
                f"{cx:.1f},{icon_y:.1f} {x + _LEGEND_ICON:.1f},{cy:.1f} "
                f"{cx:.1f},{icon_y + _LEGEND_ICON:.1f} {x:.1f},{cy:.1f}"
            )
            parts.append(f'<polygon class="{css}" points="{points}"/>')
        else:
            rx = _LEGEND_ICON / 2 if shape == _SHAPE_TERMINAL else 3
            parts.append(
                f'<rect class="{css}" x="{x:.1f}" y="{icon_y:.1f}" width="{_LEGEND_ICON:.0f}" '
                f'height="{_LEGEND_ICON:.0f}" rx="{rx:.0f}"/>'
            )
        parts.append(
            f'<text class="flow-legend-label" x="{x + _LEGEND_ICON + 6:.1f}" y="{y - _LEGEND_ICON / 2:.1f}" '
            f'font-size="{_LEGEND_FONT_SIZE:.1f}">{text}</text>'
        )
        x += _LEGEND_ITEM_W
    return parts


_ARROW_DEFS = (
    '<defs><marker id="flow-arrow-mk" viewBox="0 0 10 10" refX="9" refY="5" '
    'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    '<path class="flow-arrow" d="M0,0 L10,5 L0,10 z"/></marker></defs>'
)


def _neighbors(node: str, outgoing: dict[str, dict[int, list[str]]], node_ids: set[str]):
    """Wired successor ids of one node — flattens outgoing's per-pin lists into one stream,
    filtered to nodes actually in this diagram (extracted so the DFS below stays flat)."""
    for dsts in outgoing.get(node, {}).values():
        yield from (dst for dst in dsts if dst in node_ids)


def _find_back_edges(node_ids: set[str], outgoing: dict[str, dict[int, list[str]]]) -> set[tuple[str, str]]:
    """Iterative DFS (explicit stack, not recursion — no call-depth risk on a very large plan)
    over the wiring graph; an edge whose destination is still on the current DFS path closes a
    cycle (e.g. a self-reset marker loop) and is excluded from the DAG used for layer
    assignment below — it's still drawn (see render_flow_svg), just as a looping arrow below
    the row instead of a left-to-right one (see _back_edge_path)."""
    visited: set[str] = set()
    back_edges: set[tuple[str, str]] = set()
    for start in node_ids:
        if start not in visited:
            _dfs_mark_back_edges(start, node_ids, outgoing, visited, back_edges)
    return back_edges


def _dfs_mark_back_edges(
    start: str,
    node_ids: set[str],
    outgoing: dict[str, dict[int, list[str]]],
    visited: set[str],
    back_edges: set[tuple[str, str]],
) -> None:
    on_stack = {start}
    visited.add(start)
    stack = [(start, iter(_neighbors(start, outgoing, node_ids)))]
    while stack:
        node, neighbors = stack[-1]
        dst = next(neighbors, None)
        if dst is None:
            on_stack.discard(node)
            stack.pop()
        elif dst in on_stack:
            back_edges.add((node, dst))
        elif dst not in visited:
            visited.add(dst)
            on_stack.add(dst)
            stack.append((dst, iter(_neighbors(dst, outgoing, node_ids))))


def _predecessors(
    node_ids: set[str], incoming: dict[str, dict[int, tuple[str, int]]], back_edges: set[tuple[str, str]]
) -> dict[str, set[str]]:
    """DAG predecessor set per node, with _find_back_edges' cycle-closing edges excluded."""
    preds: dict[str, set[str]] = {node: set() for node in node_ids}
    for dst, srcs in incoming.items():
        if dst not in preds:
            continue
        for src, _pos in srcs.values():
            if src in node_ids and (src, dst) not in back_edges:
                preds[dst].add(src)
    return preds


def _assign_layers(node_ids: set[str], preds: dict[str, set[str]]) -> dict[str, int]:
    """Iterative Kahn layering: layer 0 = no predecessors, else 1 + max(predecessor layer).
    Iterative on purpose (no recursion depth risk on a very large plan)."""
    layer: dict[str, int] = {}
    remaining = set(node_ids)
    lvl = 0
    while remaining:
        # The `or set(remaining)` is a safety net — shouldn't trigger once back-edges made the
        # graph acyclic, but never hang on unexpected data: dump whatever's left into one
        # final layer instead.
        ready = {node for node in remaining if preds[node] <= layer.keys()} or set(remaining)
        for node in ready:
            layer[node] = lvl
        remaining -= ready
        lvl += 1
    return layer


def _column_sort_key(eid: str) -> tuple[int, Any]:
    return (0, int(eid)) if eid.isdigit() else (1, eid)


def _push_sinks_to_bottom(layer_of: dict[str, int], succs_of: dict[str, set[str]]) -> None:
    """A leaf with no outgoing wiring (e.g. a relay-write terminal) keeps whatever early layer
    Kahn/ASAP assigned it — right after its own predecessor — even when sibling branches run
    several layers deeper, so it visually stops mid-diagram instead of lining up with the
    other outputs at the bottom (user feedback, 2026-08-24). Every true sink (no successor in
    the rendered graph) is pushed down to the deepest layer in use; a sink's ASAP layer is
    always <= that maximum, so this can only move it later, never violate a predecessor order."""
    if not layer_of:
        return
    max_layer = max(layer_of.values())
    for eid in layer_of:
        if not succs_of.get(eid):
            layer_of[eid] = max_layer


def _build_boxes(
    wired_ids: set[str],
    elements: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    cycle_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """One box per wired element — just enough shape info for _position_boxes/drawing. No pin/
    port/type detail (that's the whole point of this simplified diagram), but a per-role
    flowchart shape (see _element_shape)."""
    fub_base = catalog.get("fub_base", {})
    boxes: dict[str, dict[str, Any]] = {}
    for eid in wired_ids:
        label_text = resolve_element_label(elements[eid], catalog, markers_by_id, webio_by_id, ios_by_id)
        full_label = label_text.splitlines()[0]
        label = full_label if len(full_label) <= _MAX_LABEL_LEN else f"{full_label[: _MAX_LABEL_LEN - 1]}…"
        if eid in cycle_ids:
            label = f"⟲ {label}"
        shape = _element_shape(elements[eid], fub_base)
        w, h = _box_size(shape, label)
        boxes[eid] = {
            "label": label,
            "full_label": full_label,
            "w": w,
            "h": h,
            "cycle": eid in cycle_ids,
            "shape": shape,
        }
    return boxes


_BARYCENTER_PASSES = 4


def _neighbor_maps(edges: set[tuple[str, str]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    preds_of: dict[str, set[str]] = {}
    succs_of: dict[str, set[str]] = {}
    for src_id, dst_id in edges:
        succs_of.setdefault(src_id, set()).add(dst_id)
        preds_of.setdefault(dst_id, set()).add(src_id)
    return preds_of, succs_of


def _reorder_row(row_ids: list[str], reference: dict[str, set[str]], position: dict[str, int]) -> None:
    """Sorts one row in place by the average column position of its neighbors in the adjacent,
    already-fixed row (its barycenter) — nodes with no such neighbor keep their current spot."""
    row_ids.sort(key=lambda eid: _row_barycenter(eid, reference, position))
    for i, eid in enumerate(row_ids):
        position[eid] = i


def _row_barycenter(eid: str, reference: dict[str, set[str]], position: dict[str, int]) -> float:
    if neighbors := reference.get(eid):
        return sum(position[n] for n in neighbors) / len(neighbors)
    return position[eid]


def _minimize_crossings(
    by_row: dict[int, list[str]], preds_of: dict[str, set[str]], succs_of: dict[str, set[str]]
) -> None:
    """A handful of passes of the standard Sugiyama barycenter heuristic (alternating
    top-down/bottom-up sweeps, each row reordered by its already-fixed neighbor row) — not
    crossing-minimal (that's NP-hard), but the usual practical compromise for a layered
    graph like this one. The first/last row of each sweep direction has no reference row and
    stays an anchor for that pass."""
    rows = sorted(by_row)
    position = {eid: i for r in rows for i, eid in enumerate(by_row[r])}
    for pass_no in range(_BARYCENTER_PASSES):
        top_down = pass_no % 2 == 0
        sweep = rows[1:] if top_down else rows[-2::-1]
        reference = preds_of if top_down else succs_of
        for r in sweep:
            _reorder_row(by_row[r], reference, position)


_MAX_ROW_WIDTH = 1600.0  # a layer wider than this wraps onto additional sub-rows, see _position_one_row


def _position_boxes(
    boxes: dict[str, dict[str, Any]],
    layer_of: dict[str, int],
    preds_of: dict[str, set[str]],
    succs_of: dict[str, set[str]],
) -> None:
    """Top-to-bottom flow: rows = layers (y = cumulative row offset), x = left-to-right spread
    within a row, order chosen by _minimize_crossings instead of just element id."""
    by_row: dict[int, list[str]] = {}
    for eid, lvl in layer_of.items():
        by_row.setdefault(lvl, []).append(eid)
    for row_ids in by_row.values():
        row_ids.sort(key=_column_sort_key)
    _minimize_crossings(by_row, preds_of, succs_of)

    y = 0.0
    for r in sorted(by_row):
        y = _position_one_row(boxes, by_row[r], y, preds_of)


def _position_one_row(
    boxes: dict[str, dict[str, Any]], row_ids: list[str], y: float, preds_of: dict[str, set[str]]
) -> float:
    """Places one layer's boxes left to right, wrapping onto additional sub-rows once the row
    would otherwise sprawl far wider than the rest of the diagram — a plan with many
    independent, unrelated source elements (several dozen sensors/markers feeding one small
    downstream logic cluster, the common case for a Kahn/ASAP layer 0) would otherwise spend
    its ENTIRE canvas width on a single razor-thin top row while everything else stays
    squeezed into a tiny corner (user feedback, 2026-08-24, on exactly such a plan). A wrapped
    layer is still one logical layer — sub-rows only affect where its boxes are drawn, not
    which layer edges connect to. Returns the y for the next layer."""
    sub_rows: list[list[str]] = [[]]
    width = 0.0
    for eid in row_ids:
        box_w = boxes[eid]["w"]
        if sub_rows[-1] and width + _H_GAP + box_w > _MAX_ROW_WIDTH:
            sub_rows.append([])
            width = 0.0
        sub_rows[-1].append(eid)
        width += (_H_GAP if len(sub_rows[-1]) > 1 else 0) + box_w
    for sub in sub_rows:
        row_h = max(boxes[eid]["h"] for eid in sub)
        x = 0.0
        for eid in sub:
            # Pull toward the average center of already-placed predecessors (previous rows,
            # which are finalized by the time this row is laid out) so a chain of single-in/
            # single-out elements lines up in one straight, centered column instead of drifting
            # with each row's own independent left-to-right packing (user feedback, 2026-08-24).
            # Never moves left of the previous sibling in this row — collisions just push right,
            # bending that one edge instead of overlapping boxes.
            if centers := [boxes[p]["x"] + boxes[p]["w"] / 2 for p in preds_of.get(eid, ()) if "x" in boxes[p]]:
                x = max(x, sum(centers) / len(centers) - boxes[eid]["w"] / 2)
            boxes[eid]["x"] = x
            boxes[eid]["y"] = y + (row_h - boxes[eid]["h"]) / 2
            x += boxes[eid]["w"] + _H_GAP
        y += row_h + _V_GAP
    return y


_CORNER_R = 5.0  # max bend radius on a direction change — plain 90° elbow connectors read as a
# typical flowchart; the S-curve bezier an earlier version used did not (user feedback, 2026-08-24)


def _sign(v: float) -> float:
    if v > 0:
        return 1.0
    return -1.0 if v < 0 else 0.0


def _elbow_path(pts: list[tuple[float, float]]) -> str:
    """Orthogonal polyline through pts (each consecutive pair sharing an x or a y), corners
    rounded to at most _CORNER_R — the radius shrinks on short segments so arcs never overlap."""
    parts = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(1, len(pts) - 1):
        (px, py), (cx, cy), (nx, ny) = pts[i - 1], pts[i], pts[i + 1]
        len_in = abs(cx - px) + abs(cy - py)
        len_out = abs(nx - cx) + abs(ny - cy)
        r = min(_CORNER_R, len_in / 2, len_out / 2)
        if r < 0.3:
            parts.append(f"L{cx:.1f},{cy:.1f}")
            continue
        ix, iy = cx - r * _sign(cx - px), cy - r * _sign(cy - py)
        ox, oy = cx + r * _sign(nx - cx), cy + r * _sign(ny - cy)
        parts.append(f"L{ix:.1f},{iy:.1f} Q{cx:.1f},{cy:.1f} {ox:.1f},{oy:.1f}")
    parts.append(f"L{pts[-1][0]:.1f},{pts[-1][1]:.1f}")
    return " ".join(parts)


def _route_orthogonal(x1: float, y1: float, x2: float, y2: float) -> str:
    """Straight vertical when src/dst already share a column; otherwise straight down from src
    first, one 90° lane change at the halfway ROW, then straight down into dst — a wire must
    leave a box's bottom edge going down and arrive at a top edge coming from above, not depart
    sideways right at the box edge (a departure-order mix-up during the obstacle-avoidance
    round swapped this to leaving sideways first, which read as an ugly kink at the box edge;
    fixed back, user feedback, 2026-08-24). An earlier version also routed around intervening
    boxes, but on a very dense plan (~110 elements) the dodges it picked produced long,
    crossing-everything detours that read as MORE cluttered than a wire simply crossing under
    an unrelated box — reverted to the plain halfway bend; the hover-highlight and fan-out
    junction dots introduced alongside it stay."""
    if abs(x1 - x2) < 0.75:
        return f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"
    my = (y1 + y2) / 2
    return _elbow_path([(x1, y1), (x1, my), (x2, my), (x2, y2)])


def _back_edge_path(src: dict[str, Any], dst: dict[str, Any]) -> str:
    """A real cycle (dst at or above src): a right-angle loop out to whichever side — left or
    right — is nearer the target, so the loop doesn't have to reach all the way across the
    canvas when the cycle actually closes back to something sitting to the LEFT of its source.
    An earlier version always looped right regardless of where the target actually was, which
    read as a long, crossing-everything detour in denser plans (user feedback, 2026-08-24)."""
    src_cx, dst_cx = src["x"] + src["w"] / 2, dst["x"] + dst["w"] / 2
    if dst_cx < src_cx:
        x1, y1 = src["x"], src["y"] + src["h"] / 2
        x2, y2 = dst["x"], dst["y"] + dst["h"] / 2
        loop_x = min(x1, x2) - _H_GAP / 2
    else:
        x1, y1 = src["x"] + src["w"], src["y"] + src["h"] / 2
        x2, y2 = dst["x"] + dst["w"], dst["y"] + dst["h"] / 2
        loop_x = max(x1, x2) + _H_GAP / 2
    return _elbow_path([(x1, y1), (loop_x, y1), (loop_x, y2), (x2, y2)])


def _render_fanout(boxes: dict[str, dict[str, Any]], src_id: str, dst_ids: list[str], net: int) -> list[str]:
    """A single source feeding several forward destinations shares one trunk down to a bend row
    plus a junction dot there, then splits into per-branch wires — without it, several edges
    leaving the same box at the same point are indistinguishable from unrelated wires that
    happen to run close together (user feedback, 2026-08-24). The bend row is halfway to the
    NEAREST branch's row, matching how the main plan renderer picks its shared fan-out column."""
    src = boxes[src_id]
    x1, y1 = src["x"] + src["w"] / 2, src["y"] + src["h"]
    bend_y = (y1 + min(boxes[d]["y"] for d in dst_ids)) / 2
    parts = [
        f'<path class="flow-edge" data-net="{net}" d="M{x1:.1f},{y1:.1f} L{x1:.1f},{bend_y:.1f}"/>',
        f'<circle class="flow-junction" data-net="{net}" cx="{x1:.1f}" cy="{bend_y:.1f}" r="3.2"/>',
    ]
    for dst_id in dst_ids:
        dst = boxes[dst_id]
        x2, y2 = dst["x"] + dst["w"] / 2, dst["y"]
        path = _route_orthogonal(x1, bend_y, x2, y2)
        parts.append(f'<path class="flow-edge" data-net="{net}" d="{path}" marker-end="url(#flow-arrow-mk)"/>')
    return parts


def _render_edges_svg(edges: set[tuple[str, str]], boxes: dict[str, dict[str, Any]]) -> list[str]:
    """One path per edge, plus a shared trunk + junction dot for every source that fans out to
    several forward destinations (see _render_fanout). Each edge/fan-out group gets its own
    data-net id so a frontend card can hover-highlight the whole thing (mirrors the main plan
    preview's own net-hover, see comexio-plan-card.js)."""
    by_src: dict[str, list[str]] = {}
    for src_id, dst_id in edges:
        by_src.setdefault(src_id, []).append(dst_id)

    parts: list[str] = []
    net = 0
    for src_id, dst_ids in by_src.items():
        src_y = boxes[src_id]["y"]
        forward = sorted(d for d in dst_ids if boxes[d]["y"] > src_y)
        back = [d for d in dst_ids if d not in forward]
        if len(forward) >= 2:
            parts += _render_fanout(boxes, src_id, forward, net)
            net += 1
        else:
            for dst_id in forward:
                src, dst = boxes[src_id], boxes[dst_id]
                x1, y1 = src["x"] + src["w"] / 2, src["y"] + src["h"]
                x2, y2 = dst["x"] + dst["w"] / 2, dst["y"]
                path = _route_orthogonal(x1, y1, x2, y2)
                parts.append(f'<path class="flow-edge" data-net="{net}" d="{path}" marker-end="url(#flow-arrow-mk)"/>')
                net += 1
        for dst_id in back:
            path = _back_edge_path(boxes[src_id], boxes[dst_id])
            parts.append(f'<path class="flow-edge" data-net="{net}" d="{path}" marker-end="url(#flow-arrow-mk)"/>')
            net += 1
    return parts


def _collect_edges(
    incoming: dict[str, dict[int, tuple[str, int]]], boxes: dict[str, dict[str, Any]]
) -> set[tuple[str, str]]:
    """Unique (src, dst) box-id pairs — several pins between the same two elements collapse to
    one arrow, since this diagram doesn't distinguish individual pins."""
    return {
        (src_id, dst_id)
        for dst_id, srcs in incoming.items()
        if dst_id in boxes
        for src_id, _pos in srcs.values()
        if src_id in boxes
    }


def _svg_header(width: float, height: float, title: str, title_suffix: str) -> list[str]:
    suffix_tspan = (
        f'<tspan dx="12" font-size="10" font-weight="normal">{escape(title_suffix)}</tspan>' if title_suffix else ""
    )
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'style="max-width:{width:.0f}px" font-family="sans-serif" font-size="{_FONT_SIZE:.1f}">',
        _STYLE,
        _ARROW_DEFS,
        '<rect class="flow-bg" width="100%" height="100%"/>',
        f'<text class="flow-title" x="{_MARGIN}" y="24" font-size="18" font-weight="bold">'
        f"{escape(title)}{suffix_tspan}</text>",
    ]


def _diamond_points(box: dict[str, Any]) -> str:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    cx, cy = x + w / 2, y + h / 2
    return f"{cx:.1f},{y:.1f} {x + w:.1f},{cy:.1f} {cx:.1f},{y + h:.1f} {x:.1f},{cy:.1f}"


def _render_boxes_svg(boxes: dict[str, dict[str, Any]]) -> list[str]:
    parts = []
    for eid in sorted(boxes, key=_column_sort_key):
        box = boxes[eid]
        box_class = "flow-box-cycle" if box["cycle"] else _SHAPE_CSS[box["shape"]]
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        title = f"<title>{escape(box['full_label'])}</title>"
        if box["shape"] == _SHAPE_DECISION:
            shape_svg = f'<polygon class="{box_class}" points="{_diamond_points(box)}">{title}</polygon>'
        else:
            rx = box["h"] / 2 if box["shape"] == _SHAPE_TERMINAL else 6
            shape_svg = (
                f'<rect class="{box_class}" x="{box["x"]:.1f}" y="{box["y"]:.1f}" width="{box["w"]:.0f}" '
                f'height="{box["h"]:.0f}" rx="{rx:.0f}">{title}</rect>'
            )
        parts.extend(
            (
                shape_svg,
                f'<text class="flow-label" x="{cx:.1f}" y="{cy:.1f}" font-size="{_FONT_SIZE:.1f}">'
                f"{escape(box['label'])}</text>",
            )
        )
    return parts


def _wired_element_ids(elements: dict[str, Any], incoming: dict[str, Any], outgoing: dict[str, Any]) -> set[str]:
    return {eid for eid in elements if incoming.get(eid) or outgoing.get(eid)}


# Types whose ref_id names a specific, persistent instance (a marker, IO or WebIO command)
# rather than a catalog/type definition (block, time module: several elements can share their
# ref_id while being independent instances with their own state — never safe to merge). Mirrors
# function_plan_analysis._find_conflicts's own (1, 2, 10) set for the same reason.
_DEDUPE_REF_TYPES = (1, 2, 10)


def _compute_source_remap(wired_ids: set[str], elements: dict[str, Any], incoming: dict[str, Any]) -> dict[str, str]:
    """Comexio Studio commonly places several separate "read" copies of the very same marker/
    IO/WebIO near wherever each is wired, to keep the physical wiring short in ITS view — a
    47-element real-world plan had five separate boxes all labeled "M118 Alle Bad Lichter Aus"
    in the very first version of this diagram, which did nothing but inflate the source row
    with identical-looking boxes and stretch otherwise-short edges across the whole canvas
    (user feedback, 2026-08-24). A plain read has no wiring role beyond "what it feeds", so
    its copies are interchangeable; this maps every duplicate id to one canonical (lowest-id)
    survivor. Elements that DO have incoming wiring (e.g. a marker's WRITE side sitting
    elsewhere in the plan) keep their own identity even when they share a label with a read
    of the same marker — they are a different role, not an interchangeable copy.
    """
    groups: dict[tuple[int, str], list[str]] = {}
    for eid in wired_ids:
        if incoming.get(eid):
            continue
        ref = elements[eid].get("reference") or {}
        etype = ref.get("type")
        if etype in _DEDUPE_REF_TYPES:
            groups.setdefault((etype, str(ref.get("ref_id"))), []).append(eid)
    remap: dict[str, str] = {}
    for dupes in groups.values():
        if len(dupes) < 2:
            continue
        canonical = min(dupes, key=_column_sort_key)
        remap |= {eid: canonical for eid in dupes if eid != canonical}
    return remap


def _remap_wiring(
    remap: dict[str, str],
    incoming: dict[str, dict[int, tuple[str, int]]],
    outgoing: dict[str, dict[int, list[str]]],
) -> tuple[dict[str, dict[int, tuple[str, int]]], dict[str, dict[int, list[str]]]]:
    """Rewrites both wiring maps so every merged-away duplicate id (see _compute_source_remap)
    is replaced by its canonical survivor — on the outgoing side (the duplicate's own edges)
    AND wherever the duplicate shows up as somebody else's source in incoming, since dropping
    it from `wired_ids` without this would silently delete those elements' inbound edges."""
    new_incoming: dict[str, dict[int, tuple[str, int]]] = {}
    for dst_id, srcs in incoming.items():
        bucket = new_incoming.setdefault(remap.get(dst_id, dst_id), {})
        for pos, (src_id, src_pos) in srcs.items():
            bucket[pos] = (remap.get(src_id, src_id), src_pos)
    new_outgoing: dict[str, dict[int, list[str]]] = {}
    for src_id, pins in outgoing.items():
        bucket = new_outgoing.setdefault(remap.get(src_id, src_id), {})
        for pin, dsts in pins.items():
            bucket.setdefault(pin, []).extend(remap.get(dst_id, dst_id) for dst_id in dsts)
    return new_incoming, new_outgoing


def render_flow_svg(
    elements: dict[str, Any],
    connections: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    title: str,
    title_suffix: str = "",
) -> tuple[str, int]:
    """Render a plain box-and-arrow flow chart of the plan's signal order.

    Elements with no wiring at all (comments included — they carry no connections either)
    are left out; a flow diagram only makes sense for actual signal flow. Returns
    (svg, skipped_count); when non-zero, skipped_count is also folded into the returned
    title_suffix so the diagram itself says how many were hidden.
    """
    incoming, outgoing = build_wiring(connections)
    wired_ids = _wired_element_ids(elements, incoming, outgoing)
    skipped_count = len(elements) - len(wired_ids)
    if skipped_count:
        note = f"{skipped_count} unverdrahtete Element(e) ausgeblendet"
        title_suffix = f"{title_suffix} · {note}" if title_suffix else note

    if remap := _compute_source_remap(wired_ids, elements, incoming):
        incoming, outgoing = _remap_wiring(remap, incoming, outgoing)
        wired_ids = wired_ids - remap.keys()

    back_edges = _find_back_edges(wired_ids, outgoing)
    layer_of = _assign_layers(wired_ids, _predecessors(wired_ids, incoming, back_edges))
    reset_groups = detect_self_reset_cycles(elements, connections, catalog)
    cycle_ids = {eid for group in reset_groups for eid in group if eid in wired_ids}
    boxes = _build_boxes(wired_ids, elements, catalog, markers_by_id, webio_by_id, ios_by_id, cycle_ids)
    edges = _collect_edges(incoming, boxes)
    preds_of, succs_of = _neighbor_maps(edges)
    _push_sinks_to_bottom(layer_of, succs_of)
    _position_boxes(boxes, layer_of, preds_of, succs_of)
    width, height = _fit_to_bounding_box(boxes)

    legend_svg: list[str] = []
    if boxes:
        # _fit_to_bounding_box already shifted every box to start at (_MARGIN, _MARGIN + 34) —
        # push the content down by one more row to make room for the shape legend in between.
        legend_baseline = _MARGIN + 34 + _LEGEND_H - 10
        for box in boxes.values():
            box["y"] += _LEGEND_H
        height += _LEGEND_H
        width = max(width, _LEGEND_MIN_WIDTH)
        legend_svg = _render_legend_svg(legend_baseline)

    parts = _svg_header(width, height, title, title_suffix)
    parts += legend_svg
    parts += _render_edges_svg(edges, boxes)
    parts += _render_boxes_svg(boxes)
    parts.append("</svg>")
    return "\n".join(parts), skipped_count
