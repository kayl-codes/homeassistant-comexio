# Version: 0.8.4
"""Shared geometry/style constants for the Function Plan SVG renderer.

Split out of function_plan_render.py (2026-08) so every render submodule (labels, values,
geometry, wiring, svg) can import the same numbers without creating import cycles between
those modules. No logic lives here — pure constants + the CSS block.
"""

# Plan pixels per Studio SVG unit — plan coordinates are Studio units 1:1 (see
# function_plan_render module docstring; stacked adjacent inputs are exactly one 15-unit
# row apart in backup data).
_UNIT = 1.0
_ROW_H = 15.0 * _UNIT  # port-row pitch — identical to the plan's own Y grid
_PIN_LEN = 9.5 * _UNIT  # pin zone outside the body edge, per side
_BODY_PAD = 0.5 * _UNIT  # body rects are inset half a unit from the row bounds
_PILL_WIDTH = 180.0 * _UNIT  # markers/IOs/WebIOs/time modules (Studio: 180×15 units)
_CONST_WIDTH = 60.0 * _UNIT  # constants share the unit-1 block footprint
_BLOCK_UNIT_WIDTH = 60.0 * _UNIT  # fubBase width unit ($FubModules["5"].Width) in plan px
_RADIUS = 7.0 * _UNIT  # rounded corners (Studio rx=7)
_BEND_RADIUS = 14.0 * _UNIT  # wire bend radius (user-tuned: larger arcs than Studio's)
_DOCK_LEAD = 3.0 * _UNIT  # straight run kept at the wire's dock points before/after the outermost arcs
_ID_BOX_MIN = 35.5 * _UNIT  # pill ID box (Studio: fill band up to x=45, body starts 9.5)
_MARGIN = 25.0
_FONT_SIZE = 9.8 * _UNIT  # pill/const text (Studio font sizes, unit-true)
_PORT_FONT_SIZE = 9.0 * _UNIT
_HEAD_FONT_SIZE = 10.5 * _UNIT
_COMMENT_LINE_H = 12.0 * _UNIT
# Legacy fallback for catalogs persisted before n_in/n_out existed: variadic blocks
# (e.g. Oder defines inputs a..p = 16) then show only the used ports.
_VARIADIC_PORT_THRESHOLD = 8

_STYLE = """
<style>
  .canvas-bg { fill: #ffffff; }
  .plan-title { fill: #111111; }
  .node { fill: #ebf4ff; stroke: #2b6cb0; fill-opacity: 0.86; }
  .node-webio { fill: #2b6cb0; stroke: #1a4971; fill-opacity: 0.92; }
  .node-orphan { fill: #f5f5f5; stroke: #999999; fill-opacity: 0.86; }
  /* Same default as node-orphan, but a separate class: an inactive IO is an expected,
     valid state (Comexio won't even let it be wired), not a broken/orphaned reference. */
  .node-inactive { fill: #f5f5f5; stroke: #999999; fill-opacity: 0.86; }
  /* Whole-group grey-out for inactive IOs and unnamed ("#nn") markers: the body rect
     alone is too subtle (ID box, texts and pins keep their colors), so the wrapping
     g.node-g-inactive mutes those too. Quick restyle hook: these rules + dark twins. */
  .node-g-inactive .block-head { fill: #9e9e9e; }
  .node-g-inactive .block-head-label { fill: #f5f5f5; }
  .node-g-inactive .node-label { fill: #8a8a8a; }
  .node-g-inactive .port-glyph { fill: #aaaaaa; }
  .node-label { fill: #111111; }
  .block-head { fill: #2b6cb0; }
  .block-head-label { fill: #ffffff; }
  .port-label { fill: #333333; }
  .port-glyph { fill: #555555; }
  .node-comment { fill: #666666; font-style: italic; }
  .edge-line { stroke: #555555; fill: none; }
  .edge-dot { fill: #ffffff; stroke: #555555; }
  .edge-junction { fill: #555555; stroke: none; }
  .edge-line.edge-hot { stroke: #d05c5c; }
  .edge-junction.edge-hot { fill: #d05c5c; }
  @media (prefers-color-scheme: dark) {
    .canvas-bg { fill: #1e1e1e; }
    .plan-title { fill: #e8e8e8; }
    .node { fill: #16324a; stroke: #5b9bd5; fill-opacity: 0.86; }
    .node-webio { fill: #2b6cb0; stroke: #5b9bd5; fill-opacity: 0.92; }
    .node-orphan { fill: #2a2a2a; stroke: #777777; fill-opacity: 0.86; }
    .node-inactive { fill: #2a2a2a; stroke: #777777; fill-opacity: 0.86; }
    .node-g-inactive .block-head { fill: #4a4a4a; }
    .node-g-inactive .block-head-label { fill: #a8a8a8; }
    .node-g-inactive .node-label { fill: #8a8a8a; }
    .node-g-inactive .port-glyph { fill: #666666; }
    .node-label { fill: #e8e8e8; }
    .block-head { fill: #2b6cb0; }
    .block-head-label { fill: #ffffff; }
    .port-label { fill: #cccccc; }
    .port-glyph { fill: #bbbbbb; }
    .node-comment { fill: #999999; }
    .edge-line { stroke: #aaaaaa; fill: none; }
    .edge-dot { fill: #1e1e1e; stroke: #aaaaaa; }
    .edge-junction { fill: #aaaaaa; }
    .edge-line.edge-hot { stroke: #e57373; }
    .edge-junction.edge-hot { fill: #e57373; }
  }
</style>
""".strip()
