# Version: 0.8.0
import logging

from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
import voluptuous as vol

from .const import (
    CONF_BUS_WATCHDOG_AUTO_REBOOT,
    CONF_BUS_WATCHDOG_ENABLED,
    CONF_COVER_KEYWORDS,
    CONF_ENABLE_NOTIFICATIONS,
    CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    CONF_FUNCTION_PLAN_IO_EXTENSIONS,
    CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    CONF_FUNCTION_PLAN_PLAN_PREFIX,
    CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_SCHEMA_IO,
    DEFAULT_BUS_WATCHDOG_AUTO_REBOOT,
    DEFAULT_BUS_WATCHDOG_ENABLED,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    DEFAULT_FUNCTION_PLAN_PLAN_PREFIX,
    DEFAULT_SCHEMA_IO,
    DOMAIN,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
    SourceCategory,
    WebioClass,
    ignore_list_categories,
    is_valid_entity_name_schema,
    parse_ignored_marker_tokens,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_ignored_ids(raw_input: str | None, prefix_chars: str = "Mm", category_label: str = "Merker") -> str:
    """Normalize and validate user input for ignored marker/KNX IDs before saving.

    Accepts: comma/semicolon/space/dot separators, an optional leading letter prefix
    (`prefix_chars` — "Mm" for markers, "Kk" for KNX objects), ranges like '8-12'.
    Returns sorted, deduplicated comma-separated string (e.g. '1,3,4,6,20-25'). A single ID
    already covered by a range (e.g. '1-3,2') is dropped rather than kept as a redundant entry.
    Raises vol.Invalid if any token is not a valid integer or range.
    """
    if not raw_input or not isinstance(raw_input, str):
        return ""

    ints: set[int] = set()
    ranges: set[tuple[int, int]] = set()
    invalid_tokens: list[str] = []

    for token, parsed in parse_ignored_marker_tokens(raw_input, prefix_chars):
        if parsed is None:
            invalid_tokens.append(token)
        elif isinstance(parsed, tuple):
            ranges.add(parsed)
        else:
            ints.add(parsed)

    if invalid_tokens:
        example_prefix = prefix_chars[0]
        raise vol.Invalid(
            f"Ungültige {category_label}-IDs: {', '.join(repr(t) for t in invalid_tokens[:3])}. "
            f"Zahlen (z.B. '3', '{example_prefix}12'), Bereiche (z.B. '8-12') oder Listen (z.B. '1, 3, 5') eingeben."
        )

    ints -= {i for start, end in ranges for i in range(start, end + 1)}

    items: list[int | tuple[int, int]] = [*ranges, *ints]
    items.sort(key=_ignored_marker_sort_key)
    return ",".join(_format_ignored_marker_item(item) for item in items)


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
        source_cats = ignore_list_categories()

        # {} before the coordinator's first successful poll — never hide a category in that
        # case, only once real counts are known. See coordinator.available_source_counts
        # docstring. A category stays visible once opted in even at count 0, so a user who
        # opted in before the server had objects (or during a transient scrape gap) can still
        # find the toggle to turn it back off — mirrors the ext_names multi-select pattern
        # below (keep already-saved selections choosable even once no longer "current").
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        available_counts = getattr(coordinator, "available_source_counts", None) or {}
        hidden_cats = {
            cat.key
            for cat in source_cats
            if available_counts
            and not available_counts.get(cat.key)
            and not conf.get(cat.import_conf_key, cat.import_default)
        }

        if user_input is not None:
            self._normalize_user_input(user_input, conf, errors, hidden_cats)
            if not errors:
                return self._create_options_entry(user_input)

        # Extension names for the IO cluster multi-select: live coordinator data first,
        # excluding offline extensions (no hardware present — wiring them is pointless),
        # but keeping already-saved selections choosable so they can be deselected.
        coordinator_data = getattr(coordinator, "data", None) or {}
        ext_names = sorted({io["ext_name"] for io in coordinator_data.get("io", []) if not io.get("offline")})
        ext_names.extend(ext for ext in conf.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, []) if ext not in ext_names)

        # Marker + KNX share the same schema/import/ignored-ids field shape (registry-driven
        # via ignore_list_categories — the source categories that support an ignore-list);
        # IO keeps its own fields since its unique_id/no-ignore-list shape differs.
        schema_dict: dict = {}
        for cat in source_cats:
            # Same visibility as the opt-in toggle below — a naming-schema field for a category
            # the user can't even opt into would be a dead field.
            if cat.key in hidden_cats:
                continue
            schema_dict[
                vol.Optional(cat.schema_conf_key, default=conf.get(cat.schema_conf_key, cat.schema_default))
            ] = str
        schema_dict[vol.Optional(CONF_SCHEMA_IO, default=conf.get(CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO))] = str
        for cat in source_cats:
            if cat.key in hidden_cats:
                continue
            schema_dict[
                vol.Required(cat.import_conf_key, default=conf.get(cat.import_conf_key, cat.import_default))
            ] = bool
        schema_dict[vol.Required("import_ios", default=conf.get("import_ios", True))] = bool
        schema_dict[
            vol.Required(
                "scan_interval",
                default=str(conf.get("scan_interval", SCAN_INTERVAL_DEFAULT)),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=SCAN_INTERVAL_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="scan_interval",
            )
        )
        schema_dict[
            vol.Required(
                CONF_ENABLE_NOTIFICATIONS,
                default=conf.get(CONF_ENABLE_NOTIFICATIONS, DEFAULT_ENABLE_NOTIFICATIONS),
            )
        ] = bool
        schema_dict[vol.Required("audit_ignored", default=conf.get("audit_ignored", False))] = bool
        schema_dict[
            vol.Required(
                CONF_INCLUDE_OFFLINE_EXTENSIONS,
                default=conf.get(CONF_INCLUDE_OFFLINE_EXTENSIONS, False),
            )
        ] = bool
        schema_dict[
            vol.Required(
                CONF_BUS_WATCHDOG_ENABLED,
                default=conf.get(CONF_BUS_WATCHDOG_ENABLED, DEFAULT_BUS_WATCHDOG_ENABLED),
            )
        ] = bool
        schema_dict[
            vol.Required(
                CONF_BUS_WATCHDOG_AUTO_REBOOT,
                default=conf.get(CONF_BUS_WATCHDOG_AUTO_REBOOT, DEFAULT_BUS_WATCHDOG_AUTO_REBOOT),
            )
        ] = bool
        schema_dict[
            vol.Optional(CONF_COVER_KEYWORDS, default=conf.get(CONF_COVER_KEYWORDS, DEFAULT_COVER_KEYWORDS))
        ] = str
        for cat in source_cats:
            # Same visibility as the opt-in toggle above — an ignore-list for a category the
            # user can't even opt into would be a dead field.
            if cat.key in hidden_cats:
                continue
            schema_dict[vol.Optional(cat.ignored_conf_key, default=conf.get(cat.ignored_conf_key, ""))] = str
        schema_dict[
            vol.Optional(
                CONF_FUNCTION_PLAN_PLAN_PREFIX,
                default=conf.get(CONF_FUNCTION_PLAN_PLAN_PREFIX, DEFAULT_FUNCTION_PLAN_PLAN_PREFIX),
            )
        ] = str
        schema_dict[
            vol.Required(
                CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
                default=str(conf.get(CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN, DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN)),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=["50", "100", "150"],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="function_plan_max_pairs_per_plan",
            )
        )
        schema_dict[
            vol.Optional(
                CONF_FUNCTION_PLAN_IO_EXTENSIONS,
                default=list(conf.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, [])),
            )
        ] = SelectSelector(
            # No translation_key: the options are live Comexio extension names,
            # so there is nothing to translate them against.
            SelectSelectorConfig(options=ext_names, multiple=True, mode=SelectSelectorMode.DROPDOWN)
        )
        schema_dict[
            vol.Required(
                CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                default=str(
                    conf.get(
                        CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                        DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
                    )
                ),
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=["1", "3", "6", "12"],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="function_plan_backup_retention_months",
            )
        )
        schema_dict[
            vol.Optional(
                CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY,
                default=list(conf.get(CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY, [])),
            )
        ] = SelectSelector(
            # No translation_key: the options are this instance's notify service names.
            SelectSelectorConfig(
                options=sorted(self.hass.services.async_services_for_domain("notify")),
                multiple=True,
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict), errors=errors)

    @staticmethod
    def _restore_missing_ignored_fields(
        user_input: dict, conf: dict, source_cats: list[SourceCategory], hidden_cats: set[WebioClass]
    ) -> None:
        """Preserve the old ignored_conf_key value for any category voluptuous sent no change for.

        A hidden category (see async_step_init's docstring) is expectedly absent from
        user_input — restored quietly. Any other absence is unexpected and logged.
        """
        for cat in source_cats:
            if cat.ignored_conf_key in user_input:
                continue
            if cat.key not in hidden_cats:
                _LOGGER.warning("%s field missing from user_input — restoring from saved options", cat.ignored_conf_key)
            user_input[cat.ignored_conf_key] = conf.get(cat.ignored_conf_key, "")

    @staticmethod
    def _normalize_user_input(user_input: dict, conf: dict, errors: dict, hidden_cats: set[WebioClass]) -> None:
        """Validate and normalize user_input in-place; populate errors on failure."""
        source_cats = ignore_list_categories()
        ComexioOptionsFlow._restore_missing_ignored_fields(user_input, conf, source_cats, hidden_cats)

        numeric_option_keys = (
            "scan_interval",
            CONF_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
            CONF_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
        )
        for key in numeric_option_keys:
            if key not in user_input:
                continue
            try:
                user_input[key] = int(user_input[key])
            except (ValueError, TypeError) as e:
                # Kept separate from the ignored-ids try/except below so a numeric-field
                # error is never misattributed to an ignored-ids error key.
                # Logged at debug only: an expected user-input validation failure, the
                # translated form error below already tells the user what to fix.
                _LOGGER.debug("Invalid numeric option %s=%r: %s", key, user_input[key], e)
                errors["base"] = "invalid_number"
                return

        schema_keys = [cat.schema_conf_key for cat in source_cats if cat.key not in hidden_cats]
        schema_keys.append(CONF_SCHEMA_IO)
        for key in schema_keys:
            if key in user_input and not is_valid_entity_name_schema(user_input[key]):
                errors[key] = "invalid_schema"

        for cat in source_cats:
            try:
                ignored_raw = user_input.get(cat.ignored_conf_key, "").strip()
                prefix_chars = cat.audit_key_prefix + cat.audit_key_prefix.lower()
                user_input[cat.ignored_conf_key] = _normalize_ignored_ids(ignored_raw, prefix_chars, cat.german_label)
            except vol.Invalid as e:
                errors[cat.ignored_conf_key] = str(e)
            except Exception as e:
                _LOGGER.exception("Unexpected error validating %s: %s", cat.ignored_conf_key, e)
                errors[cat.ignored_conf_key] = f"Fehler bei Validierung: {e}"

    def _create_options_entry(self, user_input: dict):
        """Merge new options with existing entry options and create the config entry.

        Preserves fields not shown in the form (e.g. passwords); explicitly removes
        empty ignore-list fields since HA won't auto-delete an emptied optional field.
        """
        merged_options = {**self._config_entry.options, **user_input}
        for cat in ignore_list_categories():
            if not merged_options.get(cat.ignored_conf_key, "").strip():
                merged_options.pop(cat.ignored_conf_key, None)
        return self.async_create_entry(title="", data=merged_options)
