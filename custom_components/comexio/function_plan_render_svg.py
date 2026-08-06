# Version: 0.8.4
"""Node/pill/block/comment SVG markup for the Function Plan renderer.

Split out of function_plan_render.py (2026-08). Consumes the geo dicts built by
function_plan_render_geometry.py (and the resolved labels from
function_plan_render_labels.py) and emits the actual <rect>/<path>/<text> markup for each
plan element. Wire markup lives in function_plan_render_wiring.py; this module only draws
node bodies, title bars, port rows and pin glyphs.
"""

from html import escape
from typing import Any

from .function_plan_render_constants import (
    _BODY_PAD,
    _COMMENT_LINE_H,
    _FONT_SIZE,
    _HEAD_FONT_SIZE,
    _ID_BOX_MIN,
    _PIN_LEN,
    _PORT_FONT_SIZE,
    _RADIUS,
    _ROW_H,
)
from .function_plan_render_geometry import _row_analog, _row_y


def _pin(x0: float, y: float, analog: bool) -> str:
    """One pin glyph filling the FULL zone [x0, x0 + _PIN_LEN], pointing right.

    Input pins sit in the zone LEFT of the body edge, output pins in the zone RIGHT of
    it — the caller picks x0 accordingly; both point in flow direction. The glyph spans
    the whole zone so it touches the wire dock point on one side and the body edge on
    the other — no stub line and no gap (see module docstring: Studio deviation).
    The analog circle is drawn well under the zone radius (4.0 vs 4.75, user-tuned):
    a full circle reads optically larger than the digital triangle of the same zone.
    """
    if analog:
        return f'<circle class="port-glyph" cx="{x0 + 0.5 * _PIN_LEN:.2f}" cy="{y:.1f}" r="4.0"/>'
    tri = f"M{x0:.1f},{y - 0.4 * _PIN_LEN:.1f} L{x0 + _PIN_LEN:.1f},{y:.1f} L{x0:.1f},{y + 0.4 * _PIN_LEN:.1f} z"
    return f'<path class="port-glyph" d="{tri}"/>'


def _fit_chars(width: float, font: float = _FONT_SIZE, factor: float = 0.55) -> int:
    """How many characters fit into a box of the given width (avg char ≈ factor·font)."""
    return max(int(width / (font * factor)), 4)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else f"{text[: max_len - 1]}…"


def _block_head_path(x: float, y: float, w: float) -> str:
    """Title-bar path with ONLY the top corners rounded (Studio: header band is square
    at the bottom because it's a fill region inside the rounded body, not its own rect)."""
    r = _RADIUS
    return (
        f"M{x:.1f},{y + _ROW_H:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + _ROW_H:.1f} Z"
    )


def _render_block(parts: list[str], geo: dict[str, Any], label: str, orphan: bool) -> None:
    x, y = geo["x"] + _PIN_LEN, geo["y"] + _BODY_PAD
    w = geo["w"] - 2 * _PIN_LEN
    h = geo["h"] - 2 * _BODY_PAD
    body_class = "node-orphan" if orphan else "node"
    parts.append(
        f'<rect class="{body_class}" x="{x:.1f}" y="{y:.1f}" width="{w:.0f}" height="{h:.0f}" '
        f'rx="{_RADIUS:.1f}" stroke-width="1"><title>{escape(label)}</title></rect>'
    )
    parts.append(f'<path class="block-head" d="{_block_head_path(x, y, w)}"/>')
    parts.append(
        f'<text class="block-head-label" x="{x + w / 2:.1f}" y="{geo["y"] + _ROW_H / 2 + 0.36 * _HEAD_FONT_SIZE:.1f}" '
        f'text-anchor="middle" font-size="{_HEAD_FONT_SIZE:.1f}" font-weight="bold">'
        f"{escape(_truncate(label, _fit_chars(w, _HEAD_FONT_SIZE, 0.48)))}</text>"
    )
    for row, name in enumerate(geo["in"]):
        row_y = _row_y(geo, "in", row)
        parts.append(
            f'<text class="port-label" x="{x + 3:.1f}" y="{row_y + 0.36 * _PORT_FONT_SIZE:.1f}" '
            f'font-size="{_PORT_FONT_SIZE:.1f}">{escape(name)}</text>'
        )
        parts.append(_pin(geo["x"], row_y, _row_analog(geo, "in", row)))
    for row, name in enumerate(geo["out"]):
        row_y = _row_y(geo, "out", row)
        parts.append(
            f'<text class="port-label" x="{x + w - 3:.1f}" y="{row_y + 0.36 * _PORT_FONT_SIZE:.1f}" '
            f'text-anchor="end" font-size="{_PORT_FONT_SIZE:.1f}">{escape(name)}</text>'
        )
        parts.append(_pin(geo["x"] + geo["w"] - _PIN_LEN, row_y, _row_analog(geo, "out", row)))


def _render_comment(parts: list[str], geo: dict[str, Any], text: str) -> None:
    lines = text.splitlines() or [text]
    tspans = "".join(
        f'<tspan x="{geo["x"]:.1f}" dy="{0 if i == 0 else _COMMENT_LINE_H:.0f}">{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    parts.append(
        f'<text class="node-comment" x="{geo["x"]:.1f}" y="{geo["y"] + _FONT_SIZE:.1f}" '
        f'font-size="{_FONT_SIZE:.1f}">{tspans}</text>'
    )


def _pill_id_box_path(bx: float, by: float, x_sep: float, bh: float) -> str:
    """ID box filling the pill's left end — only its LEFT corners rounded (Studio: the
    colored band is a fill region inside the rounded body, square at the separator)."""
    r = min(_RADIUS, bh / 2)
    return (
        f"M{x_sep:.1f},{by:.1f} H{bx + r:.1f} Q{bx:.1f},{by:.1f} {bx:.1f},{by + r:.1f} "
        f"V{by + bh - r:.1f} Q{bx:.1f},{by + bh:.1f} {bx + r:.1f},{by + bh:.1f} H{x_sep:.1f} Z"
    )


def _render_pill(parts: list[str], geo: dict[str, Any], label: str, node_class: str) -> None:
    bx = geo["x"] + _PIN_LEN
    bw = geo["w"] - 2 * _PIN_LEN
    by = geo["y"] + _BODY_PAD
    bh = _ROW_H - 2 * _BODY_PAD
    cy = geo["y"] + _ROW_H / 2
    ty = cy + 0.36 * _FONT_SIZE
    # WebIO pills wear the header color over their whole body, like Studio.
    if geo.get("etype") == 10 and node_class == "node":
        node_class = "node-webio"
    parts.append(
        f'<rect class="{node_class}" x="{bx:.1f}" y="{by:.1f}" width="{bw:.0f}" height="{bh:.0f}" '
        f'rx="{_RADIUS:.1f}" stroke-width="1"><title>{escape(label)}</title></rect>'
    )
    if geo["kind"] == "const":
        parts.append(
            f'<text class="node-label" x="{bx + bw / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
            f'font-size="{_FONT_SIZE:.1f}">{escape(_truncate(label, _fit_chars(bw)))}</text>'
        )
    elif node_class == "node-webio":
        parts.append(
            f'<text class="block-head-label" x="{bx + 5:.1f}" y="{ty:.1f}" '
            f'font-size="{_FONT_SIZE:.1f}">{escape(_truncate(geo["desc"] or label, _fit_chars(bw - 10)))}</text>'
        )
    else:
        _render_pill_content(parts, geo, label, bx, bw, by, bh, ty)
    if geo["pins"]:
        # Constants and IO inputs are pure sources — output pin only (an IO input
        # cannot be written from the plan side, so an input pin would be a lie).
        if geo["kind"] != "const" and not geo.get("io_input"):
            parts.append(_pin(geo["x"], cy, geo["analog"]))
        parts.append(_pin(geo["x"] + geo["w"] - _PIN_LEN, cy, geo["analog"]))


def _render_pill_content(
    parts: list[str], geo: dict[str, Any], label: str, bx: float, bw: float, by: float, bh: float, ty: float
) -> None:
    """Studio pill interior: |ID box| description … value."""
    id_text, value = geo["id_text"], geo["value"]
    desc = geo["desc"] or label
    text_x = bx + 4
    if id_text:
        x_sep = bx + max(_ID_BOX_MIN, len(id_text) * 0.62 * _FONT_SIZE + 6)
        parts.append(f'<path class="block-head" d="{_pill_id_box_path(bx, by, x_sep, bh)}"/>')
        parts.append(
            f'<text class="block-head-label" x="{(bx + x_sep) / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
            f'font-size="{_FONT_SIZE:.1f}">{escape(id_text)}</text>'
        )
        text_x = x_sep + 3
    value_w = (len(value) * 0.62 * _FONT_SIZE + 8) if value else 2
    if value:
        parts.append(
            f'<text class="node-label" x="{bx + bw - 4:.1f}" y="{ty:.1f}" text-anchor="end" '
            f'font-size="{_FONT_SIZE:.1f}">{escape(value)}</text>'
        )
    max_chars = _fit_chars(bx + bw - value_w - text_x)
    parts.append(
        f'<text class="node-label" x="{text_x:.1f}" y="{ty:.1f}" '
        f'font-size="{_FONT_SIZE:.1f}">{escape(_truncate(desc, max_chars))}</text>'
    )


def _render_nodes(
    parts: list[str],
    geos: dict[str, dict[str, Any]],
    labels: dict[str, str],
    connected: set[str],
) -> None:
    for elem_id, geo in geos.items():
        _render_one_node(parts, elem_id, geo, labels.get(elem_id, "?"), connected)


def _render_one_node(parts: list[str], elem_id: str, geo: dict[str, Any], label: str, connected: set[str]) -> None:
    """Render one plan element's <g> wrapper + interior (see _render_nodes)."""
    # Group each element so a frontend card can address it as ONE unit (hover
    # highlight, search); data-label carries the full, untruncated label text.
    # node-g-inactive greys the WHOLE group (ID box, texts, pins) via _STYLE.
    # Markers/IOs additionally carry their set_value address + type/state as
    # data-* attributes — the card's debug box builds click-to-fill,
    # autocomplete and plan-local validation from exactly these.
    g_class = "node-g node-g-inactive" if geo.get("inactive") else "node-g"
    attrs = ""
    if target := geo.get("target"):
        value_raw = geo.get("value_raw")
        attrs = (
            f' data-target="{escape(str(target))}"'
            f' data-analog="{1 if geo.get("analog") else 0}"'
            f' data-writable="{1 if geo.get("writable") else 0}"'
        )
        if value_raw is not None:
            attrs += f' data-value="{escape(str(value_raw))}"'
    parts.append(f'<g class="{g_class}" data-label="{escape(label)}"{attrs}>')
    if geo["kind"] == "comment":
        _render_comment(parts, geo, label)
    elif geo["kind"] == "block":
        _render_block(parts, geo, label, elem_id not in connected)
    elif geo.get("inactive"):
        _render_pill(parts, geo, label, "node-inactive")
    else:
        _render_pill(parts, geo, label, "node" if elem_id in connected else "node-orphan")
    parts.append("</g>")
