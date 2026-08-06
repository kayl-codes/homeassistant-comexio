# Version: 0.8.4
"""Wire routing + SVG wire/junction markup for the Function Plan renderer.

Split out of function_plan_render.py (2026-08). Consumes the geo dicts built by
function_plan_render_geometry.py and the raw connections dict, computes Studio-style
orthogonal wire paths (obstacle-aware lane changes, shared fan-out trunks, feedback
loops), and emits the <path>/<circle> markup for wires, junction dots and inversion
circles. Node/pill body markup itself lives in function_plan_render_svg.py.
"""

from html import escape
import logging
from typing import Any

from .function_plan_render_constants import _BEND_RADIUS, _DOCK_LEAD, _PIN_LEN, _PORT_FONT_SIZE
from .function_plan_render_geometry import _port_row, _port_y, _row_analog, _sink_list
from .function_plan_render_values import _is_high

_LOGGER = logging.getLogger(__name__)

_Rect = tuple[float, float, float, float]


def _sign(v: float) -> float:
    if v > 0:
        return 1.0
    return -1.0 if v < 0 else 0.0


def _rounded_path(pts: list[tuple[float, float]]) -> str:
    """Orthogonal polyline with rounded corners (quadratic bends, Studio-style).

    The bend radius shrinks to half the shorter adjacent segment so arcs never overlap —
    except on the FIRST and LAST segment, which only one corner touches: there (almost)
    the full segment length is available, so a wire leaving a pill can start with a proper
    arc even when its lead-out run is short (the halved radius made a visible kink there).
    A short _DOCK_LEAD is kept straight at both dock points, though: an arc that starts
    curving right AT the dock point only touches a pin glyph in a single point, which at
    a triangle tip reads as the wire not reaching the symbol."""
    parts = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    last = len(pts) - 2
    for i in range(1, len(pts) - 1):
        (px, py), (cx, cy), (nx, ny) = pts[i - 1], pts[i], pts[i + 1]
        len_in = abs(cx - px) + abs(cy - py)
        len_out = abs(nx - cx) + abs(ny - cy)
        r_in = len_in - _DOCK_LEAD if i == 1 else len_in / 2
        r_out = len_out - _DOCK_LEAD if i == last else len_out / 2
        r = min(_BEND_RADIUS, r_in, r_out)
        if r < 0.3:
            parts.append(f"L{cx:.1f},{cy:.1f}")
            continue
        ix, iy = cx - r * _sign(cx - px), cy - r * _sign(cy - py)
        ox, oy = cx + r * _sign(nx - cx), cy + r * _sign(ny - cy)
        parts.append(f"L{ix:.1f},{iy:.1f} Q{cx:.1f},{cy:.1f} {ox:.1f},{oy:.1f}")
    parts.append(f"L{pts[-1][0]:.1f},{pts[-1][1]:.1f}")
    return " ".join(parts)


def _h_blocked(y: float, xa: float, xb: float, obstacles: list[_Rect]) -> bool:
    """Does a horizontal run at y between xa/xb cut through an element body?

    Endpoints are pulled in half a unit and the y test excludes the element's boundary
    rows, so a wire DOCKING at an element or running in the 15-unit gap between two
    stacked pills does not count as a collision.
    """
    lo, hi = min(xa, xb) + 0.5, max(xa, xb) - 0.5
    return any(ox < hi and ox + ow > lo and oy + 1 < y < oy + oh - 1 for ox, oy, ow, oh in obstacles)


def _v_blocked(x: float, ya: float, yb: float, obstacles: list[_Rect]) -> bool:
    lo, hi = min(ya, yb) + 0.5, max(ya, yb) - 0.5
    return any(oy < hi and oy + oh > lo and ox + 1 < x < ox + ow - 1 for ox, oy, ow, oh in obstacles)


def _column_candidates(obstacles: list[_Rect], mid: float) -> list[float]:
    """Fallback lane-change columns: one bend radius outside each obstacle edge, nearest
    to the preferred (halfway) column first — Studio climbs BEFORE a blocking element."""
    return sorted(
        {c for ox, _oy, ow, _oh in obstacles for c in (ox - _BEND_RADIUS, ox + ow + _BEND_RADIUS)},
        key=lambda c: abs(c - mid),
    )


def _free_column(x1: float, y1: float, x2: float, y2: float, obstacles: list[_Rect]) -> float:
    """Lane-change column of a forward wire: the halfway point, unless one of the
    three runs (source row, column, sink row) would cut through an element body —
    then the clear column nearest to halfway (see _column_candidates). Falls back
    to halfway when nothing clears (dense plans)."""
    mid = (x1 + x2) / 2

    def clear(c: float) -> bool:
        return (
            x1 + 3 < c < x2 - 3
            and not _h_blocked(y1, x1, c, obstacles)
            and not _h_blocked(y2, c, x2, obstacles)
            and not _v_blocked(c, y1, y2, obstacles)
        )

    if clear(mid):
        return mid
    return next((c for c in _column_candidates(obstacles, mid) if clear(c)), mid)


def _shared_column(
    sx: float,
    sy: float,
    ends: list[tuple[Any, Any, float, float]],
    obstacles: list[_Rect],
) -> float | None:
    """One COMMON lane-change column for all forward branches of a fan-out (None = n/a).

    Independently routed branches each switch lanes halfway to THEIR sink, so branches to
    sinks at different x left the source row one after another even while they still had
    a common way — visibly parallel wires where Studio draws one trunk. Sharing the
    halfway column of the NEAREST forward sink keeps the row trunk and the vertical run
    common as long as possible; each branch leaves the shared column on its own sink row,
    where the junction dots already mark the exits. Returns None (per-branch routing)
    when fewer than two branches lane-change or no column clears every branch's runs.
    """
    drops = [(tx, ty) for _geo, _sink, tx, ty in ends if tx > sx + 3 and abs(ty - sy) >= 0.75]
    if len(drops) < 2:
        return None
    nearest = min(tx for tx, _ty in drops)

    def clear(c: float) -> bool:
        return (
            sx + 3 < c < nearest - 3
            and not _h_blocked(sy, sx, c, obstacles)
            and all(not _v_blocked(c, sy, ty, obstacles) and not _h_blocked(ty, c, tx, obstacles) for tx, ty in drops)
        )

    mid = (sx + nearest) / 2
    if clear(mid):
        return mid
    return next((c for c in _column_candidates(obstacles, mid) if clear(c)), None)


def _edge_route(
    x1: float, y1: float, x2: float, y2: float, obstacles: list[_Rect], shared_col: float | None = None
) -> tuple[str, float | None, float]:
    """Studio-style orthogonal wire: forward wires run on the source row, switch lanes
    HALFWAY between source and sink (verified against Studio's rendered plans) — or at
    the nearest clear column when the halfway route would cut through an element (see
    _free_column) — then finish on the sink's row. A fan-out passes shared_col (from
    _shared_column) so all its forward branches depart together instead of splitting
    one after another. When the sink lies left of the source (feedback), the wire steps
    a short stub forward, crosses the rows halfway, and docks from a column left of the
    sink — Studio's loop shape.

    Returns (path, departure x, vertical end y): the x where this wire bends away from
    the source row (None when it stays on the row) and the y its vertical run at that
    column ends on — junction dots need both to sit at the exact branch-off points.
    """
    if abs(y2 - y1) < 0.75:
        return f"M{x1:.1f},{y1:.1f} H{x2:.1f}", None, y1
    if x2 > x1 + 3:
        xcol = shared_col if shared_col is not None else _free_column(x1, y1, x2, y2, obstacles)
        pts = [(x1, y1), (xcol, y1), (xcol, y2), (x2, y2)]
        return _rounded_path(pts), xcol, y2
    # Feedback stub = one bend radius plus the dock lead, so the wire leaves the pill
    # straight for _DOCK_LEAD and still bends with the full arc radius after it.
    stub = _BEND_RADIUS + _DOCK_LEAD
    ym = (y1 + y2) / 2
    pts = [(x1, y1), (x1 + stub, y1), (x1 + stub, ym), (x2 - stub, ym), (x2 - stub, y2), (x2, y2)]
    return _rounded_path(pts), x1 + stub, ym


def _junction_points(
    sx: float,
    sy: float,
    cols: dict[float, list[tuple[float, float]]],
    stays_on_row: bool,
) -> list[tuple[float, float]]:
    """T-junction dots of one multi-sink wire, each sitting at the point where a branch
    VISIBLY leaves a through-running trunk — i.e. at the branch arc's start, not at the
    corner itself (the rounded bend begins its radius BEFORE the corner, so a dot at the
    corner would float past the split on vertical trunks).

    Branches share the source row up to their departure column and share that column's
    vertical run. Dots go on a column at every exit except the farthest per direction
    (that one is the trunk's own corner), offset back by the branch's effective bend
    radius; and on the row at each departure column that a trunk continues past (or
    where the wire splits both up and down), offset left by the same radius rule —
    matching how _rounded_path computes each corner's arc.
    """
    dots: list[tuple[float, float]] = []
    col_xs = sorted(cols)
    for i, xcol in enumerate(col_xs):
        down = sorted(b for b in cols[xcol] if b[0] > sy + 0.75)
        up = sorted((b for b in cols[xcol] if b[0] < sy - 0.75), reverse=True)
        for vy, hx in down[:-1]:
            r = min(_BEND_RADIUS, (vy - sy) / 2, abs(hx - xcol))
            dots.append((xcol, vy - r))
        for vy, hx in up[:-1]:
            r = min(_BEND_RADIUS, (sy - vy) / 2, abs(hx - xcol))
            dots.append((xcol, vy + r))
        row_continues = stays_on_row or i < len(col_xs) - 1
        if row_continues or (down and up):
            run = max((abs(vy - sy) for vy, _ in cols[xcol]), default=0.0)
            r = min(_BEND_RADIUS, xcol - sx, run / 2)
            dots.append((xcol - r, sy))
    return dots


def _render_edges(
    parts: list[str],
    overlays: list[str],
    connections: dict[str, Any],
    geos: dict[str, dict[str, Any]],
    connection_values: dict[str, Any] | None = None,
) -> set[str]:
    """Draw wires source→sink; returns connected element ids.

    Direction is carried by the pins at the boxes (Studio-style), not by arrowheads.
    A wire with several sinks runs a short trunk to a junction dot first, then fans out
    from there (Studio's Knotenpunkt). Inversion circles and junction dots go into
    overlays so they stay visible on top of node borders/pins.

    Every path/junction of one connection carries the same data-net id so a frontend
    card can hover-highlight the WHOLE net — the branches of a fan-out share their
    trunk segment, so highlighting only the hovered path looks like a broken wire.

    connection_values: optional {source_element_id: [value_per_output_row]} ground truth
    (Stufe 2, from api.get_function_plan_connection_values) — keyed by the SOURCE
    FubElementId, indexed by its output IOPos (see _render_net), independent of `net` (a
    plain render-local sequence number used only for the data-net hover grouping).
    """
    connected: set[str] = set()
    const_labeled: set[str] = set()
    # Element bodies are routing obstacles (comments are not — wires may cross text).
    obstacles: list[_Rect] = [(g["x"], g["y"], g["w"], g["h"]) for g in geos.values() if g["kind"] != "comment"]
    for net, conn in enumerate(connections.values()):
        _render_net(parts, overlays, net, conn, geos, obstacles, connected, const_labeled, connection_values)
    return connected


def _render_net(
    parts: list[str],
    overlays: list[str],
    net: int,
    conn: dict[str, Any],
    geos: dict[str, dict[str, Any]],
    obstacles: list[_Rect],
    connected: set[str],
    const_labeled: set[str],
    connection_values: dict[str, Any] | None = None,
) -> None:
    """One connection: const wire label, hot state, branch wires, junctions, inversions."""
    src = conn.get("input") or {}
    src_id = str(src.get("FubElementId"))
    src_geo = geos.get(src_id)
    if src_geo is None:
        return
    connected.add(src_id)
    sx = src_geo["x"] + src_geo["w"]
    sy = _port_y(src_geo, src.get("IOPos"), "out")
    if src_geo["kind"] == "const" and src_id not in const_labeled:
        # A constant's value labels the wire ONCE, at its start (not per sink end).
        # Kept tight to the const body but clear of the pin glyph (user-tuned twice:
        # past-the-pin-tip was too far, halfway along the pin zone squeezed single
        # digits against the circle) — start just past the wire dock point.
        const_labeled.add(src_id)
        overlays.append(
            f'<text class="port-label" x="{sx + 1.0:.1f}" y="{sy - 3:.1f}" '
            f'font-size="{0.85 * _PORT_FONT_SIZE:.1f}">{escape(src_geo["const_value"])}</text>'
        )
    sinks = [(geos[sid], s) for s in _sink_list(conn) if (sid := str(s.get("FubElementId"))) in geos]
    # A HIGH wire tree renders red — driven by the SOURCE only (no sink inference;
    # wires INTO a HIGH marker stay grey) and only for DIGITAL signals (see _net_hot).
    # A real per-connection value (Stufe 2 ground truth) overrides the inference entirely,
    # including for block-internal sources _net_hot cannot read at all. Ground truth is
    # keyed by the SOURCE element id and holds one value per output row (several sinks
    # fed from the same output row share that one slot) — see get_function_plan_connection_values.
    src_values = connection_values.get(src_id) if connection_values else None
    src_row = int(src.get("IOPos") or 0)
    real_value = src_values[src_row] if src_values and src_row < len(src_values) else None
    if real_value is not None:
        is_hot = _is_high(real_value) and _src_analog(src_geo, src.get("IOPos")) is False
    else:
        is_hot = _net_hot(src_geo, sinks)
    if connection_values is not None:
        _LOGGER.debug(
            "net=%s src_id=%s src_row=%s src_values=%s real_value=%s analog=%s is_hot=%s sinks=%s",
            net,
            src_id,
            src_row,
            src_values,
            real_value,
            _src_analog(src_geo, src.get("IOPos")),
            is_hot,
            [str(s.get("FubElementId")) for _, s in sinks],
        )
    hot = " edge-hot" if is_hot else ""
    ends = [(sg, s, sg["x"], _port_y(sg, s.get("IOPos"), "in")) for sg, s in sinks]
    cols, stays_on_row = _render_branches(parts, overlays, net, hot, sx, sy, ends, obstacles, connected)
    if len(sinks) > 1:
        overlays.extend(
            f'<circle class="edge-junction{hot}" data-net="{net}" cx="{jx:.1f}" cy="{jy:.1f}" r="3.0"/>'
            for jx, jy in _junction_points(sx, sy, cols, stays_on_row)
        )
    if src.get("Inverted"):
        overlays.append(f'<circle class="edge-dot" cx="{sx:.1f}" cy="{sy:.1f}" r="3.2"/>')


def _render_branches(
    parts: list[str],
    overlays: list[str],
    net: int,
    hot: str,
    sx: float,
    sy: float,
    ends: list[tuple[Any, Any, float, float]],
    obstacles: list[_Rect],
    connected: set[str],
) -> tuple[dict[float, list[tuple[float, float]]], bool]:
    """Route + emit every branch wire of one net; returns (departure columns,
    stays_on_row) exactly as _junction_points expects them."""
    cols: dict[float, list[tuple[float, float]]] = {}
    stays_on_row = False
    shared_col = _shared_column(sx, sy, ends, obstacles)
    for _sink_geo, sink, tx, ty in ends:
        connected.add(str(sink.get("FubElementId")))
        path, depart_x, vert_end_y = _edge_route(sx, sy, tx, ty, obstacles, shared_col)
        # Both docks run on UNDER the pin glyphs to the body edges (nodes render after
        # edges, so the run-on stays covered): a wire meeting a triangle tip or circle
        # rim only in that single point reads as a gap wherever the glyph silhouette is
        # thinner than the stroke.
        path = f"M{sx - _PIN_LEN:.1f},{sy:.1f} L{path[1:]} L{tx + _PIN_LEN:.1f},{ty:.1f}"
        parts.append(f'<path class="edge-line{hot}" data-net="{net}" d="{path}" stroke-width="1.2"/>')
        if depart_x is None:
            stays_on_row = True
        else:
            cols.setdefault(round(depart_x, 1), []).append((vert_end_y, tx))
        if sink.get("Inverted"):
            overlays.append(f'<circle class="edge-dot" cx="{tx:.1f}" cy="{ty:.1f}" r="3.2"/>')
    return cols, stays_on_row


def _sink_analog(sink_geo: dict[str, Any], sink: dict[str, Any]) -> bool:
    """Analog-ness of the input port a wire ends at (unknown counts as analog)."""
    if sink_geo["kind"] == "block" and sink_geo["in"]:
        return _row_analog(sink_geo, "in", _port_row(sink_geo, sink.get("IOPos"), "in"))
    return sink_geo["analog"] is not False


def _src_analog(src_geo: dict[str, Any], src_iopos: Any) -> bool | None:
    """Analog-ness of the source's specific OUTPUT port — extends _element_analog's
    pill-only coverage to block (kind="block") sources, whose per-row port type is the
    only way to gate a ground-truth connection value (Stufe 2) on digital-ness."""
    if src_geo["kind"] == "block" and src_geo["out"]:
        return _row_analog(src_geo, "out", _port_row(src_geo, src_iopos, "out"))
    return src_geo["analog"]


def _net_hot(src_geo: dict[str, Any], sinks: list[tuple[dict[str, Any], dict[str, Any]]]) -> bool:
    """Red = digital HIGH only (user rule). Pill sources are gated on their own
    analog flag in _build_geometries; a constant has no type of its own and takes
    it from the input(s) it feeds — a "1" on an analog input (Dimmer level, Min/Max)
    is an analog value, not a HIGH state, so its wire stays grey."""
    if not src_geo["hot"]:
        return False
    if src_geo["kind"] != "const":
        return True
    return bool(sinks) and not any(_sink_analog(sg, s) for sg, s in sinks)
