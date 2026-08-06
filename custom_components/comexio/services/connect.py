# Version: 0.7.6
"""logikplan_connect_poc — wiring helper + handler.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
connects markers to their matching WebIO commands on the plan canvas, reusing existing
canvas elements where possible and claiming free grid slots (via ._grid) for new ones.
"""

import logging
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall

from ..const import (
    CONF_IGNORED_MARKERS,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH as _LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_X_MARKER as _LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_X_WEBIO as _LAYOUT_X_WEBIO,
    FUNCTION_PLAN_LAYOUT_Y_START as _LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP as _LAYOUT_Y_STEP,
    expand_ignored_marker_ids,
)
from ._context import _ensure_comexio_login, _get_canvas_grid_dims, _plan_activation_note, _resolve_logikplan_plan
from ._grid import _find_first_free_grid_position, _get_occupied_grid_slots

_LOGGER = logging.getLogger(__name__)


def _parse_ignored_marker_ids(conf: dict) -> set[int]:
    """Parse the ignored_markers option string into a set of marker IDs (supports ranges, e.g. '8-12')."""
    return expand_ignored_marker_ids(conf.get(CONF_IGNORED_MARKERS, ""))


def _resolve_requested_marker_ids(
    raw_input: str, all_markers: bool, markers_by_id: dict, ignored_ids: set[int]
) -> tuple[list[int], list[str]]:
    """Resolve the requested marker IDs from the service call's `marker_id`/`all_markers` input.

    Returns (marker_ids, invalid_tokens); invalid_tokens is only ever non-empty for the explicit-list path.
    """
    if all_markers or raw_input == "*":
        return [mid for mid in markers_by_id if mid not in ignored_ids], []

    raw_ids = [tok.strip().lstrip("Mm") for tok in raw_input.split(",") if tok.strip()]
    marker_ids: list[int] = []
    invalid_tokens: list[str] = []
    for raw_id in raw_ids:
        try:
            mid = int(raw_id)
        except ValueError:
            invalid_tokens.append(raw_id)
            continue
        if mid not in ignored_ids:
            marker_ids.append(mid)
    return marker_ids, invalid_tokens


async def _load_connect_poc_topology(
    api, fub_id: int, rows_per_col: int, max_cols: int
) -> tuple[dict[tuple[int, int], int], set[tuple[int, int]], set[tuple[int, int]]]:
    """Load current plan elements/connections and derive existing-element refs, wired pairs, occupied grid slots."""
    plan_data = await api.logikplan_load_elements(fub_id)
    existing_by_ref: dict[tuple[int, int], int] = {}
    connected_pairs: set[tuple[int, int]] = set()
    occupied_slots: set[tuple[int, int]] = set()

    if not plan_data:
        _LOGGER.warning("Logikplan POC: loadelements fehlgeschlagen, fahre ohne Plan-Zustand fort")
        return existing_by_ref, connected_pairs, occupied_slots

    for elem_id_str, elem in plan_data.get("elements", {}).items():
        ref = elem.get("reference", {})
        ref_type, ref_id = ref.get("type"), ref.get("ref_id")
        if ref_type is not None and ref_id is not None:
            existing_by_ref[(int(ref_type), int(ref_id))] = int(elem_id_str)
    for conn in plan_data.get("connections", {}).values():
        inp = conn.get("input", {})
        for out in conn.get("output", []):
            inp_id, out_id = inp.get("FubElementId"), out.get("FubElementId")
            if inp_id is not None and out_id is not None:
                connected_pairs.add((int(inp_id), int(out_id)))
    occupied_slots = _get_occupied_grid_slots(plan_data.get("elements", {}), rows_per_col, max_cols)
    _LOGGER.info("Logikplan POC: Plan fub=%s — %d belegte Grid-Slots gefunden", fub_id, len(occupied_slots))
    return existing_by_ref, connected_pairs, occupied_slots


async def _get_or_create_marker_element(
    api, fub_id: int, marker_id: int, existing_marker_elem: int | None, x: float, y: float
) -> tuple[int | None, str | None]:
    """Reuse an existing canvas marker element, or create a new one. Returns (elem_id, error)."""
    if existing_marker_elem:
        _LOGGER.info(
            "Logikplan POC: M%s — Merker-Element bereits vorhanden: elem_id=%s", marker_id, existing_marker_elem
        )
        return existing_marker_elem, None

    _LOGGER.info("Logikplan POC: M%s → add_element (Merker, x=%.1f, y=%.1f)", marker_id, x, y)
    elem_marker = await api.logikplan_add_element(fub_id=fub_id, ref_id=int(marker_id), element_type=2, x=x, y=y)
    if elem_marker is None:
        return None, f"M{marker_id}: add_element (Merker) fehlgeschlagen"
    _LOGGER.info("Logikplan POC: M%s — Merker-Element angelegt: elem_id=%s", marker_id, elem_marker)
    return elem_marker, None


async def _get_or_create_webio_element(
    api,
    fub_id: int,
    marker_id: int,
    elem_marker: int,
    web_ref_id,
    conn_type: str,
    existing_webio_elem: int | None,
    x: float,
    y: float,
) -> tuple[int | None, str | None]:
    """Reuse an existing canvas WebIO element (wiring it up if needed), or create + connect a new one."""
    if existing_webio_elem:
        _LOGGER.info("Logikplan POC: M%s — WebIO-Element bereits vorhanden: elem_id=%s", marker_id, existing_webio_elem)
        # Reused elements aren't connected yet (already_connected would have skipped this
        # marker otherwise), so the wire has to be drawn explicitly here.
        conn_id = await api.logikplan_save_connection(fub_id, elem_marker, existing_webio_elem, conn_type)
        if conn_id is None:
            return None, f"M{marker_id}: save_connection (elem {elem_marker}→{existing_webio_elem}) fehlgeschlagen"
        _LOGGER.info(
            "Logikplan POC: M%s — Verbindung nachgezogen: elem %s→%s (conn_id=%s)",
            marker_id,
            elem_marker,
            existing_webio_elem,
            conn_id,
        )
        return existing_webio_elem, None

    _LOGGER.info("Logikplan POC: M%s → add_element+connect (WebIO, x=%.1f, y=%.1f)", marker_id, x, y)
    conn_payload = {
        "0": {
            "id": "new",
            "fub_id": fub_id,
            "type": conn_type,
            "input": {"element": str(elem_marker), "pos": "0", "inverted": False},
            "output": {"0": {"element": "new", "pos": "0", "inverted": False}},
        }
    }
    elem_webio = await api.logikplan_add_element(
        fub_id=fub_id,
        ref_id=int(web_ref_id),
        element_type=10,
        x=x,
        y=y,
        connection=conn_payload,
    )
    if elem_webio is None:
        return None, f"M{marker_id}: add_element (WebIO, webIoId={web_ref_id}) fehlgeschlagen"
    _LOGGER.info("Logikplan POC: M%s — WebIO+Verbindung angelegt: elem_id=%s", marker_id, elem_webio)
    return elem_webio, None


async def _connect_marker_to_webio(
    api,
    fub_id: int,
    marker_id: int,
    marker: dict | None,
    webio_commands: dict,
    existing_by_ref: dict[tuple[int, int], int],
    connected_pairs: set[tuple[int, int]],
    occupied_slots: set[tuple[int, int]],
    rows_per_col: int,
    max_cols: int,
    canvas_format: str,
) -> tuple[str | None, str | None, str | None]:
    """Connect a single marker to its WebIO command on the canvas.

    Returns (result, skip, error) — exactly one is set, the other two are None.
    Mutates `occupied_slots` in place when a new grid slot is claimed.
    """
    if not marker:
        _LOGGER.warning("Logikplan POC: M%s nicht gefunden", marker_id)
        return None, None, f"M{marker_id}: nicht in Koordinator-Daten"

    expected_cmd_name = f"HA {marker['name']}"
    webio_cmd = webio_commands.get(expected_cmd_name)
    if not webio_cmd:
        _LOGGER.warning("Logikplan POC: M%s — WebIO '%s' nicht gefunden", marker_id, expected_cmd_name)
        return None, None, f"M{marker_id}: WebIO '{expected_cmd_name}' nicht gefunden"

    # ref_id for type=10 (WebIO) is the local FubModules dict-key (webIoId), not cmdId
    web_ref_id = webio_cmd.get("webIoId")
    if web_ref_id is None:
        return None, None, f"M{marker_id}: WebIO '{expected_cmd_name}' hat keine webIoId"

    conn_type = "binary" if marker["type"] == "digital" else "analog"

    # Reuse existing elements on canvas (avoid duplicates)
    existing_marker_elem = existing_by_ref.get((2, int(marker_id)))
    existing_webio_elem = existing_by_ref.get((10, int(web_ref_id)))

    # Skip if already connected in this specific plan
    already_connected = (
        existing_marker_elem and existing_webio_elem and (existing_marker_elem, existing_webio_elem) in connected_pairs
    )
    if already_connected:
        _LOGGER.info("Logikplan POC: M%s — bereits in Plan verbunden, übersprungen", marker_id)
        return (
            None,
            f"M{marker_id} ({marker['name']}): bereits in Plan {fub_id} verbunden"
            f" (elem {existing_marker_elem}→{existing_webio_elem})",
            None,
        )

    # Find first free grid slot, update occupied_slots
    free_pos = _find_first_free_grid_position(occupied_slots, rows_per_col, max_cols)
    if free_pos is None:
        _LOGGER.warning("Logikplan POC: Canvas %s voll bei M%s", canvas_format, marker_id)
        return None, None, f"M{marker_id}: Canvas {canvas_format} voll ({max_cols} Spalten × {rows_per_col} Zeilen)"

    col, row_in_col = free_pos
    occupied_slots.add((col, row_in_col))
    y_new = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
    x_marker_cur = _LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH
    x_webio_cur = _LAYOUT_X_WEBIO + col * _LAYOUT_COLUMN_WIDTH
    if row_in_col == 0 and col > 0:
        _LOGGER.info("Logikplan POC: Spalte %d beginnt bei x=%.1f", col, x_marker_cur)

    elem_marker, err = await _get_or_create_marker_element(
        api, fub_id, marker_id, existing_marker_elem, x_marker_cur, y_new
    )
    if err:
        return None, None, err

    elem_webio, err = await _get_or_create_webio_element(
        api, fub_id, marker_id, elem_marker, web_ref_id, conn_type, existing_webio_elem, x_webio_cur, y_new
    )
    if err:
        return None, None, err

    result = (
        f"M{marker_id} ({marker['name']}) → elem={elem_marker} | "
        f"WebIO webIoId={web_ref_id} → elem={elem_webio} ({conn_type})"
    )
    return result, None, None


def _build_connect_poc_summary(
    fub_id: int,
    results: list[str],
    skipped: list[str],
    errors: list[str],
    duration: float,
    was_active: bool,
    activated: bool,
) -> tuple[str, str]:
    """Build the final notification message + title for a logikplan_connect_poc run."""
    lines: list[str] = []
    if results:
        lines += [f"**{len(results)} verbunden:**"] + [f"- {r}" for r in results]
    if skipped:
        lines += [f"\n**{len(skipped)} bereits verbunden (übersprungen):**"] + [f"- {s}" for s in skipped]
    if errors:
        lines += [f"\n**{len(errors)} Fehler:**"] + [f"- {e}" for e in errors]

    act_note = _plan_activation_note(was_active, activated, has_changes=bool(results), fub_id=fub_id)
    lines.append(f"\n{act_note}")
    lines.append(f"Dauer: {duration:.1f}s")

    title = f"Logikplan POC — {len(results)} OK / {len(skipped)} Skip / {len(errors)} Fehler"
    return "\n".join(lines), title


async def handle_logikplan_connect_poc(hass: HomeAssistant, call: ServiceCall) -> None:
    """POC: Connect markers (comma-separated list or all) to their WebIO commands."""
    error_title = "Logikplan POC — Fehler"
    ctx = _resolve_logikplan_plan(hass, call, error_title)
    if ctx is None:
        return
    coordinator, api, fub_id = ctx

    all_markers = call.data.get("all_markers", False)
    raw_input = str(call.data.get("marker_id", "2")).strip()
    markers_by_id = {int(m["id"]): m for m in coordinator.data.get("markers", [])}
    webio_commands = coordinator.data.get("webio_commands", {})

    conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
    ignored_ids = _parse_ignored_marker_ids(conf)
    marker_ids, invalid_tokens = _resolve_requested_marker_ids(raw_input, all_markers, markers_by_id, ignored_ids)
    if invalid_tokens:
        persistent_notification.async_create(
            hass, f"Ungültige Merker-IDs (keine Ganzzahlen): {', '.join(invalid_tokens)}.", title=error_title
        )
        return
    if not marker_ids:
        persistent_notification.async_create(hass, "Keine gültigen Merker-IDs angegeben.", title=error_title)
        return

    if not await _ensure_comexio_login(hass, api, error_title):
        return

    plan_info = api.fub_data.get(str(fub_id), {})
    was_active = bool(plan_info.get("Active", True))
    if was_active:
        await api.logikplan_stop_fup(fub_id)

    # Canvas-Grenzen und Spalten-Layout — DPI+Ausrichtung immer aus Plan-Daten
    canvas_format_raw = str(call.data.get("canvas_format", "")).strip().upper()
    canvas_format, x_max, y_max, rows_per_col, max_cols = _get_canvas_grid_dims(api, fub_id, canvas_format_raw)
    _LOGGER.info(
        "Logikplan POC: Canvas %s %.0f×%.0f (Plan fub_id=%s)",
        canvas_format,
        x_max,
        y_max,
        fub_id,
    )
    _LOGGER.info(
        "Logikplan POC: Canvas %s (%.0f×%.0f) → %d Zeilen/Spalte, %d Spalten",
        canvas_format,
        x_max,
        y_max,
        rows_per_col,
        max_cols,
    )

    # Load current plan state: existing elements + connections
    existing_by_ref, connected_pairs, occupied_slots = await _load_connect_poc_topology(
        api, fub_id, rows_per_col, max_cols
    )

    _LOGGER.info("Logikplan POC: fub_id=%s, %d Merker zu verarbeiten: %s", fub_id, len(marker_ids), marker_ids)
    t_start = time.monotonic()
    results: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []

    for marker_id in marker_ids:
        marker = markers_by_id.get(marker_id)
        result, skip, error = await _connect_marker_to_webio(
            api,
            fub_id,
            marker_id,
            marker,
            webio_commands,
            existing_by_ref,
            connected_pairs,
            occupied_slots,
            rows_per_col,
            max_cols,
            canvas_format,
        )
        if result:
            results.append(result)
        if skip:
            skipped.append(skip)
        if error:
            errors.append(error)

    duration = time.monotonic() - t_start
    # Plan was stopped above whenever it was active, so it must always be resumed
    # here regardless of `results` — otherwise a no-op run (e.g. all markers
    # already connected) leaves a previously active plan stopped permanently.
    activated = await api.logikplan_run_fup(fub_id) if was_active else False
    msg, title = _build_connect_poc_summary(fub_id, results, skipped, errors, duration, was_active, activated)
    persistent_notification.async_create(hass, msg, title=title)
