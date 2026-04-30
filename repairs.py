import asyncio
import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.components.repairs import RepairsFlow
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, CONF_SERVER_ID

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry):
    return True


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None):
    return ComexioRepairFlow(issue_id, data)


class ComexioRepairFlow(RepairsFlow):

    def __init__(self, issue_id: str, data: dict | None):
        self.issue_id = issue_id
        self.issue_data = data or {}

    async def async_step_init(self, user_input=None):
        return await self.async_step_select_action()

    async def async_step_select_action(self, user_input=None):
        """Handle the action selected by the user."""
        entry_id = self.issue_data.get("entry_id")
        coordinator = self.hass.data[DOMAIN].get(entry_id)

        # BLOCKING: Wenn bereits ein Sync läuft, Menü sperren
        if coordinator and getattr(coordinator, "in_sync", False):
            return self.async_abort(reason="already_in_sync")

        if user_input is not None:
            action = user_input["action"]
            
            # 1. First, define entry so it is available for all branches
            if not entry_id:
                return self.async_abort(reason="missing_entry_id")

            entry = self.hass.config_entries.async_get_entry(entry_id)

            if not entry:
                return self.async_abort(reason="entry_not_found")

            # 2. Now handle the ignore action using the defined entry
            if action == "ignore":
                # Create a new dict for options (don't touch data)
                new_options = dict(entry.options)
                new_options["audit_ignored"] = True
                # Update entry with new options
                self.hass.config_entries.async_update_entry(entry, options=new_options)
                
                # Remove the issue from the registry
                ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
                
                return self.async_create_entry(title="Ignored", data={})

            # 3. Handle other actions (full_sync, etc.)
            _LOGGER.warning(
                "Repair gestartet: issue_id=%s action=%s data=%s",
                self.issue_id,
                action,
                self.issue_data,
            )

            server_id = entry.data.get(CONF_SERVER_ID)

            if not server_id:
                return self.async_abort(reason="missing_server_id")

            # 👉 Mapping Action -> Service Data
            # Wir rufen den Dienst über die Domäne unserer Integration (DOMAIN) auf.
            service_data = {
                "entity_id": f"button.comexio_{server_id}_webio_sync",
                "action": action  # 👈 wichtig für deinen Button!
            }

            try:
                await self.hass.services.async_call(
                    DOMAIN,
                    "press_action",
                    service_data,
                    blocking=False,  # set not True, elsewhere the longtimne runner jumps into a timeout
                )

                coordinator = self.hass.data[DOMAIN][entry.entry_id]

                await asyncio.sleep(0.5)

                # force recheck
                await coordinator.async_refresh()

                return self.async_create_entry(
                    title=f"Aktion '{action}' erfolgreich ausgeführt",
                    data={},
                )

            except Exception as err:
                _LOGGER.exception("Repair Fehler: %s", err)
                return self.async_abort(reason="sync_failed")

        # --- FIX: Sicherere Placeholder-Abfrage ---
        issue_reg = ir.async_get(self.hass)
        issue = issue_reg.async_get_issue(DOMAIN, self.issue_id)
        
        # Fallback auf leeres Dict, falls Issue nicht gefunden wird
        placeholders = issue.translation_placeholders if issue else {}

        # Prüfen, welchen Translation-Key das aktuelle Issue hat
        is_missing_class = issue.translation_key == "missing_webio_class" if issue else False

        if is_missing_class:
            # Eingeschränktes Menü, wenn alles fehlt
            options = {
                "full_sync": "🚀 Initial Setup (Create class & device)",
                "ignore": "🔇 Ignore message (Do not use Web-IO)"
            }
        else:
            # 1. Daten holen
            counts = self.issue_data.get("counts", {})
            placeholders = placeholders or {}

            # 2. Hilfsfunktion für die Zeitberechnung
            def get_time(c, is_delete=False):
                sec = c * (4 if is_delete else 35)
                if sec == 0: return ""
                if sec < 60: return f" ~{sec}s"
                return f" ~{sec // 60}:{sec % 60:02d} min"

            # 3. Spezifische Optionen mit Zeitstempel sammeln
            specific_options = {}
            
            if counts.get("type", 0) > 0:
                t = get_time(counts["type"])
                specific_options["update_types"] = f"🔧 Update Types Only ({counts['type']}x == {t})"
            
            if counts.get("missing", 0) > 0:
                t = get_time(counts["missing"])
                specific_options["create_missing"] = f"➕ Create Missing Only ({counts['missing']}x == {t})"

            if counts.get("rename", 0) > 0:
                t = get_time(counts["rename"])
                specific_options["update_renames"] = f"✏️ Update Names Only ({counts['rename']}x == {t})"

            if counts.get("orphan", 0) > 0:
                t = get_time(counts["orphan"], is_delete=True)
                specific_options["delete_orphans"] = f"🗑️ Delete Orphans Only ({counts['orphan']}x == {t})"

            # 4. Full Sync (Kombinierte Zeit)
            total_write = counts.get("type", 0) + counts.get("rename", 0) + counts.get("missing", 0)
            total_del = counts.get("orphan", 0)
            total_sec = (total_write * 35) + (total_del * 4)
            
            t_full = get_time(1) # Trick um die Formatierung der total_sec zu nutzen
            # Korrektur für total_sec Formatierung:
            if total_sec < 60: t_full = f"~{total_sec}s"
            else: t_full = f"~{total_sec // 60}:{total_sec % 60:02d} min"

            options = {}
            if len(specific_options) > 1:
                options["full_sync"] = f"🔄 Full Sync (Fix everything, {t_full})"
                default_action = "full_sync"
            else:
                default_action = list(specific_options.keys())[0] if specific_options else "full_sync"

            options.update(specific_options)

            # Fallback
            if not options:
                options["full_sync"] = "🔄 Full Sync (Fix everything)"
                default_action = "full_sync"


        # --- Ergänzung für den Hinweis zum schnellen Sync ---
#        hint_text = (
#            "\n\n--- \n"
#            "💡 **Hinweis:** Die Integration prüft automatisch, ob das Web-IO Gerät in Comexio "
#            "ungenutzt ist. Falls ja, wird unabhängig von der Wahl oben eine "
#            "**Schnell-Einrichtung (~20 Sek.)** durchgeführt."
#        )

        # Wir hängen den Text an die bestehende Zusammenfassung (summary) an
 #       if "summary" in placeholders:
 #           placeholders["summary"] = f"{placeholders['summary']}{hint_text}"

        return self.async_show_form(
            step_id="select_action",
            description_placeholders=placeholders,
            data_schema=vol.Schema({
                vol.Required("action", default=default_action): vol.In(options)
            }),
        )
