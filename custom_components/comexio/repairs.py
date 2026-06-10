# Version: 0.7.5
import asyncio
import logging

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import voluptuous as vol

from .const import (
    CONF_ENTITY_ID_MIGRATION_IGNORED,
    CONF_SERVER_ID,
    CONF_STATISTICS_CLEANUP_IGNORED,
    DOMAIN,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
)

_LOGGER = logging.getLogger(__name__)


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
        if "entity_id_mismatch" in self.issue_id:
            return await self.async_step_entity_id_fix()
        if "statistics_orphaned" in self.issue_id:
            return await self.async_step_statistics_cleanup()
        return await self.async_step_select_action()

    async def async_step_statistics_cleanup(self, user_input=None):
        """Handle the orphaned-statistics cleanup repair flow."""
        entry_id = self.issue_data.get("entry_id")

        if user_input is not None:
            action = user_input["action"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry:
                return self.async_abort(reason="entry_not_found")

            if action == "ignore":
                new_options = dict(entry.options)
                new_options[CONF_STATISTICS_CLEANUP_IGNORED] = True
                self.hass.config_entries.async_update_entry(entry, options=new_options)
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(title="Ignored", data={})

            # action == "fix": clear orphaned statistics via the recorder
            from homeassistant.components.recorder import get_instance

            coordinator = self.hass.data[DOMAIN].get(entry_id)
            if not coordinator:
                return self.async_abort(reason="entry_not_found")

            ids = list(coordinator.orphaned_statistics)
            if ids and "recorder" in self.hass.config.components:
                get_instance(self.hass).async_clear_statistics(ids)

            coordinator.orphaned_statistics = []
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            coordinator.async_set_updated_data(coordinator.data)

            is_de = self.hass.config.language == "de"
            title = f"{len(ids)} verwaiste Statistiken gelöscht" if is_de else f"{len(ids)} orphaned statistics deleted"
            return self.async_create_entry(title=title, data={})

        is_de = self.hass.config.language == "de"
        coordinator = self.hass.data[DOMAIN].get(entry_id)
        count = len(coordinator.orphaned_statistics) if coordinator else self.issue_data.get("count", 0)

        if is_de:
            description = (
                f"Es wurden **{count} verwaiste Langzeit-Statistiken** gefunden, die zu keiner "
                "existierenden Entität mehr gehören (Überreste früherer Entity-IDs).\n\n"
                "Diese können gefahrlos gelöscht werden. **Laufende Sensoren und deren aktuelle "
                "Historie sind nicht betroffen** — nur die verwaisten Einträge werden entfernt.\n\n"
                "Möchtest du sie jetzt löschen?"
            )
            options = {
                "fix": f"🧹 Jetzt bereinigen ({count} Statistiken)",
                "ignore": "🔇 Meldung unterdrücken",
            }
        else:
            description = (
                f"**{count} orphaned long-term statistics** were found that no longer belong to "
                "any existing entity (leftovers from previous entity IDs).\n\n"
                "They can be safely deleted. **Live sensors and their current history are not "
                "affected** — only the orphaned entries are removed.\n\n"
                "Would you like to delete them now?"
            )
            options = {
                "fix": f"🧹 Clean up now ({count} statistics)",
                "ignore": "🔇 Suppress this message",
            }

        return self.async_show_form(
            step_id="statistics_cleanup",
            description_placeholders={"description": description},
            data_schema=vol.Schema({vol.Required("action", default="fix"): vol.In(options)}),
        )

    async def async_step_entity_id_fix(self, user_input=None):
        """Handle the entity_id migration repair flow."""
        entry_id = self.issue_data.get("entry_id")

        if user_input is not None:
            action = user_input["action"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry:
                return self.async_abort(reason="entry_not_found")

            if action == "ignore":
                new_options = dict(entry.options)
                new_options[CONF_ENTITY_ID_MIGRATION_IGNORED] = True
                self.hass.config_entries.async_update_entry(entry, options=new_options)
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(title="Ignored", data={})

            # action == "fix": run migration via coordinator
            coordinator = self.hass.data[DOMAIN].get(entry_id)
            if not coordinator:
                return self.async_abort(reason="entry_not_found")

            ent_reg = er.async_get(self.hass)
            migrated = 0
            for mismatch in list(coordinator.entity_id_mismatches):
                try:
                    ent_reg.async_update_entity(mismatch["current_id"], new_entity_id=mismatch["corrected_id"])
                    migrated += 1
                except Exception:
                    _LOGGER.exception("Failed to migrate entity_id %s", mismatch["current_id"])

            coordinator.entity_id_mismatches = []
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            coordinator.async_set_updated_data(coordinator.data)

            is_de = self.hass.config.language == "de"
            return self.async_create_entry(
                title=f"{migrated} Entity-IDs korrigiert" if is_de else f"{migrated} entity IDs fixed",
                data={},
            )

        is_de = self.hass.config.language == "de"
        coordinator = self.hass.data[DOMAIN].get(entry_id)
        count = len(coordinator.entity_id_mismatches) if coordinator else self.issue_data.get("count", 0)

        if is_de:
            description = (
                f"Es wurden **{count} Entitäten** mit doppeltem Server-ID-Präfix gefunden.\n\n"
                "Die Entity-IDs können automatisch korrigiert werden. "
                "**History und Automationen bleiben erhalten**, sofern sie die Entity-ID direkt referenzieren — "
                "bitte prüfe Automationen nach der Migration.\n\n"
                "Möchtest du die IDs jetzt korrigieren?"
            )
            options = {
                "fix": f"✅ Jetzt korrigieren ({count} Entity-IDs)",
                "ignore": "🔇 Meldung unterdrücken",
            }
        else:
            description = (
                f"**{count} entities** were found with a duplicate server-ID prefix in their entity_id.\n\n"
                "The entity IDs can be corrected automatically. "
                "**History and automations are preserved**, but please review automations "
                "that reference these entity IDs directly.\n\n"
                "Would you like to fix the IDs now?"
            )
            options = {
                "fix": f"✅ Fix now ({count} entity IDs)",
                "ignore": "🔇 Suppress this message",
            }

        return self.async_show_form(
            step_id="entity_id_fix",
            description_placeholders={"description": description},
            data_schema=vol.Schema({vol.Required("action", default="fix"): vol.In(options)}),
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
                "entity_id": btn_entity_id or f"button.comexio_{server_id}_webio_sync_start",
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
            # Minimal options if the device class is completely absent
            if is_de:
                options = {
                    "full_sync": "🚀 Initial Setup (Klasse & Gerät anlegen)",
                    "ignore": "🔇 Nachricht ignorieren (Web-IO nicht nutzen)",
                }
            else:
                options = {
                    "full_sync": "🚀 Initial Setup (Create class & device)",
                    "ignore": "🔇 Ignore message (Do not use Web-IO)",
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

            config_issues = t_c + m_c + r_c + o_c
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
            total_del = o_c
            total_sec = (total_write * SYNC_DURATION_WRITE) + (total_del * SYNC_DURATION_DELETE)
            # Always add IP duration if a mismatch exists, as it's a separate call in Delta-Sync
            if i_c > 0:
                total_sec += SYNC_DURATION_WRITE

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
                    lines.append(f"* 🔧 **{'Typ-Konflikte' if is_de else 'Type conflicts'}:** {t_c}")
                if m_c > 0:
                    lines.append(f"* ➕ **{'Fehlend' if is_de else 'Missing'}:** {m_c}")
                if r_c > 0:
                    lines.append(f"* ✏️ **{'Umbenannt' if is_de else 'Renamed'}:** {r_c}")
                if o_c > 0:
                    lines.append(f"* 🗑️ **{'Verwaist' if is_de else 'Orphaned'}:** {o_c}")
                if i_c > 0:
                    lines.append(
                        f"* 🌐 **{'Server-Adresse' if is_de else 'Server Address'}:** "
                        f"{'Falsch' if is_de else 'Incorrect'}"
                    )

                if is_de:
                    footer = f"\n\nGesamt: {ha_count} (HA) zu {com_count} (Comexio)."
                else:
                    footer = f"\n\nTotal: {ha_count} (HA) vs {com_count} (Comexio)."

                prompt = "Bitte wähle eine Aktion aus:" if is_de else "Please select an action:"
                summary = header + "\n".join(lines) + footer + "\n\n" + prompt

                # Append Fast-Track Note only if config issues exist
                if total_sec > SYNC_DURATION_RECREATE:
                    if is_de:
                        summary += (
                            "\n\n💡 **Hinweis:** Die Integration prüft automatisch, ob das Web-IO Gerät "
                            "in Comexio ungenutzt ist. Falls ja, wird unabhängig von der gewählten "
                            f"Option eine **Neu-Einrichtung** durchgeführt, die in "
                            f"(~{SYNC_DURATION_RECREATE} Sek.) erledigt ist."
                        )
                    else:
                        summary += (
                            "\n\n💡 **Note:** The integration automatically checks if the Web-IO device "
                            "in Comexio is currently unused. If so, a **fresh setup** will be "
                            f"performed regardless of the selected option, which will be "
                            f"completed in (~{SYNC_DURATION_RECREATE} sec.)."
                        )

            placeholders["summary_text"] = summary

            # Collect specific options with timestamps
            specific_options = {}

            if counts.get("type", 0) > 0:
                t = get_time_for_count(counts["type"])
                label = "🔧 Typen korrigieren" if is_de else "🔧 Update Types Only"
                specific_options["update_types"] = f"{label} ({counts['type']}x == {t})"

            if counts.get("missing", 0) > 0:
                t = get_time_for_count(counts["missing"])
                label = "➕ Fehlende anlegen" if is_de else "➕ Create Missing Only"
                specific_options["create_missing"] = f"{label} ({counts['missing']}x == {t})"

            if counts.get("rename", 0) > 0:
                t = get_time_for_count(counts["rename"])
                label = "✏️ Namen aktualisieren" if is_de else "✏️ Update Names Only"
                specific_options["update_renames"] = f"{label} ({counts['rename']}x == {t})"

            if counts.get("orphan", 0) > 0:
                t = get_time_for_count(counts["orphan"], is_delete=True)
                label = "🗑️ Waisen löschen" if is_de else "🗑️ Delete Orphans Only"
                specific_options["delete_orphans"] = f"{label} ({counts['orphan']}x == {t})"

            if counts.get("ip_mismatch", 0) > 0:
                if is_de:
                    label = "🌐 HA Server-Adresse (IP:Port) aktualisieren"
                else:
                    label = "🌐 Update HA Server Address (IP:Port)"
                specific_options["update_ip"] = f"{label} (~{SYNC_DURATION_WRITE}s)"

            # Calculate Full Sync ETA
            total_write = counts.get("type", 0) + counts.get("rename", 0) + counts.get("missing", 0)
            total_del = counts.get("orphan", 0)
            total_sec = (total_write * SYNC_DURATION_WRITE) + (total_del * SYNC_DURATION_DELETE)

            # Always add IP duration if a mismatch exists (Delta Sync requires explicit update)
            if counts.get("ip_mismatch", 0) > 0:
                total_sec += SYNC_DURATION_WRITE

            t_full = format_time(total_sec)

            options = {}
            if len(specific_options) > 1:
                label = f"🔄 Full Sync ({'Alles korrigieren' if is_de else 'Fix everything'}, {t_full})"
                options["full_sync"] = label
                default_action = "full_sync"
            else:
                default_action = next(iter(specific_options)) if specific_options else "full_sync"

            options.update(specific_options)

            # Fallback
            if not options:
                label = "🔄 Full Sync (Alles korrigieren)" if is_de else "🔄 Full Sync (Fix everything)"
                options["full_sync"] = label
                default_action = "full_sync"

        return self.async_show_form(
            step_id="select_action",
            description_placeholders=placeholders,
            data_schema=vol.Schema({vol.Required("action", default=default_action): vol.In(options)}),
        )
