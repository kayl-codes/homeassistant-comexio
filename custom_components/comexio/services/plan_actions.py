# Version: 0.7.6
"""Visualize / Sort / Stop / Activate — the live-plan-state service handlers.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
these four handlers act on a single live (or, for visualize, snapshot) function plan
without touching the stored backup catalog itself (that's backup.py's job).
"""

import logging
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall

from ..coordinator import ComexioCoordinator
from ..function_plan_backup import snapshot_label_maps
from ._context import (
    _async_get_service_context,
    _get_canvas_grid_dims,
    _parse_snapshot_field,
    _plan_activation_note,
    _resolve_backup_identity,
    _resolve_logikplan_context,
)
from ._grid import (
    _assign_grid_positions,
    _assign_io_grid_positions,
    _build_sorted_pairs,
    _io_header_slots,
    _is_managed_cluster_plan,
    _park_leftover_positions,
    _pinned_template_positions,
    _resync_io_group_headers,
)

_LOGGER = logging.getLogger(__name__)

_TITLE_SORT_ERR = "Logikplan Sort — Fehler"


def _element_label(elem_id: str | int, elements: dict, markers_by_id: dict, webio_by_id: dict) -> str:
    """Human-readable label for a Logikplan element (marker name, WebIO command name, or type/ref fallback)."""
    ref = elements.get(str(elem_id), {}).get("reference", {})
    etype = ref.get("type")
    ref_id = str(ref.get("ref_id", "?"))
    if etype == 2:
        marker = markers_by_id.get(ref_id)
        return f"M{ref_id} {marker['name']}" if marker else f"M{ref_id} (unbekannt)"
    if etype == 10:
        webio = webio_by_id.get(ref_id)
        return webio["name"] if webio else f"WebIO ref={ref_id}"
    return f"Typ{etype} ref={ref_id}"


def _build_visualize_lines(
    elements: dict, connections: dict, markers_by_id: dict, webio_by_id: dict
) -> tuple[list[str], list[str]]:
    """Build the connection lines and orphan-element lines for the Logikplan visualize text diagram."""
    connected_elem_ids: set[str] = set()
    conn_lines: list[str] = []
    for conn in sorted(connections.values(), key=lambda c: c.get("input", {}).get("FubElementId", 0)):
        inp = conn.get("input", {})
        inp_id = str(inp.get("FubElementId", "?"))
        inv_in = " ¬" if inp.get("Inverted") else ""
        conn_type = conn.get("type", "?")
        connected_elem_ids.add(inp_id)
        out_parts: list[str] = []
        for out in conn.get("output", []):
            out_id = str(out.get("FubElementId", "?"))
            inv_out = " ¬" if out.get("Inverted") else ""
            out_parts.append(f"{_element_label(out_id, elements, markers_by_id, webio_by_id)}{inv_out}")
            connected_elem_ids.add(out_id)
        conn_lines.append(
            f"  {_element_label(inp_id, elements, markers_by_id, webio_by_id)}{inv_in} "
            f"→[{conn_type}]→ {', '.join(out_parts)}"
        )

    orphan_lines: list[str] = []
    for elem_id, elem in sorted(
        elements.items(), key=lambda kv: (kv[1].get("position_x", 0), kv[1].get("position_y", 0))
    ):
        if elem_id not in connected_elem_ids:
            x, y = elem.get("position_x", 0), elem.get("position_y", 0)
            label = _element_label(elem_id, elements, markers_by_id, webio_by_id)
            orphan_lines.append(f"  {label} (@ {x:.0f},{y:.0f})")

    return conn_lines, orphan_lines


async def _resolve_visualize_snapshot_source(
    hass: HomeAssistant, call: ServiceCall, snapshot_raw: str, error_title: str
):
    """Resolve handle_logikplan_visualize's data source from a stored backup snapshot
    (extracted to stay under the complexity budget). Mirrors _handle_function_plan_restore's
    snapshot resolution — entirely offline, no live Comexio call needed. Returns
    (coordinator, api, fub_id, plan_name, elements, connections, source, label_metadata),
    or None on any failure (a notification has already been posted).
    """
    ctx = await _async_get_service_context(hass, call, error_title, resolve_plan=False, do_login=False)
    if ctx is None:
        return None
    coordinator, api, _unused_fub_id = ctx
    parsed = _parse_snapshot_field(snapshot_raw)
    if parsed is None:
        persistent_notification.async_create(hass, f"Invalid 'snapshot' value: '{snapshot_raw}'.", title=error_title)
        return None
    fub_id, kind, slot, plan_name_hint = parsed
    plan_name, identity_err = await _resolve_backup_identity(coordinator, fub_id, plan_name_hint)
    if identity_err:
        persistent_notification.async_create(hass, identity_err, title=error_title)
        return None
    snapshot = await coordinator.function_plan_backup.async_get_snapshot(kind, fub_id, plan_name, slot)
    if snapshot is None:
        persistent_notification.async_create(
            hass,
            f"No snapshot found for plan '{plan_name}' (fub {fub_id}, kind={kind}, slot={slot}).",
            title=error_title,
        )
        return None
    source = f"snapshot:{kind}:{slot}"
    return (
        coordinator,
        api,
        fub_id,
        plan_name,
        snapshot.get("elements", {}),
        snapshot.get("connections", {}),
        source,
        snapshot.get("labels"),
    )


async def _resolve_visualize_live_source(hass: HomeAssistant, call: ServiceCall, error_title: str):
    """Resolve handle_logikplan_visualize's data source from the live plan — the 'snapshot'
    field was not given (extracted to stay under the complexity budget). Returns
    (coordinator, api, fub_id, plan_name, elements, connections, source, label_metadata),
    or None on any failure (a notification has already been posted).
    """
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return None
    coordinator, api, fub_id = ctx
    plan_data = await api.logikplan_load_elements(fub_id)
    if not plan_data:
        persistent_notification.async_create(hass, f"Plan {fub_id} konnte nicht geladen werden.", title=error_title)
        return None
    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    return (
        coordinator,
        api,
        fub_id,
        plan_name,
        plan_data.get("elements", {}),
        plan_data.get("connections", {}),
        "live",
        None,
    )


async def handle_logikplan_visualize(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Service to visualize a Logikplan plan (live or a stored backup snapshot) as a text
    diagram, or — format='svg' — render it to the Function Plan preview SVG and return its
    /local/ URL (used by coordinator.async_generate_plan_preview / the comexio-plan-card
    frontend, see [[project-logikplan-preview]]).

    Snapshot resolution mirrors _handle_function_plan_restore: a 'snapshot' field (composite
    'fub_id:kind:slot:plan_name' dropdown value) targets one exact stored backup, entirely
    offline — no live Comexio call needed, since snapshots already carry elements/connections
    in the same shape as a live plan (function_plan_backup.py). Without 'snapshot', behaviour
    for the text diagram is unchanged: the live plan is loaded via the usual fub_id resolution.
    """
    error_title = "Logikplan Visualize — Fehler"
    fmt = str(call.data.get("format", "text")).strip().lower()
    snapshot_raw = call.data.get("snapshot")

    source_result = (
        await _resolve_visualize_snapshot_source(hass, call, str(snapshot_raw), error_title)
        if snapshot_raw
        else await _resolve_visualize_live_source(hass, call, error_title)
    )
    if source_result is None:
        return None
    coordinator, api, fub_id, plan_name, elements, connections, source, label_metadata = source_result

    if fmt == "svg":
        preview_url = await coordinator.async_generate_plan_preview(
            fub_id, plan_name, elements, connections, source, label_metadata
        )
        persistent_notification.async_create(
            hass,
            f"Preview for '{plan_name}' updated — see the Plan Preview sensor.",
            title=f"Logikplan Visualize — {plan_name}",
        )
        return {"plan_name": plan_name, "url": preview_url}

    markers_by_id, webio_by_id, _ios_by_id = coordinator.function_plan_label_maps()
    if label_metadata:
        markers_by_id, webio_by_id, _ios_by_id = snapshot_label_maps(
            label_metadata, markers_by_id, webio_by_id, _ios_by_id
        )

    conn_lines, orphan_lines = _build_visualize_lines(elements, connections, markers_by_id, webio_by_id)

    lines = [f"**Plan {fub_id}** ({source})"]
    if source == "live":
        paper_fmt = api.get_fub_paper_format(fub_id)
        x_max, y_max = api.get_fub_canvas_bounds(fub_id)
        lines[0] += f" — {paper_fmt}, Canvas {x_max:.0f}×{y_max:.0f}"
    lines += [
        f"{len(elements)} Elemente, {len(connections)} Verbindungen",
        "",
        f"**Verbindungen ({len(connections)}):**",
    ]
    lines += conn_lines or ["  (keine)"]
    if orphan_lines:
        lines += ["", f"**Nicht verbundene Elemente ({len(orphan_lines)}):**"]
        lines += orphan_lines

    persistent_notification.async_create(
        hass, "\n".join(lines), title=f"Logikplan Plan {fub_id} — {len(connections)} Verbindungen"
    )
    return None


async def handle_logikplan_sort(hass: HomeAssistant, call: ServiceCall) -> None:
    """Sort all Logikplan elements by marker ID, snapping every element to exact grid."""
    ctx = await _resolve_logikplan_context(hass, call, _TITLE_SORT_ERR)
    if ctx is None:
        return
    coordinator, api, fub_id = ctx
    if not _is_managed_cluster_plan(coordinator, fub_id):
        plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
        persistent_notification.async_create(
            hass,
            f"Plan '{plan_name}' (ID {fub_id}) ist kein HA-verwalteter Cluster-Plan. "
            "Sort schreibt jede Elementposition neu — auf einem handgebauten Comexio-Plan "
            "würde das dessen Layout zerstören.",
            title=_TITLE_SORT_ERR,
        )
        return
    await async_sort_function_plan(
        hass, coordinator, api, fub_id, canvas_format=str(call.data.get("canvas_format", ""))
    )


async def async_sort_function_plan(
    hass: HomeAssistant,
    coordinator: ComexioCoordinator,
    api,
    fub_id: int,
    canvas_format: str | None = None,
    notify: bool = True,
    was_active: bool | None = None,
) -> dict | None:
    """Sort all plan elements by marker ID, snapping every element to the exact grid.

    Managed IO cluster plans ('{prefix} - IO [...]') are not marker-sorted — their
    deterministic extension-column grid (io_column_rows) is restored instead.

    Returns a result dict (success, pairs, orphans, act_note, activated, duration) or
    None when the plan could not be loaded or is empty. With notify=False no persistent
    notifications are created — used by the sync button, which reports the result in its
    own summary notification.

    was_active overrides the fub_data lookup: callers that already stopped the plan
    themselves must pass the pre-stop state, because any config reload in between (e.g.
    inside function_plan_add_marker_pairs) refreshes fub_data to the stopped state and
    the plan would never be reactivated.
    """
    t_start = time.monotonic()
    if was_active is None:
        was_active = bool(api.fub_data.get(str(fub_id), {}).get("Active", True))

    canvas_label, x_max, rows_per_col, max_cols = _sort_canvas_bounds(api, fub_id, canvas_format)

    plan_data = await api.logikplan_load_elements(fub_id)
    if not plan_data:
        if notify:
            persistent_notification.async_create(
                hass, f"Plan {fub_id} konnte nicht geladen werden.", title=_TITLE_SORT_ERR
            )
        return None

    new_positions, n_pairs, n_single, sort_line, header_slots, io_members = _sort_compute_positions(
        coordinator, plan_data, fub_id, x_max, rows_per_col, max_cols
    )
    if not new_positions:
        if notify:
            persistent_notification.async_create(hass, "Keine Elemente im Plan.", title=f"Logikplan Sort Plan {fub_id}")
        return None

    _LOGGER.info(
        "Logikplan Sort: Plan %s — %d Paare, %d Einzelelemente, %d Positionen (io_plan=%s, aktiv=%s)",
        fub_id,
        n_pairs,
        n_single,
        len(new_positions),
        bool(io_members),
        was_active,
    )
    success, activated = await _sort_apply_and_activate(
        coordinator, api, fub_id, plan_data, new_positions, header_slots, was_active
    )
    duration = time.monotonic() - t_start
    act_note = _plan_activation_note(was_active, activated, has_changes=True, fub_id=fub_id)
    if notify:
        msg = (
            f"Sortierung {'erfolgreich' if success else 'fehlgeschlagen'}: {sort_line}\n"
            f"Canvas {canvas_label}: {max_cols} Spalten × {rows_per_col} Zeilen.\n"
            f"{act_note}\n"
            f"Dauer: {duration:.1f}s"
        )
        persistent_notification.async_create(
            hass, msg, title=f"Logikplan Sort Plan {fub_id} — {'OK' if success else 'Fehler'}"
        )
    return {
        "success": success,
        "pairs": n_pairs,
        "orphans": n_single,
        "act_note": act_note,
        "activated": activated,
        "duration": duration,
    }


def _sort_canvas_bounds(api, fub_id: int, canvas_format: str | None) -> tuple[str, float, int, int]:
    """Canvas label + content-x-bound + grid dimensions for a sort run (see async_sort_function_plan)."""
    canvas_label, x_max, _y_max, rows_per_col, max_cols = _get_canvas_grid_dims(
        api, fub_id, (canvas_format or "").strip().upper()
    )
    return canvas_label, x_max, rows_per_col, max_cols


def _sort_compute_positions(
    coordinator: ComexioCoordinator, plan_data: dict, fub_id: int, x_max: float, rows_per_col: int, max_cols: int
) -> tuple[list[tuple[int, float, float]], int, int, str, list[tuple[float, float, str]], list[str] | None]:
    """New element positions for a sort run — IO cluster grid or marker-ID grid (see async_sort_function_plan)."""
    # Template elements (the managed comment) are pinned to their canonical spot instead
    # of being sorted into the grid as orphans.
    pinned = _pinned_template_positions(plan_data, x_max)
    pinned_ids = {eid for eid, _x, _y in pinned}
    io_members = coordinator.managed_io_plan_members(fub_id)
    header_slots: list[tuple[float, float, str]] = []
    if io_members and (coordinator.data or {}).get("io"):
        # Managed IO cluster plan: the marker sort would scramble the extension grid —
        # restore the deterministic column layout the wire path produced instead.
        placed, n_pairs, leftovers = _assign_io_grid_positions(coordinator, plan_data, io_members, rows_per_col)
        leftovers = [eid for eid in leftovers if eid not in pinned_ids]
        new_positions = placed + _park_leftover_positions(placed, leftovers, rows_per_col) + pinned
        n_single = len(leftovers)
        header_slots = _io_header_slots(coordinator, io_members, rows_per_col)
        sort_line = (
            f"{n_pairs} IO-Paare im Erweiterungs-Raster [{','.join(io_members)}] wiederhergestellt"
            f" + {n_single} weitere Elemente rechts daneben geparkt + {len(header_slots)} Blockköpfe."
        )
    else:
        pairs, orphans = _build_sorted_pairs(plan_data.get("elements", {}), plan_data.get("connections", {}))
        orphans = [eid for eid in orphans if eid not in pinned_ids]
        new_positions = _assign_grid_positions(pairs, orphans, rows_per_col, max_cols) + pinned
        n_pairs, n_single = len(pairs), len(orphans)
        sort_line = f"{n_pairs} Paare nach Merker-ID geordnet + {n_single} Einzelelemente."
    return new_positions, n_pairs, n_single, sort_line, header_slots, io_members


async def _sort_apply_and_activate(
    coordinator: ComexioCoordinator,
    api,
    fub_id: int,
    plan_data: dict,
    new_positions: list[tuple[int, float, float]],
    header_slots: list[tuple[float, float, str]],
    was_active: bool,
) -> tuple[bool, bool]:
    """Persist a sort run's positions and restore activation state; returns (success, activated)."""
    # Pre-mutation snapshot — reuse the already-loaded plan_data (no second fetch)
    await coordinator.async_function_plan_change_backup(fub_id, "sort", plan_data=plan_data)
    if was_active:
        await api.logikplan_stop_fup(fub_id)
    if header_slots:
        await _resync_io_group_headers(api, fub_id, plan_data, header_slots)
    success = await api.logikplan_save_elements_pos(new_positions)
    activated = await api.logikplan_run_fup(fub_id) if (success and was_active) else False
    return success, activated


async def handle_logikplan_stop(hass: HomeAssistant, call: ServiceCall) -> None:
    """Stop/pause a Logikplan plan."""
    error_title = "Logikplan Stop — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    _coordinator, api, fub_id = ctx

    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    _LOGGER.info("Logikplan Stop: fub_id=%s name='%s'", fub_id, plan_name)
    t_start = time.monotonic()
    success = await api.logikplan_stop_fup(fub_id)
    duration = time.monotonic() - t_start
    msg = (
        f"Plan '{plan_name}' (ID {fub_id}) gestoppt.\nDauer: {duration:.1f}s"
        if success
        else f"Stop fehlgeschlagen (Plan '{plan_name}', ID {fub_id}).\nDauer: {duration:.1f}s"
    )
    persistent_notification.async_create(hass, msg, title=f"Logikplan Stop — {'OK' if success else 'Fehler'}")


async def handle_logikplan_activate(hass: HomeAssistant, call: ServiceCall) -> None:
    """Save and activate a Logikplan plan (run_fup)."""
    error_title = "Logikplan Aktivieren — Fehler"
    ctx = await _resolve_logikplan_context(hass, call, error_title)
    if ctx is None:
        return
    _coordinator, api, fub_id = ctx

    plan_name = api.fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
    _LOGGER.info("Logikplan Aktivieren: fub_id=%s name='%s'", fub_id, plan_name)
    t_start = time.monotonic()
    success = await api.logikplan_run_fup(fub_id)
    duration = time.monotonic() - t_start
    msg = (
        f"Plan '{plan_name}' (ID {fub_id}) gespeichert und aktiviert.\nDauer: {duration:.1f}s"
        if success
        else f"Aktivierung fehlgeschlagen (Plan '{plan_name}', ID {fub_id}).\nDauer: {duration:.1f}s"
    )
    persistent_notification.async_create(hass, msg, title=f"Logikplan Aktivieren — {'OK' if success else 'Fehler'}")
