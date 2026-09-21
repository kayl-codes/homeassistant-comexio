# Version: 0.7.5
import asyncio
import logging

from homeassistant.components import persistent_notification
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.util import slugify
import voluptuous as vol

from .const import (
    CONF_ENABLE_NOTIFICATIONS,
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_KNX_DPT_SUFFIX_IGNORED,
    CONF_SERVER_ID,
    CONF_STATISTICS_CLEANUP_IGNORED,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DOMAIN,
    ICON_ADD,
    ICON_CLEANUP,
    ICON_DELETE,
    ICON_FIX,
    ICON_INFO,
    ICON_LINK,
    ICON_MUTE,
    ICON_NETWORK,
    ICON_PUZZLE,
    ICON_RENAME,
    ICON_ROCKET,
    ICON_SYNC,
    MARKER_READ_ONLY_SUFFIX,
    MARKER_TRIGGER_SUFFIXES,
    SOURCE_CATEGORIES,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_FUNCTION_PLAN_FINALIZE,
    SYNC_DURATION_FUNCTION_PLAN_PAIR,
    SYNC_DURATION_FUNCTION_PLAN_PLAN,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    WEBIO_CLASS_KNX,
    WebioClass,
    expand_ignored_marker_ids,
)

_LOGGER = logging.getLogger(__name__)

ACTION_FIX = "fix"
ACTION_IGNORE = "ignore"
ACTION_KNX_RO = "ro"
ACTION_KNX_TRIG = "trig"


def _is_knx_cluster_plan(plan_name: str) -> bool:
    """True if a cluster-plan name from detail['markers_by_plan'] is a KNX cluster plan.

    Marker and KNX cluster plans share that one dict (see
    coordinator._function_plan_missing_detail) — their plan-name strings never collide
    ("... - Marker [...]" vs "... - KNX [...]"), so the category is recovered from the name.
    """
    knx_label = SOURCE_CATEGORIES[WEBIO_CLASS_KNX].label
    return f" - {knx_label} [" in plan_name


def _add_knx_dpt_suffix_ignored_ids(existing_raw: str, new_ids: set[int]) -> str:
    """Merge newly-ignored KNX ids into the CONF_KNX_DPT_SUFFIX_IGNORED option string.

    Same comma/range-list format as CONF_IGNORED_MARKERS/CONF_IGNORED_KNX, reusing
    expand_ignored_marker_ids for parsing so the two stay in sync if that format ever changes.
    """
    knx_prefix = SOURCE_CATEGORIES[WebioClass.KNX].audit_key_prefix
    ids = expand_ignored_marker_ids(existing_raw, knx_prefix + knx_prefix.lower())
    ids.update(new_ids)
    return ",".join(f"{knx_prefix}{i}" for i in sorted(ids))


def _function_plan_gap_lines(lp_missing_c: int, detail: dict) -> list[str]:
    """Summary bullets for missing Function Plan wiring, split by marker/KNX-cluster vs.
    IO-extension gaps.

    A whole cluster with nothing wired (all markers/KNX objects of that ID range / all IOs
    of that extension missing) reads as "cluster plan missing/not yet created" instead of a
    bare gap count — this also covers a managed cluster plan that was deleted directly in
    Comexio. Issues created before the detail split carry no detail dict -> single legacy
    line.
    """
    if not detail:
        return [f"* {ICON_LINK} **Not wired in Function Plan:** {lp_missing_c}"]
    lines = []
    for plan_name, (gap, total) in detail.get("markers_by_plan", {}).items():
        if _is_knx_cluster_plan(plan_name):
            cluster_noun, item_noun, item_noun_cap = "KNX cluster plan", "KNX objects", "KNX objects"
        else:
            cluster_noun, item_noun, item_noun_cap = "Marker cluster plan", "markers", "Markers"
        if gap == total:
            lines.append(f"* {ICON_PUZZLE} **{cluster_noun} {plan_name} missing:** all {gap} {item_noun}")
        else:
            lines.append(f"* {ICON_LINK} **{item_noun_cap} not wired in {plan_name}:** {gap} of {total}")
    for ext, (gap, total) in detail.get("ios_by_ext", {}).items():
        if gap == total:
            lines.append(f"* {ICON_PUZZLE} **Extension {ext} not yet in an IO cluster plan:** all {gap} IOs")
        else:
            lines.append(f"* {ICON_PUZZLE} **Extension {ext} partially wired:** {gap} of {total} IOs missing")
    return lines


def _function_plan_option_label(lp_missing_c: int, detail: dict, eta: str) -> str:
    """Label of the add-missing repair action, worded by gap kind.

    Entirely missing cluster plans get "Add Function Plan(s)", whole extensions
    "Add extension(s) to Function Plan"; individual missing connections
    (partially wired plans/extensions, legacy issues) keep "Wire in Function Plan".
    """
    markers_by_plan = detail.get("markers_by_plan", {}) if detail else {}
    ios_by_ext = detail.get("ios_by_ext", {}) if detail else {}
    if not markers_by_plan and not ios_by_ext:
        return f"{ICON_LINK} Wire in Function Plan ({lp_missing_c}x{eta})"
    parts = []
    if marker_gap := sum(gap for name, (gap, _total) in markers_by_plan.items() if not _is_knx_cluster_plan(name)):
        parts.append(f"{marker_gap} markers")
    if knx_gap := sum(gap for name, (gap, _total) in markers_by_plan.items() if _is_knx_cluster_plan(name)):
        parts.append(f"{knx_gap} KNX objects")
    parts.extend(f"{ext} +{gap} IOs" for ext, (gap, _total) in ios_by_ext.items())
    detail_str = f"({', '.join(parts)},{eta})"
    all_whole = all(gap == total for gap, total in markers_by_plan.values()) and all(
        gap == total for gap, total in ios_by_ext.values()
    )
    if not all_whole:
        return f"{ICON_LINK} Wire in Function Plan {detail_str}"
    if markers_by_plan:
        noun = "Function Plan" if len(markers_by_plan) == 1 and not ios_by_ext else "Function Plans"
        return f"{ICON_PUZZLE} Add {noun} {detail_str}"
    noun = "extension" if len(ios_by_ext) == 1 else "extensions"
    return f"{ICON_PUZZLE} Add {noun} to Function Plan {detail_str}"


async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up the repairs platform."""
    return True


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None):
    """Create a flow to fix a repair issue."""
    return ComexioRepairFlow(issue_id, data)


class ComexioRepairFlow(RepairsFlow):
    """Handler for Comexio repair flows."""

    def __init__(self, issue_id: str, data: dict | None):
        self.issue_id = issue_id
        self.issue_data = data or {}

    async def async_step_init(self, user_input=None):
        _LOGGER.debug("async_step_init called: issue_id=%s, user_input=%s", self.issue_id, user_input)

        if "entity_id_mismatch" in self.issue_id:
            _LOGGER.debug("Routing to async_step_entity_id_fix")
            return await self.async_step_entity_id_fix()
        if "statistics_orphaned" in self.issue_id:
            _LOGGER.debug("Routing to async_step_statistics_cleanup")
            return await self.async_step_statistics_cleanup()
        if self.issue_id.startswith("uninstall_cleanup_"):
            _LOGGER.debug("Routing to async_step_uninstall_cleanup")
            return await self.async_step_uninstall_cleanup()
        if self.issue_id.startswith("knx_dpt_ambiguous_"):
            _LOGGER.debug("Routing to async_step_knx_dpt_suffix")
            return await self.async_step_knx_dpt_suffix()

        _LOGGER.debug("Routing to fallback async_step_select_action")
        return await self.async_step_select_action()

    async def async_step_statistics_cleanup(self, user_input=None):
        """Handle the orphaned-statistics cleanup repair flow."""
        entry_id = self.issue_data.get("entry_id")

        if user_input is not None:
            return await self._async_process_statistics_cleanup(entry_id, user_input["action"])

        return self._show_statistics_cleanup_form(entry_id)

    async def _async_process_statistics_cleanup(self, entry_id: str, action: str):
        """Handle the user's choice (ignore/fix) once the statistics-cleanup form was submitted."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if not entry:
            return self.async_abort(reason="entry_not_found")

        if action == ACTION_IGNORE:
            new_options = dict(entry.options)
            new_options[CONF_STATISTICS_CLEANUP_IGNORED] = True
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="Ignored", data={})

        return await self._async_clear_orphaned_statistics(entry_id)

    async def _async_clear_orphaned_statistics(self, entry_id: str):
        """Clear orphaned long-term statistics via the recorder and close the issue."""
        from homeassistant.components.recorder import get_instance

        coordinator = self.hass.data[DOMAIN].get(entry_id)
        if not coordinator:
            return self.async_abort(reason="entry_not_found")

        ids = list(coordinator.orphaned_statistics)
        if ids and "recorder" in self.hass.config.components:
            instance = get_instance(self.hass)
            instance.async_clear_statistics(ids)

        coordinator.orphaned_statistics = []
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        coordinator.async_set_updated_data(coordinator.data)

        is_de = self.hass.config.language == "de"
        title = f"{len(ids)} verwaiste Statistiken gelöscht" if is_de else f"{len(ids)} statistics cleaned up"
        return self.async_create_entry(
            title=title,
            data={},
        )

    def _show_statistics_cleanup_form(self, entry_id: str):
        """Render the confirmation form describing how many orphaned statistics were found."""
        coordinator = self.hass.data[DOMAIN].get(entry_id)
        count = len(coordinator.orphaned_statistics) if coordinator else self.issue_data.get("count", 0)

        return self.async_show_form(
            step_id="statistics_cleanup",
            description_placeholders={"count": count},
            data_schema=vol.Schema(
                {
                    vol.Required("action", default=ACTION_FIX): SelectSelector(
                        SelectSelectorConfig(
                            options=[ACTION_FIX, ACTION_IGNORE],
                            mode=SelectSelectorMode.LIST,
                            translation_key="statistics_cleanup_action",
                        )
                    )
                }
            ),
        )

    async def async_step_entity_id_fix(self, user_input=None):
        """Handle the entity_id migration repair flow."""
        entry_id = self.issue_data.get("entry_id")

        if user_input is not None:
            action = user_input["action"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry:
                return self.async_abort(reason="entry_not_found")

            if action == ACTION_IGNORE:
                new_options = dict(entry.options)
                new_options[CONF_ENTITY_ID_MIGRATION_IGNORED] = True
                self.hass.config_entries.async_update_entry(entry, options=new_options)
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(title="Ignored", data={})

            # action == "fix": run migration via coordinator
            coordinator = self.hass.data[DOMAIN].get(entry_id)
            if not coordinator:
                return self.async_abort(reason="entry_not_found")

            migrated = coordinator.async_migrate_entity_ids()
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            coordinator.async_set_updated_data(coordinator.data)

            is_de = self.hass.config.language == "de"
            title = f"Entitäts-IDs für {migrated} Einträge korrigiert" if is_de else f"{migrated} entity IDs fixed"
            return self.async_create_entry(
                title=title,
                data={},
            )

        coordinator = self.hass.data[DOMAIN].get(entry_id)
        count = len(coordinator.entity_id_mismatches) if coordinator else self.issue_data.get("count", 0)

        return self.async_show_form(
            step_id="entity_id_fix",
            description_placeholders={"count": count},
            data_schema=vol.Schema(
                {
                    vol.Required("action", default=ACTION_FIX): SelectSelector(
                        SelectSelectorConfig(
                            options=[ACTION_FIX, ACTION_IGNORE],
                            mode=SelectSelectorMode.LIST,
                            translation_key="entity_id_fix_action",
                        )
                    )
                }
            ),
        )

    async def async_step_select_action(self, user_input=None):
        """Handle the action selected by the user."""
        entry_id = self.issue_data.get("entry_id")
        coordinator = self.hass.data[DOMAIN].get(entry_id)

        # Lock menu if a sync is already in progress
        if coordinator and getattr(coordinator, "in_sync", False):
            return self.async_abort(reason="already_in_sync")

        if user_input is not None:
            action = user_input["action"]

            # Ensure the config entry is available for processing
            if not entry_id:
                return self.async_abort(reason="missing_entry_id")

            entry = self.hass.config_entries.async_get_entry(entry_id)

            if not entry:
                return self.async_abort(reason="entry_not_found")

            # Action to suppress future audit warnings
            if action == "ignore":
                new_options = dict(entry.options)
                new_options["audit_ignored"] = True

                self.hass.config_entries.async_update_entry(entry, options=new_options)

                # Remove the issue from the repair registry
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)

                return self.async_create_entry(title="Ignored", data={})

            # Trigger sync actions via the registered button service
            _LOGGER.warning(
                "Repair gestartet: issue_id=%s action=%s data=%s",
                self.issue_id,
                action,
                self.issue_data,
            )

            server_id = entry.data.get(CONF_SERVER_ID)

            if not server_id:
                return self.async_abort(reason="missing_server_id")

            # Resolve the entity ID for the sync button
            ent_reg = er.async_get(self.hass)
            sync_btn_uid = f"comexio_{server_id}_webio_sync_start_btn"
            btn_entity_id = ent_reg.async_get_entity_id("button", DOMAIN, sync_btn_uid)

            # Prepare service call for the sync action
            service_data = {
                "entity_id": btn_entity_id or f"button.comexio_{slugify(server_id)}_webio_sync_start",
                "action": action,
            }

            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "press_action",
                    service_data,
                    blocking=False,  # Use non-blocking to avoid UI timeout
                )

                coordinator = self.hass.data[DOMAIN][entry.entry_id]

                await asyncio.sleep(0.5)

                # Refresh coordinator data to verify the repair
                await coordinator.async_refresh()

                return self.async_create_entry(
                    title=f"Aktion '{action}' erfolgreich ausgeführt"
                    if self.hass.config.language == "de"
                    else f"Action '{action}' executed successfully",
                    data={},
                )

            except Exception as err:
                _LOGGER.exception("Repair Fehler: %s", err)
                return self.async_abort(reason="sync_failed")

        # Build the repair UI form
        is_de = self.hass.config.language == "de"
        issue_reg = ir.async_get(self.hass)
        issue = issue_reg.async_get_issue(DOMAIN, self.issue_id)

        # Fallback to empty dict if issue is not found
        placeholders = dict(issue.translation_placeholders if issue else {})

        # Check which translation key the current issue has
        is_missing_class = issue.translation_key == "missing_webio_class" if issue else False

        if is_missing_class:
            # Minimal options if one or both device classes are completely absent
            if is_de:
                options = {
                    "full_sync": f"{ICON_ROCKET} Initial Setup (fehlende Klasse(n) & Gerät(e) anlegen)",
                    "ignore": f"{ICON_MUTE} Nachricht ignorieren (Web-IO nicht nutzen)",
                }
            else:
                options = {
                    "full_sync": f"{ICON_ROCKET} Initial Setup (Create missing class(es) & device(s))",
                    "ignore": f"{ICON_MUTE} Ignore message (Do not use Web-IO)",
                }
            default_action = "full_sync"
        else:
            # Provide detailed Delta-Sync options based on audit counts
            counts = self.issue_data.get("counts", {})
            t_c = counts.get("type", 0)
            m_c = counts.get("missing", 0)
            r_c = counts.get("rename", 0)
            o_c = counts.get("orphan", 0)
            i_c = counts.get("ip_mismatch", 0)
            ce_c = counts.get("cleanup_entities", 0)
            lp_c = counts.get("cleanup_function_plan_count", 0)
            # Trigger-marker gaps/orphans are folded into the same counts: both are fixed by
            # the identical "function_plan_add_missing" action (button.py's _wire_trigger_pairs
            # runs alongside the normal Function Plan wiring pass), so they share its UI option
            # rather than needing a dedicated repair-dialog branch.
            lp_missing_c = counts.get("function_plan_missing", 0) + counts.get("function_plan_trigger_missing", 0)
            lp_dangling_c = counts.get("function_plan_dangling", 0) + counts.get("function_plan_trigger_orphan", 0)
            kb_c = counts.get("knx_bridge_missing", 0)
            # Phase 7: the API-Loopback fan-out gap is a distinct audit key from kb_c (a K-Element
            # can already have its bridge Marker but still lack the loopback sink — see
            # coordinator's knx_bridge_loopback_missing docstring), but button.py's
            # knx_bridge_add_missing action fixes both legs (plus the read path, if still open)
            # in one combined run (_wire_knx_full), so they share the same specific_options
            # entry below. The ETA below is based on kb_c+kb_lb_c only — it doesn't additionally
            # account for a read-path leg this same action would also close, since that gap is
            # tracked/estimated separately under its own function_plan_missing issue.
            kb_lb_c = counts.get("knx_bridge_loopback_missing", 0)
            lp_detail = self.issue_data.get("function_plan_missing_detail") or {}
            # Fallback for stale issues created before the coordinator started storing an
            # exact estimate: approximate the affected-plan count from the detail split (one
            # finalize cost per marker-cluster plan / IO extension) instead of a flat single
            # SYNC_DURATION_FUNCTION_PLAN_FINALIZE, which would undercount multi-plan repairs.
            affected_plan_count = (
                max(1, len(lp_detail.get("markers_by_plan", {})) + len(lp_detail.get("ios_by_ext", {})))
                if lp_detail
                else 1
            )
            lp_missing_eta_sec = counts.get(
                "function_plan_missing_eta_sec",
                lp_missing_c * SYNC_DURATION_FUNCTION_PLAN_PAIR
                + SYNC_DURATION_FUNCTION_PLAN_FINALIZE * affected_plan_count,
            )

            config_issues = t_c + m_c + r_c + o_c + ce_c + lp_missing_c + lp_dangling_c + kb_c + kb_lb_c
            ha_count = placeholders.get("ha_count", "0")
            com_count = placeholders.get("com_count", "0")

            # Helper function for time calculation
            def format_time(sec):
                if sec == 0:
                    return ""
                if sec < 60:
                    return f" ~{sec}s"
                return f" ~{sec // 60}:{sec % 60:02d} min"

            def get_time_for_count(c, is_delete=False):
                return format_time(c * (SYNC_DURATION_DELETE if is_delete else SYNC_DURATION_WRITE))

            # 1. Calculate Full Sync ETA first to decide on the hint visibility
            total_write = t_c + r_c + m_c
            total_del = o_c + ce_c + lp_dangling_c
            total_sec = (total_write * SYNC_DURATION_WRITE) + (total_del * SYNC_DURATION_DELETE)
            # Always add IP duration if a mismatch exists, as it's a separate call in Delta-Sync
            if i_c > 0:
                total_sec += SYNC_DURATION_WRITE
            if lp_c > 0:
                total_sec += lp_c * SYNC_DURATION_FUNCTION_PLAN_PLAN
            if lp_missing_c > 0:
                total_sec += lp_missing_eta_sec
            if kb_c > 0:
                total_sec += kb_c * SYNC_DURATION_WRITE
            if kb_lb_c > 0:
                total_sec += kb_lb_c * SYNC_DURATION_WRITE

            # Build the dynamic summary text
            if config_issues == 0 and i_c > 0:
                # CASE 1: Only IP mismatch
                if is_de:
                    summary = (
                        "Die Analyse hat festgestellt, dass die Server-Adresse (IP:Port) im "
                        "Comexio Web-IO Gerät nicht mit der aktuellen Adresse von Home Assistant "
                        "übereinstimmt. Dies verhindert den Empfang von Status-Updates (Webhooks)."
                    )
                else:
                    summary = (
                        "The analysis found that the server address (IP:Port) in the Comexio "
                        "Web-IO device does not match the current Home Assistant address. "
                        "This prevents receiving status updates via webhooks."
                    )
            else:
                # CASE 2: Configuration issues (with optional IP mismatch)
                if is_de:
                    header = "Die Analyse hat Abweichungen festgestellt:\n\n"
                else:
                    header = "The analysis has detected differences:\n\n"
                lines = []
                if t_c > 0:
                    lines.append(f"* {ICON_FIX} **{'Typ-Konflikte' if is_de else 'Type conflicts'}:** {t_c}")
                if m_c > 0:
                    lines.append(f"* {ICON_ADD} **{'Fehlend' if is_de else 'Missing'}:** {m_c}")
                if r_c > 0:
                    lines.append(f"* {ICON_RENAME} **{'Umbenannt' if is_de else 'Renamed'}:** {r_c}")
                if o_c > 0:
                    lines.append(f"* {ICON_DELETE} **{'Verwaist' if is_de else 'Orphaned'}:** {o_c}")
                if ce_c > 0:
                    lines.append(
                        f"* {ICON_CLEANUP} "
                        f"**{'Ignorierte Merker/KNX-Objekte aufräumen' if is_de else 'Ignored marker/KNX cleanup'}:** "
                        f"{ce_c}"
                    )
                if lp_missing_c > 0:
                    lines.extend(_function_plan_gap_lines(lp_missing_c, lp_detail))
                if lp_dangling_c > 0:
                    lines.append(
                        f"* {ICON_DELETE} "
                        f"**{'Verwaiste Plan-Elemente' if is_de else 'Function Plan debris'}:** {lp_dangling_c}"
                    )
                if kb_c > 0:
                    lines.append(
                        f"* {ICON_LINK} "
                        f"**{'KNX ohne Brücken-Merker' if is_de else 'KNX without bridge Marker'}:** {kb_c}"
                    )
                if kb_lb_c > 0:
                    lines.append(
                        f"* {ICON_LINK} "
                        f"**{'KNX-Brücken ohne Rückkopplung' if is_de else 'KNX bridges without feedback loop'}:** "
                        f"{kb_lb_c}"
                    )
                if i_c > 0:
                    lines.append(
                        f"* {ICON_NETWORK} **{'Server-Adresse' if is_de else 'Server Address'}:** "
                        f"{'Falsch' if is_de else 'Incorrect'}"
                    )

                if is_de:
                    footer = f"\n\nWeb-IO-Befehle: {ha_count} (HA) zu {com_count} (Comexio)"
                else:
                    footer = f"\n\nWeb-IO commands: {ha_count} (HA) vs {com_count} (Comexio)"
                # Function Plan items (missing/debris) never move this count — they're plan
                # elements, not Web-IO commands — so make the scope explicit whenever they're
                # part of the picture, otherwise a matching total reads as "nothing's wrong".
                # "  \n" is a CommonMark hard line break, keeping the note on its own line.
                if lp_missing_c > 0 or lp_dangling_c > 0:
                    footer += (
                        "  \n*(ohne Funktionsplan-Abweichungen)*"
                        if is_de
                        else "  \n*(excluding Function Plan differences)*"
                    )
                else:
                    footer += "."

                prompt = "Bitte wähle eine Aktion aus:" if is_de else "Please select an action:"
                summary = header + "\n".join(lines) + footer + "\n\n" + prompt

                # Append Fast-Track Note only if config issues exist
                if total_sec > SYNC_DURATION_RECREATE:
                    if is_de:
                        summary += (
                            f"\n\n{ICON_INFO} **Hinweis:** Die Integration prüft automatisch, ob das Web-IO Gerät "
                            "in Comexio ungenutzt ist. Falls ja, wird unabhängig von der gewählten "
                            f"Option eine **Neu-Einrichtung** durchgeführt, die in "
                            f"(~{SYNC_DURATION_RECREATE} Sek.) erledigt ist."
                        )
                    else:
                        summary += (
                            f"\n\n{ICON_INFO} **Note:** The integration automatically checks if the Web-IO device "
                            "in Comexio is currently unused. If so, a **fresh setup** will be "
                            f"performed regardless of the selected option, which will be "
                            f"completed in (~{SYNC_DURATION_RECREATE} sec.)."
                        )

            placeholders["summary_text"] = summary

            # Collect specific options with timestamps
            specific_options = {}

            if counts.get("type", 0) > 0:
                t = get_time_for_count(counts["type"])
                label = f"{ICON_FIX} Typen korrigieren" if is_de else f"{ICON_FIX} Update Types Only"
                specific_options["update_types"] = f"{label} ({counts['type']}x == {t})"

            if counts.get("missing", 0) > 0:
                t = get_time_for_count(counts["missing"])
                label = f"{ICON_ADD} Fehlende anlegen" if is_de else f"{ICON_ADD} Create Missing Only"
                specific_options["create_missing"] = f"{label} ({counts['missing']}x == {t})"

            if counts.get("rename", 0) > 0:
                t = get_time_for_count(counts["rename"])
                label = f"{ICON_RENAME} Namen aktualisieren" if is_de else f"{ICON_RENAME} Update Names Only"
                specific_options["update_renames"] = f"{label} ({counts['rename']}x == {t})"

            if counts.get("orphan", 0) > 0:
                t = get_time_for_count(counts["orphan"], is_delete=True)
                label = f"{ICON_DELETE} Waisen löschen" if is_de else f"{ICON_DELETE} Delete Orphans Only"
                specific_options["delete_orphans"] = f"{label} ({counts['orphan']}x == {t})"

            if ce_c > 0:
                ce_del_t = get_time_for_count(ce_c, is_delete=True)
                lp_eta = format_time(lp_c * SYNC_DURATION_FUNCTION_PLAN_PLAN) if lp_c > 0 else ""
                label = (
                    f"{ICON_CLEANUP} Ignorierte Merker/KNX-Objekte aufräumen (Entitäten + Function Plan + WebIO)"
                    if is_de
                    else f"{ICON_CLEANUP} Cleanup Ignored Markers/KNX (entities + Function Plan + WebIO)"
                )
                specific_options["cleanup_entities"] = f"{label} ({ce_c}x{ce_del_t}{lp_eta})"

            if lp_missing_c > 0:
                lp_add_eta = format_time(lp_missing_eta_sec)
                specific_options["function_plan_add_missing"] = _function_plan_option_label(
                    lp_missing_c, lp_detail, lp_add_eta
                )

            if kb_c > 0 or kb_lb_c > 0:
                # One action fixes both legs of Entwurf A "Merker-Brücke" in a single run
                # (button.py's knx_bridge_add_missing, via _wire_knx_full — also closes the
                # read path if that's open too) — share the combined count/ETA rather than a
                # second dialog option, since a user pressing this expects "KNX write path
                # fixed", not two separate near-identical buttons.
                kb_total = kb_c + kb_lb_c
                kb_t = get_time_for_count(kb_total)
                label = (
                    f"{ICON_LINK} Brücken-Merker + Rückkopplung für KNX anlegen"
                    if is_de
                    else f"{ICON_LINK} Create KNX bridge Markers + feedback loop"
                )
                specific_options["knx_bridge_add_missing"] = f"{label} ({kb_total}x{kb_t})"

            if counts.get("ip_mismatch", 0) > 0:
                if is_de:
                    label = f"{ICON_NETWORK} HA Server-Adresse (IP:Port) aktualisieren"
                else:
                    label = f"{ICON_NETWORK} Update HA Server Address (IP:Port)"
                specific_options["update_ip"] = f"{label} (~{SYNC_DURATION_WRITE}s)"

            # Calculate Full Sync ETA
            total_write = counts.get("type", 0) + counts.get("rename", 0) + counts.get("missing", 0)
            total_del = counts.get("orphan", 0) + ce_c
            total_sec = (total_write * SYNC_DURATION_WRITE) + (total_del * SYNC_DURATION_DELETE)

            # Always add IP duration if a mismatch exists (Delta Sync requires explicit update)
            if counts.get("ip_mismatch", 0) > 0:
                total_sec += SYNC_DURATION_WRITE
            if lp_c > 0:
                total_sec += lp_c * SYNC_DURATION_FUNCTION_PLAN_PLAN
            if lp_missing_c > 0:
                total_sec += lp_missing_eta_sec
            if kb_c > 0:
                total_sec += kb_c * SYNC_DURATION_WRITE
            if kb_lb_c > 0:
                total_sec += kb_lb_c * SYNC_DURATION_WRITE

            t_full = format_time(total_sec)

            options = {}
            if len(specific_options) > 1:
                label = f"{ICON_SYNC} Full Sync ({'Alles korrigieren' if is_de else 'Fix everything'}, {t_full})"
                options["full_sync"] = label
                default_action = "full_sync"
            else:
                default_action = next(iter(specific_options)) if specific_options else "full_sync"

            options.update(specific_options)

            # Fallback
            if not options:
                label = (
                    f"{ICON_SYNC} Full Sync (Alles korrigieren)" if is_de else f"{ICON_SYNC} Full Sync (Fix everything)"
                )
                options["full_sync"] = label
                default_action = "full_sync"

        return self.async_show_form(
            step_id="select_action",
            description_placeholders=placeholders,
            data_schema=vol.Schema({vol.Required("action", default=default_action): vol.In(options)}),
        )

    async def async_step_uninstall_cleanup(self, user_input=None):
        """Handle the uninstall/cleanup repair flow (test button).

        Tears down everything the integration created in Comexio: HA-managed Function
        Plans, then the Web-IO device instances, then the Web-IO device classes. This
        is destructive and not reversible, so — unlike the other fix flows — the
        default choice is 'ignore', not 'fix'.
        """
        entry_id = self.issue_data.get("entry_id")

        if user_input is not None:
            action = user_input["action"]

            if action == "ignore":
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(title="Cancelled", data={})

            if not entry_id:
                return self.async_abort(reason="missing_entry_id")

            # action == "fix": several sequential Comexio round-trips (one per plan,
            # device and class) can add up past the repair dialog's UI timeout — same
            # reasoning as the sync button's press_action, which also runs non-blocking
            # instead of being awaited inline here.
            entry = self.hass.config_entries.async_get_entry(entry_id)
            coordinator = self.hass.data[DOMAIN].get(entry_id)
            if not coordinator or not entry:
                return self.async_abort(reason="entry_not_found")

            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            self.hass.async_create_task(self._async_run_cleanup(coordinator, entry))

            return self.async_create_entry(title="Cleanup started in background", data={})

        plan_count = self.issue_data.get("plan_count", 0)
        device_count = self.issue_data.get("device_count", 0)
        class_count = self.issue_data.get("class_count", 0)

        options = {
            "fix": f"Delete now ({plan_count} plans, {device_count} devices, {class_count} classes)",
            "ignore": "Cancel",
        }

        return self.async_show_form(
            step_id="uninstall_cleanup",
            description_placeholders={
                "plan_count": str(plan_count),
                "device_count": str(device_count),
                "class_count": str(class_count),
            },
            data_schema=vol.Schema({vol.Required("action", default="ignore"): vol.In(options)}),
        )

    async def _async_run_cleanup(self, coordinator, entry: ConfigEntry) -> None:
        """Background task: run the actual teardown, reload the integration so it
        picks up a clean state, and report the result.

        Split out of async_step_uninstall_cleanup so the repair dialog can close
        immediately instead of blocking on multiple sequential Comexio calls. The
        reload only runs on success — the plans/devices/classes deleted in Comexio
        would otherwise leave stale entities and a stale plan_map behind until the
        next scheduled poll or manual reload. On failure we skip the reload so the
        integration keeps running against its last-known-good state instead of
        masking the failure behind a fresh reload.
        """
        conf = {**entry.data, **entry.options}
        notify_enabled = conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS)
        notif_id = f"comexio_uninstall_cleanup_{coordinator.server_id}"
        result = None
        succeeded = False

        try:
            result = await coordinator.async_uninstall_cleanup()
            succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("[%s] Uninstall cleanup failed", coordinator.server_id)
            if notify_enabled:
                persistent_notification.async_create(
                    self.hass,
                    "The cleanup task failed unexpectedly. Check the log for details.",
                    title=f"Comexio Uninstall Cleanup Failed ({coordinator.server_id})",
                    notification_id=notif_id,
                )

        if succeeded and notify_enabled:
            if result:
                msg = (
                    f"Cleanup finished\n\n"
                    f"Plans removed: {len(result['deleted_plans'])} (failed: {len(result['failed_plans'])})\n"
                    f"Devices removed: {len(result['deleted_devices'])}\n"
                    f"Classes removed: {len(result['deleted_classes'])} (failed: {len(result['failed_classes'])})\n"
                    f"Skipped (still in use): {len(result['skipped'])}\n\n"
                    f"Reloading the integration now..."
                )
            else:
                msg = "Cleanup finished, but returned no result data. Check the log for details."
            persistent_notification.async_create(
                self.hass,
                msg,
                title=f"Comexio Uninstall Cleanup ({coordinator.server_id})",
                notification_id=notif_id,
            )

        if not succeeded:
            _LOGGER.warning(
                "[%s] Uninstall cleanup failed — skipping reload to keep the integration on its last-known-good state.",
                coordinator.server_id,
            )
            return

        # Give a listener-triggered reload from _persist_plan_map's options write (R2) a
        # moment to see the skip flag and return before we force our own explicit reload.
        await asyncio.sleep(0.5)
        _LOGGER.info("[%s] Reloading integration after uninstall cleanup...", coordinator.server_id)
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def async_step_knx_dpt_suffix(self, user_input=None):
        """Handle the KNX DPT1.x ambiguous-classification repair flow.

        One item at a time (rather than one combined form with a per-item field) because
        the dynamic per-K-id field names a combined form would need have no static
        translation entries — HA's selector translations only cover fixed field/step names.
        self._knx_dpt_all_items/_knx_dpt_items/_knx_dpt_resolved are flow-instance state that
        persists across the repeated calls to this same step as the user works through the list.
        """
        if not hasattr(self, "_knx_dpt_items"):
            self._knx_dpt_all_items: dict[str, dict] = {
                str(item["id"]): item for item in self.issue_data.get("items", [])
            }
            self._knx_dpt_items: list[dict] = list(self.issue_data.get("items", []))
            self._knx_dpt_resolved: dict[str, str] = {}

        if user_input is not None and self._knx_dpt_items:
            current = self._knx_dpt_items.pop(0)
            self._knx_dpt_resolved[str(current["id"])] = user_input["action"]

        if self._knx_dpt_items:
            current = self._knx_dpt_items[0]
            return self.async_show_form(
                step_id="knx_dpt_suffix",
                description_placeholders={
                    "name": current["name"],
                    "remaining": str(len(self._knx_dpt_items) - 1),
                },
                data_schema=vol.Schema(
                    {
                        # Defaults to "ignore" (skip), not a rename action: this form pops
                        # to the next K-id immediately after submit, so a default that
                        # commits a rename (RO/Trigger) risks misclassifying an object if
                        # the user clicks/confirms through the list too quickly. Skipping
                        # is the safe no-op — it can be revisited later, a wrong suffix
                        # rename cannot be undone by this flow.
                        vol.Required("action", default=ACTION_IGNORE): SelectSelector(
                            SelectSelectorConfig(
                                options=[ACTION_KNX_RO, ACTION_KNX_TRIG, ACTION_IGNORE],
                                mode=SelectSelectorMode.LIST,
                                translation_key="knx_dpt_suffix_action",
                            )
                        )
                    }
                ),
            )

        return await self._async_apply_knx_dpt_suffix()

    async def _async_apply_knx_dpt_suffix(self):
        """Apply every classification collected by async_step_knx_dpt_suffix and close the issue."""
        entry_id = self.issue_data.get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        coordinator = self.hass.data[DOMAIN].get(entry_id)
        if not entry or not coordinator:
            return self.async_abort(reason="entry_not_found")

        suffix_by_action = {ACTION_KNX_RO: MARKER_READ_ONLY_SUFFIX, ACTION_KNX_TRIG: MARKER_TRIGGER_SUFFIXES[0]}
        renamed_count = 0
        failed: list[str] = []
        newly_ignored: set[int] = set()

        for k_id, action in self._knx_dpt_resolved.items():
            if action == ACTION_IGNORE:
                newly_ignored.add(int(k_id))
                continue
            item = self._knx_dpt_all_items[k_id]
            new_title = f"{item['title']} {suffix_by_action[action]}"
            if await coordinator.api.rename_knx_object(k_id, new_title):
                renamed_count += 1
            else:
                failed.append(item["name"])

        if newly_ignored:
            # R2: request_options_update_without_reload (not a plain async_update_entry) so
            # the listener-triggered reload it schedules doesn't race the explicit reload
            # below (renamed_count case) or fire redundantly (ignore-only case, where the
            # coordinator picks up the new option on its next regular poll without one).
            new_options = dict(entry.options)
            new_options[CONF_KNX_DPT_SUFFIX_IGNORED] = _add_knx_dpt_suffix_ignored_ids(
                new_options.get(CONF_KNX_DPT_SUFFIX_IGNORED, ""), newly_ignored
            )
            coordinator.request_options_update_without_reload(new_options)

        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        if renamed_count:
            # A rename flips the item's MarkerKind, which changes which entity platform it
            # belongs to (e.g. switch -> sensor) — entities are only built once, in each
            # platform's async_setup_entry, so a reload (not just a coordinator refresh) is
            # required for it to actually appear correctly. Mirrors button.py's sync-button
            # reload (R2): give the listener a moment to see the skip flag before forcing our
            # own explicit reload.
            await asyncio.sleep(0.5)
            await self.hass.config_entries.async_reload(entry.entry_id)
        elif newly_ignored:
            await coordinator.async_refresh()

        is_de = self.hass.config.language == "de"
        if is_de:
            title = f"{renamed_count} umbenannt, {len(newly_ignored)} unverändert gelassen"
        else:
            title = f"{renamed_count} renamed, {len(newly_ignored)} left unchanged"
        if failed:
            # Name each failed item, not just a count — otherwise, in a multi-item batch, the
            # user has no way to tell which one still needs attention short of grepping the
            # log for rename_knx_object's error line. A failed item stays dpt_ambiguous and
            # reappears in the next poll's audit, so nothing is silently lost — just re-open
            # the issue and classify it again.
            names = ", ".join(failed)
            title += f" ({len(failed)} fehlgeschlagen: {names})" if is_de else f" ({len(failed)} failed: {names})"

        return self.async_create_entry(title=title, data={})
