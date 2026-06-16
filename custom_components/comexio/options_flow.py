# Version: 0.8.0
from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .const import (
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_SCHEMA_IO,
    CONF_SCHEMA_MARKER,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_SCHEMA_IO,
    DEFAULT_SCHEMA_MARKER,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
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
            user_input["scan_interval"] = int(user_input["scan_interval"])
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCHEMA_MARKER, default=conf.get(CONF_SCHEMA_MARKER, DEFAULT_SCHEMA_MARKER)): str,
                    vol.Optional(CONF_SCHEMA_IO, default=conf.get(CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO)): str,
                    vol.Required("import_markers", default=conf.get("import_markers", True)): bool,
                    vol.Required("import_ios", default=conf.get("import_ios", True)): bool,
                    vol.Required(
                        "scan_interval",
                        default=str(conf.get("scan_interval", SCAN_INTERVAL_DEFAULT)),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=SCAN_INTERVAL_OPTIONS,
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="scan_interval",
                        )
                    ),
                    vol.Required(
                        CONF_ENABLE_NOTIFICATIONS,
                        default=conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS),
                    ): bool,
                    vol.Required("audit_ignored", default=conf.get("audit_ignored", False)): bool,
                    vol.Required(
                        CONF_INCLUDE_OFFLINE_EXTENSIONS,
                        default=conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False),
                    ): bool,
                    vol.Optional(
                        CONF_COVER_KEYWORDS, default=conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS)
                    ): str,
                }
            ),
        )
