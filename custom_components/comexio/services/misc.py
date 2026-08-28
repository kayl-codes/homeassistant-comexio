# Version: 0.7.6
"""Remaining service handlers: Web-IO export/upload, set_value, debug session, search.

Split out of the former monolithic services.py (Sourcery: "too large, multi-purpose") —
these handlers don't share enough with the other thematic groups (connect/plan_actions/
backup) to justify their own module each, so they're bundled here.
"""

import logging
import math
import re
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN, WEBIO_CLASSES, webio_class_name
from ..coordinator import ComexioCoordinator
from ..function_plan_render import resolve_element_label
from ._context import _INSTANCE_NOT_FOUND_LOG, _async_get_service_context

_LOGGER = logging.getLogger(__name__)

_TITLE_SET_VALUE_ERR = "Set Value — Error"
_TITLE_DEBUG_SESSION_ERR = "Function Plan Debug Session — Error"
_TITLE_PREVIEW_EXTEND_ERR = "Function Plan Preview Extend — Error"
_TITLE_SEARCH_ERR = "Function Plan Search — Error"
_INSTANCE_NOT_RESOLVED_ERR = "Comexio instance not resolved."

# Command syntax accepted by the set_value service (and the plan card's debug input):
# "M107" (marker) or "IOX2#Q3" (extension#identifier).
_SET_VALUE_MARKER_RX = re.compile(r"^[mM](\d+)$")
_SET_VALUE_IO_RX = re.compile(r"^([^#\s]+)#([^#\s]+)$")


def _build_label_matcher(query: str):
    """Compile the preview card's search syntax into a label predicate.

    Same rules as comexio-plan-card.js _matchesPattern, so the dashboard search box and
    this service accept identical input. Wildcards are TOKEN-ANCHORED: '?' = exactly one
    non-space character, '*' = any run of non-space characters (also empty), and the
    match must cover a whole token — 'M4?' hits M40–M49 but not M4/M400, 'M4*' hits
    M4/M40/M400…. A query without wildcards is a case-insensitive substring test with
    token guards — an alphanumeric start must not be preceded by a letter/digit and a
    trailing digit must not be followed by another digit, so 'M4' hits M4 but not
    M40/M400/PWM4 (use '*' for loose matching).
    """
    if any(ch in query for ch in "?*"):
        # Collapse "**"/"***" to a single "*" first — semantically identical, but adjacent
        # \S* \S* quantifiers on the same character class are super-linear on backtracking
        # for a non-matching input. Mirrored in comexio-plan-card.js _matchesPattern.
        query = re.sub(r"\*+", "*", query)
        body = "".join(
            "\\S" if tok == "?" else r"\S*" if tok == "*" else re.escape(tok) for tok in re.split(r"([?*])", query)
        )
        try:
            # [^\W_] = letter/digit (like JS [\p{L}\p{N}]) — both guards keep the
            # match from bleeding into neighbouring id characters (M4? vs. M400).
            pattern = re.compile(r"(?<![^\W_])" + body + r"(?![^\W_])", re.IGNORECASE)
        except re.error:
            return lambda _label: False
        return lambda label: bool(pattern.search(label))
    before = r"(?<![^\W_])" if query[0].isalnum() else ""  # [^\W_] = letter/digit, like JS [\p{L}\p{N}]
    after = r"(?!\d)" if query[-1].isdigit() else ""
    pattern = re.compile(before + re.escape(query) + after, re.IGNORECASE)
    return lambda label: bool(pattern.search(label))


async def _async_set_plan_selector(hass: HomeAssistant, coordinator: ComexioCoordinator, plan_name: str) -> bool:
    """Point the 'Function Plans' select entity at the given plan (True when it was set).

    Used by the search service on an unambiguous hit, so a follow-up action without
    fub_id (visualize/sort/…) targets the plan just found. Goes through the regular
    select_option service so the entity's own persistence logic runs.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"comexio_{coordinator.server_id}_logikplan_plan_selector"
    )
    if not entity_id:
        return False
    try:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": entity_id, "option": plan_name}, blocking=True
        )
    except HomeAssistantError as err:
        _LOGGER.warning("Function Plan Search: could not set plan selector to '%s': %s", plan_name, err)
        return False
    return True


def _resolve_set_value_marker_target(marker_id: str, value: float, data: dict) -> tuple[dict | None, str]:
    """Marker half of _resolve_set_value_target (kept separate to stay under the complexity budget)."""
    marker = next((mk for mk in data.get("markers", []) if str(mk.get("id")) == marker_id), None)
    if marker is None:
        return None, f"Unknown marker 'M{marker_id}' — not in the current Comexio configuration."
    if marker.get("read_only"):
        return None, f"Marker 'M{marker_id}' is read-only (title ends with [RO]) — writes are rejected."
    if marker.get("type") == "digital" and value not in (0, 1):
        return None, f"Digital marker 'M{marker_id}' only accepts 0 or 1 (got {value})."
    return {"target_type": "marker", "target_id": marker_id}, ""


def _resolve_set_value_io_target(
    target: str, ext: str, ident: str, value: float, data: dict
) -> tuple[dict | None, str]:
    """IO half of _resolve_set_value_target (kept separate to stay under the complexity budget)."""
    for io in data.get("io", []):
        if io.get("ext_name", "").lower() != ext or io.get("identifier", "").lower() != ident:
            continue
        if io.get("is_input"):
            # Physical inputs (I/AI/…) are read-only for the Comexio API — a write
            # would silently do nothing, so reject it with a clear message instead.
            return None, f"IO '{target}' is an input (read-only) — the Comexio API cannot write inputs."
        if io.get("is_binary") and value not in (0, 1):
            return None, f"IO '{target}' is digital — only 0 or 1 accepted (got {value})."
        io_id = io.get("id")
        if io_id is None:
            return None, f"IO '{target}' has no id in the current configuration — cannot address it."
        return {
            "target_type": "io",
            "target_id": io_id,
            "ext": io.get("ext_name"),
            "identifier": io.get("identifier"),
        }, ""
    return None, f"Unknown IO '{target}' — not in the current Comexio configuration (or inactive)."


def _resolve_set_value_target(target: str, value: float, data: dict) -> tuple[dict | None, str]:
    """Map a set_value target token onto api.set_value kwargs.

    Accepts 'M<id>' (marker) or '<Extension>#<IO>' (case-insensitive). Only targets that
    exist in the current coordinator data are accepted — unknown ids never reach the
    Comexio API. Digital targets only accept 0/1 — value is checked against the target's
    known type so a stray '2' or '0.7' is rejected here, not silently forwarded. Returns
    (kwargs, "") on success or (None, error_message).
    """
    if m := _SET_VALUE_MARKER_RX.match(target):
        return _resolve_set_value_marker_target(m.group(1), value, data)
    if m := _SET_VALUE_IO_RX.match(target):
        return _resolve_set_value_io_target(target, m.group(1).lower(), m.group(2).lower(), value, data)
    return None, f"Invalid target '{target}' — expected 'M<id>' or '<Extension>#<IO>' (e.g. M107 or IOX2#Q3)."


async def handle_generate_web_io(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service to preview or upload the Web-IO configuration."""
    entry_id = call.data.get("config_entry")

    if entry_id not in hass.data[DOMAIN]:
        _LOGGER.error(_INSTANCE_NOT_FOUND_LOG, entry_id)
        return

    coordinator = hass.data[DOMAIN][entry_id]
    api = coordinator.api
    server_id = coordinator.server_id
    do_upload = call.data.get("upload", False)

    try:
        conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
        webio_name = conf.get("webio_name", "HomeAssistant")

        if not do_upload:
            preview_parts = []
            for cls in WEBIO_CLASSES:
                class_name = webio_class_name(webio_name, cls)
                web_io_json = api.generate_webio_json(
                    server_id,
                    class_name,
                    coordinator.data,
                    webio_class=cls,
                    ignored_marker_ids=coordinator.ignored_marker_ids,
                )
                preview_parts.append(f"**{class_name}**\n```json\n{web_io_json}\n```")
            persistent_notification.async_create(
                hass, "\n\n".join(preview_parts), title=f"Comexio Preview ({server_id})"
            )
            return

        results: list[str] = []
        for cls in WEBIO_CLASSES:
            class_name = webio_class_name(webio_name, cls)
            web_io_json = api.generate_webio_json(
                server_id,
                class_name,
                coordinator.data,
                webio_class=cls,
                ignored_marker_ids=coordinator.ignored_marker_ids,
            )

            base_info = await api.get_webio_base_info(class_name)
            if base_info:
                base_id, deletable = base_info
                if deletable:
                    _LOGGER.info("Base class '%s' is deletable, performing clean reinstall.", class_name)
                    await api.delete_webio_base(base_id)
                else:
                    results.append(
                        f"{class_name}: skipped — in use by Comexio logic, use the Smart-Sync button instead"
                    )
                    continue

            success, result_val = await api.upload_web_io(server_id, class_name, web_io_json)
            results.append(
                f"{class_name}: Base-ID {result_val}" if success else f"{class_name}: Upload failed: {result_val}"
            )

        persistent_notification.async_create(
            hass, "\n".join(results) or "Nothing to do.", title=f"Comexio Sync ({server_id})"
        )

    except Exception as e:
        _LOGGER.exception("Error in Comexio service: %s", e)


async def _handle_function_plan_debug_session(hass: HomeAssistant, call: ServiceCall) -> None:
    """Switch the live plan preview's Stufe-2 connection-value poll cadence.

    Called by the plan card on debug-box open/close (no fub_id/entity targeting — it
    always addresses whichever plan is currently armed for live preview, same as
    set_value's single-instance resolution). Silent by design: this fires on every
    debug-box toggle, so a notification per call would flood the tray for no benefit —
    only ambiguous multi-instance setups get a message, via _async_get_service_context.
    """
    ctx = await _async_get_service_context(hass, call, _TITLE_DEBUG_SESSION_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return
    coordinator, _api, _fub_id = ctx
    active = bool(call.data.get("open"))
    _LOGGER.debug("Function Plan Debug Session: open=%s", active)
    coordinator.set_debug_session_active(active)


_PREVIEW_EXTEND_MIN_MINUTES = 1
_PREVIEW_EXTEND_MAX_MINUTES = 1440  # 24h — the longest window a user asked to watch for


async def _handle_function_plan_preview_extend(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Extend the currently armed preview's Stufe-2 auto-stop window (debug box `/extend <n>`).

    In-memory only, for this arm alone — see coordinator.set_preview_auto_stop_extension.
    No fub_id/entity targeting, same single-instance resolution as function_plan_debug_session.
    """
    ctx = await _async_get_service_context(hass, call, _TITLE_PREVIEW_EXTEND_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return {"success": False, "error": _INSTANCE_NOT_RESOLVED_ERR}
    coordinator, _api, _fub_id = ctx

    raw_minutes = call.data.get("minutes")
    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        error = f"Invalid minutes value '{raw_minutes}' — expected a whole number."
        _LOGGER.info("Function Plan Preview Extend: minutes='%s' error='%s'", raw_minutes, error)
        return {"success": False, "error": error}
    if not _PREVIEW_EXTEND_MIN_MINUTES <= minutes <= _PREVIEW_EXTEND_MAX_MINUTES:
        error = f"minutes must be between {_PREVIEW_EXTEND_MIN_MINUTES} and {_PREVIEW_EXTEND_MAX_MINUTES}."
        _LOGGER.info("Function Plan Preview Extend: minutes=%s error='%s'", minutes, error)
        return {"success": False, "error": error}

    extended = coordinator.set_preview_auto_stop_extension(minutes)
    _LOGGER.info("Function Plan Preview Extend: minutes=%s extended=%s", minutes, extended)
    if not extended:
        return {"success": False, "error": "No live plan preview is currently armed."}
    return {"success": True, "minutes": minutes}


async def _search_plan_labels(
    coordinator: ComexioCoordinator,
    api: Any,
    fid: str,
    fub: dict,
    catalog: dict,
    markers_by_id: dict,
    webio_by_id: dict,
    ios_by_id: dict,
    matches_label: Any,
    login_state: dict[str, bool | None],
) -> tuple[list[str] | None, str | None]:
    """Search one plan's element labels for function_plan_search (extracted to stay under
    the complexity budget — mutable login_state carries the lazy, at-most-once login
    across the caller's loop). Returns (hits, None) or (None, failure_description).
    """
    # Prefer the bulk snapshot the backup cycle already loads in the background
    # (see coordinator._async_function_plan_backup_cycle) — a live reload of all plans
    # takes ~11s against the embedded Comexio server, which processes requests
    # serially regardless of client-side concurrency. Only plans missing from the
    # snapshot (e.g. right after startup) are fetched live.
    plan_data = coordinator.function_plan_plans.get(int(fid))
    if plan_data is None:
        if login_state["ok"] is None:
            login_state["ok"] = await api.login()
        plan_data = await api.function_plan_load_elements(int(fid)) if login_state["ok"] else None
    if not plan_data:
        return None, f"{fub.get('Name', '?')} (fub {fid})"
    labels = {
        # Comments carry multi-line text — flatten like the text visualization does.
        " ".join(resolve_element_label(elem, catalog, markers_by_id, webio_by_id, ios_by_id).split())
        for elem in plan_data.get("elements", {}).values()
    }
    return sorted(label for label in labels if matches_label(label)), None


def _build_search_notification_lines(
    query: str, results: list[dict], failed: list[str], plan_count: int, selector_set: bool, duration: float
) -> list[str]:
    """Build the function_plan_search notification body (extracted to stay under the
    complexity budget)."""
    max_shown = 15  # a broad wildcard can match hundreds of labels — keep the notification readable
    lines: list[str] = []
    for res in results:
        lines.append(f"**{res['plan_name']}** (fub {res['fub_id']}) — {len(res['matches'])} match(es):")
        lines += [f"- {label}" for label in res["matches"][:max_shown]]
        if len(res["matches"]) > max_shown:
            lines.append(f"- … and {len(res['matches']) - max_shown} more (full list in the service response)")
    if not results:
        lines.append(f"No element matching '{query}' found in any of the {plan_count} plans.")
    if failed:
        lines.append(f"\n**{len(failed)} plan(s) could not be loaded:** {', '.join(failed)}")
    if selector_set:
        lines.append(f"\nSingle plan hit — the 'Function Plans' selector was set to **{results[0]['plan_name']}**.")
    lines.append(f"\nDuration: {duration:.1f}s")
    return lines


async def _handle_function_plan_search(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Find which function plans contain elements matching a text/wildcard query.

    Searches the resolved human-readable labels of EVERY element (markers, IOs, WebIOs,
    blocks, time modules, constants, comments) in EVERY live plan — the same labels the
    SVG preview shows — using the preview card's search syntax (see _build_label_matcher).
    Answers "which plan is Mxx in?" without opening plans one by one.
    """
    query = str(call.data.get("query", "")).strip()
    if not query:
        persistent_notification.async_create(hass, "Empty 'query' — nothing to search for.", title=_TITLE_SEARCH_ERR)
        return None
    t_start = time.monotonic()
    # No upfront login: the cache-hit path below reads only in-memory/storage data.
    # Only the live fallback fetch needs the admin session — logged in lazily there.
    ctx = await _async_get_service_context(hass, call, _TITLE_SEARCH_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return None
    coordinator, api, _fub_id = ctx

    matches_label = _build_label_matcher(query)
    markers_by_id, webio_by_id, ios_by_id = coordinator.function_plan_label_maps()
    catalog = await coordinator.function_plan_catalog.async_get_catalog()

    plans = sorted(api.fub_data.items(), key=lambda kv: int(kv[0]))
    results: list[dict] = []
    failed: list[str] = []
    total_hits = 0
    login_state: dict[str, bool | None] = {"ok": None}  # lazy, at most one login attempt
    for fid, fub in plans:
        hits, failure = await _search_plan_labels(
            coordinator, api, fid, fub, catalog, markers_by_id, webio_by_id, ios_by_id, matches_label, login_state
        )
        if failure:
            failed.append(failure)
            continue
        if hits:
            total_hits += len(hits)
            results.append({"fub_id": int(fid), "plan_name": fub.get("Name", "?"), "matches": hits})

    duration = time.monotonic() - t_start
    # An unambiguous hit selects the plan right away (user wish): the next call
    # without fub_id (visualize/sort/…) then targets the plan just found.
    selector_set = len(results) == 1 and await _async_set_plan_selector(hass, coordinator, results[0]["plan_name"])
    _LOGGER.info(
        "Function Plan Search: query='%s' → %d match(es) in %d of %d plans, selector_set=%s (%.1fs)",
        query,
        total_hits,
        len(results),
        len(plans),
        selector_set,
        duration,
    )
    lines = _build_search_notification_lines(query, results, failed, len(plans), selector_set, duration)
    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title=f"Function Plan Search — '{query}': {total_hits} match(es) in {len(results)} plan(s)",
    )
    return {
        "query": query,
        "plan_count": len(results),
        "match_count": total_hits,
        "selector_set": selector_set,
        "results": results,
        "duration": round(duration, 1),
    }


async def _handle_set_value(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Write a raw value to a Comexio marker or IO via the Basic-Auth API.

    Backend of the plan card's debug command input ('M107=1' / 'IOX2#Q3=0') — target
    tokens match the labels the preview shows. Success is reported via the service
    response (the card logs it); only failures post a persistent notification, so
    rapid-fire debug commands don't flood the notification tray.
    """
    ctx = await _async_get_service_context(hass, call, _TITLE_SET_VALUE_ERR, resolve_plan=False, do_login=False)
    if ctx is None:
        return {"success": False, "error": _INSTANCE_NOT_RESOLVED_ERR}
    coordinator, api, _fub_id = ctx

    target = str(call.data.get("target", "")).strip()
    raw_value = str(call.data.get("value", "")).strip().replace(",", ".")
    try:
        value: float | int = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(raw_value)
    except ValueError:
        error = f"Invalid value '{raw_value}' — expected a finite number."
        kwargs = None
    else:
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # send '1', not '1.0', for digital targets
        kwargs, error = _resolve_set_value_target(target, value, coordinator.data or {})

    _LOGGER.info("Set Value: target='%s' value='%s'%s", target, raw_value, f" error='{error}'" if error else "")
    if kwargs is None:
        persistent_notification.async_create(hass, error, title=_TITLE_SET_VALUE_ERR)
        return {"success": False, "error": error}

    t_start = time.monotonic()
    success = await api.set_value(**kwargs, value=value)
    duration = time.monotonic() - t_start
    if not success:
        persistent_notification.async_create(
            hass,
            f"Write failed: {target} = {value}.\nDuration: {duration:.1f}s",
            title=_TITLE_SET_VALUE_ERR,
        )
    return {"success": success, "target": target, "value": value, "duration": round(duration, 2)}
