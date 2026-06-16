# Version: 0.8.0
import contextlib
import logging
import socket

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .api import ComexioAPI
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_COVER_KEYWORDS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_HOST,
    DOMAIN,
    KNOWN_DOMAINS,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
)
from .options_flow import ComexioOptionsFlow

_LOGGER = logging.getLogger(__name__)


_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_API_USERNAME, CONF_API_PASSWORD}
)


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

        # Generate a suggestion for the server ID based on existing entries
        current_entries = self._async_current_entries()

        # Collect existing iosrv indices to avoid collisions
        existing_indices: set[int] = set()
        for entry in current_entries:
            server_id = entry.data.get(CONF_SERVER_ID)
            if isinstance(server_id, str) and server_id.startswith("iosrv"):
                suffix = server_id[5:]
                if suffix.isdigit():
                    existing_indices.add(int(suffix))

        # Find the lowest unused positive index
        next_index = 1
        while next_index in existing_indices:
            next_index += 1

        suggested_id = f"iosrv{next_index}"

        default_host = DEFAULT_HOST

        if user_input is None:
            # Auto-discovery attempt to find the Comexio IP before showing the form
            def guess_ip():
                for domain in [""] + KNOWN_DOMAINS:
                    test_host = f"comexio.{domain}" if domain else "comexio"
                    with contextlib.suppress(OSError):
                        return socket.gethostbyname(test_host)
                return DEFAULT_HOST

            default_host = await self.hass.async_add_executor_job(guess_ip)
            _LOGGER.debug("Auto-discovered default host: %s", default_host)
        else:
            default_host = user_input.get(CONF_HOST, DEFAULT_HOST)
            _LOGGER.debug("Processing user input for Comexio setup: %s", default_host)
            try:
                # 1. Validation: Check if the chosen Server ID is already taken
                chosen_id = user_input[CONF_SERVER_ID].strip().lower()
                for entry in current_entries:
                    existing_id = str(entry.data.get(CONF_SERVER_ID, "")).strip().lower()
                    if existing_id == chosen_id:
                        _LOGGER.warning("Setup failed: Server ID '%s' already exists", chosen_id)
                        errors[CONF_SERVER_ID] = "server_id_exists"
                        raise ServerIdExists

                # 2. Validation: Test connection with the provided UI credentials
                await self._test_connection(user_input)

                # Store the cleaned server ID and coerce scan_interval to int
                user_input[CONF_SERVER_ID] = chosen_id
                user_input["scan_interval"] = int(user_input["scan_interval"])
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

        # Preserve user input for the form if validation failed (to improve UX)
        ui = user_input or {}

        # Define the form schema with all required and optional fields
        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER_ID, default=ui.get(CONF_SERVER_ID, suggested_id)): str,
                vol.Required(CONF_HOST, default=ui.get(CONF_HOST, default_host)): str,
                vol.Required(CONF_USERNAME, default=ui.get(CONF_USERNAME, "admin")): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_API_USERNAME, default=ui.get(CONF_API_USERNAME, "")): str,
                vol.Optional(CONF_API_PASSWORD, default=ui.get(CONF_API_PASSWORD, "")): str,
                vol.Required("import_markers", default=ui.get("import_markers", True)): bool,
                vol.Required("import_ios", default=ui.get("import_ios", True)): bool,
                vol.Required("webio_name", default=ui.get("webio_name", "HomeAssistant")): str,
                vol.Required(
                    "scan_interval",
                    default=str(ui.get("scan_interval", SCAN_INTERVAL_DEFAULT)),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=SCAN_INTERVAL_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="scan_interval",
                    )
                ),
                vol.Optional(CONF_COVER_KEYWORDS, default=ui.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS)): str,
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ComexioOptionsFlow(config_entry)

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of connection credentials."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors = {}

        if user_input is not None:
            merged = {**entry.data, **user_input}
            if not user_input.get(CONF_PASSWORD):
                merged[CONF_PASSWORD] = entry.data.get(CONF_PASSWORD, "")
            if not user_input.get(CONF_API_PASSWORD):
                merged[CONF_API_PASSWORD] = entry.data.get(CONF_API_PASSWORD, "")

            try:
                await self._test_connection(merged)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during reconfigure")
                errors["base"] = "unknown"
            else:
                clean_options = {k: v for k, v in entry.options.items() if k not in _CREDENTIAL_KEYS}
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"Comexio {entry.data[CONF_SERVER_ID]} ({merged[CONF_HOST]})",
                    data=merged,
                    options=clean_options,
                )

        conf = entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=conf.get(CONF_HOST, DEFAULT_HOST)): str,
                    vol.Required(CONF_USERNAME, default=conf.get(CONF_USERNAME, "admin")): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(CONF_API_USERNAME, default=conf.get(CONF_API_USERNAME, "")): str,
                    vol.Optional(CONF_API_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _test_connection(self, user_input):
        """Test if the login works using UI credentials."""
        api = None

        try:
            api = ComexioAPI(
                self.hass,
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            _LOGGER.debug("Testing login credentials for %s", user_input[CONF_HOST])
            success = await api.login()
            if not success:
                _LOGGER.error("Login test failed for user %s", user_input[CONF_USERNAME])
                raise InvalidAuth
        except Exception as e:
            if not isinstance(e, InvalidAuth):
                _LOGGER.exception("Connection test failed: %s", e)
                raise CannotConnect from e
            raise
        finally:
            if api is not None:
                await api.close()
