# Version: 0.2.1
import logging
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol
from homeassistant.helpers.selector import (
    NumberSelector, 
    NumberSelectorConfig, 
    NumberSelectorMode
)
from .const import (
    DOMAIN, CONF_HOST, CONF_USERNAME, CONF_PASSWORD, 
    CONF_SERVER_ID, CONF_API_USERNAME, CONF_API_PASSWORD
)
from .api import ComexioAPI

_LOGGER = logging.getLogger(__name__)

class ComexioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comexio."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        current_entries = self._async_current_entries()
        suggested_id = f"iosrv{len(current_entries) + 1}"

        if user_input is not None:
            try:
                # 1. Check for duplicate Server ID
                chosen_id = user_input[CONF_SERVER_ID].strip().lower()
                for entry in current_entries:
                    if entry.data.get(CONF_SERVER_ID) == chosen_id:
                        errors[CONF_SERVER_ID] = "server_id_exists"
                        raise ServerIdExists

                # 2. Test connection
                await self._test_connection(user_input)

                user_input[CONF_SERVER_ID] = chosen_id
                return self.async_create_entry(
                    title=f"Comexio {chosen_id} ({user_input[CONF_HOST]})",
                    data=user_input,
                )

            except ServerIdExists:
                pass
            except Exception:
                errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_SERVER_ID, default=suggested_id): str,
            vol.Required(CONF_HOST, default="192.168.0.250"): str,
            vol.Required(CONF_USERNAME, default="admin"): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_API_USERNAME, default="ComexioApiUser"): str,
            vol.Optional(CONF_API_PASSWORD, default="#S3cur34Com3x1o11!"): str,
            vol.Required("import_markers", default=True): bool,
            vol.Required("import_ios", default=True): bool,
            vol.Required("webio_name", default="HomeAssistant_V1"): str,
            # Hier der fix für den Slider:
            vol.Required("scan_interval", default=15): NumberSelector(
                NumberSelectorConfig(
                    min=5, 
                    max=60, 
                    step=1, 
                    mode=NumberSelectorMode.SLIDER,
                    unit_of_measurement="Minuten"
                )
            ),
        })

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _test_connection(self, user_input):
        api = ComexioAPI(self.hass, user_input[CONF_HOST], user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
        try:
            if not await api.login(): raise Exception
        finally:
            await api.close()

class ServerIdExists(HomeAssistantError): """Error"""
