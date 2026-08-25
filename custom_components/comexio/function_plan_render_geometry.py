# Version: 0.8.5
"""Per-element geometry (position/size/ports) for the Function Plan renderer.

Split out of function_plan_render.py (2026-08). Builds, per plan element, the x/y/w/h box
plus (for blocks) the visible in/out port rows — everything downstream (wire routing in
function_plan_render_wiring.py, SVG markup in function_plan_render_svg.py) reads from the
geo dicts produced here rather than the raw element/connection data.

kind: "pill" (markers/IOs/WebIOs/time modules), "block" (fubBase with title bar + port
rows), "const" (compact value pill), "comment" (borderless text). x/y/w/h are the
element's FULL bounds like in the Studio DOM — the pin zones are part of the element, the
body rect is inset _PIN_LEN left/right (see function_plan_render module docstring).
"""

from typing import Any

from .function_plan_render_constants import (
    _BLOCK_UNIT_WIDTH,
    _COMMENT_LINE_H,
    _CONST_WIDTH,
    _FONT_SIZE,
    _MARGIN,
    _PILL_WIDTH,
    _ROW_H,
    _VARIADIC_PORT_THRESHOLD,
)
from .function_plan_render_values import _element_analog, _element_raw_value, _is_high, _pill_parts


def _sink_list(conn: dict[str, Any]) -> list[dict[str, Any]]:
    sinks = conn.get("output") or []
    return list(sinks.values()) if isinstance(sinks, dict) else sinks


def _used_ports(connections: dict[str, Any]) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Connected input/output IOPos sets per element id — sizes variadic blocks and
    decides whether auto_hide rows must stay visible (a wired pin cannot be collapsed)."""
    used_in: dict[str, set[int]] = {}
    used_out: dict[str, set[int]] = {}
    for conn in connections.values():
        src = conn.get("input") or {}
        used_out.setdefault(str(src.get("FubElementId")), set()).add(int(src.get("IOPos") or 0))
        for sink in _sink_list(conn):
            used_in.setdefault(str(sink.get("FubElementId")), set()).add(int(sink.get("IOPos") or 0))
    return used_in, used_out


def _visible_ports(defined: dict[str, Any], max_used: int) -> list[str]:
    count = len(defined)
    if count > _VARIADIC_PORT_THRESHOLD:
        count = max(2, min(count, max_used + 1))
    return [str(defined.get(str(i), i)) for i in range(count)]


def _port_names(block_def: dict[str, Any], side: str, max_used: int) -> list[str]:
    """Port names of one block side ("in"/"out"), row order = IOPos.

    Prefers the exact per-variant port count from $FubModules["5"] (n_in/n_out — the
    four "or" catalog entries really are 2/3/4/5-input variants); autogrow blocks gain
    rows up to their limit when higher IOPos values are actually connected. Falls back
    to the I18N-dict heuristic for catalogs persisted before these fields existed.
    """
    defined = block_def.get(side) or {}
    count = block_def.get("n_in" if side == "in" else "n_out")
    if not count:
        return _visible_ports(defined, max_used)
    if side == "in" and (autogrow := block_def.get("autogrow") or 0):
        count = min(max(count, max_used + 1), autogrow)
    return [str(defined.get(str(i), i)) for i in range(count)]


def _visible_rows(names: list[str], types: list[int], hidden: set[int]) -> tuple[list[str], list[bool], dict[int, int]]:
    """Filter auto_hide ports out and compact the rest upwards (Studio's collapsed view).

    Returns (visible names, per-row analog flags, {IOPos: visible row index}) — the map is
    what keeps wires docked on the right row after rows above them disappeared.
    """
    vis_names: list[str] = []
    vis_types: list[bool] = []
    pos_map: dict[int, int] = {}
    for pos, name in enumerate(names):
        if pos in hidden:
            continue
        pos_map[pos] = len(vis_names)
        vis_names.append(name)
        vis_types.append(pos < len(types) and types[pos] == 1)
    return vis_names, vis_types, pos_map


def _block_geometry(geo: dict[str, Any], block_def: dict[str, Any], used_in: set[int], used_out: set[int]) -> None:
    geo["kind"] = "block"
    names_in = _port_names(block_def, "in", max(used_in, default=0))
    names_out = _port_names(block_def, "out", max(used_out, default=0))
    # Collapsed by default like Studio; a wired auto_hide pin forces the expanded view
    # (the "Alles anzeigen" flag itself is not part of the plan data). Port types come
    # from $FubModules["5"].input/output[..].Type (0=digital, 1=analog); catalogs
    # persisted before in_types/out_types existed fall back to digital triangles.
    hide_in = set(block_def.get("in_hide") or [])
    hide_out = set(block_def.get("out_hide") or [])
    if (hide_in & used_in) or (hide_out & used_out):
        hide_in = set()
        hide_out = set()
    geo["in"], geo["in_types"], geo["in_map"] = _visible_rows(names_in, block_def.get("in_types") or [], hide_in)
    geo["out"], geo["out_types"], geo["out_map"] = _visible_rows(names_out, block_def.get("out_types") or [], hide_out)
    geo["h"] = _ROW_H * (1 + max(len(geo["in"]), len(geo["out"]), 1))
    if width_units := block_def.get("width"):
        geo["w"] = _BLOCK_UNIT_WIDTH * width_units


def _shape_special(
    geo: dict[str, Any],
    elem: dict[str, Any],
    fub_base: dict[str, Any],
    used_in: set[int],
    used_out: set[int],
) -> None:
    """Upgrade the pill default to block/const/comment geometry where the type says so."""
    etype = geo["etype"]
    if etype == 5:
        ref = elem.get("reference") or {}
        block_def = fub_base.get(str(ref.get("ref_id"))) or {}
        if block_def.get("in") or block_def.get("out"):
            _block_geometry(geo, block_def, used_in, used_out)
    elif etype == 16:
        geo["kind"] = "const"
        geo["w"] = _CONST_WIDTH
        geo["const_value"] = (elem.get("name") or "?").strip() or "?"
        # A constant IS its value — but whether a "1" const is a digital HIGH or just
        # an analog value depends on the input it feeds, so "hot" here only records
        # the raw value; _net_hot gates it on the sink port types at wire time.
        geo["hot"] = _is_high(geo["const_value"].replace(",", "."))
    elif etype == 14:
        lines = (elem.get("name") or "").splitlines() or [""]
        geo["kind"] = "comment"
        geo["w"] = max(60.0, 0.6 * _FONT_SIZE * max(len(line) for line in lines))
        geo["h"] = max(_ROW_H, _COMMENT_LINE_H * len(lines))


def _build_geometries(
    elements: dict[str, Any],
    connections: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> dict[str, dict[str, Any]]:
    """Per-element geometry at Comexio's own position_x/position_y.

    x/y/w/h are the element's FULL bounds like in the Studio DOM — the pin zones are part
    of the element, the body rect is inset _PIN_LEN left/right. kind: "pill" (markers/IOs/
    WebIOs/time modules), "block" (fubBase with title bar + port rows), "const" (compact
    value pill), "comment" (borderless text). "pins" says whether pins are drawn; wires
    dock at the element bounds (pin outer ends).
    """
    used_in, used_out = _used_ports(connections)
    fub_base = catalog.get("fub_base", {})
    return {
        elem_id: _build_one_geometry(
            elem_id, elem, catalog, markers_by_id, webio_by_id, ios_by_id, fub_base, used_in, used_out
        )
        for elem_id, elem in elements.items()
    }


def _geometry_is_inactive(etype: int | None, ref_id: str, ios_by_id: dict, markers_by_id: dict) -> bool:
    """Whether an element should render greyed-out: an inactive IO (Studio: cannot even be
    wired) or an unnamed ("#nn") marker (extracted from _build_one_geometry to stay under
    the complexity budget)."""
    if etype == 1:
        return bool((ios_by_id.get(ref_id) or {}).get("inactive"))
    if etype == 2:
        return bool((markers_by_id.get(ref_id) or {}).get("no_name"))
    return False


def _apply_geometry_target_fields(
    geo: dict[str, Any], etype: int | None, ref_id: str, markers_by_id: dict, ios_by_id: dict
) -> None:
    """Debug-box addressing fields (target/writable/io_input), mutating geo in place
    (extracted from _build_one_geometry to stay under the complexity budget).

    Markers and IOs are addressable via comexio.set_value ("M4" / "UD2#Q2") — the plan
    card reads these fields (as data-* attributes) for click-to-fill, autocomplete and
    plan-local command validation. An IO INPUT (I/AI/…) is a pure source: the Comexio API
    cannot write it, so it is not writable and _render_pill draws no input pin for it either.
    """
    if etype == 2 and ref_id in markers_by_id:
        geo["target"] = f"M{ref_id}"
        geo["writable"] = True
    elif etype == 1 and (io := ios_by_id.get(ref_id)):
        geo["target"] = f"{io.get('ext_name', '')}#{io.get('identifier', '')}"
        geo["io_input"] = bool(io.get("is_input"))
        geo["writable"] = not geo["io_input"] and not geo["inactive"]


def _build_one_geometry(
    elem_id: str,
    elem: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    fub_base: dict[str, Any],
    used_in: dict[str, set[int]],
    used_out: dict[str, set[int]],
) -> dict[str, Any]:
    """Geometry for a single plan element (see _build_geometries)."""
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    geo: dict[str, Any] = {
        "x": float(elem.get("position_x", 0) or 0),
        "y": float(elem.get("position_y", 0) or 0),
        "w": _PILL_WIDTH,
        "h": _ROW_H,
        "kind": "pill",
        "etype": etype,
        "in": [],
        "out": [],
        "analog": _element_analog(elem, markers_by_id, webio_by_id, ios_by_id),
        # Greyed elements — via CSS classes separate from node-orphan so all remain
        # independently stylable (see _STYLE).
        "inactive": _geometry_is_inactive(etype, ref_id, ios_by_id, markers_by_id),
    }
    # Stufe 1 of Studio's live-wire coloring: only PILL sources (markers/IOs)
    # and constants carry a readable value — block-internal outputs stay
    # unknown (grey wires) until they become readable (Stufe 2). A wire's
    # color comes from its SOURCE alone; the former sink inference is gone
    # (user-verified against Studio: M4 reading 1 must not color the wire
    # that WRITES M4 — only the wires leaving M4's output). Red additionally
    # requires a DIGITAL source (user rule): an analog output reading 1 is a
    # value, not a HIGH state.
    geo["value_raw"] = _element_raw_value(elem, markers_by_id, webio_by_id, ios_by_id)
    geo["hot"] = _is_high(geo["value_raw"]) and geo["analog"] is False
    _shape_special(geo, elem, fub_base, used_in.get(elem_id, set()), used_out.get(elem_id, set()))
    _apply_geometry_target_fields(geo, etype, ref_id, markers_by_id, ios_by_id)
    geo["pins"] = geo["kind"] == "block" or (geo["kind"] in ("pill", "const") and geo["analog"] is not None)
    if geo["kind"] == "pill":
        geo["id_text"], geo["desc"], geo["value"] = _pill_parts(elem, catalog, markers_by_id, webio_by_id, ios_by_id)
    return geo


def _row_y(geo: dict[str, Any], side: str, row: int) -> float:
    """Y center of a block port row — Studio centers EACH side's rows on the body
    vertically (a Tastenwechsler's single Taste input sits mid-body between its two
    outputs, not on the first row); a side using every row is centered already."""
    mid = geo["y"] + (geo["h"] + _ROW_H) / 2
    return mid + (row - (len(geo[side]) - 1) / 2) * _ROW_H


def _port_row(geo: dict[str, Any], iopos: Any, side: str) -> int:
    """Row index of a block port — IOPos translated through the collapsed-view map
    (hidden rows compact upwards); positions unknown to the map clamp to the nearest
    existing row as a safety net."""
    pos = int(iopos or 0)
    row = (geo.get(f"{side}_map") or {}).get(pos)
    if row is None:
        row = min(max(pos, 0), len(geo[side]) - 1)
    return row


def _port_y(geo: dict[str, Any], iopos: Any, side: str) -> float:
    """Y coordinate of a port row (side: "in"/"out"); pills dock at their vertical center."""
    if geo["kind"] == "block" and geo[side]:
        return _row_y(geo, side, _port_row(geo, iopos, side))
    return geo["y"] + _ROW_H / 2


def _row_analog(geo: dict[str, Any], side: str, row: int) -> bool:
    """Analog flag of one block port row; grown/unknown rows fall back sensibly."""
    types = geo[f"{side}_types"]
    return types[min(row, len(types) - 1)] if types else False


def _bounding_box(geos: dict[str, dict[str, Any]]) -> tuple[float, float, float, float]:
    if not geos:
        return 0.0, 0.0, _PILL_WIDTH + 2 * _MARGIN, _ROW_H + 2 * _MARGIN
    min_x = min(g["x"] for g in geos.values())
    min_y = min(g["y"] for g in geos.values())
    max_x = max(g["x"] + g["w"] for g in geos.values())
    max_y = max(g["y"] + g["h"] for g in geos.values())
    return min_x, min_y, max_x, max_y


def _fit_to_bounding_box(geos: dict[str, dict[str, Any]]) -> tuple[float, float]:
    """Translate every geo so the content's bounding box starts at _MARGIN and return the
    (width, height) canvas that exactly fits it, plus the title's headroom (34, see
    render_plan_svg's title text) — the "no fixed Studio paper size given" sizing shared by
    every renderer that just fits its own content instead of a real plan's paper bounds."""
    min_x, min_y, max_x, max_y = _bounding_box(geos)
    width = max_x - min_x + 2 * _MARGIN
    height = max_y - min_y + 2 * _MARGIN + 34
    for geo in geos.values():
        geo["x"] += _MARGIN - min_x
        geo["y"] += _MARGIN + 34 - min_y
    return width, height
