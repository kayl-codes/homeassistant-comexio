# Version: 0.9.7
"""Manual, read-only "plan health check" for a Function Plan.

Ports the ad-hoc analysis used to review the "EG"-prefixed plans by hand (2026-08) into the
integration itself, so it's available from the Plan Preview card (function_plan_analyze
service) instead of a one-off script. Explicit, on-demand only — never scheduled, never
mutates the plan.

Findings fall into two kinds:
  - warnings (CONFLICT / MISSING_INPUT / DEAD_OUTPUT / UNUSED_BLOCK / SUSPICIOUS) — likely
    wiring mistakes, worth a manual look. Function blocks routinely leave *some* pins unwired
    by design, so DEAD_OUTPUT only fires for pins Studio shows by default (not "out_hide"),
    and only on blocks that have at least one other pin wired — a block with NO pin wired at
    all is reported once as UNUSED_BLOCK instead.
  - info (SELF_RESET) — the "virtueller Taster" self-reset idiom (see
    function_plan_render_selfreset), surfaced positively so a reviewer sees it was
    recognized as deliberate instead of having to re-derive that themselves.
"""

from collections.abc import Callable
from typing import Any

from .function_plan_render_geometry import _sink_list
from .function_plan_render_labels import resolve_element_label
from .function_plan_render_selfreset import detect_self_reset_cycles

__all__ = ["analyze_function_plan", "build_wiring"]

_ESSENTIAL_PINS = {"switch_s_r": {"Set", "Reset"}, "latch_s_r": {"Set", "Reset"}}
# Blocks whose outputs are alternative gesture events (Einfach-/Doppel-/Langklick) rather than
# a fixed set every user needs — wiring only the one gesture actually used (e.g. only "Lang")
# is normal, not a mistake, so DEAD_OUTPUT is suppressed entirely for these (as long as the
# block isn't fully unused — see _find_unused_blocks, still catches a truly orphaned one).
# roller_shutter: "Motor Auf"/"Motor Ab"/"Endpos. oben"/"Endpos. unten" are per-instance
# optional too (not every shutter has end-position feedback wired, or drives its motor through
# this block's own outputs) — the catalog's own out_hide only covers its fault-feedback pair
# ("Störung Auf"/"Störung Ab"), not these (user feedback, 2026-08-24, on a 24-finding false-
# positive spam across an entire "Rolladen" plan).
_OPTIONAL_OUTPUT_GROUP_BLOCKS = {"multiklickstein", "roller_shutter"}
_Wiring = tuple[dict[str, dict[int, tuple[str, int]]], dict[str, dict[int, list[str]]]]
_LabelFn = Callable[[str], str]


def build_wiring(connections: dict[str, Any]) -> _Wiring:
    """incoming[dst_id][dst_pos] = (src_id, src_pos); outgoing[src_id][src_pos] = [dst_id, ...]."""
    incoming: dict[str, dict[int, tuple[str, int]]] = {}
    outgoing: dict[str, dict[int, list[str]]] = {}
    for conn in connections.values():
        src = conn.get("input") or {}
        src_id, src_pos = str(src.get("FubElementId")), int(src.get("IOPos") or 0)
        for sink in _sink_list(conn):
            dst_id, dst_pos = str(sink.get("FubElementId")), int(sink.get("IOPos") or 0)
            incoming.setdefault(dst_id, {})[dst_pos] = (src_id, src_pos)
            outgoing.setdefault(src_id, {}).setdefault(src_pos, []).append(dst_id)
    return incoming, outgoing


def _find_conflicts(
    elements: dict[str, Any], incoming: dict[str, dict[int, tuple[str, int]]], label: _LabelFn
) -> list[dict[str, Any]]:
    """A marker/IO/WebIO reference placed as several plan elements, each independently written."""
    ref_groups: dict[tuple[int, str], list[str]] = {}
    for eid, el in elements.items():
        ref = el.get("reference") or {}
        etype = ref.get("type")
        if etype in (1, 2, 10):  # inOutput, marker, webIo
            ref_groups.setdefault((etype, str(ref.get("ref_id"))), []).append(eid)

    findings: list[dict[str, Any]] = []
    for eids in ref_groups.values():
        writers = [eid for eid in eids if incoming.get(eid)]
        if len(writers) <= 1:
            continue
        sources = sorted({label(src_id) for eid in writers for src_id, _pos in incoming[eid].values()})
        findings.append(
            {
                "severity": "warning",
                "category": "CONFLICT",
                "message": f"{label(writers[0])} wird von {len(writers)} unterschiedlichen Quellen beschrieben: "
                f"{', '.join(sources)}.",
                "element_ids": writers,
            }
        )
    return findings


def _find_missing_inputs(
    eid: str, block: dict[str, Any], inc: dict[int, tuple[str, int]], label: _LabelFn
) -> list[dict[str, Any]]:
    """Unwired essential Set/Reset pins (switch_s_r/latch_s_r) of one fubBase element."""
    essential = _ESSENTIAL_PINS.get((block.get("name") or "").lower(), set())
    if not essential:
        return []
    in_hide = set(block.get("in_hide") or [])
    return [
        {
            "severity": "warning",
            "category": "MISSING_INPUT",
            "message": f"{label(eid)}: Eingang '{pname}' ist nicht verdrahtet.",
            "element_ids": [eid],
        }
        for pos_str, pname in (block.get("in") or {}).items()
        if int(pos_str) not in inc and int(pos_str) not in in_hide and pname in essential
    ]


def _find_dead_outputs(
    eid: str, block: dict[str, Any], outg: dict[int, list[str]], label: _LabelFn
) -> list[dict[str, Any]]:
    """Unconsumed CORE outputs of one fubBase element.

    Function blocks routinely leave optional outputs unwired by design (e.g. limit-switch
    or fault feedback on a shutter block) — Comexio itself marks these "out_hide": Studio
    collapses them by default and only shows them once wired. Only outputs Studio shows by
    default (not in out_hide) are worth flagging; hidden/optional ones are silently skipped.
    """
    out_hide = set(block.get("out_hide") or [])
    return [
        {
            "severity": "warning",
            "category": "DEAD_OUTPUT",
            "message": f"{label(eid)}: Ausgang '{pname}' hat keinen Verbraucher.",
            "element_ids": [eid],
        }
        for pos_str, pname in (block.get("out") or {}).items()
        if int(pos_str) not in outg and int(pos_str) not in out_hide
    ]


def _find_unused_blocks(
    eid: str,
    inc: dict[int, tuple[str, int]],
    outg: dict[int, list[str]],
    label: _LabelFn,
) -> list[dict[str, Any]]:
    """A fubBase element with NOT A SINGLE pin wired — neither input nor output.

    Unlike a single unwired pin (normal — see _find_dead_outputs), a block with zero wiring
    anywhere is very likely a leftover/orphaned element, regardless of its type or which
    specific pins it happens to expose.
    """
    if inc or outg:
        return []
    return [
        {
            "severity": "warning",
            "category": "UNUSED_BLOCK",
            "message": f"{label(eid)}: Baustein hat keinen einzigen verdrahteten Ein- oder Ausgang.",
            "element_ids": [eid],
        }
    ]


def _find_pin_issues(
    elements: dict[str, Any],
    fub_base: dict[str, Any],
    incoming: dict[str, dict[int, tuple[str, int]]],
    outgoing: dict[str, dict[int, list[str]]],
    label: _LabelFn,
) -> list[dict[str, Any]]:
    """Per fubBase element: fully-unwired blocks, missing essential inputs, dead core outputs.

    A completely unwired block already says everything that needs saying, so it's reported
    once as UNUSED_BLOCK instead of also spamming one DEAD_OUTPUT per exposed output pin.
    """
    findings: list[dict[str, Any]] = []
    for eid, el in elements.items():
        ref = el.get("reference") or {}
        if ref.get("type") != 5:  # fubBase
            continue
        inc, outg = incoming.get(eid, {}), outgoing.get(eid, {})
        if unused := _find_unused_blocks(eid, inc, outg, label):
            findings.extend(unused)
            continue
        block = fub_base.get(str(ref.get("ref_id"))) or {}
        findings.extend(_find_missing_inputs(eid, block, inc, label))
        if (block.get("name") or "").lower() not in _OPTIONAL_OUTPUT_GROUP_BLOCKS:
            findings.extend(_find_dead_outputs(eid, block, outg, label))
    return findings


def _find_suspicious(
    elements: dict[str, Any], incoming: dict[str, dict[int, tuple[str, int]]], label: _LabelFn
) -> list[dict[str, Any]]:
    """Same source output feeding two different input pins of the SAME destination element."""
    findings: list[dict[str, Any]] = []
    for eid, inc in incoming.items():
        if eid not in elements:
            continue
        seen: dict[tuple[str, int], list[int]] = {}
        for pos, src in inc.items():
            seen.setdefault(src, []).append(pos)
        findings.extend(
            {
                "severity": "warning",
                "category": "SUSPICIOUS",
                "message": f"{label(eid)}: derselbe Ausgang von {label(src_id)} (Pin {src_pos}) speist "
                f"{len(pins)} verschiedene Eingänge desselben Elements.",
                "element_ids": [eid, src_id],
            }
            for (src_id, src_pos), pins in seen.items()
            if len(pins) > 1
        )
    return findings


def _find_self_reset(
    elements: dict[str, Any], connections: dict[str, Any], catalog: dict[str, Any], label: _LabelFn
) -> list[dict[str, Any]]:
    """Recognized "virtueller Taster" self-reset cycles — informational, not a warning."""
    return [
        {
            "severity": "info",
            "category": "SELF_RESET",
            "message": "Erkanntes Selbstreset-Muster (virtueller Taster), gutartig: "
            + " → ".join(label(eid) for eid in group)
            + " → (zurück).",
            "element_ids": list(group),
        }
        for group in detect_self_reset_cycles(elements, connections, catalog)
    ]


def analyze_function_plan(
    elements: dict[str, Any],
    connections: dict[str, Any],
    catalog: dict[str, Any],
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
) -> list[dict[str, Any]]:
    """Findings for one plan: CONFLICT / MISSING_INPUT / DEAD_OUTPUT / UNUSED_BLOCK / SUSPICIOUS
    (warnings) plus SELF_RESET (info). Pure/read-only — takes the same elements/connections/catalog shape
    as function_plan_render.render_plan_svg, so it works identically for a live plan or a
    stored backup snapshot. Each finding also carries "element_ids" (the plan FubElementIds it's
    about) alongside the human-readable "message" — the card uses these to jump to and highlight
    the offending element(s) instead of the reviewer having to search for them by hand.
    """
    incoming, outgoing = build_wiring(connections)
    fub_base = catalog.get("fub_base", {})

    def label(eid: str) -> str:
        # Some labels (time modules, calendar functions) are multi-line tooltips — only the
        # first line belongs in a one-line finding message.
        text = resolve_element_label(elements.get(eid, {}), catalog, markers_by_id, webio_by_id, ios_by_id)
        return text.splitlines()[0]

    return [
        *_find_conflicts(elements, incoming, label),
        *_find_pin_issues(elements, fub_base, incoming, outgoing, label),
        *_find_suspicious(elements, incoming, label),
        *_find_self_reset(elements, connections, catalog, label),
    ]
