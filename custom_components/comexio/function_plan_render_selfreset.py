# Version: 0.8.4
"""Detects the "virtueller Taster" self-reset idiom in a Function Plan.

Comexio has no native way to guarantee a HA-simulated button press resets itself: a
write-then-LOW from HA could fail to land, so plans instead wire the marker to reset
itself through an on_pulse timer — confirmed intentional (not a bug) against the
canonical example plan "Zeit Pläne u. Funktionen" (2026-08-23). Structurally this is a
short cycle back to the same marker:
  - 2-hop:  Marker --> Timer(on_pulse) --> same Marker
  - 3-hop:  Marker --> Timer(on_pulse) --> Oder --> same Marker (the Oder's other input
            is typically a shared "kill" marker resetting several such pairs at once)
Detecting it lets the plan preview mark these elements as a recognized benign pattern
(native tooltip) instead of leaving a reader to manually re-derive that a self-loop
through a debouncing timer is deliberate, not a wiring mistake.
"""

from typing import Any

from .function_plan_render_geometry import _sink_list

__all__ = ["detect_self_reset_cycles", "detect_self_reset_elements"]


def _element_type(elements: dict[str, Any], elem_id: str) -> int | None:
    return (elements.get(elem_id, {}).get("reference") or {}).get("type")


def _element_ref_id(elements: dict[str, Any], elem_id: str) -> str:
    return str((elements.get(elem_id, {}).get("reference") or {}).get("ref_id", ""))


def _build_outgoing(connections: dict[str, Any]) -> dict[str, set[str]]:
    outgoing: dict[str, set[str]] = {}
    for conn in connections.values():
        src = conn.get("input") or {}
        src_id = str(src.get("FubElementId"))
        for sink in _sink_list(conn):
            outgoing.setdefault(src_id, set()).add(str(sink.get("FubElementId")))
    return outgoing


def _is_on_pulse_timer(elements: dict[str, Any], time_modules: dict[str, Any], elem_id: str) -> bool:
    if _element_type(elements, elem_id) != 4:  # timeModule
        return False
    return (time_modules.get(_element_ref_id(elements, elem_id)) or {}).get("kind") == "on_pulse"


def _is_or_gate(elements: dict[str, Any], fub_base: dict[str, Any], elem_id: str) -> bool:
    if _element_type(elements, elem_id) != 5:  # fubBase
        return False
    return (fub_base.get(_element_ref_id(elements, elem_id)) or {}).get("name") == "or"


def _cycles_via_timer(
    elements: dict[str, Any], fub_base: dict[str, Any], outgoing: dict[str, set[str]], marker_id: str, timer_id: str
) -> list[tuple[str, ...]]:
    """Cycle(s) closing back to `marker_id` through `timer_id` — direct, or via one Oder gate."""
    if marker_id in outgoing.get(timer_id, ()):
        return [(marker_id, timer_id)]
    return [
        (marker_id, timer_id, o_id)
        for o_id in outgoing.get(timer_id, ())
        if _is_or_gate(elements, fub_base, o_id) and marker_id in outgoing.get(o_id, ())
    ]


def detect_self_reset_cycles(
    elements: dict[str, Any], connections: dict[str, Any], catalog: dict[str, Any]
) -> list[tuple[str, ...]]:
    """Return each detected self-reset cycle as (marker_id, timer_id[, oder_id])."""
    time_modules = catalog.get("time_modules", {})
    fub_base = catalog.get("fub_base", {})
    outgoing = _build_outgoing(connections)

    cycles: list[tuple[str, ...]] = []
    for eid in elements:
        if _element_type(elements, eid) != 2:  # marker
            continue
        for t_id in outgoing.get(eid, ()):
            if _is_on_pulse_timer(elements, time_modules, t_id):
                cycles.extend(_cycles_via_timer(elements, fub_base, outgoing, eid, t_id))
    return cycles


def detect_self_reset_elements(
    elements: dict[str, Any], connections: dict[str, Any], catalog: dict[str, Any]
) -> set[str]:
    """Return element ids (marker + timer + optional Oder) that form a self-reset cycle."""
    found: set[str] = set()
    for group in detect_self_reset_cycles(elements, connections, catalog):
        found.update(group)
    return found
