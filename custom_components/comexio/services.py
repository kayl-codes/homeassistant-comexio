# Version: 0.6.0
import logging
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import persistent_notification
# STELLE SICHER, DASS DIESE ZEILE SO AUSSIEHT:
from .const import DOMAIN 

_LOGGER = logging.getLogger(__name__)

async def async_setup_services(hass: HomeAssistant) -> None:
    """Register additional services for the Comexio integration."""

    async def handle_generate_web_io(call: ServiceCall):
        """Service to preview or upload the Web-IO configuration."""
        # Hole die entry_id aus dem Service-Call
        entry_id = call.data.get("config_entry")
        
        # Validierung: Existiert die Instanz in unseren Daten?
        if entry_id not in hass.data[DOMAIN]:
            _LOGGER.error("Comexio instance %s not found in hass.data", entry_id)
            return

        coordinator = hass.data[DOMAIN][entry_id]
        api = coordinator.api
        server_id = coordinator.server_id
        do_upload = call.data.get("upload", False)
        
        try:
            # webio_name aus den Options oder Daten des spezifischen Entries
            conf = {**coordinator.config_entry.data, **coordinator.config_entry.options}
            webio_name = conf.get("webio_name", "HomeAssistant")
            
            web_io_json = api.generate_webio_json(server_id, webio_name, coordinator.data)
            
            if not do_upload:
                persistent_notification.async_create(
                    hass, f"```json\n{web_io_json}\n```",
                    title=f"Comexio Preview ({server_id})"
                )
                return

            # Ab hier beginnt der Upload-Prozess
            base_info = await api.get_webio_base_info(webio_name)
            if base_info:
                base_id, deletable = base_info
                if deletable:
                    _LOGGER.info("Base class is deletable, performing clean reinstall.")
                    await api.delete_webio_base(base_id)
                else:
                    # Hier könntest du später den "langsamen" Delta-Sync triggern
                    persistent_notification.async_create(
                        hass, 
                        f"Klasse '{webio_name}' ist in Comexio-Logik eingebunden und kann nicht gelöscht werden. "
                        "Bitte nutze den Smart-Sync Button für Einzel-Updates.", 
                        title="Bulk-Sync blockiert"
                    )
                    return

            success, result_val = await api.upload_web_io(server_id, webio_name, web_io_json)
            msg = f"Sync erfolgreich! Basis-ID: {result_val}" if success else f"Fehler beim Upload: {result_val}"
            persistent_notification.async_create(hass, msg, title=f"Comexio Sync ({server_id})")

        except Exception as e:
            _LOGGER.exception("Fehler im Comexio Service: %s", e)

    if not hass.services.has_service(DOMAIN, "generate_web_io"):
        hass.services.async_register(DOMAIN, "generate_web_io", handle_generate_web_io)
