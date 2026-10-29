# Version: 0.8.0
from collections.abc import Collection, Iterable, Mapping, Set as AbstractSet
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
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
    CONF_FUNCTION_PLAN_QUICKBUS_DEVICES,
    CONF_FUNCTION_PLAN_WATCHDOG_NOTIFY,
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    CONF_SCHEMA_IO,
    CONF_WEBHOOK_ADDRESS_MODE,
    CONF_WEBHOOK_URL,
    DEFAULT_BUS_WATCHDOG_AUTO_REBOOT,
    DEFAULT_BUS_WATCHDOG_ENABLED,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_ENABLE_NOTIFICATIONS,
    DEFAULT_FUNCTION_PLAN_BACKUP_RETENTION_MONTHS,
    DEFAULT_FUNCTION_PLAN_MAX_PAIRS_PER_PLAN,
    DEFAULT_FUNCTION_PLAN_PLAN_PREFIX,
    DEFAULT_SCHEMA_IO,
    DOMAIN,
    QUICKBUS_IO_DATA_KEY,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
    WEBHOOK_ADDRESS_AUTO,
    WEBHOOK_ADDRESS_CUSTOM,
    WEBHOOK_ADDRESS_EXTERNAL,
    WEBHOOK_ADDRESS_MODES,
    SourceCategory,
    WebioClass,
    format_ignored_ids,
    ignore_list_categories,
    is_valid_entity_name_schema,
    one_line,
    parse_ignored_marker_tokens,
    parse_watchdog_user_plan_pick,
    prune_import_disabled_ignored,
    quickbus_device_id,
    quickbus_device_label,
    selected_quickbus_devices,
    watchdog_user_plan_pick,
)
from .ha_address import webhook_url_error

_LOGGER = logging.getLogger(__name__)

# Form error of an ignore-list field that does not parse (translations: config.error and options.error).
INVALID_IGNORED_IDS = "invalid_ignored_ids"


def _normalize_ignored_ids(raw_input: str | None, prefix_chars: str = "Mm", category_label: str = "marker") -> str:
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
        raise vol.Invalid(f"Invalid {category_label} IDs: {', '.join(repr(t) for t in invalid_tokens[:3])}")

    return format_ignored_ids(ints, ranges)


def normalize_ignore_lists(user_input: dict, categories: Iterable[SourceCategory], errors: dict[str, str]) -> None:
    """Normalize the ignore-list of every category in user_input in place; a field error for each invalid one."""
    for cat in categories:
        try:
            ignored_raw = user_input.get(cat.ignored_conf_key, "").strip()
            prefix_chars = cat.audit_key_prefix + cat.audit_key_prefix.lower()
            user_input[cat.ignored_conf_key] = _normalize_ignored_ids(ignored_raw, prefix_chars, cat.label)
        except vol.Invalid as e:
            # Logged at debug only: the translated form error already tells the user what to fix.
            _LOGGER.debug(
                "Invalid ignore list %s=%r: %s", cat.ignored_conf_key, user_input.get(cat.ignored_conf_key), e
            )
            errors[cat.ignored_conf_key] = INVALID_IGNORED_IDS
        except Exception:
            # A defect, not the user's input: no "invalid list" on a list that may be fine.
            _LOGGER.exception("Unexpected error validating %s", cat.ignored_conf_key)
            errors[cat.ignored_conf_key] = "unknown"


def import_enabled_ignore_list_categories(conf: Mapping[str, Any]) -> list[SourceCategory]:
    """The categories with an ignore-list whose import conf switches on: only their lists act on anything."""
    return [cat for cat in ignore_list_categories() if conf.get(cat.import_conf_key, cat.import_default)]


def _saved_user_plan_pick(value: Any, candidates: dict[int, str]) -> str:
    """A saved user-plan pick; a bare ID saved before the name was takes the plan's current name."""
    pick = parse_watchdog_user_plan_pick(value)
    if pick is not None and pick[1] is None and pick[0] in candidates:
        return watchdog_user_plan_pick(pick[0], candidates[pick[0]])
    return str(value)


def _user_plan_pick_label(value: str) -> str:
    pick = parse_watchdog_user_plan_pick(value)
    if pick is None:
        return value
    fub_id, name = pick
    return f"ID {fub_id}" if name is None else f"{one_line(name)} (ID {fub_id})"


def _watchdog_user_plan_field(coordinator: Any, conf: dict[str, Any]) -> tuple[list[str], SelectSelector]:
    """Saved picks and selector of the user plans the watchdog also watches.

    Offers every plan except HA's own, by name; a pick is the plan's ID and name, so a deleted
    plan's reused ID does not pass the pick on. Saved picks Comexio no longer has under that ID
    and name stay choosable so they can be deselected.
    """
    candidates = coordinator.watchdog_user_plan_candidates() if coordinator is not None else {}
    # dict.fromkeys: an upgraded bare ID may equal a pick saved next to it.
    saved = list(
        dict.fromkeys(
            _saved_user_plan_pick(value, candidates) for value in conf.get(CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS) or []
        )
    )
    options = [
        SelectOptionDict(value=watchdog_user_plan_pick(fub_id, name), label=f"{one_line(name)} (ID {fub_id})")
        for fub_id, name in sorted(candidates.items(), key=lambda item: (item[1].casefold(), item[0]))
    ]
    known = {option["value"] for option in options}
    options.extend(
        SelectOptionDict(value=pick, label=_user_plan_pick_label(pick)) for pick in saved if pick not in known
    )
    # No translation_key: the options are this server's plan names.
    return saved, SelectSelector(SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN))


# Example the custom webhook URL field shows (a description placeholder, see async_step_webhook_url).
WEBHOOK_URL_EXAMPLE = "https://ha.example.com:8443"


def _hidden_categories(
    source_cats: list[SourceCategory], available_counts: dict, conf: dict[str, Any]
) -> set[WebioClass]:
    """The source categories the init step hides: no objects on the server, and not opted in."""
    return {
        cat.key
        for cat in source_cats
        if available_counts
        and not available_counts.get(cat.key)
        and not conf.get(cat.import_conf_key, cat.import_default)
    }


def _hidden_ignore_lists(
    source_cats: list[SourceCategory], conf: Mapping[str, Any], hidden_cats: set[WebioClass]
) -> set[WebioClass]:
    """The categories whose ignore-list field the init step hides: hidden ones and those with their import off.

    The list of a category that is not imported acts on nothing, so its field would only confuse. The
    saved list stays (see _fill_emptied_ignore_lists) and applies again once the import is back on.
    """
    enabled = {cat.key for cat in import_enabled_ignore_list_categories(conf)}
    return hidden_cats | ({cat.key for cat in source_cats} - enabled)


class ComexioOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the component."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry
        # The init step's input, held while the custom webhook URL step asks for the URL.
        self._pending_input: dict[str, Any] = {}
        # The Quickbus selection the form last showed, to tell an untouched default from a pick.
        self._quickbus_shown: list[str] = []
        # The hidden categories and hidden ignore-list fields of the init form last shown (see async_step_init).
        self._form_hidden: tuple[set[WebioClass], set[WebioClass]] | None = None

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
        hidden_cats, hidden_ignore_lists = self._init_form_hidden(
            source_cats, available_counts, conf, submitting=user_input is not None
        )

        if user_input is not None and (
            result := await self._async_submit_init(user_input, conf, errors, hidden_cats, hidden_ignore_lists)
        ):
            return result

        # Extension names for the IO cluster multi-select: live coordinator data first,
        # excluding offline extensions (no hardware present — wiring them is pointless),
        # but keeping already-saved selections choosable so they can be deselected.
        coordinator_data = getattr(coordinator, "data", None) or {}
        ext_names = _io_extension_options(coordinator_data, conf)
        quickbus_options = _quickbus_device_options(coordinator_data, conf)

        # Marker + KNX share the same schema/import/ignored-ids field shape (registry-driven
        # via ignore_list_categories — the source categories that support an ignore-list);
        # IO keeps its own fields since its unique_id/no-ignore-list shape differs.
        schema_dict: dict = {
            vol.Optional(cat.schema_conf_key, default=conf.get(cat.schema_conf_key, cat.schema_default)): str
            for cat in source_cats
            # Same visibility as the opt-in toggle below — a naming-schema field for a category
            # the user can't even opt into would be a dead field.
            if cat.key not in hidden_cats
        }
        schema_dict[vol.Optional(CONF_SCHEMA_IO, default=conf.get(CONF_SCHEMA_IO, DEFAULT_SCHEMA_IO))] = str
        for cat in source_cats:
            if cat.key in hidden_cats:
                continue
            schema_dict[
                vol.Required(cat.import_conf_key, default=conf.get(cat.import_conf_key, cat.import_default))
            ] = bool
        schema_dict[vol.Required("import_ios", default=conf.get("import_ios", True))] = bool
        schema_dict[vol.Required("import_quickbus", default=conf.get("import_quickbus", True))] = bool
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
            vol.Required(CONF_WEBHOOK_ADDRESS_MODE, default=conf.get(CONF_WEBHOOK_ADDRESS_MODE, WEBHOOK_ADDRESS_AUTO))
        ] = SelectSelector(
            SelectSelectorConfig(
                options=WEBHOOK_ADDRESS_MODES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="webhook_address_mode",
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
        # After an error, the lists as submitted: an invalid one stays in its field to be corrected.
        shown = conf if user_input is None else {**conf, **user_input}
        for cat in source_cats:
            # Hidden with the opt-in toggle above, and while the category's import is off: an
            # ignore-list for a category that is not imported would be a dead field.
            if cat.key in hidden_ignore_lists:
                continue
            # suggested_value, not default: HA sends no key for a field the user emptied, and a
            # default would fill the old list back in — the last entry could never be removed.
            schema_dict[
                vol.Optional(cat.ignored_conf_key, description={"suggested_value": shown.get(cat.ignored_conf_key, "")})
            ] = str
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
        if quickbus_options:
            # Only with Quickbus devices to pick (or a saved pick to drop) — a dead field otherwise.
            self._quickbus_shown = _shown_quickbus_pick(quickbus_options, conf, coordinator_data)
            schema_dict[vol.Optional(CONF_FUNCTION_PLAN_QUICKBUS_DEVICES, default=self._quickbus_shown)] = (
                SelectSelector(
                    SelectSelectorConfig(options=quickbus_options, multiple=True, mode=SelectSelectorMode.DROPDOWN)
                )
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
        saved_user_plans, user_plan_selector = _watchdog_user_plan_field(coordinator, conf)
        schema_dict[vol.Optional(CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS, default=saved_user_plans)] = user_plan_selector

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict), errors=errors)

    def _init_form_hidden(
        self, source_cats: list[SourceCategory], available_counts: dict, conf: dict[str, Any], *, submitting: bool
    ) -> tuple[set[WebioClass], set[WebioClass]]:
        """The hidden categories and hidden ignore-list fields of the init form.

        On a submit, those of the form shown, not of the entry as it is now: another dialog can switch an
        import on meanwhile, and a list field this form hid would then read as emptied.
        """
        if not submitting or self._form_hidden is None:
            hidden_cats = _hidden_categories(source_cats, available_counts, conf)
            self._form_hidden = (hidden_cats, _hidden_ignore_lists(source_cats, conf, hidden_cats))
        return self._form_hidden

    async def async_step_webhook_url(self, user_input=None):
        """Ask for the custom webhook address (only reached with the "custom" webhook address mode)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input.get(CONF_WEBHOOK_URL, "").strip()
            if error := webhook_url_error(url):
                errors[CONF_WEBHOOK_URL] = error
            else:
                return self._create_options_entry({**self._pending_input, CONF_WEBHOOK_URL: url})
        conf = {**self._config_entry.data, **self._config_entry.options}
        schema = vol.Schema({vol.Required(CONF_WEBHOOK_URL, default=conf.get(CONF_WEBHOOK_URL, "")): str})
        return self.async_show_form(
            step_id="webhook_url",
            data_schema=schema,
            errors=errors,
            # hassfest allows no URL in a translation string.
            description_placeholders={"example_url": WEBHOOK_URL_EXAMPLE},
        )

    async def _async_submit_init(
        self,
        user_input: dict,
        conf: dict,
        errors: dict,
        hidden_cats: set[WebioClass],
        hidden_ignore_lists: AbstractSet[WebioClass] = frozenset(),
    ):
        """Save the init step's input, or open the custom webhook URL step; None: the form again with the errors."""
        _drop_unchanged_quickbus_default(user_input, conf, self._quickbus_shown)
        self._normalize_user_input(user_input, errors, hidden_cats, hidden_ignore_lists)
        self._validate_webhook_address_mode(user_input, errors)
        if errors:
            return None
        if user_input.get(CONF_WEBHOOK_ADDRESS_MODE) == WEBHOOK_ADDRESS_CUSTOM:
            self._pending_input = user_input
            return await self.async_step_webhook_url()
        return self._create_options_entry(user_input)

    def _validate_webhook_address_mode(self, user_input: dict, errors: dict) -> None:
        """The "external" mode needs a usable external URL in HA's network settings."""
        if user_input.get(CONF_WEBHOOK_ADDRESS_MODE) != WEBHOOK_ADDRESS_EXTERNAL:
            return
        if (error := webhook_url_error(self.hass.config.external_url)) is not None:
            errors[CONF_WEBHOOK_ADDRESS_MODE] = (
                "insecure_external_url" if error == "insecure_url" else "no_external_url"
            )

    @staticmethod
    def _fill_emptied_ignore_lists(
        user_input: dict, source_cats: list[SourceCategory], hidden_cats: AbstractSet[WebioClass]
    ) -> None:
        """Set the ignore list of every shown category user_input has none for to "" (emptied).

        A visible field the user emptied is sent without its key, so it reads as an emptied list.
        A hidden list (see _hidden_ignore_lists) stays out of user_input: _create_options_entry then
        keeps the value the entry holds when it is saved, which another dialog may have changed while
        this one waited at the webhook URL step.
        """
        for cat in source_cats:
            if cat.key not in hidden_cats:
                user_input.setdefault(cat.ignored_conf_key, "")

    @staticmethod
    def _normalize_user_input(
        user_input: dict,
        errors: dict,
        hidden_cats: set[WebioClass],
        hidden_ignore_lists: AbstractSet[WebioClass] = frozenset(),
    ) -> None:
        """Validate and normalize user_input in-place; populate errors on failure.

        hidden_ignore_lists: the categories whose ignore-list field the form hid on top of hidden_cats.
        """
        source_cats = ignore_list_categories()
        hidden_lists = hidden_cats | hidden_ignore_lists
        ComexioOptionsFlow._fill_emptied_ignore_lists(user_input, source_cats, hidden_lists)

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

        # A hidden list keeps its stored value as it is: an error there would sit on a field the
        # form does not show, and block every other option.
        normalize_ignore_lists(user_input, [cat for cat in source_cats if cat.key not in hidden_lists], errors)

    def _create_options_entry(self, user_input: dict):
        """Merge new options with existing entry options and create the config entry.

        Preserves fields not shown in the form (e.g. passwords); explicitly removes
        empty ignore-list fields since HA won't auto-delete an emptied optional field, and
        the import_disabled "ignore" choice of every category whose import is on again.
        """
        merged_options = prune_import_disabled_ignored(
            {**self._config_entry.options, **user_input}, self._config_entry.data
        )
        for cat in ignore_list_categories():
            if not merged_options.get(cat.ignored_conf_key, "").strip():
                merged_options.pop(cat.ignored_conf_key, None)
        return self.async_create_entry(title="", data=merged_options)


def _io_extension_options(coordinator_data: dict[str, Any], conf: Mapping[str, Any]) -> list[str]:
    """Online extension names, plus saved picks that are offline or gone so they can be deselected."""
    ext_names = sorted({io["ext_name"] for io in coordinator_data.get("io", []) if not io.get("offline")})
    ext_names.extend(ext for ext in conf.get(CONF_FUNCTION_PLAN_IO_EXTENSIONS, []) if ext not in ext_names)
    return ext_names


def _shown_quickbus_pick(
    quickbus_options: list[SelectOptionDict], conf: Mapping[str, Any], coordinator_data: Mapping[str, Any]
) -> list[str]:
    """The Quickbus devices the form shows selected, in option order."""
    selected = selected_quickbus_devices(conf, coordinator_data)
    return [option["value"] for option in quickbus_options if option["value"] in selected]


def _drop_unchanged_quickbus_default(user_input: dict, conf: Mapping[str, Any], shown: Collection[str]) -> None:
    """Keep "every Quickbus device" implicit while a never-saved selection is submitted as shown.

    The form shows that default as a list; saving it would freeze the list, so a device added in
    Comexio later would stay unselected — no Web-IO command, no wiring, no value in HA. Compared
    with what the form showed, not with fresh data: a poll between show and submit changes nothing.
    """
    if CONF_FUNCTION_PLAN_QUICKBUS_DEVICES in conf or CONF_FUNCTION_PLAN_QUICKBUS_DEVICES not in user_input:
        return
    submitted = {str(device) for device in user_input[CONF_FUNCTION_PLAN_QUICKBUS_DEVICES]}
    # An empty pick is always deliberate: dropping it would select every device added later.
    if submitted and submitted == set(shown):
        _LOGGER.debug("Quickbus device selection left at its default (every device), not saved as a list")
        user_input.pop(CONF_FUNCTION_PLAN_QUICKBUS_DEVICES)


def _quickbus_device_options(coordinator_data: dict[str, Any], conf: Mapping[str, Any]) -> list[SelectOptionDict]:
    """Quickbus devices for the QB cluster multi-select: value = device id, label "QB3 MT1".

    Live devices first (by id), excluding those on an offline extension, then selected devices
    that are offline or gone — kept choosable so saving the form never drops them by accident
    (while the option was never saved, every device is selected).
    """
    names: dict[str, str] = {}
    offline: set[str] = set()
    for io in coordinator_data.get(QUICKBUS_IO_DATA_KEY, []):
        device = quickbus_device_id(io)
        names.setdefault(device, io["quickbus"].get("name") or "")
        if io.get("offline"):
            offline.add(device)
    by_id = sorted(names, key=lambda device: (len(device), device))
    live = [device for device in by_id if device not in offline]
    selected = dict.fromkeys(str(device) for device in conf.get(CONF_FUNCTION_PLAN_QUICKBUS_DEVICES, by_id))
    kept = [device for device in selected if device not in live]
    return [
        SelectOptionDict(value=device, label=one_line(quickbus_device_label(device, names.get(device))))
        for device in [*live, *kept]
    ]
