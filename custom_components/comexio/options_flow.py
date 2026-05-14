# Version: 0.6.0
from homeassistant import config_entries
import voluptuous as vol
from homeassistant.helpers.selector import (
    NumberSelector, 
    NumberSelectorConfig, 
    NumberSelectorMode
)

from .const import (
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_API_USERNAME,
    CONF_API_PASSWORD,
    CONF_SCHEMA_MARKER,
    CONF_SCHEMA_IO,
    DEFAULT_SCHEMA_MARKER,
    DEFAULT_SCHEMA_IO,
    SCAN_INTERVAL_MIN,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_DEFAULT,
    CONF_ENABLE_NOTIFICATIONS,
    DEFAULT_ENABLE_NOTIFICATIONS
)

class ComexioOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the component."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        super().__init__()

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        conf = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=conf.get(CONF_HOST)): str,
                vol.Required(CONF_USERNAME, default=conf.get(CONF_USERNAME)): str,
                vol.Required(CONF_PASSWORD, default=conf.get(CONF_PASSWORD)): str,
                vol.Optional(CONF_API_USERNAME, default=conf.get(CONF_API_USERNAME)): str,
                vol.Optional(CONF_API_PASSWORD, default=conf.get(CONF_API_PASSWORD)): str,
                vol.Optional(CONF_SCHEMA_MARKER, default=conf.get(CONF_SCHEMA_MARKER, DEFAULT_SCHEMA_MARKER)): str,
                vol.Optional(CONF_SCHEMA_IO, default=conf.get(CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO)): str,
                vol.Required("import_markers", default=conf.get("import_markers", True)): bool,
                vol.Required("import_ios", default=conf.get("import_ios", True)): bool,
                vol.Required("scan_interval", default=conf.get("scan_interval", SCAN_INTERVAL_DEFAULT)): NumberSelector(
                    NumberSelectorConfig(
                        min=SCAN_INTERVAL_MIN, 
                        max=SCAN_INTERVAL_MAX, 
                        step=1, 
                        mode=NumberSelectorMode.SLIDER,
                        unit_of_measurement="min"
                    )
                ),
                vol.Required(CONF_ENABLE_NOTIFICATIONS, default=conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS)): bool,
                vol.Required("audit_ignored", default=conf.get("audit_ignored", False)): bool,
            }),
        )
