# Version: 0.8.4
"""Renders a Function Plan (elements + connections) as a labelled diagram.

Pure functions only — no Comexio API calls, no HA dependencies. Works identically for a
live plan (api.function_plan_load_elements) and a stored backup snapshot (both share the same
elements/connections shape, see function_plan_backup.py), so the caller decides the data source
and this module just draws what it's given.

Connection semantics (verified against live backup data — constants, which have no inputs,
appear exclusively on this side): conn["input"] is the SOURCE end (its IOPos indexes the
element's OUTPUT ports), conn["output"][] are the SINK ends (IOPos indexes INPUT ports).

Geometry is a unit-true clone of Comexio Studio's own DOM (Flur-plan export): Studio lays
everything out on a 15-SVG-unit port-row grid, and plan coordinates (position_x/y) ARE
those units 1:1 — verified in backups, where two pills stacked onto adjacent block inputs
sit exactly 15 apart (FUNCTION_PLAN_LAYOUT_Y_STEP = 22.5 is 1.5 rows: HA's marker-PAIR
spacing, not the row pitch). Every size below therefore mirrors the DOM numbers directly.
An element's position INCLUDES its pin zones (9.5 units each side); the body rect is inset
by that much, pins (triangle = digital, circle = analog) fill their zone edge-to-edge, and
wires dock at the element's outer bounds. (Deliberate deviation from Studio: its shorter
glyph + stub line left a visible gap at the wire end and a loose line fragment at the glyph
— user-tuned to full-zone glyphs instead.) Only Studio's proportions make rows
line up: a pill placed one Y_STEP below a block top is exactly on the block's first port
row, as in the original. Title bars are square at the bottom (fill band over the rounded
body); block OUTPUT ports are vertically centered on the body. Wires route orthogonally
(H-V-H with rounded bends) like Studio, not as straight diagonals; feedback wires (sink
left of the source) loop across the rows halfway and dock from a column left of the sink.
Pills use Studio's split layout — colored ID box (M141 / IOX3#AI5 / T12), separator,
description, live value right-aligned — and wires fed by a constant carry its value once,
as a small label at the wire's start.

Blocks render COLLAPSED like Studio: fubBase "auto_hide" ports (catalog in_hide/out_hide)
are omitted and the remaining rows compact upwards, unless one of the hidden ports is
actually wired — the expanded state ("Alles anzeigen") is not part of the plan data, so
usage is the only reliable signal, and Studio cannot hide a wired pin either.

Implementation note (2026-08): the module used to hold all of the above in one file; it is
now split by responsibility across several function_plan_render_*.py siblings so each stays
focused and reviewable:
  - function_plan_render_constants.py — shared geometry/style constants + the CSS block
  - function_plan_render_labels.py    — human-readable element labels/tooltips
  - function_plan_render_values.py    — element analog/value classification, pill text parts
  - function_plan_render_geometry.py  — per-element position/size/port-row geometry
  - function_plan_render_wiring.py    — wire routing + wire/junction SVG markup
  - function_plan_render_svg.py       — node/pill/block/comment SVG markup
This file keeps only the top-level orchestration (render_plan_svg) and re-exports the two
symbols other modules import (render_plan_svg itself, resolve_element_label).
"""

from html import escape
from typing import Any

from .function_plan_render_constants import _FONT_SIZE, _MARGIN, _STYLE
from .function_plan_render_geometry import _bounding_box, _build_geometries
from .function_plan_render_labels import resolve_element_label
from .function_plan_render_svg import _render_nodes
from .function_plan_render_wiring import _render_edges

__all__ = ["render_plan_svg", "resolve_element_label"]


def render_plan_svg(
    elements: dict[str, Any],
    connections: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    title: str,
    title_suffix: str = "",
    sun_times: dict[str, str] | None = None,
    canvas: tuple[float, float] | None = None,
    connection_values: dict[str, Any] | None = None,
) -> str:
    """Render elements at Comexio's own position_x/position_y with port-accurate wiring.

    Node placement mirrors Comexio's layout exactly (no auto-layout) so a generated preview
    is visually recognizable against the plan as seen in Comexio Studio. Colors are set via
    CSS classes (not inline fill/stroke) so the @media(prefers-color-scheme: dark) block in
    _STYLE can override them for dark-theme browsers/OS.

    sun_times: optional {Freq: "DD.MM. HH:MM"} for calendar-function tooltips (see
    _calendar_function_tooltip) — pre-formatted by the caller since this module has no HA
    dependency of its own.

    canvas: optional (width, height) of the plan's paper in Studio units (see
    api.get_fub_canvas_bounds). When given, the viewBox spans the whole paper and elements
    keep their absolute canvas coordinates, so plans of the same paper size all render at
    the same relative scale (a nearly empty plan no longer blows up to card width). None
    (e.g. snapshots of deleted plans) falls back to fitting the content bounding box.

    connection_values: optional Stufe-2 ground truth (see _render_edges) — only meaningful
    for a LIVE render of the plan it was fetched for; a caller rendering a backup snapshot
    must leave this None since a snapshot's element ids belong to a different point in
    time (or a deleted/recreated plan) and would color wires from unrelated data.
    """
    geos = _build_geometries(elements, connections, catalog, markers_by_id, webio_by_id, ios_by_id)
    min_x, min_y, max_x, max_y = _bounding_box(geos)
    if canvas:
        # Paper mode: absolute coordinates, canvas grows only if content overflows the paper.
        width = max(canvas[0], max_x + _MARGIN)
        height = max(canvas[1], max_y + _MARGIN) + 34  # extra headroom for the title
        for geo in geos.values():
            geo["y"] += 34
    else:
        width = max_x - min_x + 2 * _MARGIN
        height = max_y - min_y + 2 * _MARGIN + 34  # extra headroom for the title
        for geo in geos.values():
            geo["x"] += _MARGIN - min_x
            geo["y"] += _MARGIN + 34 - min_y

    labels = {
        elem_id: resolve_element_label(elem, catalog, markers_by_id, webio_by_id, ios_by_id, sun_times)
        for elem_id, elem in elements.items()
    }

    parts: list[str] = [
        # Inline max-width caps browser upscaling at 1:1 Studio units no matter how the SVG
        # is embedded (the plan card styles it width:100%; inline style wins over that).
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'style="max-width:{width:.0f}px" '
        f'font-family="sans-serif" font-size="{_FONT_SIZE:.1f}">',
        _STYLE,
        '<rect class="canvas-bg" width="100%" height="100%"/>',
        f'<text class="plan-title" x="{_MARGIN}" y="24" font-size="20" font-weight="bold">{escape(title)}'
        # Status/paper info at half the plan-name size, normal weight, on the same baseline.
        # dx gives a reliable gap — literal spaces inside the tspan collapse to one in SVG.
        + (f'<tspan dx="12" font-size="10" font-weight="normal">{escape(title_suffix)}</tspan>' if title_suffix else "")
        + "</text>",
    ]
    overlays: list[str] = []
    connected = _render_edges(parts, overlays, connections, geos, connection_values)
    _render_nodes(parts, geos, labels, connected)
    parts.extend(overlays)
    parts.append("</svg>")
    return "\n".join(parts)
