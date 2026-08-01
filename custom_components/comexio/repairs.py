# Version: 0.7.5
import asyncio
import logging
import re

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
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
_MARKER_ID_SUFFIX_RE = re.compile(r"(\d+)")


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
        if "ignored_markers_cleanup" in self.issue_id:
            _LOGGER.debug("Routing to async_step_ignored_markers_cleanup")
            return await self.async_step_ignored_markers_cleanup()
        if "ignored_markers_invalid" in self.issue_id:
            _LOGGER.debug("Routing to async_step_ignored_markers_invalid")
            try:
                return await self.async_step_ignored_markers_invalid()
            except Exception as e:
                _LOGGER.exception("Error in async_step_ignored_markers_invalid: %s", e)
                raise

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

        if action == "ignore":
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
                    vol.Required("action", default="fix"): SelectSelector(
                        SelectSelectorConfig(
                            options=["fix", "ignore"],
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
                    vol.Required("action", default="fix"): SelectSelector(
                        SelectSelectorConfig(
                            options=["fix", "ignore"],
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
            # Minimal options if one or both device classes are completely absent
            if is_de:
                options = {
                    "full_sync": "🚀 Initial Setup (fehlende Klasse(n) & Gerät(e) anlegen)",
                    "ignore": "🔇 Nachricht ignorieren (Web-IO nicht nutzen)",
                }
            else:
                options = {
                    "full_sync": "🚀 Initial Setup (Create missing class(es) & device(s))",
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

    async def async_step_ignored_markers_cleanup(self, user_input=None):
        """Handle cleanup of entities for ignored markers."""
        entry_id = self.issue_data.get("entry_id")
        marker_ids = self.issue_data.get("ignored_marker_ids", [])

        if user_input is not None:
            return await self._async_process_ignored_markers_cleanup(entry_id, marker_ids, user_input)

        return self._show_ignored_markers_cleanup_form(marker_ids)

    async def _async_process_ignored_markers_cleanup(self, entry_id: str, marker_ids: list[int], user_input: dict):
        """Handle the user's choice (cleanup/cancel) once the confirmation form was submitted."""
        if user_input.get("action", "cancel") != "cleanup":
            return self.async_abort(reason="user_cancelled")

        coordinator = self.hass.data[DOMAIN].get(entry_id)
        if not coordinator:
            return self.async_abort(reason="entry_not_found")

        deleted_count = self._delete_ignored_marker_entities(coordinator, marker_ids)

        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(
            title=f"{deleted_count} marker entities removed",
            data={},
        )

    def _delete_ignored_marker_entities(self, coordinator, marker_ids: list[int]) -> int:
        """Remove HA entities for the given ignored marker IDs; return the deleted-entity count.

        Comexio webhooks are registered once per config entry (not per marker), so ignoring a
        marker never needs to touch webhook registration — only its HA entity is removed here.
        """
        server_id = coordinator.server_id
        registry = er.async_get(self.hass)
        marker_id_set = set(marker_ids)
        prefix_base = f"{DOMAIN}_{server_id}_m".lower()
        deleted_count = 0

        # Single pass over the registry, matching the marker ID out of the unique_id suffix, instead of
        # one full registry scan per marker. list() snapshots the values so async_remove_entity() below
        # (which mutates registry.entities) doesn't invalidate the iterator mid-loop.
        for entity in list(registry.entities.values()):
            uid = (entity.unique_id or "").lower()
            if not uid.startswith(prefix_base):
                continue
            match = _MARKER_ID_SUFFIX_RE.match(uid[len(prefix_base) :])
            if not match or int(match.group(1)) not in marker_id_set:
                continue
            registry.async_remove(entity.entity_id)
            deleted_count += 1
            _LOGGER.info(
                "[%s] Deleted entity %s for ignored marker M%s",
                server_id,
                entity.entity_id,
                match.group(1),
            )

        return deleted_count

    def _show_ignored_markers_cleanup_form(self, marker_ids: list[int]):
        """Render the confirmation form listing the markers whose entities will be deleted."""
        ids_str = ", ".join(f"M{mid}" for mid in marker_ids)
        is_de = self.hass.config.language == "de"

        if is_de:
            description = (
                f"Dies wird **Entitäten** für die folgenden Merker **PERMANENT** löschen: "
                f"**{ids_str}**\n\n"
                "Diese Aktion kann **nicht rückgängig gemacht** werden. Die Merker selbst bleiben in "
                "Comexio bestehen, aber all ihre Home Assistant Entitäten und Verknüpfungen werden gelöscht.\n\n"
                "**Fortfahren?**"
            )
        else:
            description = (
                f"This will **permanently delete entities** for the following markers: "
                f"**{ids_str}**\n\n"
                "This action **cannot be undone**. The markers themselves will remain in Comexio, but all "
                "their Home Assistant entities and connections will be deleted.\n\n"
                "**Continue?**"
            )

        options = {
            "cleanup": f"🗑️ {'Ja, löschen' if is_de else 'Yes, delete'}",
            "cancel": f"❌ {'Abbrechen' if is_de else 'Cancel'}",
        }

        return self.async_show_form(
            step_id="ignored_markers_cleanup",
            description=description,
            data_schema=vol.Schema({vol.Required("action", default="cancel"): vol.In(options)}),
        )

    async def async_step_ignored_markers_invalid(self, user_input=None):
        """Handle invalid ignored marker IDs — inform user and offer to delete issue."""
        invalid_ids = self.issue_data.get("invalid_ids", [])

        if user_input is not None:
            action = user_input.get("action", "dismiss")

            if action == "dismiss":
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                return self.async_create_entry(
                    title="Issue dismissed",
                    data={},
                )

            return self.async_abort(reason="user_cancelled")

        # Show info form
        ids_str = ", ".join(f"M{mid}" for mid in invalid_ids) if invalid_ids else "Unknown"
        is_de = self.hass.config.language == "de"

        if is_de:
            description = (
                f"Die folgenden Merker-IDs in der `ignored_markers` Liste sind ungültig oder haben keine "
                f"Beschreibung: **{ids_str}**\n\n"
                "**Bitte entfernen Sie diese IDs aus der Comexio-Optionen:**\n"
                "1. Öffnen Sie **Einstellungen → Geräte und Services → Comexio**\n"
                "2. Wählen Sie den Eintrag aus\n"
                "3. Klicken Sie auf **Optionen**\n"
                "4. Entfernen Sie die ungültigen IDs aus dem Feld `ignored_markers`\n"
                "5. Speichern Sie die Änderungen\n\n"
                "Nach dem Speichern wird diese Meldung automatisch verschwinden (beim nächsten Poll oder "
                "nach HA-Neustart)."
            )
        else:
            description = (
                f"The following marker IDs in the `ignored_markers` list are invalid or have no description: "  # nosec B608
                f"**{ids_str}**\n\n"
                "**Please remove these IDs from the Comexio options:**\n"
                "1. Open **Settings → Devices & Services → Comexio**\n"
                "2. Select the entry\n"
                "3. Click **Options**\n"
                "4. Remove the invalid IDs from the `ignored_markers` field\n"
                "5. Save the changes\n\n"
                "This issue will disappear automatically after you save (on next poll or after HA restart)."
            )

        options = {
            "dismiss": f"✓ {'Jetzt ausblenden' if is_de else 'Dismiss now'}",
            "cancel": f"❌ {'Später' if is_de else 'Later'}",
        }

        return self.async_show_form(
            step_id="ignored_markers_invalid",
            description=description,
            data_schema=vol.Schema({vol.Required("action", default="dismiss"): vol.In(options)}),
        )
