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
"""

from html import escape
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Plan pixels per Studio SVG unit — plan coordinates are Studio units 1:1 (see module
# docstring; stacked adjacent inputs are exactly one 15-unit row apart in backup data).
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


def _named_ref_label(by_id: dict, ref_id: str, fallback: str) -> str:
    """Label of a marker/IO/WebIO reference: the entity's HA display name or a typed
    fallback. The name is already formatted per the configured naming schema (default
    "M{MarkerId} {MarkerTitle}" / "{ExtName} {IoId} {IoTitle}") — it already contains
    the element's own id, so no separate prefix is added here."""
    entry = by_id.get(ref_id)
    return entry["name"] if entry else fallback


# Comexio's "Type" vocabulary for time modules (type 4), confirmed against Studio's own
# "Zeitglied bearbeiten" dialog — German labels matching the dropdown. An unrecognized
# value (e.g. a future firmware type) falls back to the raw string rather than guessing.
_TIME_MODULE_KIND_LABELS = {
    "on_delayed": "Einschaltverzögert",
    "off_delayed": "Ausschaltverzögert",
    "on_pulse": "Einschaltwischend",
}
# For these confirmed kinds, Studio's dialog shows t2 as "keine Funktion" (greyed out) —
# only a Taktgeber-style repeating type uses both t1 (on-time) and t2 (off-time).
_TIME_MODULE_KINDS_WITHOUT_T2 = frozenset(_TIME_MODULE_KIND_LABELS)


def _time_module_tooltip(catalog: dict[str, Any], ref_id: str) -> str:
    """Multi-line hover tooltip for a time module (type 4) pill.

    t1/t2 are stored raw (tenths of a second, confirmed by comparing several instances'
    raw values against their Studio dialog display, e.g. raw 80 == "8 Sek."); formatted
    here with the same trimmed-decimal/German-comma convention as live values (_fmt_value).
    """
    entry = catalog.get("time_modules", {}).get(ref_id) or {}
    name = entry.get("name") or "Zeitglied"
    kind = entry.get("kind")
    lines = [f"T{ref_id} {name}"]
    if kind:
        lines.append(f"Typ: {_TIME_MODULE_KIND_LABELS.get(kind, kind)}")
    if (t1 := entry.get("t1")) is not None:
        lines.append(f"t1: {_fmt_value(t1 / 10)} Sek.")
    if kind not in _TIME_MODULE_KINDS_WITHOUT_T2 and (t2 := entry.get("t2")) is not None:
        lines.append(f"t2: {_fmt_value(t2 / 10)} Sek.")
    return "\n".join(lines)


# Comexio's "Freq" vocabulary for calendar functions (type 3), confirmed via live scrape —
# German labels matching Studio's own "Funktion" dropdown. An unrecognized value (future
# firmware) falls back to the raw string rather than guessing a translation.
_CALENDAR_FREQ_LABELS = {
    "SUN_RISE": "Sonnenaufgang",
    "SUN_SET": "Sonnenuntergang",
    "DAWN": "Morgendämmerung",
    "DUSK": "Abenddämmerung",
    "WEEKLY": "Wochentag",
}


def _calendar_function_tooltip(catalog: dict[str, Any], ref_id: str, sun_times: dict[str, str] | None = None) -> str:
    """Multi-line hover tooltip for a calendar function (type 3) pill.

    Shows the settings visible in Studio's "Kalenderfunktion bearbeiten" dialog that we
    can read with confidence (Aktiv/Funktion/Wochentage) — "Startversatz"/"Beenden an"/
    "Koordinaten" are deliberately left out (see function_plan_catalog._extract_calendar_functions:
    no reliable per-instance offset can be derived from the scraped data).

    sun_times is an optional {Freq: "DD.MM. HH:MM"} lookup (pre-formatted by the caller, e.g.
    coordinator.py from HA's own sun.sun entity — this module stays HA-free) for the astro
    Freq values (SUN_RISE/SUN_SET/DAWN/DUSK). When present, it answers the user's actual
    question ("wann ist das überhaupt") instead of leaving the trigger time a mystery.
    Omitted entirely for WEEKLY or when no sun_times were supplied (e.g. backup-diff/search
    labels, which have no live HA state to draw from).
    """
    entry = catalog.get("calendar_functions", {}).get(ref_id) or {}
    name = entry.get("name") or "Kalenderfunktion"
    freq = entry.get("freq")
    lines = [f"C{ref_id} {name}", f"Aktiv: {'Ja' if entry.get('active') else 'Nein'}"]
    if freq:
        lines.append(f"Funktion: {_CALENDAR_FREQ_LABELS.get(freq, freq)}")
    if freq == "WEEKLY" and (by_day := entry.get("by_day")):
        lines.append(f"Wochentage: {by_day}")
    if sun_times and freq in sun_times:
        lines.append(f"Nächster Zeitpunkt: {sun_times[freq]}")
    return "\n".join(lines)


def _block_label(catalog: dict[str, Any], ref_id: str, elem_name: str) -> str:
    """Label of a fubBase block (type 5), preferring catalog name + instance name."""
    fub_base = catalog.get("fub_base", {}).get(ref_id) or {}
    block = fub_base.get("short_name") or fub_base.get("display_name") or fub_base.get("name")
    if block and elem_name:
        return f"{block}: {elem_name}"
    return block or elem_name or f"Baustein ref={ref_id}"


def resolve_element_label(
    elem: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    sun_times: dict[str, str] | None = None,
) -> str:
    """Build a human-readable label for one plan element.

    Routing is strictly by reference type — ref_id is only unique WITHIN one element type,
    so a catalog lookup without a type check can hit a coincidentally-numbered entry of a
    different kind (this mislabelled e.g. a Schütz block with a time-module name before).
    Constants (16) and comments (14) carry their content in the element's own "name" field
    (their ref_id is always 0/meaningless), not in any catalog.
    """
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    elem_name = (elem.get("name") or "").strip()
    # For type 1, ref_id is Comexio's internal IO id ($FubModules["1"][ext]["inoutput"]
    # [..]["Id"]), exposed as data["io"][..]["id"] by api.parse_config.
    named_by_type: dict[Any, tuple[dict, str]] = {
        2: (markers_by_id, f"M{ref_id} (unknown)"),
        10: (webio_by_id, f"WebIO ref={ref_id}"),
        1: (ios_by_id, f"IO ref={ref_id}"),
    }
    if named := named_by_type.get(etype):
        return _named_ref_label(named[0], ref_id, named[1])
    if etype == 4:
        return _time_module_tooltip(catalog, ref_id)
    if etype == 3:
        return _calendar_function_tooltip(catalog, ref_id, sun_times)
    if etype == 16:
        return elem_name or "?"
    if etype == 14:
        return elem_name or "(Kommentar)"
    if etype == 5:
        return _block_label(catalog, ref_id, elem_name)
    type_name = catalog.get("fub_types", {}).get(str(etype)) or f"Typ{etype}"
    return f"{type_name} {elem_name}".strip() if elem_name else f"{type_name} ref={ref_id}"


def _element_analog(
    elem: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> bool | None:
    """Analog/digital classification of a pill element for its pins (None = unknown)."""
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    if etype == 2:
        marker = markers_by_id.get(ref_id)
        return None if marker is None else marker.get("type") == "analog"
    if etype == 1:
        io = ios_by_id.get(ref_id)
        return None if io is None else not io.get("is_binary", False)
    if etype == 10:
        webio = webio_by_id.get(ref_id)
        return None if webio is None else bool(webio.get("analog"))
    if etype in (3, 4):
        return False  # calendar functions and time modules pulse digitally
    # constants (16) are value sources; anything else stays unknown
    return True if etype == 16 else None


def _element_raw_value(
    elem: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> Any:
    """Live value of a pill element (None when the source carries no value)."""
    ref = elem.get("reference") or {}
    rid = str(ref.get("ref_id", "?"))
    by_id = {2: markers_by_id, 1: ios_by_id, 10: webio_by_id}.get(ref.get("type"))
    return (by_id.get(rid) or {}).get("value") if by_id is not None else None


def _is_high(value: Any) -> bool:
    """True when a live value reads exactly 1 — the raw half of the red-wire rule.

    Digital-ness is enforced by the callers (pill gating in _build_geometries,
    const gating in _net_hot): red wires mark HIGH on DIGITAL outputs only (user
    rule) — an analog 1 (dim level, a "1" const on a Dimmer input) is a value,
    not a state.
    """
    try:
        return abs(float(value) - 1.0) < 1e-9
    except (TypeError, ValueError):
        return False


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


def _fmt_value(value: Any) -> str:
    """Live value display like Studio: compact number, German decimal comma."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _strip_prefix(text: str, prefix: str) -> str:
    """Drop a leading id prefix from a schema-formatted name (best effort)."""
    text = text.strip()
    if prefix and text.startswith(prefix):
        return text[len(prefix) :].strip() or text
    return text


def _pill_parts_marker(rid: str, markers_by_id: dict) -> tuple[str, str, str]:
    if marker := markers_by_id.get(rid):
        return f"M{rid}", _strip_prefix(marker["name"], f"M{rid}"), _fmt_value(marker.get("value"))
    return f"M{rid}", "(unknown)", ""


def _pill_parts_io(rid: str, ios_by_id: dict) -> tuple[str, str, str]:
    if io := ios_by_id.get(rid):
        ext, ident = io.get("ext_name", ""), io.get("identifier", "")
        # Two non-breaking spaces between extension and IO (SVG collapses plain
        # whitespace runs); the "#" stays as Comexio's own IO marker.
        id_text = f"{ext}  #{ident}"
        return id_text, _strip_prefix(io["name"], f"{ext} {ident}"), _fmt_value(io.get("value"))
    return "IO", f"ref={rid}", ""


def _pill_parts(
    elem: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> tuple[str, str, str]:
    """(id_text, description, value) for Studio's split pill layout |ID|description value|.

    The description strips the id prefix the naming schema usually puts first (default
    schemas start with "M{MarkerId}" / "{ExtName} {IoId}"); with a custom schema the strip
    simply doesn't match and the full name is shown next to the ID box.
    """
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    rid = str(ref.get("ref_id", "?"))
    if etype == 2:
        return _pill_parts_marker(rid, markers_by_id)
    if etype == 1:
        return _pill_parts_io(rid, ios_by_id)
    if etype == 4:
        time_module = catalog.get("time_modules", {}).get(rid) or {}
        return f"T{rid}", time_module.get("name") or "Zeitglied", ""
    if etype == 3:
        calendar_function = catalog.get("calendar_functions", {}).get(rid) or {}
        return f"C{rid}", calendar_function.get("name") or "Kalenderfunktion", ""
    if etype == 10:
        webio = webio_by_id.get(rid)
        return "", webio["name"] if webio else f"WebIO ref={rid}", ""
    return "", "", ""


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


_Rect = tuple[float, float, float, float]


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


def _row_analog(geo: dict[str, Any], side: str, row: int) -> bool:
    """Analog flag of one block port row; grown/unknown rows fall back sensibly."""
    types = geo[f"{side}_types"]
    return types[min(row, len(types) - 1)] if types else False


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


def _bounding_box(geos: dict[str, dict[str, Any]]) -> tuple[float, float, float, float]:
    if not geos:
        return 0.0, 0.0, _PILL_WIDTH + 2 * _MARGIN, _ROW_H + 2 * _MARGIN
    min_x = min(g["x"] for g in geos.values())
    min_y = min(g["y"] for g in geos.values())
    max_x = max(g["x"] + g["w"] for g in geos.values())
    max_y = max(g["y"] + g["h"] for g in geos.values())
    return min_x, min_y, max_x, max_y


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


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else f"{text[: max_len - 1]}…"
