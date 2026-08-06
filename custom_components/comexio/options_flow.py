# Version: 0.8.0
import logging

from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .const import (
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
    CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    CONF_FUNCTION_PLAN_PLAN_PREFIX,
    CONF_IGNORED_MARKERS,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_SCHEMA_IO,
    CONF_SCHEMA_MARKER,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    DEFAULT_FUNCTION_PLAN_PLAN_PREFIX,
    DEFAULT_SCHEMA_IO,
    DEFAULT_SCHEMA_MARKER,
    DOMAIN,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_ignored_markers(raw_input: str | None) -> str:
    """Normalize and validate user input for ignored markers before saving.

    Accepts: comma/semicolon/space/dot separators, optional M/m prefix, ranges like '8-12'.
    Returns sorted, deduplicated comma-separated string (e.g. '1,3,4,6,20-25').
    Raises vol.Invalid if any token is not a valid integer or range.
    """
    if not raw_input or not isinstance(raw_input, str):
        return ""

    seen_ints: set[int] = set()
    seen_ranges: set[tuple[int, int]] = set()
    items: list[int | tuple[int, int]] = []
    invalid_tokens: list[str] = []

    for token in raw_input.replace(".", ",").replace(";", ",").replace(" ", ",").split(","):
        stripped = token.strip().lstrip("Mm")
        if stripped:
            _collect_ignored_marker_token(stripped, seen_ints, seen_ranges, items, invalid_tokens)

    if invalid_tokens:
        raise vol.Invalid(
            f"Ungültige Merker-IDs: {', '.join(repr(t) for t in invalid_tokens[:3])}. "
            "Zahlen (z.B. '3', 'M12'), Bereiche (z.B. '8-12') oder Listen (z.B. '1, 3, 5') eingeben."
        )

    items.sort(key=_ignored_marker_sort_key)
    return ",".join(_format_ignored_marker_item(item) for item in items)


def _collect_ignored_marker_token(
    stripped: str,
    seen_ints: set[int],
    seen_ranges: set[tuple[int, int]],
    items: list[int | tuple[int, int]],
    invalid_tokens: list[str],
) -> None:
    """Parse one normalized ignored-markers token and append it to items if new and valid."""
    parsed = _parse_ignored_marker_token(stripped)
    if parsed is None:
        invalid_tokens.append(stripped)
    elif isinstance(parsed, tuple):
        if parsed not in seen_ranges:
            seen_ranges.add(parsed)
            items.append(parsed)
    elif parsed not in seen_ints:
        seen_ints.add(parsed)
        items.append(parsed)


def _parse_ignored_marker_token(stripped: str) -> int | tuple[int, int] | None:
    """Parse one normalized token into an int or a (start, end) range; None if invalid."""
    if "-" not in stripped:
        try:
            return int(stripped)
        except ValueError:
            return None
    parts = stripped.split("-", 1)
    try:
        start, end = int(parts[0]), int(parts[1].lstrip("Mm"))
    except ValueError:
        return None
    return (min(start, end), max(start, end))


def _ignored_marker_sort_key(item: int | tuple[int, int]) -> int:
    return item[0] if isinstance(item, tuple) else item


def _format_ignored_marker_item(item: int | tuple[int, int]) -> str:
    return f"{item[0]}-{item[1]}" if isinstance(item, tuple) else str(item)


class ComexioOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the component."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        conf = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            self._normalize_user_input(user_input, conf, errors)
            if not errors:
                return self._create_options_entry(user_input)

        # Extension names for the IO cluster multi-select: live coordinator data first,
        # excluding offline extensions (no hardware present — wiring them is pointless),
        # but keeping already-saved selections choosable so they can be deselected.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        coordinator_data = getattr(coordinator, "data", None) or {}
        ext_names = sorted({io["ext_name"] for io in coordinator_data.get("io", []) if not io.get("offline")})
        ext_names.extend(ext for ext in conf.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, []) if ext not in ext_names)

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
                    vol.Optional(
                        CONF_FUNCTION_PLAN_PLAN_PREFIX,
                        default=conf.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX),
                    ): str,
                    vol.Required(
                        CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
                        default=str(
                            conf.get(CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN)
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=["50", "100", "150"],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="function_plan_max_pairs_per_plan",
                        )
                    ),
                    vol.Optional(
                        CONF_FUNCTION_PLAN_IO_EXTENSIONS,
                        default=list(conf.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, [])),
                    ): SelectSelector(
                        # No translation_key: the options are live Comexio extension names,
                        # so there is nothing to translate them against.
                        SelectSelectorConfig(options=ext_names, multiple=True, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required(
                        CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                        default=str(
                            conf.get(
                                CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                                DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                            )
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=["1", "3", "6", "12"],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="function_plan_backup_retention_months",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    def _normalize_user_input(user_input: dict, conf: dict, errors: dict) -> None:
        """Validate and normalize user_input in-place; populate errors on failure."""
        user_input["scan_interval"] = int(user_input["scan_interval"])
        if CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN in user_input:
            user_input[CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN] = int(user_input[CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN])
        if CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS in user_input:
            user_input[CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS] = int(
                user_input[CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS]
            )

        # If field not in user_input, voluptuous didn't receive changes; preserve old value
        if CONF_IGNORED_MARKERS not in user_input:
            _LOGGER.debug("ignored_markers field missing from user_input — restoring from saved options")
            user_input[CONF_IGNORED_MARKERS] = conf.get(CONF_IGNORED_MARKERS, "")

        try:
            ignored_raw = user_input.get(CONF_IGNORED_MARKERS, "").strip()
            user_input[CONF_IGNORED_MARKERS] = _normalize_ignored_markers(ignored_raw)
        except vol.Invalid as e:
            errors[CONF_IGNORED_MARKERS] = str(e)
        except Exception as e:
            _LOGGER.exception("Unexpected error validating ignored_markers: %s", e)
            errors[CONF_IGNORED_MARKERS] = f"Fehler bei Validierung: {e}"

    def _create_options_entry(self, user_input: dict):
        """Merge new options with existing entry options and create the config entry.

        Preserves fields not shown in the form (e.g. passwords); explicitly removes
        ignored_markers when empty since HA won't auto-delete an emptied optional field.
        """
        merged_options = {**self._config_entry.options, **user_input}
        if not merged_options.get(CONF_IGNORED_MARKERS, "").strip():
            merged_options.pop(CONF_IGNORED_MARKERS, None)
        return self.async_create_entry(title="", data=merged_options)
