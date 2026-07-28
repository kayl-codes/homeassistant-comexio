# Version: 0.7.6
import contextlib
import logging
import pathlib
import re
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
import yaml

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Canvas layout constants for Logikplan POC
_LAYOUT_X_MARKER = 7.5
_LAYOUT_X_WEBIO = 202.5
_LAYOUT_Y_START = 7.5
_LAYOUT_Y_STEP = 22.5
_LAYOUT_COLUMN_WIDTH = 450.0  # x-Abstand zwischen Spaltengruppen


_PLAN_LABEL_ID_RE = re.compile(r"\(ID (\d+)\)\s*$")


def format_plan_label(name: str, fub_id) -> str:
    """Format a plan's select-option label as "<name> (ID <fub_id>)", parseable by _resolve_fub_id()."""
    return f"{name} (ID {fub_id})"


def _resolve_fub_id(fub_id_input: str, fub_data: dict, hass=None) -> int | None:
    """Resolve fub_id from a numeric string, plan name, "<name> (ID <n>)" select label, or select entity_id."""
    stripped = str(fub_id_input).strip()
    # If it looks like an entity_id, read the select entity's current state
    if hass and "." in stripped:
        state = hass.states.get(stripped)
        if state and state.state not in ("unknown", "unavailable", ""):
            stripped = state.state
    # Select options carry "(ID <n>)" — parse it directly so duplicate plan names
    # (Comexio only guarantees fub_id is unique) can't resolve to the wrong plan.
    if match := _PLAN_LABEL_ID_RE.search(stripped):
        return int(match.group(1))
    try:
        return int(stripped)
    except ValueError:
        name_lower = stripped.lower()
        for fid, fub in fub_data.items():
            if fub.get("Name", "").lower() == name_lower:
                return int(fid)
    return None


def _available_plans_str(fub_data: dict) -> str:
    """Return human-readable list of available plans from _fub_data."""
    if not fub_data:
        return "keine Pläne geladen — Integration neu laden"
    return ", ".join(
        f"'{fub.get('Name', '?')}' (ID {fid})" for fid, fub in sorted(fub_data.items(), key=lambda x: int(x[0]))
    )


def _build_sorted_pairs(
    elements: dict,
    connections: dict,
) -> tuple[list[tuple[int, int, int]], list[int]]:
    """Return marker→WebIO pairs sorted by marker ref_id and a list of orphan element IDs."""
    elem_ref: dict[int, dict] = {
        int(eid): {
            "type": e.get("reference", {}).get("type"),
            "ref_id": e.get("reference", {}).get("ref_id"),
        }
        for eid, e in elements.items()
    }
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int, int]] = []  # (marker_ref_id, marker_elem_id, webio_elem_id)
    for conn in connections.values():
        inp_eid = conn.get("input", {}).get("FubElementId")
        if inp_eid is None or elem_ref.get(inp_eid, {}).get("type") != 2:
            continue
        marker_ref_id = int(elem_ref[inp_eid].get("ref_id", 0))
        for out in conn.get("output", []):
            out_eid = out.get("FubElementId")
            if out_eid is not None and (inp_eid, out_eid) not in seen:
                seen.add((inp_eid, out_eid))
                pairs.append((marker_ref_id, inp_eid, out_eid))
    pairs.sort(key=lambda p: p[0])
    paired: set[int] = {eid for _, m, w in pairs for eid in (m, w)}
    orphans = [int(eid) for eid in elements if int(eid) not in paired]
    return pairs, orphans


def _get_occupied_grid_slots(
    elements: dict,
    rows_per_col: int,
    max_cols: int,
) -> set[tuple[int, int]]:
    """Collect all occupied (col, row) grid slots from existing elements."""
    occupied: set[tuple[int, int]] = set()
    for elem in elements.values():
        y = elem.get("position_y", 0.0)
        if y >= _LAYOUT_Y_START:
            row_in_col = round((y - _LAYOUT_Y_START) / _LAYOUT_Y_STEP)
            x = elem.get("position_x", 0.0)
            col = round((x - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH)
            if 0 <= col < max_cols and 0 <= row_in_col < rows_per_col:
                occupied.add((col, row_in_col))
    return occupied


def _find_first_free_grid_position(
    occupied: set[tuple[int, int]],
    rows_per_col: int,
    max_cols: int,
) -> tuple[int, int] | None:
    """Find the first free (col, row) position, scanning left-to-right, top-to-bottom."""
    for col in range(max_cols):
        for row in range(rows_per_col):
            if (col, row) not in occupied:
                return (col, row)
    return None


def _assign_grid_positions(
    pairs: list[tuple[int, int, int]],
    orphans: list[int],
    rows_per_col: int,
    max_cols: int,
) -> list[tuple[int, float, float]]:
    """Calculate exact grid positions for sorted pairs and orphan elements."""
    positions: dict[int, tuple[float, float]] = {}
    pairs_placed = 0
    for row_idx, (_, m_eid, w_eid) in enumerate(pairs):
        col = row_idx // rows_per_col
        if col >= max_cols:
            break
        row_in_col = row_idx % rows_per_col
        y = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
        if m_eid not in positions:
            positions[m_eid] = (_LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH, y)
        if w_eid not in positions:
            positions[w_eid] = (_LAYOUT_X_WEBIO + col * _LAYOUT_COLUMN_WIDTH, y)
        pairs_placed += 1
    for i, eid in enumerate(orphans):
        row_idx = pairs_placed + i
        col = row_idx // rows_per_col
        if col >= max_cols:
            _LOGGER.warning(
                "Grid layout full (%d cols × %d rows): %d orphan element(s) left unsorted",
                max_cols,
                rows_per_col,
                len(orphans) - i,
            )
            break
        row_in_col = row_idx % rows_per_col
        y = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
        positions[eid] = (_LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH, y)
    return [(eid, x, y) for eid, (x, y) in positions.items()]


_LOGIKPLAN_SERVICES = (
    "logikplan_connect_poc",
    "logikplan_sort",
    "logikplan_stop",
    "logikplan_activate",
    "logikplan_visualize",
)
_SERVICES_YAML_PATH = pathlib.Path(__file__).parent / "services.yaml"


def _rewrite_services_yaml_plans(plan_options: list[str]) -> None:
    """Blocking read/modify/write of services.yaml; run via executor job only."""
    try:
        content = yaml.safe_load(_SERVICES_YAML_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _LOGGER.warning("services.yaml missing or invalid, skipping plan option rewrite: %s", exc)
        return
    for svc in _LOGIKPLAN_SERVICES:
        fub_field = content.get(svc, {}).get("fields", {}).get("fub_id")
        if fub_field:
            fub_field["selector"] = {"select": {"options": plan_options, "custom_value": True}}
    _SERVICES_YAML_PATH.write_text(
        yaml.dump(content, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


async def _update_services_yaml_plans(hass: HomeAssistant) -> None:
    """Rewrite fub_id select options in services.yaml with current plan labels from all active coordinators.

    Labels use the same "<name> (ID <fid>)" format as the select entity, so duplicate
    plan names across coordinators still resolve unambiguously via _resolve_fub_id().
    """
    from .coordinator import ComexioCoordinator

    plan_labels: set[str] = set()
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, ComexioCoordinator):
            for fub_id, fub in getattr(coordinator.api, "_fub_data", {}).items():
                name = fub.get("Name", "")
                if name:
                    plan_labels.add(format_plan_label(name, fub_id))

    if not plan_labels:
        _LOGGER.debug("_update_services_yaml_plans: no plans available, skipping")
        return

    plan_options = sorted(plan_labels, key=str.lower)
    try:
        await hass.async_add_executor_job(_rewrite_services_yaml_plans, plan_options)
        _LOGGER.debug("Updated services.yaml: %d Logikplan plan options (labels) written", len(plan_options))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not update services.yaml with plan labels: %s", exc)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register additional services for the Comexio integration."""

    async def handle_generate_web_io(call: ServiceCall):
        """Service to preview or upload the Web-IO configuration."""
        entry_id = call.data.get("config_entry")

        if entry_id not in hass.data[DOMAIN]:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = hass.data[DOMAIN][entry_id]
        api = coordinator.api
        server_id = coordinator.server_id
        do_upload = call.data.get("upload", False)

        try:
            conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
            webio_name = conf.get("webio_name", "HomeAssistant")

            web_io_json = api.generate_webio_json(server_id, webio_name, coordinator.data)

            if not do_upload:
                persistent_notification.async_create(
                    hass, f"```json\n{web_io_json}\n```", title=f"Comexio Preview ({server_id})"
                )
                return

            base_info = await api.get_webio_base_info(webio_name)
            if base_info:
                base_id, deletable = base_info
                if deletable:
                    _LOGGER.info("Base class is deletable, performing clean reinstall.")
                    await api.delete_webio_base(base_id)
                else:
                    persistent_notification.async_create(
                        hass,
                        f"Class '{webio_name}' is in use by Comexio logic and cannot be deleted. "
                        "Please use the Smart-Sync button for individual updates.",
                        title="Bulk-Sync blocked",
                    )
                    return

            success, result_val = await api.upload_web_io(server_id, webio_name, web_io_json)
            msg = f"Sync successful! Base-ID: {result_val}" if success else f"Upload failed: {result_val}"
            persistent_notification.async_create(hass, msg, title=f"Comexio Sync ({server_id})")

        except Exception as e:
            _LOGGER.exception("Error in Comexio service: %s", e)

    async def handle_logikplan_connect_poc(call: ServiceCall):
        """POC: Connect markers (comma-separated list or all) to their WebIO commands."""
        from .const import CONF_IGNORED_MARKERS

        entry_id = call.data.get("config_entry")
        domain_data = hass.data.get(DOMAIN, {})
        if not entry_id:
            entries = list(domain_data.keys())
            if len(entries) != 1:
                _LOGGER.error("config_entry required when multiple Comexio instances exist: %s", entries)
                persistent_notification.async_create(
                    hass,
                    "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.",
                    title="Logikplan POC — Fehler",
                )
                return
            entry_id = entries[0]
        if entry_id not in domain_data:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = domain_data[entry_id]
        api = coordinator.api
        fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api._fub_data, hass)
        if fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api._fub_data)}",
                title="Logikplan POC — Fehler",
            )
            return
        all_markers = call.data.get("all_markers", False)
        raw_input = str(call.data.get("marker_id", "2")).strip()

        markers_by_id = {m["id"]: m for m in coordinator.data.get("markers", [])}
        webio_commands = coordinator.data.get("webio_commands", {})

        # Load ignored_markers from config/options
        conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
        ignored_raw = conf.get(CONF_IGNORED_MARKERS, "").strip()
        ignored_ids: set[int] = set()
        if ignored_raw:
            for token in ignored_raw.replace(";", ",").split(","):
                stripped = token.strip()
                if stripped:
                    with contextlib.suppress(ValueError):
                        ignored_ids.add(int(stripped))

        if all_markers or raw_input == "*":
            marker_ids = [mid for mid in markers_by_id if mid not in ignored_ids]
        else:
            # Parse comma-separated list; strip optional "M"/"m" prefix
            raw_ids = [tok.strip().lstrip("Mm") for tok in raw_input.split(",") if tok.strip()]
            marker_ids = []
            invalid_tokens = []
            for raw_id in raw_ids:
                try:
                    mid = int(raw_id)
                except ValueError:
                    invalid_tokens.append(raw_id)
                    continue
                if mid not in ignored_ids:
                    marker_ids.append(mid)
            if invalid_tokens:
                persistent_notification.async_create(
                    hass,
                    f"Ungültige Merker-IDs (keine Ganzzahlen): {', '.join(invalid_tokens)}.",
                    title="Logikplan POC — Fehler",
                )
                return

        if not marker_ids:
            persistent_notification.async_create(
                hass, "Keine gültigen Merker-IDs angegeben.", title="Logikplan POC — Fehler"
            )
            return

        if not await api.login():
            persistent_notification.async_create(
                hass, "Comexio Admin-Login fehlgeschlagen.", title="Logikplan POC — Fehler"
            )
            return

        plan_info = api._fub_data.get(str(fub_id), {})
        was_active = bool(plan_info.get("Active", True))
        if was_active:
            await api.logikplan_stop_fup(fub_id)

        # Canvas-Grenzen und Spalten-Layout — DPI+Ausrichtung immer aus Plan-Daten
        canvas_format_raw = str(call.data.get("canvas_format", "")).strip().upper()
        if canvas_format_raw and canvas_format_raw != "AUTO":
            canvas_format = canvas_format_raw
            x_max, y_max = api.get_fub_canvas_bounds(fub_id, paper_name=canvas_format)
        else:
            canvas_format = api.get_fub_paper_format(fub_id).upper()
            x_max, y_max = api.get_fub_canvas_bounds(fub_id)
        _LOGGER.info(
            "Logikplan POC: Canvas %s %.0f×%.0f (Plan fub_id=%s)",
            canvas_format,
            x_max,
            y_max,
            fub_id,
        )
        rows_per_col = max(1, int((y_max - _LAYOUT_Y_START) / _LAYOUT_Y_STEP))
        max_cols = max(1, round((x_max - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH))
        _LOGGER.info(
            "Logikplan POC: Canvas %s (%.0f×%.0f) → %d Zeilen/Spalte, %d Spalten",
            canvas_format,
            x_max,
            y_max,
            rows_per_col,
            max_cols,
        )

        # Load current plan state: existing elements + connections
        plan_data = await api.logikplan_load_elements(fub_id)
        existing_by_ref: dict[tuple[int, int], int] = {}  # (type, ref_id) → elem_id
        connected_pairs: set[tuple[int, int]] = set()  # (input_elem_id, output_elem_id)
        occupied_slots: set[tuple[int, int]] = set()

        if plan_data:
            for elem_id_str, elem in plan_data.get("elements", {}).items():
                ref = elem.get("reference", {})
                existing_by_ref[(ref.get("type"), ref.get("ref_id"))] = int(elem_id_str)
            for conn in plan_data.get("connections", {}).values():
                inp = conn.get("input", {})
                for out in conn.get("output", []):
                    connected_pairs.add((inp.get("FubElementId"), out.get("FubElementId")))
            occupied_slots = _get_occupied_grid_slots(plan_data.get("elements", {}), rows_per_col, max_cols)
            _LOGGER.info("Logikplan POC: Plan fub=%s — %d belegte Grid-Slots gefunden", fub_id, len(occupied_slots))
        else:
            _LOGGER.warning("Logikplan POC: loadelements fehlgeschlagen, fahre ohne Plan-Zustand fort")

        _LOGGER.info("Logikplan POC: fub_id=%s, %d Merker zu verarbeiten: %s", fub_id, len(marker_ids), marker_ids)
        t_start = time.monotonic()
        results: list[str] = []
        errors: list[str] = []
        skipped: list[str] = []

        for marker_id in marker_ids:
            marker = markers_by_id.get(marker_id)
            if not marker:
                errors.append(f"M{marker_id}: nicht in Koordinator-Daten")
                _LOGGER.warning("Logikplan POC: M%s nicht gefunden", marker_id)
                continue

            expected_cmd_name = f"HA {marker['name']}"
            webio_cmd = webio_commands.get(expected_cmd_name)
            if not webio_cmd:
                errors.append(f"M{marker_id}: WebIO '{expected_cmd_name}' nicht gefunden")
                _LOGGER.warning("Logikplan POC: M%s — WebIO '%s' nicht gefunden", marker_id, expected_cmd_name)
                continue

            # ref_id for type=10 (WebIO) is the local FubModules dict-key (webIoId), not cmdId
            web_ref_id = webio_cmd.get("webIoId")
            if web_ref_id is None:
                errors.append(f"M{marker_id}: WebIO '{expected_cmd_name}' hat keine webIoId")
                continue

            conn_type = "binary" if marker["type"] == "digital" else "analog"

            # Reuse existing elements on canvas (avoid duplicates)
            existing_marker_elem = existing_by_ref.get((2, int(marker_id)))
            existing_webio_elem = existing_by_ref.get((10, int(web_ref_id)))

            # Skip if already connected in this specific plan
            already_connected = (
                existing_marker_elem
                and existing_webio_elem
                and (existing_marker_elem, existing_webio_elem) in connected_pairs
            )
            if already_connected:
                skipped.append(
                    f"M{marker_id} ({marker['name']}): bereits in Plan {fub_id} verbunden"
                    f" (elem {existing_marker_elem}→{existing_webio_elem})"
                )
                _LOGGER.info("Logikplan POC: M%s — bereits in Plan verbunden, übersprungen", marker_id)
                continue

            # Find first free grid slot, update occupied_slots
            free_pos = _find_first_free_grid_position(occupied_slots, rows_per_col, max_cols)
            if free_pos is None:
                errors.append(f"M{marker_id}: Canvas {canvas_format} voll ({max_cols} Spalten × {rows_per_col} Zeilen)")
                _LOGGER.warning("Logikplan POC: Canvas %s voll bei M%s", canvas_format, marker_id)
                continue

            col, row_in_col = free_pos
            occupied_slots.add((col, row_in_col))
            y_new = _LAYOUT_Y_START + row_in_col * _LAYOUT_Y_STEP
            x_marker_cur = _LAYOUT_X_MARKER + col * _LAYOUT_COLUMN_WIDTH
            x_webio_cur = _LAYOUT_X_WEBIO + col * _LAYOUT_COLUMN_WIDTH
            if row_in_col == 0 and col > 0:
                _LOGGER.info("Logikplan POC: Spalte %d beginnt bei x=%.1f", col, x_marker_cur)

            # Marker element: reuse existing or create new
            if existing_marker_elem:
                elem_marker = existing_marker_elem
                _LOGGER.info(
                    "Logikplan POC: M%s — Merker-Element bereits vorhanden: elem_id=%s", marker_id, elem_marker
                )
            else:
                _LOGGER.info("Logikplan POC: M%s → add_element (Merker, col=%d, y=%.1f)", marker_id, col, y_new)
                elem_marker = await api.logikplan_add_element(
                    fub_id=fub_id, ref_id=int(marker_id), element_type=2, x=x_marker_cur, y=y_new
                )
                if elem_marker is None:
                    errors.append(f"M{marker_id}: add_element (Merker) fehlgeschlagen")
                    continue
                _LOGGER.info("Logikplan POC: M%s — Merker-Element angelegt: elem_id=%s", marker_id, elem_marker)

            # WebIO element: reuse existing or create new (connection embedded in add_element)
            if existing_webio_elem:
                elem_webio = existing_webio_elem
                _LOGGER.info("Logikplan POC: M%s — WebIO-Element bereits vorhanden: elem_id=%s", marker_id, elem_webio)
                # Reused elements aren't connected yet (already_connected would have skipped this
                # marker otherwise), so the wire has to be drawn explicitly here.
                conn_id = await api.logikplan_save_connection(fub_id, elem_marker, elem_webio, conn_type)
                if conn_id is None:
                    errors.append(f"M{marker_id}: save_connection (elem {elem_marker}→{elem_webio}) fehlgeschlagen")
                    continue
                _LOGGER.info(
                    "Logikplan POC: M%s — Verbindung nachgezogen: elem %s→%s (conn_id=%s)",
                    marker_id,
                    elem_marker,
                    elem_webio,
                    conn_id,
                )
            else:
                _LOGGER.info("Logikplan POC: M%s → add_element+connect (WebIO, col=%d, y=%.1f)", marker_id, col, y_new)
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
                    x=x_webio_cur,
                    y=y_new,
                    connection=conn_payload,
                )
                if elem_webio is None:
                    errors.append(f"M{marker_id}: add_element (WebIO, webIoId={web_ref_id}) fehlgeschlagen")
                    continue
                _LOGGER.info("Logikplan POC: M%s — WebIO+Verbindung angelegt: elem_id=%s", marker_id, elem_webio)

            results.append(
                f"M{marker_id} ({marker['name']}) → elem={elem_marker} | "
                f"WebIO webIoId={web_ref_id} → elem={elem_webio} ({conn_type})"
            )

        duration = time.monotonic() - t_start
        lines = []
        if results:
            lines += [f"**{len(results)} verbunden:**"] + [f"- {r}" for r in results]
        if skipped:
            lines += [f"\n**{len(skipped)} bereits verbunden (übersprungen):**"] + [f"- {s}" for s in skipped]
        if errors:
            lines += [f"\n**{len(errors)} Fehler:**"] + [f"- {e}" for e in errors]
        # Plan was stopped above whenever it was active, so it must always be resumed
        # here regardless of `results` — otherwise a no-op run (e.g. all markers
        # already connected) leaves a previously active plan stopped permanently.
        activated = await api.logikplan_run_fup(fub_id) if was_active else False
        if not was_active:
            act_note = (
                "Plan war inaktiv — Änderungen gespeichert, Plan bleibt inaktiv."
                if results
                else f"Plan fub_id={fub_id} — keine neuen Verbindungen, Plan unverändert."
            )
        elif activated:
            act_note = "Plan gespeichert und aktiviert." if results else "Plan unverändert, weiterhin aktiv."
        else:
            act_note = "Plan-Aktivierung fehlgeschlagen — bitte manuell im Comexio-UI speichern."
        lines.append(f"\n{act_note}")
        lines.append(f"Dauer: {duration:.1f}s")

        title = f"Logikplan POC — {len(results)} OK / {len(skipped)} Skip / {len(errors)} Fehler"
        persistent_notification.async_create(hass, "\n".join(lines), title=title)

    async def handle_logikplan_visualize(call: ServiceCall):
        """Service to visualize current state of a Logikplan plan as a text diagram."""
        entry_id = call.data.get("config_entry")
        domain_data = hass.data.get(DOMAIN, {})
        if not entry_id:
            entries = list(domain_data.keys())
            if len(entries) != 1:
                persistent_notification.async_create(
                    hass,
                    "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.",
                    title="Logikplan Visualize — Fehler",
                )
                return
            entry_id = entries[0]
        if entry_id not in domain_data:
            _LOGGER.error("Comexio instance %s not found", entry_id)
            return

        coordinator = domain_data[entry_id]
        api = coordinator.api
        fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api._fub_data, hass)
        if fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api._fub_data)}",
                title="Logikplan Visualize — Fehler",
            )
            return

        if not await api.login():
            persistent_notification.async_create(
                hass, "Comexio Admin-Login fehlgeschlagen.", title="Logikplan Visualize — Fehler"
            )
            return

        plan_data = await api.logikplan_load_elements(fub_id)
        if not plan_data:
            persistent_notification.async_create(
                hass, f"Plan {fub_id} konnte nicht geladen werden.", title="Logikplan Visualize — Fehler"
            )
            return

        markers_by_id = {str(m["id"]): m for m in coordinator.data.get("markers", [])}
        webio_commands = coordinator.data.get("webio_commands", {})
        webio_by_id = {
            str(cmd.get("webIoId")): name for name, cmd in webio_commands.items() if cmd.get("webIoId") is not None
        }
        elements = plan_data.get("elements", {})
        connections = plan_data.get("connections", {})

        def elem_label(elem_id: str | int) -> str:
            ref = elements.get(str(elem_id), {}).get("reference", {})
            etype = ref.get("type")
            ref_id = str(ref.get("ref_id", "?"))
            if etype == 2:
                marker = markers_by_id.get(ref_id)
                return f"M{ref_id} {marker['name']}" if marker else f"M{ref_id} (unbekannt)"
            if etype == 10:
                return webio_by_id.get(ref_id, f"WebIO ref={ref_id}")
            return f"Typ{etype} ref={ref_id}"

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
                out_parts.append(f"{elem_label(out_id)}{inv_out}")
                connected_elem_ids.add(out_id)
            conn_lines.append(f"  {elem_label(inp_id)}{inv_in} →[{conn_type}]→ {', '.join(out_parts)}")

        def _pos_key(kv: tuple) -> tuple:
            return (kv[1].get("position_x", 0), kv[1].get("position_y", 0))

        orphan_lines: list[str] = []
        for elem_id, elem in sorted(elements.items(), key=_pos_key):
            if elem_id not in connected_elem_ids:
                x, y = elem.get("position_x", 0), elem.get("position_y", 0)
                orphan_lines.append(f"  {elem_label(elem_id)} (@ {x:.0f},{y:.0f})")

        paper_fmt = api.get_fub_paper_format(fub_id)
        x_max, y_max = api.get_fub_canvas_bounds(fub_id)
        lines = [
            f"**Plan {fub_id}** — {paper_fmt}, Canvas {x_max:.0f}×{y_max:.0f}",
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

    async def handle_logikplan_sort(call: ServiceCall):
        """Sort all Logikplan elements by marker ID, snapping every element to exact grid."""
        domain_data = hass.data.get(DOMAIN, {})
        entry_id = call.data.get("config_entry")
        if not entry_id:
            entries = list(domain_data.keys())
            if len(entries) != 1:
                persistent_notification.async_create(
                    hass,
                    "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.",
                    title="Logikplan Sort — Fehler",
                )
                return
            entry_id = entries[0]
        if entry_id not in domain_data:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = domain_data[entry_id]
        api = coordinator.api
        fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api._fub_data, hass)
        if fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api._fub_data)}",
                title="Logikplan Sort — Fehler",
            )
            return

        if not await api.login():
            persistent_notification.async_create(
                hass, "Comexio Admin-Login fehlgeschlagen.", title="Logikplan Sort — Fehler"
            )
            return

        t_start = time.monotonic()
        plan_info = api._fub_data.get(str(fub_id), {})
        was_active = bool(plan_info.get("Active", True))

        canvas_format_raw = str(call.data.get("canvas_format", "")).strip().upper()
        if canvas_format_raw and canvas_format_raw != "AUTO":
            canvas_label = canvas_format_raw
            x_max, y_max = api.get_fub_canvas_bounds(fub_id, paper_name=canvas_format_raw)
        else:
            canvas_label = api.get_fub_paper_format(fub_id).upper()
            x_max, y_max = api.get_fub_canvas_bounds(fub_id)

        rows_per_col = max(1, int((y_max - _LAYOUT_Y_START) / _LAYOUT_Y_STEP))
        max_cols = max(1, round((x_max - _LAYOUT_X_MARKER) / _LAYOUT_COLUMN_WIDTH))

        plan_data = await api.logikplan_load_elements(fub_id)
        if not plan_data:
            persistent_notification.async_create(
                hass, f"Plan {fub_id} konnte nicht geladen werden.", title="Logikplan Sort — Fehler"
            )
            return

        pairs, orphans = _build_sorted_pairs(plan_data.get("elements", {}), plan_data.get("connections", {}))
        new_positions = _assign_grid_positions(pairs, orphans, rows_per_col, max_cols)

        if not new_positions:
            persistent_notification.async_create(hass, "Keine Elemente im Plan.", title=f"Logikplan Sort Plan {fub_id}")
            return

        _LOGGER.info(
            "Logikplan Sort: Plan %s — %d Paare, %d Waisen, %d Positionen (aktiv=%s)",
            fub_id,
            len(pairs),
            len(orphans),
            len(new_positions),
            was_active,
        )
        if was_active:
            await api.logikplan_stop_fup(fub_id)
        success = await api.logikplan_save_elements_pos(new_positions)
        activated = await api.logikplan_run_fup(fub_id) if (success and was_active) else False
        duration = time.monotonic() - t_start
        status = "erfolgreich" if success else "fehlgeschlagen"
        if not was_active:
            act_note = "Plan war inaktiv — Änderungen gespeichert, Plan bleibt inaktiv."
        elif activated:
            act_note = "Plan gespeichert und aktiviert."
        else:
            act_note = "Plan-Aktivierung fehlgeschlagen — bitte manuell im Comexio-UI speichern."
        msg = (
            f"Sortierung {status}: {len(pairs)} Paare nach Merker-ID geordnet"
            f" + {len(orphans)} Einzelelemente.\n"
            f"Canvas {canvas_label}: {max_cols} Spalten × {rows_per_col} Zeilen.\n"
            f"{act_note}\n"
            f"Dauer: {duration:.1f}s"
        )
        persistent_notification.async_create(
            hass, msg, title=f"Logikplan Sort Plan {fub_id} — {'OK' if success else 'Fehler'}"
        )

    async def handle_logikplan_stop(call: ServiceCall):
        """Stop/pause a Logikplan plan."""
        domain_data = hass.data.get(DOMAIN, {})
        entry_id = call.data.get("config_entry")
        if not entry_id:
            entries = list(domain_data.keys())
            if len(entries) != 1:
                persistent_notification.async_create(
                    hass,
                    "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.",
                    title="Logikplan Stop — Fehler",
                )
                return
            entry_id = entries[0]
        if entry_id not in domain_data:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = domain_data[entry_id]
        api = coordinator.api
        fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api._fub_data, hass)
        if fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api._fub_data)}",
                title="Logikplan Stop — Fehler",
            )
            return

        if not await api.login():
            persistent_notification.async_create(
                hass, "Comexio Admin-Login fehlgeschlagen.", title="Logikplan Stop — Fehler"
            )
            return

        plan_name = api._fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
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

    async def handle_logikplan_activate(call: ServiceCall):
        """Save and activate a Logikplan plan (run_fup)."""
        domain_data = hass.data.get(DOMAIN, {})
        entry_id = call.data.get("config_entry")
        if not entry_id:
            entries = list(domain_data.keys())
            if len(entries) != 1:
                persistent_notification.async_create(
                    hass,
                    "Mehrere Comexio-Instanzen — bitte `config_entry` angeben.",
                    title="Logikplan Aktivieren — Fehler",
                )
                return
            entry_id = entries[0]
        if entry_id not in domain_data:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = domain_data[entry_id]
        api = coordinator.api
        fub_id_raw = call.data.get("fub_id") or f"select.comexio_{coordinator.server_id}_logikplan_plan_selector"
        fub_id = _resolve_fub_id(str(fub_id_raw), api._fub_data, hass)
        if fub_id is None:
            persistent_notification.async_create(
                hass,
                f"Plan '{fub_id_raw}' nicht gefunden.\nVerfügbar: {_available_plans_str(api._fub_data)}",
                title="Logikplan Aktivieren — Fehler",
            )
            return

        if not await api.login():
            persistent_notification.async_create(
                hass, "Comexio Admin-Login fehlgeschlagen.", title="Logikplan Aktivieren — Fehler"
            )
            return

        plan_name = api._fub_data.get(str(fub_id), {}).get("Name", str(fub_id))
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

    if not hass.services.has_service(DOMAIN, "generate_web_io"):
        hass.services.async_register(DOMAIN, "generate_web_io", handle_generate_web_io)
    if not hass.services.has_service(DOMAIN, "logikplan_connect_poc"):
        hass.services.async_register(DOMAIN, "logikplan_connect_poc", handle_logikplan_connect_poc)
    if not hass.services.has_service(DOMAIN, "logikplan_visualize"):
        hass.services.async_register(DOMAIN, "logikplan_visualize", handle_logikplan_visualize)
    if not hass.services.has_service(DOMAIN, "logikplan_sort"):
        hass.services.async_register(DOMAIN, "logikplan_sort", handle_logikplan_sort)
    if not hass.services.has_service(DOMAIN, "logikplan_stop"):
        hass.services.async_register(DOMAIN, "logikplan_stop", handle_logikplan_stop)
    if not hass.services.has_service(DOMAIN, "logikplan_activate"):
        hass.services.async_register(DOMAIN, "logikplan_activate", handle_logikplan_activate)

    await _update_services_yaml_plans(hass)
