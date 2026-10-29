# Version: 0.8.0
import logging
import socket
from urllib.parse import urlsplit

from aiocomexio import format_fingerprint
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.util import slugify
import voluptuous as vol

from .api import ComexioAPI
from .certificate import (
    async_read_fingerprint,
    async_replaced_fingerprint,
    async_report_certificate_changed,
    connection_placeholders,
    is_valid_host,
    notify_https_disabled,
    notify_https_enabled,
)
from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_COVER_KEYWORDS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SERVER_ID,
    CONF_USERNAME,
    CONFIG_ENTRY_MINOR_VERSION,
    DEFAULT_COVER_KEYWORDS,
    DEFAULT_HOST,
    DOMAIN,
    KNOWN_DOMAINS,
    LOGIN_ERROR_CERTIFICATE,
    LOGIN_ERROR_CONNECTION,
    SCAN_INTERVAL_DEFAULT,
    SCAN_INTERVAL_OPTIONS,
)
from .options_flow import ComexioOptionsFlow, import_enabled_ignore_list_categories, normalize_ignore_lists

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


class HostAlreadyConfigured(HomeAssistantError):
    """Error to indicate an existing entry already talks to this Comexio server."""


class CertificateChanged(HomeAssistantError):
    """Error to indicate Comexio presents another certificate than the pinned one."""

    def __init__(self, actual: str | None) -> None:
        super().__init__("Comexio presents another certificate than the pinned one")
        self.actual = actual


# What a reconfigure does to the entry's HTTPS (see _async_pin_for_host).
_HTTPS_ENABLED = "enabled"
_HTTPS_DROPPED = "dropped"
_HTTPS_REPLACED = "replaced"
# The changes the user confirms first, and the step that asks.
_HTTPS_CONFIRM_STEPS = {_HTTPS_DROPPED: "reconfigure_http", _HTTPS_REPLACED: "reconfigure_certificate"}
# The ports a host without one reaches: 443 over HTTPS, 80 over HTTP. Which one an entry uses is known only
# after the TLS probe, so a duplicate check counts both.
_IMPLICIT_PORTS = (80, 443)
# Error and abort key: another entry already talks to this server.
_ALREADY_CONFIGURED = "already_configured"


def _lookup(host: str) -> str | None:
    """The host's IPv4 address, or None when it does not resolve (blocking: run in the executor)."""
    try:
        return socket.gethostbyname(host)
    except (OSError, ValueError, TypeError) as err:
        # ValueError: a name the IDNA codec refuses (an empty label "a..b", a label over 63 characters);
        # TypeError: an embedded NUL, which is_valid_host lets through (only reachable through the API)
        _LOGGER.debug("Host %s does not resolve: %s", host, err)
        return None


def _resolve_host(host: str) -> str:
    """The host's IPv4 address, or the host itself when it does not resolve (blocking).

    gethostbyname is IPv4 only: an IPv6 literal or an AAAA-only name is compared as written.
    """
    return _lookup(host) or host


def _split_port(host: str) -> tuple[str, int | None]:
    """The host's name or address and its explicit port, or the host itself when it does not parse."""
    try:
        parts = urlsplit(f"//{host}")
        return parts.hostname or host, parts.port
    except ValueError as err:  # bad port, IPv6 brackets or bare IPv6: compared as written
        _LOGGER.debug("Host %s has no parsable port: %s", host, err)
        return host, None


def _host_addresses(host: str) -> set[tuple[str, int]]:
    """The host as written and as resolved, each with its port, so a DNS name matches its IP (blocking).

    A host without a port reaches both implicit ports, so "comexio.local" matches "comexio.local:443";
    different explicit ports stay apart.
    """
    host = host.strip().lower()
    if not host:
        return set()
    name, port = _split_port(host)
    ports = _IMPLICIT_PORTS if port is None else (port,)
    return {(address, each) for address in (name, _resolve_host(name)) for each in ports}


def _configured_addresses(hosts: list[str]) -> set[tuple[str, int]]:
    """Every address and port the configured entries reach their Comexio server under (blocking)."""
    return set().union(*map(_host_addresses, hosts))


def _host_configured(host: str, configured_hosts: list[str]) -> bool:
    """Whether the host, as written or resolved, is one an existing entry uses (blocking)."""
    return bool(_host_addresses(host) & _configured_addresses(configured_hosts))


def _taken_server_id(server_id: str, entries) -> str | None:
    """The existing server ID that server_id collides with, verbatim or slugified; None while it is free.

    Slugified too: entity_ids are built from slugify(server_id) (see webio_range_check_entity_id()), so two
    distinct raw IDs (e.g. "my-server" vs. "my_server") would otherwise collide there.
    """
    slug = slugify(server_id)
    for entry in entries:
        existing_id = str(entry.data.get(CONF_SERVER_ID, "")).strip().lower()
        if existing_id == server_id or slugify(existing_id) == slug:
            return existing_id
    return None


def _discover_host(configured_hosts: list[str]) -> str:
    """The first resolvable comexio.<domain> no existing entry reaches yet, else DEFAULT_HOST if free (blocking).

    Skipping configured servers makes adding a second instance suggest the next one.
    """
    configured = _configured_addresses(configured_hosts)
    for domain in [""] + KNOWN_DOMAINS:
        test_host = f"comexio.{domain}" if domain else "comexio"
        address = _lookup(test_host)
        if address is None:
            continue
        if _host_addresses(address).isdisjoint(configured):
            return address
        _LOGGER.debug("Auto-discovery skips %s (%s): an entry already uses it", test_host, address)
    # An empty field rather than a placeholder the duplicate check would refuse
    return DEFAULT_HOST if _host_addresses(DEFAULT_HOST).isdisjoint(configured) else ""


class ComexioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comexio with detailed validation."""

    VERSION = 1
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    # The reconfigure settings waiting in a confirmation step (see _HTTPS_CONFIRM_STEPS).
    _pending_reconfigure: dict
    # The entry data the user step validated, waiting for the ignore_lists step.
    _pending_entry: dict
    # The configured hosts the pending host was already compared with (see _async_pending_entry_still_free).
    _checked_hosts: set[str]

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
        configured_hosts = [str(entry.data.get(CONF_HOST, "")) for entry in current_entries]

        if user_input is None:
            # Auto-discovery attempt to find the Comexio IP before showing the form
            default_host = await self.hass.async_add_executor_job(_discover_host, configured_hosts)
            _LOGGER.debug("Auto-discovered default host: %s", default_host)
        else:
            default_host = user_input.get(CONF_HOST, DEFAULT_HOST)
            _LOGGER.debug("Processing user input for Comexio setup: %s", default_host)
            try:
                # 1. Validation: Check if the chosen Server ID is already taken — compared both
                # verbatim and slugified, since entity_ids are built from slugify(server_id)
                # (see webio_range_check_entity_id()) and two distinct raw IDs (e.g. "my-server"
                # vs. "my_server") would otherwise collide there despite passing this check.
                chosen_id = user_input[CONF_SERVER_ID].strip().lower()
                if (existing_id := _taken_server_id(chosen_id, current_entries)) is not None:
                    _LOGGER.warning(
                        "Setup failed: Server ID '%s' collides with existing entry '%s'",
                        chosen_id,
                        existing_id,
                    )
                    errors[CONF_SERVER_ID] = "server_id_exists"
                    raise ServerIdExists

                # 2. Validation: no existing entry talks to this server already, compared resolved
                # (comexio.fritz.box vs. its IP) — two entries would fight over one Web-IO device
                if await self.hass.async_add_executor_job(_host_configured, user_input[CONF_HOST], configured_hosts):
                    _LOGGER.warning("Setup failed: host '%s' is already configured", user_input[CONF_HOST])
                    errors[CONF_HOST] = _ALREADY_CONFIGURED
                    raise HostAlreadyConfigured

                # 3. Validation: Test connection with the provided UI credentials — over HTTPS
                # with the certificate pinned when Comexio serves it, else plain HTTP.
                fingerprint = await async_read_fingerprint(user_input[CONF_HOST])
                await self._test_connection(user_input, fingerprint)

                # Store the cleaned server ID and coerce scan_interval to int
                user_input[CONF_SERVER_ID] = chosen_id
                user_input["scan_interval"] = int(user_input["scan_interval"])
                if fingerprint is not None:
                    user_input[CONF_CERTIFICATE_FINGERPRINT] = fingerprint
                self._pending_entry = user_input
                self._checked_hosts = set(configured_hosts)
            except (ServerIdExists, HostAlreadyConfigured):
                # Error already added to the errors dict
                pass
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CertificateChanged:
                # Read a moment ago, refused at the login: there is no entry (and no repair) yet.
                errors["base"] = "certificate_unstable"
            except Exception as e:
                _LOGGER.exception("Unexpected error during setup: %s", e)
                errors["base"] = "unknown"
            else:
                # Outside the try: an error from here on is no setup validation failure.
                if import_enabled_ignore_list_categories(user_input):
                    return await self.async_step_ignore_lists()
                return await self._async_create_entry_if_free()

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
                # Opt-in, default OFF — blind implementation, see project_knx_objects memory.
                vol.Required("import_knx", default=ui.get("import_knx", False)): bool,
                vol.Required("import_quickbus", default=ui.get("import_quickbus", True)): bool,
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

    async def async_step_ignore_lists(self, user_input=None):
        """Ask for the ignore-lists of the categories whose import the user step switched on.

        Optional fields: empty imports everything. The lists go to the entry's options, where the
        coordinator and the options flow read them.
        """
        categories = import_enabled_ignore_list_categories(self._pending_entry)
        errors: dict[str, str] = {}
        ignore_lists = {cat.ignored_conf_key: (user_input or {}).get(cat.ignored_conf_key, "") for cat in categories}
        if user_input is not None:
            if not await self._async_pending_entry_still_free():
                return self.async_abort(reason=_ALREADY_CONFIGURED)
            normalize_ignore_lists(ignore_lists, categories, errors)
            if not errors:
                return self._create_setup_entry({key: value for key, value in ignore_lists.items() if value})

        # suggested_value, not default: after an error the field shows what the user typed, and
        # a field the user emptied stays empty.
        schema = vol.Schema(
            {vol.Optional(key, description={"suggested_value": value}): str for key, value in ignore_lists.items()}
        )
        return self.async_show_form(step_id="ignore_lists", data_schema=schema, errors=errors)

    async def _async_create_entry_if_free(self):
        """Create the entry without ignore lists, unless another entry took its server meanwhile."""
        if not await self._async_pending_entry_still_free():
            return self.async_abort(reason=_ALREADY_CONFIGURED)
        return self._create_setup_entry({})

    async def _async_pending_entry_still_free(self) -> bool:
        """Whether no entry took the pending server ID or host since the user step checked them.

        Another setup flow can create an entry while this one awaits (the login, a host lookup). The
        final check therefore runs on fresh entries with no await before the caller creates the entry,
        and an entry added during a host lookup triggers another round. Only hosts not compared yet
        are looked up, so an unchanged set of entries costs no DNS query.
        """
        server_id, host = self._pending_entry[CONF_SERVER_ID], self._pending_entry[CONF_HOST]
        while True:
            entries = self._async_current_entries()
            if _taken_server_id(server_id, entries) is not None:
                _LOGGER.warning("Setup aborted: server ID '%s' was configured meanwhile", server_id)
                return False
            new_hosts = {str(entry.data.get(CONF_HOST, "")) for entry in entries} - self._checked_hosts
            if not new_hosts:
                return True
            if await self.hass.async_add_executor_job(_host_configured, host, sorted(new_hosts)):
                _LOGGER.warning("Setup aborted: host '%s' was configured meanwhile", host)
                return False
            self._checked_hosts |= new_hosts

    def _create_setup_entry(self, options: dict[str, str]):
        """Create the entry the user step validated, with the ignore-lists as its options."""
        data = self._pending_entry
        server_id, host = data[CONF_SERVER_ID], data[CONF_HOST]
        if (fingerprint := data.get(CONF_CERTIFICATE_FINGERPRINT)) is not None:
            # Only now: an abandoned ignore_lists step creates no entry to tell about.
            notify_https_enabled(self.hass, server_id, host, fingerprint)
        _LOGGER.info("Config entry created for server: %s", server_id)
        return self.async_create_entry(title=f"Comexio {server_id} ({host})", data=data, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ComexioOptionsFlow(config_entry)

    async def async_step_reconfigure(self, user_input=None):
        """Handle reconfiguration of connection credentials."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self._show_reconfigure_form(entry.data, {})

        merged = {**entry.data, **user_input}
        if not user_input.get(CONF_PASSWORD):
            merged[CONF_PASSWORD] = entry.data.get(CONF_PASSWORD, "")
        if not user_input.get(CONF_API_PASSWORD):
            merged[CONF_API_PASSWORD] = entry.data.get(CONF_API_PASSWORD, "")

        if not is_valid_host(merged[CONF_HOST]):
            # Not "no HTTPS": a pinned entry would be asked to switch to plain HTTP for a typo.
            return self._show_reconfigure_form(merged, {"base": "invalid_host"})
        if await self._async_host_taken(entry, merged[CONF_HOST]):
            _LOGGER.warning("Reconfigure refused: host '%s' is already configured", merged[CONF_HOST])
            return self._show_reconfigure_form(merged, {CONF_HOST: _ALREADY_CONFIGURED})
        try:
            https_change = await self._async_pin_for_host(entry, merged)
        except CertificateChanged as err:
            return self._show_reconfigure_form(merged, {"base": self._certificate_error(entry, merged, err.actual)})
        if https_change in _HTTPS_CONFIRM_STEPS:
            # No request is sent before the user agreed (only the TLS handshake that read the certificate):
            # neither credentials over plain HTTP nor to that certificate.
            self._pending_reconfigure = merged
            return self._show_https_confirm(entry, merged, https_change)
        return await self._async_apply_reconfigure(entry, merged, https_change)

    async def async_step_reconfigure_http(self, user_input=None):
        """Confirmed: the pinned entry goes back to plain HTTP, as its host answered no HTTPS."""
        return await self._async_apply_reconfigure(
            self._get_reconfigure_entry(), self._pending_reconfigure, _HTTPS_DROPPED
        )

    async def async_step_reconfigure_certificate(self, user_input=None):
        """Confirmed: the new host's certificate replaces the pinned one."""
        return await self._async_apply_reconfigure(
            self._get_reconfigure_entry(), self._pending_reconfigure, _HTTPS_REPLACED
        )

    async def _async_host_taken(self, entry, host: str) -> bool:
        """Whether the reconfigure moves the entry onto a host another entry already uses (compared as on setup).

        Only a changed host is checked, so entries that already share one still take new credentials.
        """
        if host.strip().lower() == str(entry.data.get(CONF_HOST, "")).strip().lower():
            return False
        others = [
            str(other.data.get(CONF_HOST, ""))
            for other in self._async_current_entries()
            if other.entry_id != entry.entry_id
        ]
        return bool(others) and await self.hass.async_add_executor_job(_host_configured, host, others)

    def _show_reconfigure_form(self, conf, errors: dict[str, str]):
        """The reconfigure form, prefilled from conf: the entry, or after an error what the user entered."""
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
            description_placeholders=connection_placeholders(self._get_reconfigure_entry().data),
        )

    def _show_https_confirm(self, entry, merged, https_change: str):
        """The confirmation step of a reconfigure that drops or replaces the pin, with the fingerprints involved."""
        placeholders = {
            "host": merged[CONF_HOST],
            "pinned": format_fingerprint(entry.data[CONF_CERTIFICATE_FINGERPRINT]),
        }
        if https_change == _HTTPS_REPLACED:
            placeholders["actual"] = format_fingerprint(merged[CONF_CERTIFICATE_FINGERPRINT])
        return self.async_show_form(step_id=_HTTPS_CONFIRM_STEPS[https_change], description_placeholders=placeholders)

    async def _async_apply_reconfigure(self, entry, merged, https_change: str | None):
        """Test the login with the merged settings and store them; the form again with the errors if it fails."""
        errors = await self._connection_errors(entry, merged)
        if errors:
            return self._show_reconfigure_form(merged, errors)
        self._notify_https_change(entry, merged, https_change)
        clean_options = {k: v for k, v in entry.options.items() if k not in _CREDENTIAL_KEYS}
        return self._async_update_and_reload_once(
            entry,
            title=f"Comexio {entry.data[CONF_SERVER_ID]} ({merged[CONF_HOST]})",
            data=merged,
            options=clean_options,
        )

    def _async_update_and_reload_once(self, entry, **changes):
        """Store the changes, abort the flow and reload the entry exactly once.

        A loaded entry's update listener reloads it when the changes land; HA's own reload on top would load it
        twice (and is refused from HA 2026.12 for entries with an update listener). Without a listener run — the
        entry is not loaded (a setup refused on the credentials), or nothing changed — the flow starts the
        reload: a reauth must restart the polls the refused login stopped, even with unchanged credentials. On a
        loaded entry it goes through the coordinator like the listener's, so it never cuts into a running sync.
        """
        loaded = entry.state is config_entries.ConfigEntryState.LOADED
        before = (entry.title, dict(entry.data), dict(entry.options))
        result = self.async_update_and_abort(entry, **changes)
        if loaded and (entry.title, dict(entry.data), dict(entry.options)) != before:
            return result
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id) if loaded else None
        if coordinator is None:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        else:
            self.hass.async_create_task(
                coordinator.async_reload_entry(f"{self.source} without changes"),
                f"comexio reload after {self.source} {entry.entry_id}",
            )
        return result

    @staticmethod
    async def _async_pin_for_host(entry, merged) -> str | None:
        """Read the certificate of the reconfigured host into merged; how the entry's HTTPS changes, None if not.

        An entry on plain HTTP pins the certificate its host serves, like at setup (_HTTPS_ENABLED). A pinned
        entry never changes its pin without asking: a host that answers no HTTPS — switched off, or just not
        reachable this moment — goes to plain HTTP only once the user confirms it (_HTTPS_DROPPED), and so
        does another host with another certificate (_HTTPS_REPLACED, both fingerprints shown). A changed
        certificate on the pinned host raises CertificateChanged before any login: the certificate_changed
        repair's business, never accepted along with other settings.
        """
        pinned = entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
        fingerprint = await async_read_fingerprint(merged[CONF_HOST])
        if fingerprint is None:
            return None if merged.pop(CONF_CERTIFICATE_FINGERPRINT, None) is None else _HTTPS_DROPPED
        if pinned is None:
            merged[CONF_CERTIFICATE_FINGERPRINT] = fingerprint
            return _HTTPS_ENABLED
        if fingerprint == pinned:
            return None
        if merged[CONF_HOST] == entry.data.get(CONF_HOST):
            raise CertificateChanged(fingerprint)
        merged[CONF_CERTIFICATE_FINGERPRINT] = fingerprint
        return _HTTPS_REPLACED

    def _certificate_error(self, entry, merged, actual: str | None) -> str:
        """Error key of a login test refused for the certificate; raises the repair for the pinned one.

        Only the entry's own host with its own pin is the repair's case: the repair checks that host. Another
        host — even one that presented the pinned certificate a moment ago — goes through reconfigure again.
        """
        if (
            actual is None
            or merged.get(CONF_CERTIFICATE_FINGERPRINT) != entry.data.get(CONF_CERTIFICATE_FINGERPRINT)
            or merged[CONF_HOST] != entry.data[CONF_HOST]
        ):
            # A certificate read a moment ago and refused at the login: nothing pinned to compare with.
            return "certificate_unstable"
        async_report_certificate_changed(self.hass, entry, actual)
        return "certificate_changed"

    def _notify_https_change(self, entry, merged, https_change: str | None) -> None:
        """Tell the user when a reconfigure switched the entry to HTTPS or back to plain HTTP."""
        server_id = entry.data[CONF_SERVER_ID]
        if https_change in (_HTTPS_ENABLED, _HTTPS_REPLACED):
            notify_https_enabled(self.hass, server_id, merged[CONF_HOST], merged[CONF_CERTIFICATE_FINGERPRINT])
        elif https_change == _HTTPS_DROPPED:
            _LOGGER.warning("Comexio at %s serves no HTTPS — the entry is back on plain HTTP", merged[CONF_HOST])
            notify_https_disabled(self.hass, server_id, merged[CONF_HOST])

    async def async_step_reauth(self, entry_data):
        """Start when Comexio refuses the stored admin credentials (at setup or on a poll)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for new admin credentials, test them and reload the entry with them."""
        entry = self._get_reauth_entry()
        errors = {}

        if user_input is not None:
            data_updates, errors = await self._reauth_test(entry, user_input)
            if not errors:
                # Like reconfigure: credentials left in options from older versions would shadow the new ones.
                clean_options = {k: v for k, v in entry.options.items() if k not in _CREDENTIAL_KEYS}
                return self._async_update_and_reload_once(entry, data_updates=data_updates, options=clean_options)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "admin")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def _reauth_test(self, entry, user_input) -> tuple[dict, dict[str, str]]:
        """The entry data to update and the form errors of a reauth's login test.

        Tested over the transport the next setup uses (async_create_api): an entry not yet pinned tries
        HTTPS first and is pinned when the login works there. Comexio refusing the credentials over
        HTTPS is the answer — a test over plain HTTP could pass where the setup is refused again, an
        endless reauth. Only when the HTTPS login cannot connect does the test fall back to HTTP, as
        the setup does.
        """
        merged = {**entry.data, **user_input}
        if CONF_CERTIFICATE_FINGERPRINT in merged:
            return user_input, await self._connection_errors(entry, merged)
        fingerprint = await async_read_fingerprint(merged[CONF_HOST])
        if fingerprint is not None:
            pinned = {**user_input, CONF_CERTIFICATE_FINGERPRINT: fingerprint}
            errors = await self._connection_errors(entry, {**merged, **pinned})
            if not errors:
                notify_https_enabled(self.hass, entry.data[CONF_SERVER_ID], merged[CONF_HOST], fingerprint)
                return pinned, errors
            if errors != {"base": "cannot_connect"}:
                return user_input, errors
            _LOGGER.warning(
                "Comexio at %s serves HTTPS but its login cannot be reached there — testing over plain HTTP",
                merged[CONF_HOST],
            )
        return user_input, await self._connection_errors(entry, merged)

    async def _connection_errors(self, entry, merged) -> dict[str, str]:
        """Form errors of a login test of entry with the settings merged; empty when it succeeds.

        The test goes over HTTPS with merged's pinned certificate, if any. _test_connection turns every
        other failure into CannotConnect (and logs it).
        """
        try:
            await self._test_connection(merged, merged.get(CONF_CERTIFICATE_FINGERPRINT))
        except CannotConnect:
            return {"base": "cannot_connect"}
        except InvalidAuth:
            return {"base": "invalid_auth"}
        except CertificateChanged as err:
            return {"base": self._certificate_error(entry, merged, err.actual)}
        return {}

    async def _test_connection(self, user_input, fingerprint: str | None = None):
        """Test if the login works using UI credentials, over HTTPS with fingerprint pinned if given."""
        api = None

        try:
            api = ComexioAPI(
                self.hass,
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                fingerprint=fingerprint,
            )

            _LOGGER.debug("Testing login credentials for %s", user_input[CONF_HOST])
            success = await api.login()
            if not success:
                if api.last_login_error == LOGIN_ERROR_CERTIFICATE:
                    mismatch = api.certificate_mismatch
                    raise CertificateChanged(mismatch.actual if mismatch is not None else None)
                if api.last_login_error == LOGIN_ERROR_CONNECTION:
                    # aiohttp reports a foreign certificate on a host name with several addresses as one.
                    if (actual := await async_replaced_fingerprint(api)) is not None:
                        raise CertificateChanged(actual)
                    _LOGGER.error("Login test could not reach Comexio at %s", user_input[CONF_HOST])
                    raise CannotConnect
                _LOGGER.error("Login test failed for user %s", user_input[CONF_USERNAME])
                raise InvalidAuth
        except Exception as e:
            if not isinstance(e, InvalidAuth | CannotConnect | CertificateChanged):
                _LOGGER.exception("Connection test failed: %s", e)
                raise CannotConnect from e
            raise
        finally:
            if api is not None:
                api.close()
