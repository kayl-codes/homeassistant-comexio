# Version: 0.8.4
"""Human-readable labels/tooltips for Function Plan elements.

Split out of function_plan_render.py (2026-08). `resolve_element_label` is the public
entry point (imported by services.py and used by function_plan_render.render_plan_svg for
title/hover text) — pure lookup/formatting, no SVG markup, no geometry.
"""

from typing import Any

from .function_plan_render_values import _fmt_value


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
