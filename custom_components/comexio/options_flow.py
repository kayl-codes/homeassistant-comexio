# Version: 0.8.0
import logging

from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .const import (
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_IGNORED_MARKERS,
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

_LOGGER = logging.getLogger(__name__)


def _parse_ignored_markers(raw_input: str | None) -> list[int]:
    """Parse comma/semicolon-separated marker IDs from user input, return list of ints.

    Accepts both plain integers (123, 456) and with M prefix (M123, M456).
    Raises vol.Invalid if any token is not a valid integer.
    """
    if not raw_input or not isinstance(raw_input, str):
        return []
    marker_ids = []
    invalid_tokens = []
    for token in raw_input.replace(";", ",").split(","):
        if stripped := token.strip():
            # Remove optional M/m prefix
            if stripped.upper().startswith("M"):
                stripped = stripped[1:]
            try:
                marker_ids.append(int(stripped))
            except ValueError:
                invalid_tokens.append(stripped)
    if invalid_tokens:
        raise vol.Invalid(
            f"Ungültige Merker-IDs (keine Ganzzahlen): {', '.join(repr(t) for t in invalid_tokens[:3])}. "
            "Bitte Zahlen eingeben, mit oder ohne 'M'-Präfix (z.B. '123, 456; 789' oder 'M123, M456; M789')."
        )
    return marker_ids


def _validate_ignored_markers_against_coordinator(coordinator, marker_ids: list[int]) -> None:
    """No-op: actual validation happens at runtime in coordinator.async_check_ignored_markers().

    Timing issues during options-save make early validation unreliable.
    The coordinator will detect invalid IDs at the next poll and create a repair issue.
    """
    pass


class ComexioOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the component."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        conf = {**self._config_entry.data, **self._config_entry.options}
        errors = {}

        _LOGGER.info(f"=== OPTIONS FLOW STEP INIT ===")
        _LOGGER.info(f"Current conf[{CONF_IGNORED_MARKERS}] = {conf.get(CONF_IGNORED_MARKERS, 'NICHT_VORHANDEN')}")

        if user_input is not None:
            _LOGGER.info(f">>> USER_INPUT ERHALTEN <<<")
            _LOGGER.info(f"user_input.keys() = {list(user_input.keys())}")
            _LOGGER.info(f"user_input[{CONF_IGNORED_MARKERS}] = {user_input.get(CONF_IGNORED_MARKERS, 'NICHT_IN_DICT')}")

            try:
                user_input["scan_interval"] = int(user_input["scan_interval"])

                # If field not in user_input, voluptuous didn't receive changes; preserve old value
                if CONF_IGNORED_MARKERS not in user_input:
                    _LOGGER.warning(f"!!! {CONF_IGNORED_MARKERS} NICHT IN USER_INPUT - restoring from conf")
                    user_input[CONF_IGNORED_MARKERS] = conf.get(CONF_IGNORED_MARKERS, "")
                    _LOGGER.info(f"Restored value: {user_input[CONF_IGNORED_MARKERS]}")
                else:
                    _LOGGER.info(f"✓ {CONF_IGNORED_MARKERS} ist in user_input")

                ignored_raw = user_input.get(CONF_IGNORED_MARKERS, "").strip()
                _LOGGER.info(f"After strip: '{ignored_raw}' (empty={not ignored_raw})")

                if ignored_raw:
                    ignored_ids = _parse_ignored_markers(ignored_raw)
                    if coordinator := self.hass.data.get("comexio", {}).get(self._config_entry.entry_id):
                        _validate_ignored_markers_against_coordinator(coordinator, ignored_ids)
                    user_input[CONF_IGNORED_MARKERS] = ignored_raw
                    _LOGGER.info(f"✓ Speichere ignored_markers: {ignored_raw}")
                else:
                    user_input[CONF_IGNORED_MARKERS] = ""
                    _LOGGER.info(f"✓ Speichere ignored_markers: (leer)")
            except vol.Invalid as e:
                errors[CONF_IGNORED_MARKERS] = str(e)
                _LOGGER.error(f"Validierungsfehler: {e}")
            except Exception as e:
                _LOGGER.exception("Error validating ignored_markers: %s", e)
                errors[CONF_IGNORED_MARKERS] = f"Fehler bei Validierung: {e}"

            if not errors:
                # Merge new options with existing to preserve any fields not shown (e.g., passwords)
                merged_options = dict(self._config_entry.options)
                _LOGGER.info(f"Before update: merged_options[{CONF_IGNORED_MARKERS}] = {merged_options.get(CONF_IGNORED_MARKERS, 'NICHT_VORHANDEN')}")

                merged_options.update(user_input)
                _LOGGER.info(f"After update: merged_options[{CONF_IGNORED_MARKERS}] = {merged_options.get(CONF_IGNORED_MARKERS, 'NICHT_VORHANDEN')}")

                # Explicitly remove ignored_markers if empty (HA won't auto-delete it)
                if not merged_options.get(CONF_IGNORED_MARKERS, "").strip():
                    merged_options.pop(CONF_IGNORED_MARKERS, None)
                    _LOGGER.info(f"Removed empty {CONF_IGNORED_MARKERS}")

                _LOGGER.info(f">>> SPEICHERE ENTRY mit async_create_entry <<<")
                return self.async_create_entry(title="", data=merged_options)
            else:
                _LOGGER.error(f"Fehler vorhanden: {errors}")

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
                    vol.Optional(CONF_IGNORED_MARKERS, default=conf.get(CONF_IGNORED_MARKERS, "")): str,
                }
            ),
            errors=errors,
        )
