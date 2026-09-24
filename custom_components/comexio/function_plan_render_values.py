# Version: 0.8.4
"""Element value/type classification and pill text formatting for the Function Plan renderer.

Split out of function_plan_render.py (2026-08). Pure functions only, no HA/Comexio API
calls — reads the already-parsed markers_by_id/webio_by_id/ios_by_id maps and a plan
element dict, and answers "what is this element's live value / is it analog / what text
goes in its pill" — no SVG markup and no geometry/coordinate math (see
function_plan_render_geometry.py for that).
"""

from typing import Any


def _element_analog(
    elem: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    knx_by_id: dict | None = None,
) -> bool | None:
    """Analog/digital classification of a pill element for its pins (None = unknown)."""
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    if etype in (2, 11):
        # KNX objects (11) share the marker item shape (see api._process_source_items).
        source = (markers_by_id if etype == 2 else knx_by_id or {}).get(ref_id)
        return None if source is None else source.get("type") == "analog"
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
    knx_by_id: dict | None = None,
) -> Any:
    """Live value of a pill element (None when the source carries no value)."""
    ref = elem.get("reference") or {}
    rid = str(ref.get("ref_id", "?"))
    by_id = {2: markers_by_id, 1: ios_by_id, 10: webio_by_id, 11: knx_by_id or {}}.get(ref.get("type"))
    return (by_id.get(rid) or {}).get("value") if by_id is not None else None


# reference.type -> id prefix of the element kinds addressed by a plain "<prefix><ref_id>" id.
_SEARCH_ID_PREFIX = {2: "M", 11: "K", 4: "T", 3: "C"}


def element_search_id(elem: dict[str, Any], ios_by_id: dict) -> str:
    """Object id the plan search matches by default ("M416", "K54", "T12", "C3", "IOX3#AI5").

    Empty for elements without an own object id (Web-IO commands, blocks, constants,
    comments) — those are only found by a quoted text search over the full label (see
    services/misc.py _build_element_matcher and comexio-plan-card-utils.js matchesElement).
    """
    ref = elem.get("reference") or {}
    etype = ref.get("type")
    rid = str(ref.get("ref_id", "?"))
    if prefix := _SEARCH_ID_PREFIX.get(etype):
        return f"{prefix}{rid}"
    if etype == 1 and (io := ios_by_id.get(rid)):
        return f"{io.get('ext_name', '')}#{io.get('identifier', '')}"
    return ""


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


def _pill_parts_knx(rid: str, knx_by_id: dict) -> tuple[str, str, str]:
    """Studio-style |K51|title value| pill; the name follows the KNX naming schema
    (default "K{KnxId} {KnxTitle}"), so its "K{id}" prefix is stripped like a marker's."""
    if knx := knx_by_id.get(rid):
        return f"K{rid}", _strip_prefix(knx["name"], f"K{rid}"), _fmt_value(knx.get("value"))
    return f"K{rid}", "(unknown)", ""


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
    knx_by_id: dict | None = None,
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
    if etype == 11:
        return _pill_parts_knx(rid, knx_by_id or {})
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
