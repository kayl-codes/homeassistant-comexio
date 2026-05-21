# Version: 0.7.3
from homeassistant import config_entries
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode
import voluptuous as vol

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCHEMA_IO,
    CONF_SCHEMA_MARKER,
    CONF_USERNAME,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_SCHEMA_IO,
    DEFAULT_SCHEMA_MARKER,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_MAX,
    SCAN_INTERVAL_MIN,
)


class ComexioOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the component."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        conf = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            # Prevent password exposure: Reuse stored passwords when the user leaves fields blank
            if CONF_PASSWORD in user_input and not user_input[CONF_PASSWORD]:
                user_input[CONF_PASSWORD] = conf.get(CONF_PASSWORD, "")
            if CONF_API_PASSWORD in user_input and not user_input[CONF_API_PASSWORD]:
                user_input[CONF_API_PASSWORD] = conf.get(CONF_API_PASSWORD, "")

            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=conf.get(CONF_HOST)): str,
                    vol.Required(CONF_USERNAME, default=conf.get(CONF_USERNAME)): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(CONF_API_USERNAME, default=conf.get(CONF_API_USERNAME)): str,
                    vol.Optional(CONF_API_PASSWORD): str,
                    vol.Optional(CONF_SCHEMA_MARKER, default=conf.get(CONF_SCHEMA_MARKER, DEFAULT_SCHEMA_MARKER)): str,
                    vol.Optional(CONF_SCHEMA_IO, default=conf.get(CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO)): str,
                    vol.Required("import_markers", default=conf.get("import_markers", True)): bool,
                    vol.Required("import_ios", default=conf.get("import_ios", True)): bool,
                    vol.Required(
                        "scan_interval", default=conf.get("scan_interval", SCAN_INTERVAL_DEFAULT)
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=SCAN_INTERVAL_MIN,
                            max=SCAN_INTERVAL_MAX,
                            step=1,
                            mode=NumberSelectorMode.SLIDER,
                            unit_of_measurement="min",
                        )
                    ),
                    vol.Required(
                        CONF_ENABLE_NOTIFICATIONS,
                        default=conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS),
                    ): bool,
                    vol.Required("audit_ignored", default=conf.get("audit_ignored", False)): bool,
                    vol.Optional(
                        CONF_COVER_KEYWORDS, default=conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS)
                    ): str,
                }
            ),
        )
