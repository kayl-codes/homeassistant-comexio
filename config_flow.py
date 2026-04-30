from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector, 
    NumberSelectorConfig, 
    NumberSelectorMode
)
import voluptuous as vol
import logging

from .const import (
    DOMAIN, 
    CONF_HOST, 
    CONF_USERNAME, 
    CONF_PASSWORD, 
    CONF_SERVER_ID,
    CONF_API_USERNAME,
    CONF_API_PASSWORD,
    SCAN_INTERVAL_MIN,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_DEFAULT
)
from .api import ComexioAPI
from .options_flow import ComexioOptionsFlow

_LOGGER = logging.getLogger(__name__)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

class ServerIdExists(HomeAssistantError):
    """Error to indicate the Server ID is already in use."""

class ComexioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comexio with detailed validation."""
    VERSION = 1
    
    async def async_step_user(self, user_input=None):
        """Handle the initial setup step when a user adds the integration."""
        errors = {}
        
        # Add additional debug logs to trace execution
        _LOGGER.debug("ComexioConfigFlow: Entered async_step_user")
        _LOGGER.debug("ComexioConfigFlow: Current entries: %s", self._async_current_entries())

        # Generate a suggestion for the server ID based on existing entries
        current_entries = self._async_current_entries()
        suggested_id = f"iosrv{len(current_entries) + 1}"

        if user_input is not None:
            _LOGGER.debug("Processing user input for Comexio setup: %s", user_input[CONF_HOST])
            try:
                # 1. Validation: Check if the chosen Server ID is already taken
                chosen_id = user_input[CONF_SERVER_ID].strip().lower()
                for entry in current_entries:
                    if entry.data.get(CONF_SERVER_ID) == chosen_id:
                        _LOGGER.warning("Setup failed: Server ID '%s' already exists", chosen_id)
                        errors[CONF_SERVER_ID] = "server_id_exists"
                        raise ServerIdExists

                # 2. Validation: Test connection with the provided UI credentials
                await self._test_connection(user_input)

                # Store the cleaned server ID and create the entry
                user_input[CONF_SERVER_ID] = chosen_id
                _LOGGER.info("Config entry created for server: %s", chosen_id)

                return self.async_create_entry(
                    title=f"Comexio {chosen_id} ({user_input[CONF_HOST]})",
                    data=user_input,
                )

            except ServerIdExists:
                # Error already added to the errors dict
                pass
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.exception("Unexpected error during setup: %s", e)
                errors["base"] = "unknown"

        # Define the form schema with all required and optional fields
        schema = vol.Schema({
            vol.Required(CONF_SERVER_ID, default=suggested_id): str,
            vol.Required(CONF_HOST, default="192.168.0.250"): str,
            vol.Required(CONF_USERNAME, default="admin"): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_API_USERNAME, default="ComexioApiUser"): str,
            vol.Optional(CONF_API_PASSWORD, default="#S3cur34Com3x1o11!"): str,
            vol.Required("import_markers", default=True): bool,
            vol.Required("import_ios", default=True): bool,
            vol.Required("webio_name", default="HomeAssistant_v1"): str,
            vol.Required("scan_interval", default=SCAN_INTERVAL_DEFAULT): NumberSelector(
                NumberSelectorConfig(
                    min=SCAN_INTERVAL_MIN, 
                    max=SCAN_INTERVAL_MAX, 
                    step=1, 
                    mode=NumberSelectorMode.SLIDER,
                    unit_of_measurement="min"
                )
            ),
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ComexioOptionsFlow(config_entry)

    async def _test_connection(self, user_input):
        """Test if the login works using UI credentials."""
        api = ComexioAPI(
            self.hass,
            user_input[CONF_HOST],
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )

        try:
            _LOGGER.debug("Testing login credentials for %s", user_input[CONF_HOST])
            success = await api.login()
            if not success:
                _LOGGER.error("Login test failed for user %s", user_input[CONF_USERNAME])
                raise InvalidAuth
        except Exception as e:
            if not isinstance(e, InvalidAuth):
                _LOGGER.error("Connection test failed: %s", e)
                raise CannotConnect
            raise e
        finally:
            await api.close()
