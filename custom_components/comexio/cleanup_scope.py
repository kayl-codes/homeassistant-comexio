"""Scope selection for the uninstall cleanup (full / marker / io / knx).

Pure logic, no HA imports — decides which HA-managed function plans and Web-IO classes a
cleanup scope covers, so the repair flow can tear down only one part of what the
integration created on the Comexio server.
"""

from __future__ import annotations

from typing import Any

from .const import (
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    MARKER_KNX_BRIDGE_SUFFIX_RE,
    SOURCE_CATEGORIES,
    WEBIO_CLASSES,
    WebioClass,
)

CLEANUP_SCOPE_FULL = "full"
CLEANUP_SCOPE_MARKER = "marker"
CLEANUP_SCOPE_IO = "io"
CLEANUP_SCOPE_KNX = "knx"
CLEANUP_SCOPES = (CLEANUP_SCOPE_FULL, CLEANUP_SCOPE_MARKER, CLEANUP_SCOPE_IO, CLEANUP_SCOPE_KNX)

_SCOPE_CLASS: dict[str, WebioClass] = {
    CLEANUP_SCOPE_MARKER: WebioClass.MARKER,
    CLEANUP_SCOPE_IO: WebioClass.IO,
    CLEANUP_SCOPE_KNX: WebioClass.KNX,
}

# What a partial cleanup does with the shared trigger plan (see trigger_plan_action).
TRIGGER_PLAN_DELETE = "delete"
TRIGGER_PLAN_REMOVE_PAIRS = "remove_pairs"
TRIGGER_PLAN_KEEP = "keep"

# skipped-dict key for bridge markers the user wired into an own plan: reported, but no
# retry can change that, so it does not make a run "incomplete".
SKIPPED_KNX_BRIDGE_MARKERS = "knx_bridge_markers"


def webio_classes_in_scope(scope: str) -> tuple[WebioClass, ...]:
    """Web-IO device classes a scope deletes."""
    if scope == CLEANUP_SCOPE_FULL:
        return WEBIO_CLASSES
    return (_SCOPE_CLASS[scope],)


def scope_includes_knx(scope: str) -> bool:
    """Whether a scope also removes the KNX API-Loopback Web-IO and resets bridge markers."""
    return scope in (CLEANUP_SCOPE_FULL, CLEANUP_SCOPE_KNX)


def plan_in_scope(plan_name: str, scope: str) -> bool:
    """Whether a managed plan (a CONF_FUNCTION_PLAN_PLAN_MAP key) belongs to a scope.

    The shared trigger plan belongs to "full" only: it holds the trigger pairs of every
    trigger-capable category, so a partial scope decides per content (trigger_plan_action)
    instead of deleting it wholesale. Cluster plans are named "{prefix} - {category label} [...]" (see
    coordinator._cluster_plan_name and the IO cluster naming); the trigger plan has a fixed
    name. Matched on the " - {label} [" part only, NOT the configured prefix: the plan map
    only ever holds HA-managed plans, and one created under an earlier prefix must still
    fall into its scope instead of silently surviving a partial cleanup.
    """
    if scope == CLEANUP_SCOPE_FULL:
        return True
    if plan_name == FUNCTION_PLAN_TRIGGER_PLAN_NAME:
        return False
    label = SOURCE_CATEGORIES[_SCOPE_CLASS[scope]].label
    return f" - {label} [" in plan_name


def scope_trigger_ref_type(scope: str) -> int | None:
    """Plan-element ref_type of the trigger pairs a partial scope owns (marker=2, knx=11);
    None for "full" (deletes the whole plan) and for scopes without trigger pairs (io)."""
    if scope == CLEANUP_SCOPE_FULL:
        return None
    category = SOURCE_CATEGORIES[_SCOPE_CLASS[scope]]
    return int(category.fub_module_type) if category.supports_trigger_pairs else None


_MARKER_REF_TYPE = int(SOURCE_CATEGORIES[WebioClass.MARKER].fub_module_type)
_KNX_REF_TYPE = int(SOURCE_CATEGORIES[WebioClass.KNX].fub_module_type)


def trigger_sources_by_category(
    refs: list[tuple[int, int]], marker_titles: dict[int, str], trigger_types: set[int]
) -> dict[int, list[tuple[int, int]]]:
    """Group the trigger plan's source elements by the category that owns them.

    refs: (ref_type, ref_id) of every plan element. Keyed by the owning category's
    ref_type (marker=2, knx=11); the values keep each element's own (ref_type, ref_id)
    for the removal. A marker element titled as a KNX bridge ("... [K<n>]") belongs to
    KNX, not to the markers: KNX trigger pairs can sit in the plan via their bridge
    marker, and a partial "markers" cleanup must leave them alone just as a "knx"
    cleanup must remove them (else the bridge marker stays placed and cannot be reset).
    """
    grouped: dict[int, list[tuple[int, int]]] = {}
    for ref_type, ref_id in refs:
        if ref_type not in trigger_types:
            continue
        owner = ref_type
        if ref_type == _MARKER_REF_TYPE and MARKER_KNX_BRIDGE_SUFFIX_RE.search(marker_titles.get(ref_id, "")):
            owner = _KNX_REF_TYPE
        grouped.setdefault(owner, []).append((ref_type, ref_id))
    return grouped


def trigger_plan_action(own_ref_type: int, source_ref_types: set[int]) -> str:
    """What a partial cleanup does with the shared trigger plan.

    source_ref_types: ref_types of the trigger sources currently placed in the plan.
    Other categories' pairs present -> only this scope's pairs go (or nothing, if it has
    none); otherwise the plan holds nothing worth keeping and is deleted as a whole.
    """
    if not source_ref_types - {own_ref_type}:
        return TRIGGER_PLAN_DELETE
    if own_ref_type in source_ref_types:
        return TRIGGER_PLAN_REMOVE_PAIRS
    return TRIGGER_PLAN_KEEP


def plans_in_scope(plan_map: dict[str, Any], scope: str) -> dict[str, Any]:
    """The subset of plan_map whose plans belong to a scope."""
    return {name: fub_id for name, fub_id in plan_map.items() if plan_in_scope(name, scope)}


def scope_counts(plan_map: dict[str, Any], webio_devices: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per scope: how many managed plans and Web-IO devices it would delete.

    webio_devices is the audit's {class: {"device_id", "base_id", ...}} map. Feeds the
    counters shown next to each option in the uninstall-cleanup repair dialog.
    """
    counts: dict[str, dict[str, int]] = {}
    for scope in CLEANUP_SCOPES:
        classes = webio_classes_in_scope(scope)
        counts[scope] = {
            "plans": len(plans_in_scope(plan_map, scope)),
            "devices": sum(1 for cls in classes if (webio_devices.get(cls) or {}).get("device_id")),
            "classes": sum(1 for cls in classes if (webio_devices.get(cls) or {}).get("base_id")),
        }
    return counts


def has_knx_artifacts(
    plan_map: dict[str, Any], webio_devices: dict[str, Any], has_bridge_markers: bool = False
) -> bool:
    """Whether a KNX cluster plan, the KNX Web-IO device or a KNX bridge marker exists
    (trigger plan not counted).

    Used to decide whether an update from a KNX pre-release needs the one-time KNX cleanup:
    the trigger plan also exists on marker-only installs, so it is no KNX evidence on its own.
    Titled bridge markers ("... [K<n>]") alone are leftovers too — resetting them is part of
    the KNX cleanup.
    """
    return (
        has_bridge_markers
        or bool(plans_in_scope(plan_map, CLEANUP_SCOPE_KNX))
        or bool((webio_devices.get(WebioClass.KNX) or {}).get("device_id"))
    )
