# Version: 0.1.3
import voluptuous as vol
import logging

from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback

from .const import (
    DOMAIN, 
    CONF_HOST, 
    CONF_USERNAME, 
    CONF_PASSWORD, 
    CONF_SERVER_ID,
    CONF_API_USERNAME,
    CONF_API_PASSWORD
)
from .api import ComexioAPI

_LOGGER = logging.getLogger(__name__)

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

class ServerIdExists(HomeAssistantError):
    """Error to indicate the Server ID is already in use."""

class ComexioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comexio."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        
        # Determine the next suggested server ID
        current_entries = self._async_current_entries()
        suggested_id = f"iosrv{len(current_entries) + 1}"

        if user_input is not None:
            try:
                # 1. Check if the chosen Server ID is already taken
                chosen_id = user_input[CONF_SERVER_ID].strip().lower()
                for entry in current_entries:
                    if entry.data.get(CONF_SERVER_ID) == chosen_id:
                        raise ServerIdExists

                # 2. Validate connection with UI Credentials
                await self._test_connection(user_input)

                # Store the cleaned server ID
                user_input[CONF_SERVER_ID] = chosen_id

                return self.async_create_entry(
                    title=f"Comexio {chosen_id} ({user_input[CONF_HOST]})",
                    data=user_input,
                )

            except ServerIdExists:
                errors[CONF_SERVER_ID] = "server_id_exists"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.exception("Unexpected error: %s", e)
                errors["base"] = "unknown"

        # Form schema with optional API credentials and editable Server ID
        schema = vol.Schema({
            vol.Required(CONF_SERVER_ID, default=suggested_id): str,
            vol.Required(CONF_HOST, default="192.168.0.250"): str,
            vol.Required(CONF_USERNAME, default="admin"): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_API_USERNAME, default="ComexioApiUser"): str,
            vol.Optional(CONF_API_PASSWORD, default="#S3cur34Com3x1o11!"): str,
            vol.Required("import_markers", default=True): bool,
            vol.Required("import_ios", default=True): bool
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors
        )

    async def _test_connection(self, user_input):
        """Test if the login works using UI credentials."""
        api = ComexioAPI(
            self.hass,
            user_input[CONF_HOST],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )

        try:
            success = await api.login()
        except Exception:
            raise CannotConnect
        finally:
            await api.close()

        if not success:
            raise InvalidAuth
