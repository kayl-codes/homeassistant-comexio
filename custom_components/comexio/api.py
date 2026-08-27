# Version: 0.7.5
import asyncio
import base64
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
import io
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from multidict import MultiDict

# Mandatory DOMAIN import for Audit logic
from .const import (
    COMEXIO_HTTP_TIMEOUT_SEC,
    FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH,
    FUNCTION_PLAN_LAYOUT_X_MARKER,
    FUNCTION_PLAN_LAYOUT_X_WEBIO,
    FUNCTION_PLAN_LAYOUT_Y_START,
    FUNCTION_PLAN_LAYOUT_Y_STEP,
    FUNCTION_PLAN_MANAGED_PLAN_COMMENT,
    FUNCTION_PLAN_PAIR_RELOAD_INITIAL_DELAY,
    FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS,
    KNOWN_DOMAINS,
    WEBIO_CLASS_IO,
    WEBIO_CLASS_MARKER,
    WEBIO_CLASSES,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    io_column_rows,
    io_sort_key,
    webio_class_label,
    webio_class_name,
)


class SafeDict(dict):
    """Safe dictionary for string formatting that doesn't crash on missing keys."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


LOCAL_HOSTNAME_RE = re.compile(r"^(?:localhost|[a-zA-Z0-9_-]+\.local|[a-zA-Z0-9_-]+\.lan|[a-zA-Z0-9_-]+\.home)\.?$")

# Function-plan element reference types needing special handling in function_plan_rebuild_plan_from_snapshot.
FUNCTION_PLAN_COMMENT_TYPE = 14
FUNCTION_PLAN_CONSTANT_TYPE = 16

# Module-level compiled patterns for get_raw_config (used on every coordinator refresh).
# $ioTypes = legacy (pre-v11); $IOTypesBinary = v11+ replacement with identical structure.
_IO_TYPES_DECL_RE = re.compile(r"var\s+\$ioTypes\s*=\s*")
_IO_BINARY_TYPES_DECL_RE = re.compile(r"var\s+\$IOTypesBinary\s*=\s*")
_IO_INPUT_TYPES_DECL_RE = re.compile(r"var\s+\$IOInputTypes\s*=\s*")
_SCRIPT_BLOCK_RE = re.compile(r"<script[^>]*>(.*?)</script[^>]{0,32}>", re.DOTALL | re.IGNORECASE)
# Comexio's own firmware/frontend version (e.g. "11.0.2"), from static asset paths
# (cache-busting), e.g. src="/11.0.2/js/cmb_admin.js" — cmb_admin.js is the generic
# admin-wide script, cmb_function_function_module.js is specific to the page we fetch;
# matching either is redundancy against a future filename change.
_COMEXIO_VERSION_RE = re.compile(
    r'src="/(\d+\.\d+\.\d+)/(?:js/cmb_admin\.js|'
    r'module/admin/function_function_module/js/cmb_function_function_module\.js)"'
)
_VAR_DECL_RE = re.compile(r"var\s+\$(\w+)\s*=\s*", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_CONTENT_TYPE_JSON = "Content-Type: application/json"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _js_timestamp() -> str:
    """Return a millisecond-precision UTC timestamp in JS Date.toISOString() format."""
    return datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)[:-3] + "Z"


def _is_local_address(host: str) -> bool:
    """Return True if host looks like a local IP or local hostname."""
    if not host:
        return False

    host = host.strip()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return False
        host = host[1:closing]
    elif ":" in host:
        host, _, _ = host.partition(":")

    with suppress(ValueError):
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    return bool(LOCAL_HOSTNAME_RE.match(host))


def _normalize_js_like_object(obj_str: str) -> str:
    """Remove trailing commas before closing braces/brackets to make JS objects JSON-compatible."""
    return _TRAILING_COMMA_RE.sub(r"\1", obj_str)


def _extract_js_object_literal(script_text: str, start_index: int) -> tuple[str | None, int]:
    """Extract a JS object literal starting at start_index (pointing at '{')."""
    if start_index >= len(script_text) or script_text[start_index] != "{":
        return None, start_index

    depth = 0
    i = start_index
    in_string: str | None = None
    escape = False

    while i < len(script_text):
        ch = script_text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script_text[start_index : i + 1], i + 1
        i += 1
    return None, start_index


_LOGGER = logging.getLogger(__name__)


def _is_extension_offline(identifier: str) -> bool:
    """Return True when identifier indicates an offline extension module.

    Online extensions report a serial number in 'XXXX-XXXX-XXXX' format;
    offline ones carry only a short model code without dashes (e.g. '5010').
    An empty string (missing field) is also treated as offline.
    """
    return "-" not in identifier


class ComexioAPI:
    """
    Detailed interface to communicate with the Comexio API.

    Handles:
    - RSA Login for Admin tasks.
    - Dashboard Refresh for live values.
    - Full Web-IO Lifecycle management.
    - Smart Delta Sync for individual command updates.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
        api_user: str | None = None,
        api_pass: str | None = None,
    ) -> None:
        """Initialize the API class with all required credentials."""
        self.hass: HomeAssistant = hass
        self.host: str = host
        self.username: str = username
        self.password: str = password
        self.api_user: str | None = api_user
        self.api_pass: str | None = api_pass

        # This context is vital for the Audit logic to find the configured webio_name
        self.config_entry: ConfigEntry | None = None

        # Dedicated cookie management for Comexio session persistence.
        # unsafe=True is only enabled if the target host is a local address.
        # Explicit timeout: without it, a stalled Comexio response (e.g. mid-firmware-update)
        # can hang a caller indefinitely instead of surfacing as a catchable error — this
        # previously wedged the Function Plan backup cycle's lock forever, silently stopping
        # all future backups.
        self.session: aiohttp.ClientSession = async_create_clientsession(hass, **self._build_session_kwargs())

        # Second, independently logged-in session used exclusively by the Function Plan
        # preview's Stufe-2 connection-value poll (see coordinator._async_poll_connection_values).
        # Own cookie jar/connection so that poll can never queue behind (or be queued behind
        # by) the main coordinator's own requests on the shared session — see
        # [[project-logikplan-preview]] on why a stuck live-poll starved the whole coordinator.
        # Created lazily on first use, not here — most setups never open the preview.
        self._preview_session: aiohttp.ClientSession | None = None

        # Comexio's own firmware/frontend version (e.g. "11.0.2"), from static asset paths
        self.comexio_version: str | None = None
        # io_types: TypeId → {binary, min, max, unit}  (from $ioTypes or $IOTypesBinary)
        self.io_types: dict[str, Any] = {}
        # io_input_types: TypeId → {input: bool}  (from $IOInputTypes)
        self.io_input_types: dict[str, Any] = {}
        # Function plan + paper metadata (populated by parse_config)
        self._fub_data: dict[str, Any] = {}  # fub_id_str → {Id, Name, Paper, ...}
        self._paper_data: dict[str, Any] = {}  # paper_id_str → {Id, Name, MMX, MMY}
        self._auth_warned: bool = False
        self._login_warned: bool = False
        # Set by login() on failure so callers (setup) can tell a transient connection
        # problem (retry) apart from a genuine credential rejection (needs reauth).
        self.last_login_error: str | None = None

    def _build_session_kwargs(self) -> dict[str, Any]:
        """Session kwargs shared by the main session and the preview session (own cookie jar each)."""
        session_kwargs: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=COMEXIO_HTTP_TIMEOUT_SEC)}
        if _is_local_address(self.host):
            session_kwargs["cookie_jar"] = aiohttp.CookieJar(unsafe=True)
        return session_kwargs

    async def ensure_preview_session(self) -> aiohttp.ClientSession | None:
        """Lazily create + log in the dedicated Stufe-2 preview session, reused after that.

        Returns None if the login fails — the caller falls back to the main session for that
        one poll tick rather than blocking the preview on a retry loop; the next tick tries
        the dedicated session again from scratch (a fresh session, since a stale/rejected
        cookie jar wouldn't fix itself).
        """
        if self._preview_session is not None:
            return self._preview_session
        session = async_create_clientsession(self.hass, **self._build_session_kwargs())
        if not await self.login(session=session):
            _LOGGER.warning("Preview session login failed — Stufe-2 poll falls back to the main session")
            await session.close()
            return None
        self._preview_session = session
        return session

    @property
    def _base_url(self) -> str:
        """Return the base URL for the Comexio IO-Server."""
        return f"http://{self.host}"

    @property
    def fub_data(self) -> dict[str, Any]:
        """Return function plan metadata (fub_id_str → {Id, Name, Paper, ...}), populated by parse_config()."""
        return self._fub_data

    def update_fub_cache_entry(self, fub_id: int | str, fub_info: dict[str, Any]) -> None:
        """Refresh a single plan's cached metadata (e.g. after an out-of-band get_raw_config() lookup)."""
        self._fub_data[str(fub_id)] = fub_info

    def _clean_value(self, val: Any) -> float:
        """Standardizes values: replaces German comma with dot and converts to numbers."""
        if val is None:
            return 0
        if isinstance(val, str):
            val = val.replace(",", ".")
        try:
            return float(val)
        except (ValueError, TypeError):
            _LOGGER.warning("Failed to clean value: %s", val)
            return 0

    async def get_ha_address(self) -> str:
        """
        Dynamically determines the Home Assistant address (DNS:Port or IP:Port)
        to be used for Comexio webhooks.
        """
        try:
            internal_url = self.hass.config.internal_url
            port = 8123
            fallback_ip = None

            if internal_url:
                parsed = urlparse(internal_url)
                port = parsed.port or 8123
                fallback_ip = parsed.hostname

            def resolve_dns():
                for domain in KNOWN_DOMAINS:
                    test_host = f"homeassistant.{domain}"
                    try:
                        socket.gethostbyname(test_host)
                        return test_host
                    except OSError:
                        continue
                return None

            hostname = await self.hass.async_add_executor_job(resolve_dns)

            if not hostname:
                if not fallback_ip or fallback_ip in ["localhost", "127.0.0.1", "::1"]:

                    def get_local_ip():
                        # Try private IPv4 routing first
                        with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                            s.connect(("10.255.255.255", 1))
                            return s.getsockname()[0]

                        # Fallback to IPv6 local routing (ULA prefix)
                        with suppress(OSError), socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
                            s.connect(("fd00::", 1))
                            return s.getsockname()[0]

                        with suppress(OSError):
                            return socket.gethostbyname(socket.gethostname())

                        return "127.0.0.1"

                    hostname = await self.hass.async_add_executor_job(get_local_ip)
                else:
                    hostname = fallback_ip

            # Wrap IPv6 addresses in brackets for URL compatibility
            with suppress(ValueError):
                if ipaddress.ip_address(hostname).version == 6:
                    hostname = f"[{hostname}]"

            return f"{hostname}:{port}"
        except Exception as e:
            _LOGGER.exception("Failed to determine HA address: %s", e)
            return "127.0.0.1:8123"

    def _encrypt_block(self, data_str: str, mod: int, exp: int) -> str:
        """RSA encryption logic matching Comexio v11 (PKCS1v15)."""
        try:
            pub_key = rsa.RSAPublicNumbers(exp, mod).public_key()
            encrypted = pub_key.encrypt(data_str.encode("iso-8859-1"), padding.PKCS1v15())
            required_len = ((pub_key.key_size + 7) // 8) * 2
            return encrypted.hex().zfill(required_len)
        except Exception:
            _LOGGER.exception("RSA Block encryption failed")
            raise

    async def login(self, session: aiohttp.ClientSession | None = None) -> bool:
        """Performs the RSA login procedure for admin access.

        session: defaults to the main session; pass the dedicated preview session
        (see ensure_preview_session) to log that one in independently instead.
        """
        sess = session if session is not None else self.session
        if not _is_local_address(self.host) and not self._login_warned:
            _LOGGER.warning(
                "Logging into Comexio over plain HTTP on a non-local address (%s). "
                "Credentials may be transmitted in clear text.",
                self.host,
            )
            self._login_warned = True

        _LOGGER.debug("Starting v11 RSA login procedure for host: %s", self.host)
        url = f"{self._base_url}/board/home/login/"

        sess.cookie_jar.update_cookies({"comexio-client-time": str(int(time.time()))})
        try:
            async with sess.post(url, data={"login_keys": "true"}) as resp:
                keys = await resp.json(content_type=None)

            salt_str = base64.b64decode(keys["salt"]).decode("iso-8859-1")
            mod, exp = int(keys["modulus"], 16), int(keys["exponent"], 16)
            nonce = "".join(secrets.choice("0123456789ABCDEF") for _ in range(20))

            pw_part1 = self._encrypt_block(salt_str + nonce + self.password, mod, exp)
            pw_part2 = self._encrypt_block(salt_str + nonce + "", mod, exp)
            pw = f"{pw_part1} {pw_part2}"
            payload = MultiDict(
                [
                    ("target", "/board/home/login"),
                    ("username", self.username),
                    ("password", pw),
                    ("loginsubmit", "Anmelden"),
                    ("encryption", "rsa"),
                ]
            )

            async with (
                sess.post(url, data=payload, headers={"Referer": url}) as resp,
                sess.get(f"{self._base_url}/admin/") as v_resp,
            ):
                html = await v_resp.text()
                if "Anmeldung" not in html and html != "":
                    _LOGGER.info("Successfully logged into Comexio Admin interface")
                    self.last_login_error = None
                    return True
            self.last_login_error = "rejected"
            return False
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as e:
            _LOGGER.exception("Critical error during Comexio login: %s", e)
            self.last_login_error = "connection"
            return False

    async def get_raw_config(self) -> dict[str, Any]:
        """
        Downloads JS config objects and global IO types from the admin interface.
        This provides the source of truth for all device properties and units.
        """
        # 1. Fetch the main admin page to get global variables like $ioTypes
        url_main = f"{self._base_url}/admin/"
        async with self.session.get(url_main) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch admin page for IO types (HTTP %s)", resp.status)
                return {}

            main_html = await resp.text()

        # Try $ioTypes (legacy) then $IOTypesBinary (Comexio v11+) — both have identical structure.
        self.io_types = {}
        for decl_re, var_name in (
            (_IO_TYPES_DECL_RE, "$ioTypes"),
            (_IO_BINARY_TYPES_DECL_RE, "$IOTypesBinary"),
        ):
            if assign_match := decl_re.search(main_html):
                brace_index = main_html.find("{", assign_match.end())
                if brace_index != -1:
                    raw_object, _ = _extract_js_object_literal(main_html, brace_index)
                    if raw_object:
                        try:
                            self.io_types = json.loads(_normalize_js_like_object(raw_object))
                            _LOGGER.debug("Loaded %d IO types from %s", len(self.io_types), var_name)
                            break
                        except json.JSONDecodeError as exc:
                            _LOGGER.warning("Failed to decode %s: %s", var_name, exc)
        else:
            _LOGGER.warning("No IO type data found ($ioTypes / $IOTypesBinary) — using identifier fallback")

        # Extract $IOInputTypes: TypeId → {input: bool}  (input=True means read-only sensor)
        self.io_input_types = {}
        if assign_match := _IO_INPUT_TYPES_DECL_RE.search(main_html):
            brace_index = main_html.find("{", assign_match.end())
            if brace_index != -1:
                raw_object, _ = _extract_js_object_literal(main_html, brace_index)
                if raw_object:
                    try:
                        self.io_input_types = json.loads(_normalize_js_like_object(raw_object))
                        _LOGGER.debug("Loaded %d IO input types", len(self.io_input_types))
                    except json.JSONDecodeError as exc:
                        _LOGGER.warning("Failed to decode $IOInputTypes: %s", exc)

        # 2. Fetch the function module page for the technical device configuration
        url_conf = f"{self._base_url}/admin/function_function_module/home"
        async with self.session.get(url_conf) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch function module page (HTTP %s)", resp.status)
                return {}
            html = await resp.text()

        if version_match := _COMEXIO_VERSION_RE.search(html):
            self.comexio_version = version_match.group(1)

        # Restrict search to script tags to avoid scanning entire HTML with a single DOTALL regex
        script_blocks = _SCRIPT_BLOCK_RE.findall(html)

        result: dict[str, Any] = {}
        for script in script_blocks:
            for m in _VAR_DECL_RE.finditer(script):
                var_name = m.group(1)
                search_start = m.end()
                brace_index = script.find("{", search_start)
                if brace_index == -1:
                    continue

                raw_obj, _ = _extract_js_object_literal(script, brace_index)
                if raw_obj is None:
                    continue

                normalized_obj = _normalize_js_like_object(raw_obj)

                try:
                    result[var_name] = json.loads(normalized_obj)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning(
                        "Failed to decode JSON for variable $%s on function module page: %s",
                        var_name,
                        exc,
                    )
                    continue
        return result

    async def get_live_states(self, marker_count: int) -> dict[str, Any]:
        """Fetches current live values for markers from the dashboard refresh endpoint."""
        url = f"{self._base_url}/board/dashboard/refresh/"
        markers_dict = {str(i): {"action": "get", "MarkerName": f"M{i}"} for i in range(1, marker_count + 1)}
        markers_dict["messages"] = {"action": "messages"}

        form_data = aiohttp.FormData()
        form_data.add_field("json", json.dumps(markers_dict))

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/",
            "User-Agent": "Mozilla/5.0",
        }

        try:
            async with self.session.post(url, data=form_data, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Live states fetch failed with HTTP status: %s", resp.status)
                    return {}
                try:
                    data = await resp.json(content_type=None)
                    return data.get("result", {})
                except Exception:
                    raw_text = await resp.text()
                    _LOGGER.exception(
                        "Failed to parse live states response as JSON; raw response: %s",
                        raw_text,
                    )
                    return {}
        except aiohttp.ClientError as err:
            _LOGGER.exception("HTTP request error fetching live states: %s", err)
            return {}
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching live states: %s", e)
            return {}

    async def get_function_plan_connection_values(
        self, fub_id: int, session: aiohttp.ClientSession | None = None
    ) -> dict[str, list[Any]]:
        """Fetch live per-SOURCE-ELEMENT output values for one Function Plan — Studio's own
        "fupValueData" refresh action, the same /board/dashboard/refresh/ endpoint
        get_live_states uses. Unlike markers/IOs/WebIOs (get_live_states, resolved to
        pill elements), this reports values for every block-internal output (an "Oder"
        gate, a Zeitglied, ...) that carries no marker/IO of its own — the ground truth
        behind the Function Plan preview's Stufe-2 wire coloring (see
        [[project-logikplan-preview]]). Returns {source_element_id: [value_per_output_row]}
        — the dict key is the SOURCE FubElementId (not a connection/wire id: a block with
        several outputs reports one array for all of them, indexed by output IOPos, and
        several wires from the same output row share that one value).

        session: defaults to the main session; the Stufe-2 poll passes its own dedicated,
        independently logged-in session (see ensure_preview_session) so this high-frequency
        call can never queue behind — or block — the main coordinator poll on a shared
        connection.
        """
        sess = session if session is not None else self.session
        url = f"{self._base_url}/board/dashboard/refresh/"
        payload = {"connection": {"action": "fupValueData", "fupId": fub_id}}
        form_data = aiohttp.FormData()
        form_data.add_field("json", json.dumps(payload))

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/",
            "User-Agent": "Mozilla/5.0",
        }

        _LOGGER.debug("Connection values request: POST %s payload=%s", url, payload)
        try:
            async with sess.post(url, data=form_data, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("Connection values fetch failed (fub=%s, HTTP %s)", fub_id, resp.status)
                    return {}
                try:
                    data = await resp.json(content_type=None)
                    raw = (data.get("result") or {}).get("connection")
                    _LOGGER.debug("Connection values raw response (fub=%s): %s", fub_id, raw)
                    # Comexio returns the plain-text sentinel "0:not_found" (not JSON) instead
                    # of a value dict when the plan isn't currently running — confirmed live
                    # 2026-08-22: an active plan (fub=1) returns a real JSON dict every poll,
                    # an inactive one (freshly restored/stopped plans included) always returns
                    # this sentinel. Expected/frequent, not a parse failure — the old code
                    # logged a full ERROR-level exception for it on every 2s poll tick.
                    if isinstance(raw, str) and raw and not raw.lstrip().startswith(("{", "[")):
                        _LOGGER.debug("Connection values: no live data for fub=%s (plan not active: %s)", fub_id, raw)
                        return {}
                    parsed = json.loads(raw) if raw else {}
                    # Same PHP array/object ambiguity as function_plan_load_elements: an
                    # associative array is serialized as a JSON list whenever its keys are
                    # exactly 0..N-1 in order — meaning the list position IS the real source
                    # FubElementId, not a guess (a small/quiet plan's element ids can easily
                    # land on that sequential shape, e.g. fub=19 with 0 connections -> "[]").
                    if isinstance(parsed, list):
                        parsed = {str(i): vals for i, vals in enumerate(parsed)}
                    result = {elem_id: vals if isinstance(vals, list) else [vals] for elem_id, vals in parsed.items()}
                    _LOGGER.debug("Connection values parsed (fub=%s): %s", fub_id, result)
                    return result
                except Exception:
                    _LOGGER.exception("Failed to parse connection values response (fub=%s)", fub_id)
                    return {}
        except aiohttp.ClientError:
            _LOGGER.exception("HTTP request error fetching connection values (fub=%s)", fub_id)
            return {}
        except Exception:
            _LOGGER.exception("Unexpected error fetching connection values (fub=%s)", fub_id)
            return {}

    def parse_config(
        self,
        conf: dict[str, Any],
        live_states: dict[str, Any] | None = None,
        referenced_markers: set[str] | None = None,
    ) -> dict[str, Any]:
        """
        Processes the raw configuration and performs a technical audit.
        Uses dynamic IO type mapping to determine binary vs analog states and units.
        """
        data = {
            "markers": [],
            "io": [],
            "io_all": [],
            "webio_commands": {},
            "webio_names": {},
            # Two separate Web-IO device classes on the Comexio server — see const.webio_class_name.
            "webio_devices": {cls: {"device_id": None, "device_ip": None, "base_id": None} for cls in WEBIO_CLASSES},
            # Per-extension identity (name + stable serial), see _process_ios — used by the
            # coordinator's extension-rename migration to detect a Comexio-side rename.
            "extensions": {},
        }
        live_states = live_states or {}

        # Cache function plan + paper metadata for later use (e.g. auto canvas-format detection)
        self._fub_data = conf.get("Fubs", {})
        self._paper_data = conf.get("Paper", {})

        # Load configuration
        config_names = self._load_config_names()
        webio_name, schema_marker, schema_io, server_alias = config_names

        # Extract FubModules once
        fub_modules = conf.get("FubModules", {})

        # 2. Map Webhooks and device info
        self._process_device_info(conf, data, webio_name, fub_modules)

        # 3. Process Markers
        self._process_markers(data, live_states, schema_marker, server_alias, fub_modules, referenced_markers)

        # 4. Process IOs
        self._process_ios(data, schema_io, server_alias, fub_modules)

        _LOGGER.info(
            "Audit: %d Markers, %d IOs, %d Webhooks in Comexio for %s",
            len(data["markers"]),
            len(data["io"]),
            len(data["webio_commands"]),
            webio_name,
        )
        return data

    def _load_config_names(self) -> tuple[str, str, str, str]:
        """Load configuration names from config_entry."""
        webio_name = "HomeAssistant"
        schema_marker = "M{MarkerId} {MarkerTitle}"
        schema_io = "{ExtName} {IoId} {IoTitle}"
        server_alias = "comexio"

        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", webio_name)
            schema_marker = conf_data.get("schema_marker", schema_marker)
            schema_io = conf_data.get("schema_io", schema_io)
            server_alias = conf_data.get("server_id", server_alias)

        return webio_name, schema_marker, schema_io, server_alias

    # Reference canvas bounds: A4 landscape at 90 DPI (empirically measured on live Comexio)
    _CANVAS_REF_X: float = 870.0
    _CANVAS_REF_Y: float = 720.0
    _CANVAS_REF_MM_LONG: int = 297  # A4 long side (landscape width)
    _CANVAS_REF_MM_SHORT: int = 210  # A4 short side (landscape height)
    _CANVAS_REF_RES: int = 90
    # Paper name → (long side mm, short side mm) for explicit format override
    _PAPER_MM_BY_NAME: dict[str, tuple[int, int]] = {
        "A2": (594, 420),
        "A3": (420, 297),
        "A4": (297, 210),
        "A5": (210, 148),
    }

    def get_fub_paper_format(self, fub_id: int) -> str:
        """Return the paper format name (e.g. 'A4') for a function plan, defaulting to 'A4'."""
        fub = self._fub_data.get(str(fub_id), {})
        paper_id = str(fub.get("Paper", ""))
        paper = self._paper_data.get(paper_id, {})
        return str(paper.get("Name", "A4"))

    def get_fub_dpi(self, fub_id: int) -> int:
        """Return the configured resolution (DPI) for a function plan, defaulting to 90."""
        fub = self._fub_data.get(str(fub_id), {})
        return int(fub.get("Resolution", self._CANVAS_REF_RES))

    def get_fub_orientation(self, fub_id: int) -> str:
        """Return 'portrait' or 'landscape' for a function plan, defaulting to 'landscape'."""
        fub = self._fub_data.get(str(fub_id), {})
        return "portrait" if int(fub.get("Orientation", 0)) == 1 else "landscape"

    def get_fub_active(self, fub_id: int) -> bool | None:
        """Return a function plan's active flag, or None if the plan is not known live."""
        fub = self._fub_data.get(str(fub_id))
        if fub is None:
            return None
        return bool(int(fub.get("Active") or 0))

    def get_fub_canvas_bounds(
        self, fub_id: int, paper_name: str | None = None, orientation: str | None = None
    ) -> tuple[float, float]:
        """Return estimated (x_max, y_max) canvas bounds for a function plan.

        Scales proportionally from the A4-landscape-90-DPI reference (870×720).
        DPI (Resolution) is always taken from $Fubs plan data.
        paper_name overrides the format (A2/A3/A4/A5); None = read from $Fubs/$Paper.
        orientation overrides as "landscape"/"portrait" — needed for a plan that was just
        created and is not in the cached $Fubs data yet; None = read from $Fubs, where
        0 = landscape (long side → X), 1 = portrait (long side → Y).
        """
        fub = self._fub_data.get(str(fub_id), {})
        res = int(fub.get("Resolution", self._CANVAS_REF_RES))
        if orientation is None:
            orient_id = int(fub.get("Orientation", 0))
        else:
            orient_id = 1 if orientation.lower() == "portrait" else 0

        if paper_name and paper_name in self._PAPER_MM_BY_NAME:
            mm_long, mm_short = self._PAPER_MM_BY_NAME[paper_name]
        else:
            paper_id = str(fub.get("Paper", ""))
            paper = self._paper_data.get(paper_id, {})
            mm_long = paper.get("MMX", self._CANVAS_REF_MM_LONG)
            mm_short = paper.get("MMY", self._CANVAS_REF_MM_SHORT)

        if orient_id == 0:
            width_mm, height_mm = mm_long, mm_short
        else:
            width_mm, height_mm = mm_short, mm_long

        x_max = self._CANVAS_REF_X * (width_mm / self._CANVAS_REF_MM_LONG) * (res / self._CANVAS_REF_RES)
        y_max = self._CANVAS_REF_Y * (height_mm / self._CANVAS_REF_MM_SHORT) * (res / self._CANVAS_REF_RES)
        return x_max, y_max

    def _process_device_info(
        self,
        conf: dict[str, Any],
        data: dict[str, Any],
        webio_name: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process device info and webhooks for both Web-IO classes (marker/io)."""
        web_devices = conf.get("WebDevices", {})
        fub_10 = fub_modules.get("10", {})

        missing_classes = []
        for webio_class in WEBIO_CLASSES:
            target_dev_id = self._assign_webio_device_id(web_devices, data, webio_name, webio_class)
            if target_dev_id and target_dev_id in fub_10:
                for w_id, w_obj in fub_10[target_dev_id].items():
                    self._add_webhook_command(data, w_id, w_obj, webio_class)
            elif not target_dev_id:
                missing_classes.append(webio_class)

        if missing_classes and any(d.get("Name") == webio_name for d in web_devices.values()):
            # Pre-split installs have a single Web-IO device named exactly `webio_name`; it
            # won't match the new "<name> [M]"/"<name> [IO]" class names, so both classes look
            # missing right after upgrading. No automatic migration on purpose (see CLAUDE.md:
            # no backwards-compat shims) — a Full Sync creates the new class(es); the old device
            # is left untouched and can be removed manually once no longer needed.
            _LOGGER.warning(
                "Found a legacy Web-IO device named '%s' without a Marker/IO class suffix. "
                "This version splits Web-IO into separate classes ('%s' / '%s'); run a Full Sync "
                "to create the missing class(es): %s. The old device is left in place.",
                webio_name,
                webio_class_name(webio_name, WEBIO_CLASS_MARKER),
                webio_class_name(webio_name, WEBIO_CLASS_IO),
                ", ".join(webio_class_label(c) for c in missing_classes),
            )

        self._build_webio_name_lexicon(data, fub_10)

    def _assign_webio_device_id(
        self,
        web_devices: dict[str, Any],
        data: dict[str, Any],
        webio_name: str,
        webio_class: str,
    ) -> str | None:
        """Find the WebDevices entry for one Web-IO class, populate its device_info, return its id."""
        class_name = webio_class_name(webio_name, webio_class)
        for d_id, d_data in web_devices.items():
            if d_data.get("Name") != class_name:
                continue
            target_dev_id = str(d_id)
            dev_info = data["webio_devices"][webio_class]
            dev_info["device_id"] = target_dev_id
            # Comexio has been observed to scrape a leading space into the Ip field, which
            # broke the IP-mismatch audit (mismatch reported against an otherwise-identical
            # address) — stripped here, at the single point the value enters HA.
            raw_ip = d_data.get("Ip")
            dev_info["device_ip"] = raw_ip.strip() if isinstance(raw_ip, str) else raw_ip
            raw_base_id = d_data.get("BaseId")
            dev_info["base_id"] = str(raw_base_id) if raw_base_id is not None else None
            return target_dev_id
        return None

    def _build_webio_name_lexicon(self, data: dict[str, Any], fub_10: dict[str, Any]) -> None:
        """Build the webio_names label lexicon over ALL Web-IO classes (read-only, for Function Plan rendering).

        Plans may wire commands of foreign Web-IO devices, whose names are otherwise unknown to
        HA. Names mirror Comexio Studio's pill labels: '{deviceId}. {commandName}' (e.g.
        '16. R1 SZ Rollo % IST') — Studio does NOT include the device name in the pill (verified
        against the Netzteil plan). Kept separate from webio_commands on purpose — that dict
        drives the sync/audit logic and must only ever contain HA's own class.
        """
        groups = fub_10.items() if isinstance(fub_10, dict) else enumerate(fub_10 or [])
        for dev_id, dev_commands in groups:
            prefix = f"{dev_id}. "
            # Comexio serializes gap-free id groups as JSON arrays instead of objects
            # (same quirk as $FubModules groups) — the array index then IS the webIoId.
            items = dev_commands.items() if isinstance(dev_commands, dict) else enumerate(dev_commands or [])
            for w_id, w_obj in items:
                if not isinstance(w_obj, dict):
                    continue
                name = w_obj.get("Name")
                if name:
                    data["webio_names"][str(w_id)] = {
                        "name": f"{prefix}{name}",
                        "analog": w_obj.get("TypeId") in {2, "2"},
                    }

    def _add_webhook_command(self, data: dict[str, Any], w_id: str, w_obj: dict[str, Any], webio_class: str) -> None:
        """Add a webhook command to data."""
        raw_type = w_obj.get("TypeId")
        try:
            val_type = int(raw_type) if raw_type is not None else 1
        except (ValueError, TypeError):
            val_type = 1

        # webIoId (w_id) is a global counter across ALL Web-IO devices on the server (verified
        # live) — safe to key this single flat dict by command name regardless of webio_class.
        data["webio_commands"][w_obj.get("Name")] = {
            "webIoId": w_id,
            "cmdId": w_obj.get("WebCommandId"),
            "typeId": val_type,
            "webioClass": webio_class,
        }

    # Placeholder title for a Comexio marker with an empty label that is still wired into a
    # plan (see _process_markers) — kept out of the entity/name comparison logic nowhere
    # special-cased on purpose: once imported, it behaves exactly like any other marker name,
    # so a later real rename in Comexio is picked up by the normal sync/rename detection.
    _NO_NAME_MARKER_TITLE = "#nn"

    def _process_markers(
        self,
        data: dict[str, Any],
        live_states: dict[str, Any],
        schema_marker: str,
        server_alias: str,
        fub_modules: dict[str, Any],
        referenced_marker_ids: set[str] | None = None,
    ) -> None:
        """Process markers from config.

        A marker without a Comexio label is normally excluded entirely — but one that is
        actually wired into a function plan (referenced_marker_ids) is imported anyway with
        a synthetic "#nn" title, so it gets a real HA entity/webhook/Web-IO command and the
        plan preview knows its type + live value. If the marker later gets a real name in
        Comexio, or drops out of every plan, it naturally reverts to the normal path (named
        marker, or orphaned like any other unused marker) — no special-case cleanup needed.
        """
        referenced_marker_ids = referenced_marker_ids or set()
        for m in fub_modules.get("2", {}).values():
            if m.get("Id") is None:
                continue

            m_id = str(m.get("Id"))
            has_name = bool(m.get("Name"))
            if not has_name and m_id not in referenced_marker_ids:
                continue

            m_type_raw = m.get("Type", 1)
            m_type_str = "analog" if m_type_raw in [2, 3] else "digital"
            m_title = m.get("Name") or self._NO_NAME_MARKER_TITLE

            ha_name = schema_marker.format_map(SafeDict(ServerAlias=server_alias, MarkerId=m_id, MarkerTitle=m_title))

            data["markers"].append(
                {
                    "id": m_id,
                    "ha_name": " ".join(ha_name.split()),
                    "name": f"M{m_id} {m_title}",
                    # Unnamed-but-referenced marker ("#nn"): the plan preview greys it out
                    # like an inactive IO as a visual hint that it has no label in Comexio.
                    "no_name": not has_name,
                    "type": m_type_str,
                    "type_raw": m_type_raw,
                    "value": self._clean_value(live_states.get(m_id, 0)),
                }
            )

    def _process_ios(
        self,
        data: dict[str, Any],
        schema_io: str,
        server_alias: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process IOs from config.

        Inactive IOs (Active=False, e.g. an extension slot the user prepared but hasn't
        wired up yet) get no entity/webhook — Comexio itself refuses to wire a connection
        to an inactive IO, so there is nothing meaningful to read/write. They still get a
        proper label in "io_all" (unfiltered) so the Function Plan preview can resolve their
        name instead of falling back to a bare "IO ref=N".
        """
        for ext_id, ext_content in fub_modules.get("1", {}).items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")
            ext_serial = ext_meta.get("Identifier", "")
            ext_offline = _is_extension_offline(ext_serial)
            data["extensions"][ext_id] = {"name": ext_name, "serial": ext_serial}

            for io_item in ext_content.get("inoutput", {}).values():
                if not io_item:
                    continue

                io_type_id = str(io_item.get("InOutputTypeId"))
                type_info = self.io_types.get(io_type_id, {})

                ident = io_item.get("Identifier") or str(io_item.get("Id", "unknown"))
                desc = io_item.get("Description") or ident

                self._add_io_entry(
                    data, io_item, ext_name, ident, desc, type_info, schema_io, server_alias, ext_offline
                )

    @staticmethod
    def _normalize_io_unit(unit: str) -> str:
        """Normalize Comexio IO unit strings to HA-compatible values."""
        if unit in ("\\u00b0C", "°C", "°C", "C"):
            return "°C"
        return "" if unit in ("0/1", "1/0", "?") else unit

    def _add_io_entry(
        self,
        data: dict[str, Any],
        io_item: dict[str, Any],
        ext_name: str,
        ident: str,
        desc: str,
        type_info: dict[str, Any],
        schema_io: str,
        server_alias: str,
        ext_offline: bool = False,
    ) -> None:
        """Add an IO entry to data."""
        is_binary = type_info.get("binary", False)
        v_min = type_info.get("min", 0)
        v_max = type_info.get("max", 1)
        unit = type_info.get("unit", "")
        ident_upper = ident.upper()

        try:
            type_id_raw = int(io_item.get("InOutputTypeId", 1))
        except (ValueError, TypeError):
            type_id_raw = 1

        # Fallback classification when $IOTypesBinary unavailable.
        if not self.io_types:
            if re.match(r"^QI\d+$", ident_upper):
                is_binary, v_max = False, 0
            elif re.match(r"^Q\d+$", ident_upper) or re.match(r"^I\d+$", ident_upper):
                is_binary, v_max = True, 1

        # is_input=True → read-only sensor/binary_sensor; False → writable switch/number.
        # Identifier prefix is the reliable source: Q* are relay/dimmer outputs (writable),
        # I*/AI*/QI* and special names are inputs. $IOInputTypes cannot be used here because
        # the same TypeId (e.g. 2 = binary 0/1) is shared by both inputs and outputs.
        if re.match(r"^Q\d+$", ident_upper):
            is_input = False
        elif re.match(r"^(?:I|AI|QI)\d+$", ident_upper):
            is_input = True
        elif self.io_input_types:
            is_input = self.io_input_types.get(str(type_id_raw), {}).get("input", True)
        else:
            is_input = True

        unit = self._normalize_io_unit(unit)

        if desc and desc.strip() and desc != ident:
            io_name = f"{ext_name} {ident} {desc.strip()}"
        else:
            io_name = f"{ext_name} {ident}"

        ha_name = schema_io.format_map(
            SafeDict(ServerAlias=server_alias, ExtName=ext_name, IoId=ident, IoTitle=desc or "")
        )

        entry = {
            "id": str(io_item.get("Id")),
            "ext_name": ext_name,
            "identifier": ident,
            "ha_name": " ".join(ha_name.split()),
            "name": io_name,
            "is_binary": is_binary,
            "is_input": is_input,
            "unit": unit,
            "min": v_min,
            "max": v_max,
            "type_id_raw": type_id_raw,
            "value": self._clean_value(io_item.get("Value", 0)),
            "offline": ext_offline,
            # Inactive IOs get no entity/webhook (see _process_ios) but still need a label
            # for the Function Plan preview — "io_all" carries every IO, this flag tells the
            # renderer to grey the pill (Studio's own convention for an inactive element).
            "inactive": not io_item.get("Active"),
        }
        data["io_all"].append(entry)
        if not entry["inactive"]:
            data["io"].append(entry)

    # --- WEB-IO MANAGEMENT ---
    async def get_webio_base_info(self, webio_name: str) -> tuple[str, bool] | None:
        """Scans classes via add-page."""
        url_add = f"{self._base_url}/admin/web_io/add"
        async with self.session.get(url_add) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch Web-IO add page (HTTP %s)", resp.status)
                return None
            html = await resp.text()
            pattern = rf'<option value="(\d+)"[^>]*>{re.escape(webio_name)}</option>'
            if match := re.search(pattern, html, re.IGNORECASE):
                b_id = match[1]
                url_win = f"{self._base_url}/admin/web_io/baseDeviceWindow/"
                async with self.session.get(url_win) as win_resp:
                    if win_resp.status != 200:
                        _LOGGER.error("Failed to fetch Web-IO base window (HTTP %s)", win_resp.status)
                        return None
                    win_html = await win_resp.text()

                    return (b_id, f"delete_web_device_base/?id={b_id}" in win_html)
        return None

    async def get_webio_device_info(self, device_name: str) -> str | None:
        """Checks instance existence via HTML tabs."""
        url_home = f"{self._base_url}/admin/web_io/home"
        async with self.session.get(url_home) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch Web-IO home page (HTTP %s)", resp.status)
                return None
            html = await resp.text()
            pattern = rf'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
            return m[1] if (m := re.search(pattern, html, re.IGNORECASE)) else None

    async def delete_webio_device(self, device_id: str | int) -> bool:
        """
        Tries to delete the device instance.
        Returns True if successful, False if blocked by Comexio logic.
        """
        url = f"{self._base_url}/admin/web_io/delete_device/?id={device_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        _LOGGER.debug("Deleting Web-IO device %s via GET: %s", device_id, url)
        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO device %s (HTTP %s)", device_id, resp.status)
                return False
            html = await resp.text()

            # Check for jQuery UI error state which indicates the device is in use
            if 'class="ui-state-error' in html or "ui-state-error" in html:
                _LOGGER.warning("Device %s is in use within Comexio logic and cannot be deleted.", device_id)
                return False
            return True

    async def delete_webio_base(self, base_id: str | int) -> bool:
        """Sends DELETE command for device class template."""
        url = f"{self._base_url}/admin/web_io/delete_web_device_base/?id={base_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO base %s (HTTP %s)", base_id, resp.status)
            return resp.status == 200

    async def delete_fup(self, fub_id: int) -> bool:
        """Delete an entire Function Plan (not just elements within it).

        Same redirect-verification pattern as create_fup: the server responds with a
        302 redirect to the plan overview, carrying 'delete=ok' in the Location header
        on success.
        """
        url = f"{self._base_url}/admin/function_function_module/delete/?id={fub_id}"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("delete_fup: deleting Function Plan fub_id=%s", fub_id)
        try:
            async with self.session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("delete_fup: unexpected HTTP status %s for fub_id=%s", resp.status, fub_id)
                    return False
                redirect_location = resp.headers.get("Location", "")
                success = "delete=ok" in redirect_location
                if not success:
                    _LOGGER.error(
                        "delete_fup: redirect missing 'delete=ok' (fub_id=%s, location: %s)",
                        fub_id,
                        redirect_location,
                    )
                return success
        except aiohttp.ClientError:
            _LOGGER.exception("delete_fup: HTTP request error deleting fub_id=%s", fub_id)
            return False
        except Exception:
            _LOGGER.exception("delete_fup: unexpected error deleting fub_id=%s", fub_id)
            return False

    async def update_webio_device_ip(self, device_id: str | int, ha_address: str, webio_name: str) -> bool:
        """
        Updates the server address (IP:Port) of an existing device.
        Uses the specific POST format required by Comexio's main save handler.

        webio_name must be the class-specific name (see const.webio_class_name) matching
        the device_id being updated — the caller resolves it, since a single config entry
        now maps to two Web-IO devices (marker/io).
        """
        _LOGGER.info("Updating Web-IO device %s address to %s", device_id, ha_address)
        url = f"{self._base_url}/admin/web_io/save"

        # Construct the payload based on user observations.
        device_data = {
            "web_device_id": str(device_id),
            f"name_{device_id}": webio_name,
            f"ip_{device_id}": ha_address,
            f"username_{device_id}": "",
            f"password_{device_id}": "",  # nosec B105
            f"checkca_{device_id}": "0",
            f"pinnedpubkey_{device_id}": "",
            f"form_login_{device_id}": "2",
        }

        payload = {"no_reload": "true", "JSON": json.dumps(device_data)}

        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}

        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status == 200:
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    raw_text = await resp.text()
                    _LOGGER.exception(
                        "Failed to parse Web-IO device IP update response as JSON; raw response: %s",
                        raw_text,
                    )
                    return False
                return result.get("save") == 1

            _LOGGER.error("Failed to update Web-IO device IP, HTTP status: %s", resp.status)
            return False

    async def delete_single_command(self, cmd_id: str | int, device_id: str | int) -> bool:
        """Removes an individual command instance (Delta Sync)."""
        _LOGGER.info("Deleting individual Web-IO command ID: %s", cmd_id)
        url = f"{self._base_url}/admin/web_io/delete_web_command/?id={cmd_id}&dev={device_id}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to delete Web-IO command %s (HTTP %s)", cmd_id, resp.status)
            return resp.status == 200

    async def save_single_command(
        self,
        base_id: str | int | None,
        device_id: str | int,
        cmd_payload: dict[str, Any],
        existing_cmd_id: str | int | None = None,
    ) -> bool:
        """
        Adds or updates a single command in an existing device (Delta Sync).
        If existing_cmd_id is provided, an UPDATE is performed.
        """
        _LOGGER.info("Applying command: %s (Update: %s)", cmd_payload.get("Name"), existing_cmd_id is not None)
        url = f"{self._base_url}/admin/web_io/save_command"

        # Base64 identifier for Comexio
        if existing_cmd_id:
            # Update format exactly like original Comexio trace: {"src":"command","id":1324}
            cmd_ref = json.dumps({"src": "command", "id": int(existing_cmd_id)}, separators=(",", ":"))
            cmd_id_b64 = base64.b64encode(cmd_ref.encode()).decode()
        else:
            # New format: {"src":"command","id":null}
            cmd_id_b64 = "eyJzcmMiOiJjb21tYW5kIiwiaWQiOm51bGx9"

        payload = {
            "dlg_web_device_id": str(device_id),
            "protocol": 0,
            "parameter": cmd_payload["Parameter"],
            "header_modifier": _CONTENT_TYPE_JSON,
            "data": cmd_payload["Data"],
            "port": "",
            "post_get": 1,
            "authentication": 0,
            "req_freq": "",
            "reply_interpreter": "",
            "id_cmd_io_0": cmd_id_b64,
            "name_cmd_io_0": cmd_payload["Name"],
            "function_cmd_io_0": "1_1_0",
            "input_cmd_io_0": 1,
            "type_cmd_io_0": cmd_payload["TypeId"],
            "send_on_one_cmd_io_0": 0,
            "min_cmd_io_0": 0,
            "max_cmd_io_0": cmd_payload["Max"],
            "default_value_cmd_io_0": "",
            "id_cmd_io_sample": "",
            "name_cmd_io_sample": "",
            "function_cmd_io_sample": "0_1_0",
            "input_cmd_io_sample": 1,
            "type_cmd_io_sample": 2,
            "send_on_one_cmd_io_sample": 0,
            "min_cmd_io_sample": 0,
            "max_cmd_io_sample": 1,
            "default_value_cmd_io_sample": "",
            "DefaultActive": 1,
        }

        if existing_cmd_id:
            payload["id"] = str(existing_cmd_id)
        else:
            payload["deviceBaseId"] = str(base_id) if base_id is not None else "0"

        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}

        # _LOGGER.debug("Sending save_single_command payload: %s", payload)

        async with self.session.post(url, data=payload, headers=headers) as resp:
            await resp.text()
            # _LOGGER.debug("save_single_command response [%s]: %s", resp.status, resp_text)
            return resp.status == 200

    def build_webio_commands(
        self,
        server_id: str,
        parsed_data: dict[str, Any],
        webio_class: str | None = None,
        ignored_marker_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the list of Web-IO command dicts for the given parsed configuration.

        webio_class restricts the result to one Web-IO class ("marker"/"io"), for the bulk
        class-upload path (generate_webio_json); None returns both (delta-sync payload lookup,
        where the destination device is chosen separately per command).
        ignored_marker_ids excludes markers the user configured as ignored — they have no HA
        entity, so they need no Web-IO command pushing values back via webhook.
        Returns the list directly so callers can use it without a json.dumps/json.loads roundtrip.
        """
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands: list[dict[str, Any]] = []

        # 1. Create Web-IO for markers
        markers = parsed_data.get("markers", []) if webio_class != WEBIO_CLASS_IO else []
        for m in markers:
            if ignored_marker_ids and int(m["id"]) in ignored_marker_ids:
                continue
            commands.append(self._build_marker_webio_command(m, webhook_path))

        # 2. Create Web-IO for IOs
        io_entries = parsed_data.get("io", []) if webio_class != WEBIO_CLASS_MARKER else []
        for io_item in io_entries:
            commands.append(self._build_io_webio_command(io_item, webhook_path))

        return commands

    @staticmethod
    def _lua_escape(value: Any) -> str:
        """Escape a value for embedding in a double-quoted Lua string literal."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _webio_data_lua(payload: str) -> str:
        """Build the Lua `data(a)` webhook body for a Web-IO command, given its JSON payload fields."""
        return f"function data(a)\r\n  local d = {{ {payload} }}\r\n  return json_stringify(d)\r\nend"

    @staticmethod
    def _webio_command(
        *, name: str, type_id: int, min_v: int, max_v: int, data: str, webhook_path: str
    ) -> dict[str, Any]:
        """Build a Web-IO command dict, filling in the fields shared by markers and IOs."""
        return {
            "Name": name,
            "TypeId": type_id,
            "Min": min_v,
            "Max": max_v,
            "Parameter": webhook_path,
            "HeaderModifier": _CONTENT_TYPE_JSON,
            "Data": data,
            "Protocol": 0,
            "PostGet": 1,
            "WebDeviceId": 0,
            "Authentication": 0,
            "Input": 1,
            "ReqFreq": "",
            "ReplyInterpreter": "",
            "Port": "",
            "SendOnOne": 0,
            "Changed": 1,
            "BaseId": 0,
            "DefaultValue": "",
            "DefaultActive": 1,
            "io": [],
        }

    @staticmethod
    def _build_marker_webio_command(m: dict[str, Any], webhook_path: str) -> dict[str, Any]:
        """Build the Web-IO command dict for a single marker."""
        is_ana = m["type"] == "analog"
        safe_id = ComexioAPI._lua_escape(m["id"])
        lua = ComexioAPI._webio_data_lua(f'id="{safe_id}", value=a, type="marker"')
        return ComexioAPI._webio_command(
            name=f"HA {m['name']}",
            type_id=2 if is_ana else 1,
            min_v=WEBIO_MARKER_ANALOG_MIN if is_ana else 0,
            max_v=WEBIO_MARKER_ANALOG_MAX if is_ana else 1,
            data=lua,
            webhook_path=webhook_path,
        )

    @staticmethod
    def _build_io_webio_command(io_item: dict[str, Any], webhook_path: str) -> dict[str, Any]:
        """Build the Web-IO command dict for a single physical IO."""
        is_ana = not io_item.get("is_binary", False)
        # Use the authentic min/max from the Comexio type definition
        v_min = io_item.get("min", 0)
        v_max = io_item.get("max", 100 if is_ana else 1)
        safe_ext = ComexioAPI._lua_escape(io_item["ext_name"])
        safe_io_id = ComexioAPI._lua_escape(io_item["identifier"])
        lua = ComexioAPI._webio_data_lua(f'ext="{safe_ext}", io="{safe_io_id}", value=a, type="io"')
        return ComexioAPI._webio_command(
            name=f"HA IO {io_item['ext_name']} {io_item['identifier']}",
            type_id=2 if is_ana else 1,
            min_v=v_min,
            max_v=v_max,
            data=lua,
            webhook_path=webhook_path,
        )

    def generate_webio_json(
        self,
        server_id: str,
        webio_name: str,
        parsed_data: dict[str, Any],
        webio_class: str | None = None,
        ignored_marker_ids: set[int] | None = None,
    ) -> str:
        """Generate the upload-ready JSON string for the Comexio Web-IO importer.

        webio_name here is already the class-specific name (see const.webio_class_name) —
        callers append the ' [M]'/' [IO]' suffix before calling this.
        ignored_marker_ids is forwarded to build_webio_commands() to exclude ignored markers.
        """
        return json.dumps(
            {
                "data": "web_io",
                "format": 1,
                "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 2, "BaseId": 0},
                "commands": self.build_webio_commands(server_id, parsed_data, webio_class, ignored_marker_ids),
            }
        )

    async def upload_web_io(self, server_id: str, webio_name: str, web_io_json: str) -> tuple[bool, str]:
        """Uploads JSON class template."""
        url = f"{self._base_url}/admin/web_io/upload_device_settings"
        file_data = io.BytesIO(web_io_json.encode("utf-8"))
        form = aiohttp.FormData()
        form.add_field("file", file_data, filename=f"ha_{server_id}.json", content_type="application/json")
        form.add_field("set_name", webio_name)
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/web_io/home"}
        async with self.session.post(url, data=form, headers=headers) as resp:
            if resp.status == 200:
                result = await resp.json(content_type=None)
                if result.get("ok"):
                    return True, result.get("base_id")
            return False, await resp.text()

    async def create_webio_device(self, name: str, base_id: str | int, ha_address: str | None = None) -> bool:
        """Creates a device instance. Automatically determines HA address if not provided."""
        if not ha_address:
            ha_address = await self.get_ha_address()

        url = f"{self._base_url}/admin/web_io/saveDeviceWindow"

        payload = {
            "name": name,
            "ip": ha_address,
            "web_device_base": base_id,
            "username": "",
            "password": "",  # nosec B105
            "web_device_base_sample": "none",
            "identifier": "",
            "form_login": "2",
        }

        async with self.session.post(url, data=payload, headers={"X-Requested-With": "XMLHttpRequest"}) as resp:
            return resp.status == 200

    # --- LOGIKPLAN (FUNCTION PLAN) ---

    async def function_plan_add_element(
        self,
        fub_id: int,
        ref_id: int,
        element_type: int,
        x: float = 100.0,
        y: float = 100.0,
        connection: dict | None = None,
    ) -> int | None:
        """Place a Marker, WebIO, IO, or catalog function block on a plan canvas.

        Pass `connection` to wire the element in the same API call (skips saveconnection).
        For type=10 (WebIO): connection = {"0": {"id":"new","fub_id":...,"type":"binary|analog",
          "input":{"element":"<marker_elem_id>","pos":"0","inverted":false},
          "output":{"0":{"element":"new","pos":"0","inverted":false}}}}
        Comment (type=14) and Constant (type=16) blocks aren't backed by a catalog ref_id —
        use function_plan_add_comment_element / function_plan_add_constant_element instead.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": "",
            "ref_id": str(ref_id),
            "type": str(element_type),
            "id": "undefined",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        if connection is not None:
            payload["connection"] = json.dumps(connection, separators=(",", ":"))
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error(
                    "function_plan_add_element failed (HTTP %s, fub=%s, ref=%s, type=%s)",
                    resp.status,
                    fub_id,
                    ref_id,
                    element_type,
                )
                return None
            try:
                result = await resp.json(content_type=None)
                elem_id = result.get("id")
                if elem_id is None:
                    _LOGGER.error(
                        "function_plan_add_element: no id in response (fub=%s, ref=%s, type=%s): %s",
                        fub_id,
                        ref_id,
                        element_type,
                        result,
                    )
                    return None
                _LOGGER.debug(
                    "function_plan_add_element: fub=%s ref=%s type=%s → elem_id=%s",
                    fub_id,
                    ref_id,
                    element_type,
                    elem_id,
                )
                return int(elem_id)
            except Exception:
                _LOGGER.exception("function_plan_add_element: failed to parse response")
                return None

    async def function_plan_save_connection(
        self,
        fub_id: int,
        input_elem_id: int,
        outputs: list[tuple[int, int, bool]],
        value_type: str = "binary",
        input_pos: int = 0,
        input_inverted: bool = False,
    ) -> int | None:
        """Draw a wire (or fan-out) from input_elem (source) to one or more output elements.

        value_type: "binary" for digital, "analog" for analog.
        input_pos: source output-port index, for elements with more than one port — 0 for
        single-port elements.
        outputs: (output_elem_id, output_pos, output_inverted) per sink. Comexio models a
        fan-out as ONE connection record with multiple "output" entries, not as several
        independent connections from the same source pin — sending them separately caused
        both wires to silently vanish for IO/Constant sources (confirmed live 2026-08-22 on a
        restored copy of a real plan; catalog function-block sources tolerated it, IO/Constant
        sources did not), so every sink for a given source must be sent in a single call.
        Returns the connection ID assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/saveconnection/"
        timestamp = _js_timestamp()
        output_dict = {
            str(i): {"element": str(dst), "pos": str(pos), "inverted": inverted}
            for i, (dst, pos, inverted) in enumerate(outputs)
        }
        conn_json = json.dumps(
            {
                "id": "new",
                "fub_id": fub_id,
                "input": {"element": str(input_elem_id), "pos": str(input_pos), "inverted": input_inverted},
                "type": value_type,
                "output": output_dict,
            },
            separators=(",", ":"),
        )
        payload = {"JSON": conn_json, "timestamp": timestamp}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        dst_ids = [dst for dst, _pos, _inv in outputs]
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error(
                    "function_plan_save_connection failed (HTTP %s, fub=%s, %s→%s)",
                    resp.status,
                    fub_id,
                    input_elem_id,
                    dst_ids,
                )
                return None
            try:
                result = await resp.json(content_type=None)
                conn_id = result.get("id")
                _LOGGER.debug(
                    "function_plan_save_connection: fub=%s %s→%s conn_id=%s",
                    fub_id,
                    input_elem_id,
                    dst_ids,
                    conn_id,
                )
                return int(conn_id) if conn_id is not None else None
            except Exception:
                _LOGGER.exception("function_plan_save_connection: failed to parse response")
                return None

    async def function_plan_save_elements_pos(self, positions: list[tuple[int, float, float]]) -> bool:
        """Reposition multiple function plan elements in one call.

        positions: list of (fubElementId, x, y) tuples.
        Returns True on success.
        """
        url = f"{self._base_url}/admin/function_function_module/saveelementspos/"
        timestamp = _js_timestamp()
        pos_dict = {str(i): {"x": x, "y": y, "id": elem_id} for i, (elem_id, x, y) in enumerate(positions)}
        payload = {
            "Json": json.dumps(pos_dict, separators=(",", ":")),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("function_plan_save_elements_pos: repositioning %d elements", len(positions))
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_save_elements_pos failed (HTTP %s)", resp.status)
                return False
            try:
                result = await resp.json(content_type=None)
                success = result.get("result") == 1
                _LOGGER.info("function_plan_save_elements_pos: result=%s (raw: %s)", success, result)
                return success
            except Exception:
                _LOGGER.exception("function_plan_save_elements_pos: failed to parse response")
                return False

    async def function_plan_delete_elements(self, elem_ids: list[int]) -> bool:
        """Delete elements from a function plan (removes elements + their connections).

        elem_ids: list of fubElementId integers to delete.
        Returns True on success.
        """
        url = f"{self._base_url}/admin/function_function_module/deleteelements/"
        timestamp = _js_timestamp()
        payload = {
            "Json": json.dumps([str(eid) for eid in elem_ids]),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        _LOGGER.info("function_plan_delete_elements: %d Elemente löschen: %s", len(elem_ids), elem_ids)
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_delete_elements failed (HTTP %s)", resp.status)
                return False
            try:
                result = await resp.json(content_type=None)
                success = result.get("delete") is True
                _LOGGER.info("function_plan_delete_elements: result=%s", success)
                return success
            except Exception:
                _LOGGER.exception("function_plan_delete_elements: failed to parse response")
                return False

    @staticmethod
    def _keyed_by_list_position(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Re-key a list-shaped loadelements collection back into an {id: item} dict."""
        return {str(item.get("id", i)): item for i, item in enumerate(items)}

    async def function_plan_load_elements(self, fub_id: int) -> dict | None:
        """Load elements and connections for a function plan (GET loadelements).

        Returns dict with 'elements' and 'connections' keys, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/loadelements/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.get(url, params={"fubid": fub_id}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_load_elements failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return None
                data = await resp.json(content_type=None)
                # Comexio's PHP backend serializes an associative array as a JSON array
                # (not object) whenever its keys happen to be sequential integers from 0 —
                # a shape coincidence, not a signal that the collection is empty. A small,
                # rarely-edited plan's connection ids can easily stay sequential, so treating
                # "is a list" as "is empty" (the old assumption here) silently discarded real
                # elements/connections. Re-key by each item's own "id" when present (elements
                # carry one); connections don't, so fall back to the list position.
                elements = data.get("elements")
                data["elements"] = (
                    self._keyed_by_list_position(elements) if isinstance(elements, list) else (elements or {})
                )
                connections = data.get("connections")
                data["connections"] = (
                    self._keyed_by_list_position(connections) if isinstance(connections, list) else (connections or {})
                )
                elem_count = len(data.get("elements", {}))
                conn_count = len(data.get("connections", {}))
                _LOGGER.info(
                    "function_plan_load_elements fub=%s: %d Elemente, %d Verbindungen", fub_id, elem_count, conn_count
                )
                return data
        except Exception:
            _LOGGER.exception("function_plan_load_elements fub_id=%s failed", fub_id)
            return None

    async def function_plan_load_all_plans(self, concurrency: int = 4) -> dict[int, dict]:
        """Load elements and connections for ALL known function plans concurrently.

        Uses the fub list cached by parse_config (self._fub_data). Requests run in
        parallel, limited by a semaphore so the embedded Comexio server is not
        overwhelmed. Plans that fail to load are skipped.
        Returns {fub_id: {"elements": {...}, "connections": {...}}}.
        """
        fub_ids = [int(fid) for fid in self._fub_data]
        if not fub_ids:
            _LOGGER.warning("function_plan_load_all_plans: self._fub_data is empty — nothing to load")
            return {}

        semaphore = asyncio.Semaphore(concurrency)

        async def _load_one(fid: int) -> tuple[int, dict | None]:
            async with semaphore:
                return fid, await self.function_plan_load_elements(fid)

        t_start = time.monotonic()
        results = await asyncio.gather(*(_load_one(fid) for fid in fub_ids))
        duration = time.monotonic() - t_start
        plans = {fid: data for fid, data in results if data is not None}
        _LOGGER.info(
            "function_plan_load_all_plans: %d/%d plans loaded in %.2fs (concurrency=%d)",
            len(plans),
            len(fub_ids),
            duration,
            concurrency,
        )
        return plans

    async def function_plan_stop_fup(self, fub_id: int) -> bool:
        """Stop/pause a function plan (stop_fup)."""
        url = f"{self._base_url}/admin/function_function_module/stop_fup/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(url, data={"id": str(fub_id)}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_stop_fup failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                result = await resp.json(content_type=None)
                success = result.get("result") is True
                _LOGGER.info("function_plan_stop_fup: fub=%s result=%s state=%s", fub_id, success, result.get("state"))
                return success
        except Exception:
            _LOGGER.exception("function_plan_stop_fup: fub_id=%s failed", fub_id)
            return False

    async def function_plan_add_comment_element(
        self,
        fub_id: int,
        text: str,
        x: float = 100.0,
        y: float = 7.5,
    ) -> int | None:
        """Place a text/comment block (type=14, ref_id=3) on a function plan canvas.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": text,
            "ref_id": "3",
            "type": "14",
            "id": "0",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_add_comment_element failed (HTTP %s, fub=%s)", resp.status, fub_id)
                return None
            try:
                result = await resp.json(content_type=None)
                elem_id = result.get("id")
                if elem_id is None:
                    _LOGGER.error("function_plan_add_comment_element: no id in response (fub=%s): %s", fub_id, result)
                    return None
                _LOGGER.debug("function_plan_add_comment_element: fub=%s → elem_id=%s", fub_id, elem_id)
            except Exception:
                _LOGGER.exception("function_plan_add_comment_element: failed to parse response")
                return None
        # add_element has no width parameter — the width lives in the comment
        # properties dialog, saved via a separate endpoint.
        await self._function_plan_set_comment_width(int(elem_id), text)
        return elem_id

    async def function_plan_add_constant_element(
        self,
        fub_id: int,
        value: str,
        x: float = 100.0,
        y: float = 100.0,
    ) -> int | None:
        """Place a Constant block (type=16) on a function plan canvas.

        The server always normalizes a saved Constant's reference.ref_id back to 0 (see
        function_plan_catalog.py's docstring — $FubModules["16"] is empty, nothing to
        reference), but the *create* call itself rejects ref_id="0" with
        {"error": "data faulty"} — confirmed live 2026-08-22 via a throwaway test plan.
        Any positive placeholder (ref_id="1") is accepted and gets normalized away.

        Returns the fubElementId assigned by the server, or None on failure.
        """
        url = f"{self._base_url}/admin/function_function_module/add_element/"
        timestamp = _js_timestamp()
        payload = {
            "fubid": str(fub_id),
            "name": value,
            "ref_id": "1",
            "type": "16",
            "id": "undefined",
            "x": str(x),
            "y": str(y),
            "timestamp": timestamp,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.error("function_plan_add_constant_element failed (HTTP %s, fub=%s)", resp.status, fub_id)
                return None
            try:
                result = await resp.json(content_type=None)
                elem_id = result.get("id")
                if elem_id is None:
                    _LOGGER.error("function_plan_add_constant_element: no id in response (fub=%s): %s", fub_id, result)
                    return None
                _LOGGER.debug("function_plan_add_constant_element: fub=%s → elem_id=%s", fub_id, elem_id)
                return int(elem_id)
            except Exception:
                _LOGGER.exception("function_plan_add_constant_element: failed to parse response")
                return None

    async def _function_plan_set_comment_width(self, elem_id: int, text: str, width: int = 5) -> bool:
        """Set a comment element's text width via savefupcommentelement (5 = 'Sehr Breit')."""
        url = f"{self._base_url}/admin/function_function_module/savefupcommentelement/"
        payload = {
            "id": str(elem_id),
            "use_base_64": "1",
            "name": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "width": str(width),
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status != 200:
                _LOGGER.warning("savefupcommentelement failed (HTTP %s, elem=%s)", resp.status, elem_id)
                return False
            try:
                result = await resp.json(content_type=None)
            except Exception:
                _LOGGER.exception("savefupcommentelement: failed to parse response (elem=%s)", elem_id)
                return False
        if result.get("result") != 1:
            _LOGGER.warning("savefupcommentelement rejected (elem=%s): %s", elem_id, result)
            return False
        _LOGGER.debug("savefupcommentelement: elem=%s width=%s → %s", elem_id, width, result.get("data"))
        return True

    async def create_fup(
        self,
        plan_name: str,
        plan_comment: str = "",
        paper_format: str = "A4",
        orientation: str = "landscape",
        dpi: int = 90,
    ) -> int | None:
        """Create a new function plan. Returns the new fub_id on success, None on failure.

        Args:
            plan_name: Name of the new plan
            plan_comment: Optional comment/description
            paper_format: Paper size (A3, A4, A5; defaults to A4)
            orientation: 'landscape' or 'portrait' (defaults to landscape)
            dpi: Resolution in dots per inch, 45-120 (defaults to 90)

        Steps:
        1. Check uniqueness via /admin/_helper/isunique
        2. POST to /admin/function_function_module/save_fub
        3. Verify plan was created by checking the response redirect
        """
        # Step 1: Unique check
        url_check = f"{self._base_url}/admin/_helper/isunique"
        try:
            async with self.session.post(url_check, data={"model": "fub", "field": "name", "value": plan_name}) as resp:
                if resp.status != 200:
                    _LOGGER.error("create_fup: uniqueness check failed (HTTP %s)", resp.status)
                    return None
                result = await resp.json(content_type=None)
                if not result.get("result"):
                    _LOGGER.error("create_fup: plan name '%s' already exists", plan_name)
                    return None
        except Exception:
            _LOGGER.exception("create_fup: uniqueness check failed")
            return None

        # Step 2: Create the plan
        paper_map = {"A3": "2", "A4": "3", "A5": "4"}
        paper_id = paper_map.get(paper_format.upper(), "3")
        orient_id = "1" if orientation.lower() == "portrait" else "0"

        url_create = f"{self._base_url}/admin/function_function_module/save_fub"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        payload = {
            "fub_position": "-1",
            "fub_type": "1",
            "fub_page_count_x": "1",
            "fub_page_count_y": "1",
            "fub_name": plan_name,
            "fub_comment": plan_comment,
            "fub_active": "0",
            "fub_reset_on_close": "0",
            "fub_paper": paper_id,
            "fub_orientation": orient_id,
            "fub_resolution": str(dpi),
            "fub_create": "Erzeugen",
        }

        try:
            async with self.session.post(url_create, data=payload, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("create_fup: save_fub failed (HTTP %s)", resp.status)
                    return None

                redirect_location = resp.headers.get("Location", "")
                if "added=1" not in redirect_location:
                    _LOGGER.error("create_fup: redirect missing 'added=1' (location: %s)", redirect_location)
                    return None

                _LOGGER.info("create_fup: plan '%s' created successfully (redirect: %s)", plan_name, redirect_location)
        except Exception:
            _LOGGER.exception("create_fup: save_fub request failed")
            return None

        # Step 3: Verify plan was created by reloading config and checking $Fubs
        # (same key parse_config() uses for _fub_data — "FubModules" holds
        # markers/IOs, not plans, and would never see the new entry here)
        try:
            raw_config = await self.get_raw_config()
            fub_data = raw_config.get("Fubs", {})
            for fub_id_str, fub_info in fub_data.items():
                if fub_info.get("Name") == plan_name:
                    new_fub_id = int(fub_id_str)
                    _LOGGER.info("create_fup: verification successful, new fub_id=%s", new_fub_id)
                    # Update internal _fub_data
                    if not hasattr(self, "_fub_data"):
                        self._fub_data = {}
                    self._fub_data[fub_id_str] = fub_info
                    return new_fub_id
            _LOGGER.error("create_fup: verification failed — plan '%s' not found in $Fubs after creation", plan_name)
            return None
        except Exception:
            _LOGGER.exception("create_fup: verification (config reload) failed")
            return None

    async def function_plan_update_paper(
        self, fub_id: int, paper_format: str, dpi: int, orientation: str, name: str | None = None
    ) -> bool:
        """Update an EXISTING plan's paper format/DPI/orientation (same save_fub endpoint as
        create_fup, but with fub_id set and fub_save='Speichern' instead of fub_create).

        Needed before an in-place restore whose snapshot's canvas settings differ from the
        live plan's current ones (e.g. force_override onto an unrelated plan) — otherwise
        element positions computed for the snapshot's original canvas can end up clipped or
        overlapping on the live plan's (different) canvas.

        name: if given, also renames the plan (force_override restores the snapshot's
        original name too, so the plan comes back exactly as it was — not just its content).
        None keeps the current live name unchanged.

        All other plan properties (comment, position, active state) are read from the
        current live data and passed through UNCHANGED. Known gap: Comexio's $Fubs dump does
        not expose "reset on close", so that flag is always sent as "0" (Comexio's own
        create-time default) rather than preserved — a cosmetic Comexio Studio setting this
        integration doesn't otherwise manage.
        """
        fub = self._fub_data.get(str(fub_id))
        if fub is None:
            _LOGGER.error("function_plan_update_paper: fub_id=%s not found in live data", fub_id)
            return False

        paper_map = {"A3": "2", "A4": "3", "A5": "4"}
        paper_id = paper_map.get(paper_format.upper(), "3")
        orient_id = "1" if orientation.lower() == "portrait" else "0"
        target_name = name if name is not None else fub.get("Name", "")

        url = f"{self._base_url}/admin/function_function_module/save_fub"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        payload = {
            "fub_id": str(fub_id),
            "fub_position": str(fub.get("Position", "-1")),
            "fub_type": "1",
            "fub_page_count_x": "1",
            "fub_page_count_y": "1",
            "fub_name": target_name,
            "fub_comment": fub.get("Comment", ""),
            "fub_active": str(int(bool(fub.get("Active", False)))),
            "fub_reset_on_close": "0",
            "fub_paper": paper_id,
            "fub_orientation": orient_id,
            "fub_resolution": str(dpi),
            "fub_save": "Speichern",
        }

        try:
            async with self.session.post(url, data=payload, headers=headers, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    _LOGGER.error("function_plan_update_paper: save_fub failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                redirect_location = resp.headers.get("Location", "")
                if "saved=1" not in redirect_location:
                    _LOGGER.error(
                        "function_plan_update_paper: redirect missing 'saved=1' (fub=%s, location: %s)",
                        fub_id,
                        redirect_location,
                    )
                    return False
        except Exception:
            _LOGGER.exception("function_plan_update_paper: save_fub request failed (fub=%s)", fub_id)
            return False

        # Keep the local cache in sync so get_fub_paper_format/dpi/orientation reflect the change
        fub["Paper"] = paper_id
        fub["Resolution"] = dpi
        fub["Orientation"] = int(orient_id)
        if name is not None:
            fub["Name"] = name
        _LOGGER.info(
            "function_plan_update_paper: fub=%s -> paper=%s dpi=%s orientation=%s name=%s",
            fub_id,
            paper_format,
            dpi,
            orientation,
            name,
        )
        return True

    async def function_plan_run_fup(self, fub_id: int, plan_data: dict | None = None) -> bool:
        """Save and activate a function plan (run_fup).

        By default the CURRENT state is loaded via loadelements; pass an explicit
        plan_data (e.g. a backup snapshot with 'elements' and 'connections') to
        restore that state instead.
        The output field in connections is converted from list (loadelements)
        to indexed dict (run_fup expectation).
        """
        if plan_data is None:
            plan_data = await self.function_plan_load_elements(fub_id)
        if plan_data is None:
            _LOGGER.error("function_plan_run_fup: could not load plan %s", fub_id)
            return False

        connections_transformed = {
            conn_id: {
                **conn,
                "output": {str(i): item for i, item in enumerate(conn.get("output", []))},
            }
            for conn_id, conn in plan_data.get("connections", {}).items()
        }
        data_payload = {"elements": plan_data.get("elements", {}), "connections": connections_transformed}

        url = f"{self._base_url}/admin/function_function_module/run_fup/"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base_url}/admin/function_function_module/home",
        }
        try:
            async with self.session.post(
                url, data={"id": str(fub_id), "data": json.dumps(data_payload)}, headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error("function_plan_run_fup failed (HTTP %s, fub=%s)", resp.status, fub_id)
                    return False
                result = await resp.json(content_type=None)
                success = result.get("result") is True
                _LOGGER.info("function_plan_run_fup: fub=%s result=%s state=%s", fub_id, success, result.get("state"))
                return success
        except Exception:
            _LOGGER.exception("function_plan_run_fup: fub_id=%s failed", fub_id)
            return False

    async def _reload_config_until_commands_ready(self, expected_names_fn: Callable[[dict], set[str]]) -> dict:
        """Reload Comexio config, retrying with backoff until webio_commands is ready.

        A freshly uploaded Web-IO command does not always appear in the very next
        `/admin/function_function_module/home` response — Comexio seems to regenerate
        that page on its own cycle rather than synchronously per write. expected_names_fn
        derives the Web-IO command names to wait for from each reload's own parsed data
        (marker names/IO identifiers are stable; only webio_commands is expected to lag).
        Returns the last parsed config regardless of outcome — callers report per-item
        errors for any names still missing after the final attempt.
        """
        delay = FUNCTION_PLAN_PAIR_RELOAD_INITIAL_DELAY
        fresh_data: dict = {}
        for attempt in range(FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS):
            raw = await self.get_raw_config()
            fresh_data = self.parse_config(raw)
            missing = expected_names_fn(fresh_data) - fresh_data.get("webio_commands", {}).keys()
            if not missing:
                return fresh_data
            if attempt < FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS - 1:
                _LOGGER.info(
                    "function plan pairing: %d Web-IO command(s) not yet visible after reload "
                    "(attempt %d/%d), retrying in %.1fs",
                    len(missing),
                    attempt + 1,
                    FUNCTION_PLAN_PAIR_RELOAD_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        return fresh_data

    @staticmethod
    def _function_plan_existing_refs(plan_data: dict | None) -> tuple[dict[tuple[int, int], int], list[set[int]]]:
        """Index a plan's elements by (ref_type, ref_id) and collect connection endpoint sets.

        Keys are normalized to int — the raw JSON may carry type/ref_id as strings.
        """
        existing_by_ref: dict[tuple[int, int], int] = {}
        conn_endpoints: list[set[int]] = []
        if not plan_data:
            return existing_by_ref, conn_endpoints
        for elem_id_str, elem in plan_data.get("elements", {}).items():
            ref = elem.get("reference") or {}
            with suppress(TypeError, ValueError, KeyError):
                existing_by_ref[(int(ref["type"]), int(ref["ref_id"]))] = int(elem_id_str)
        for conn in (plan_data.get("connections") or {}).values():
            endpoints: set[int] = set()
            outputs = conn.get("output") or []
            if isinstance(outputs, dict):
                outputs = list(outputs.values())
            for endpoint in [conn.get("input") or {}, *outputs]:
                with suppress(TypeError, ValueError, KeyError):
                    endpoints.add(int(endpoint["FubElementId"]))
            conn_endpoints.append(endpoints)
        return existing_by_ref, conn_endpoints

    async def _function_plan_wire_ref_pair(
        self,
        fub_id: int,
        src_type: int,
        src_ref_id: int,
        web_ref_id: int,
        conn_type: str,
        label: str,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        pos: tuple[float, float, float],
    ) -> str | None:
        """Wire one source element (Marker type=2 / IO type=1) to its Web-IO (type=10) element.

        Existing elements are reused: if both already sit in the plan but the wire
        between them is missing (orphan pair, e.g. a connection lost during a
        restore cycle), only the connection is drawn. conn_endpoints holds the
        FubElementId endpoint set of every existing connection to detect that case.
        Returns None on success, "" when the pair is already wired in the plan
        (skip, not an error), or an error message.
        """
        x_src, x_webio, y = pos

        existing_src_elem = existing_by_ref.get((src_type, src_ref_id))
        existing_webio_elem = existing_by_ref.get((10, web_ref_id))
        if existing_src_elem and existing_webio_elem:
            if any(existing_src_elem in eps and existing_webio_elem in eps for eps in conn_endpoints):
                _LOGGER.info("function plan pair %s already wired in plan fub=%s, skipping", label, fub_id)
                return ""
            # Orphan pair: both elements exist but the wire is missing — draw only the connection
            conn_id = await self.function_plan_save_connection(
                fub_id, existing_src_elem, [(existing_webio_elem, 0, False)], conn_type
            )
            if conn_id is None:
                return f"{label}: save_connection between existing elements failed"
            _LOGGER.info(
                "function plan pair %s rewired existing elements %s→%s (fub=%s, conn_id=%s)",
                label,
                existing_src_elem,
                existing_webio_elem,
                fub_id,
                conn_id,
            )
            return None

        elem_src = existing_src_elem or await self.function_plan_add_element(
            fub_id=fub_id, ref_id=src_ref_id, element_type=src_type, x=x_src, y=y
        )
        if elem_src is None:
            return f"{label}: add_element (source, type={src_type}) failed"

        if existing_webio_elem:
            # Web-IO element already in the plan — wire the (possibly fresh) source to it
            conn_id = await self.function_plan_save_connection(
                fub_id, int(elem_src), [(existing_webio_elem, 0, False)], conn_type
            )
            if conn_id is None:
                return f"{label}: save_connection to existing Web-IO element failed"
            return None

        conn_payload = {
            "0": {
                "id": "new",
                "fub_id": fub_id,
                "type": conn_type,
                "input": {"element": str(elem_src), "pos": "0", "inverted": False},
                "output": {"0": {"element": "new", "pos": "0", "inverted": False}},
            }
        }
        elem_webio = await self.function_plan_add_element(
            fub_id=fub_id,
            ref_id=web_ref_id,
            element_type=10,
            x=x_webio,
            y=y,
            connection=conn_payload,
        )
        if elem_webio is None:
            return f"{label}: add_element (Web-IO, webIoId={web_ref_id}) failed"

        _LOGGER.info(
            "function plan pair %s → src_elem=%s webio_elem=%s (fub=%s, conn=%s)",
            label,
            elem_src,
            elem_webio,
            fub_id,
            conn_type,
        )
        return None

    async def function_plan_add_marker_pairs(
        self,
        fub_id: int,
        marker_ids: list[int],
        fresh_plan: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[list[int], list[str]]:
        """Add Marker (type=2) + Web-IO (type=10) element pairs to a stopped plan.

        Reloads Comexio config to pick up freshly created Web-IO commands (webIoId),
        retrying with backoff (see _reload_config_until_commands_ready) since a
        just-uploaded command does not always show up on the very next reload.
        fresh_plan=True places the pairs directly at their final grid positions
        (sorted by marker ID) — no sort pass is needed afterwards. Otherwise the
        elements get placeholder positions and a sort run must follow.
        progress_cb(done, total) is invoked after every processed marker.
        Returns (added_marker_ids, error_messages).
        """

        def _expected_names(data: dict) -> set[str]:
            markers = {int(m["id"]): m for m in data.get("markers", [])}
            return {f"HA {markers[mid]['name']}" for mid in marker_ids if mid in markers}

        fresh_data = await self._reload_config_until_commands_ready(_expected_names)
        webio_commands = fresh_data.get("webio_commands", {})
        markers_by_id = {int(m["id"]): m for m in fresh_data.get("markers", [])}

        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        if fresh_plan:
            marker_ids = sorted(marker_ids)
        _, y_max = self.get_fub_canvas_bounds(fub_id)
        rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))

        def _pair_pos(n_added: int, n_loop: int) -> tuple[float, float, float]:
            """(x_marker, x_webio, y): final grid slot for fresh plans, placeholder otherwise."""
            if fresh_plan:
                col, row = divmod(n_added, rows_per_col)
                x_off = col * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
                return (
                    FUNCTION_PLAN_LAYOUT_X_MARKER + x_off,
                    FUNCTION_PLAN_LAYOUT_X_WEBIO + x_off,
                    FUNCTION_PLAN_LAYOUT_Y_START + row * FUNCTION_PLAN_LAYOUT_Y_STEP,
                )
            # Off-canvas parking row: the follow-up sort pass assigns the real slots.
            return (
                FUNCTION_PLAN_LAYOUT_X_MARKER,
                FUNCTION_PLAN_LAYOUT_X_WEBIO,
                10000.0 + n_loop * FUNCTION_PLAN_LAYOUT_Y_STEP,
            )

        added: list[int] = []
        errors: list[str] = []
        for i, marker_id in enumerate(marker_ids):
            err = await self._function_plan_add_single_pair(
                fub_id,
                marker_id,
                markers_by_id,
                webio_commands,
                existing_by_ref,
                conn_endpoints,
                _pair_pos(len(added), i),
            )
            if err is None:
                added.append(marker_id)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(marker_ids))

        return added, errors

    async def _function_plan_add_single_pair(
        self,
        fub_id: int,
        marker_id: int,
        markers_by_id: dict,
        webio_commands: dict,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        pos: tuple[float, float, float],
    ) -> str | None:
        """Add one Marker+Web-IO pair at pos=(x_marker, x_webio, y).

        Return semantics as _function_plan_wire_ref_pair (None = added, "" = already wired).
        """
        marker = markers_by_id.get(marker_id)
        if not marker:
            return f"M{marker_id}: not found in fresh config"

        expected_cmd_name = f"HA {marker['name']}"
        webio_cmd = webio_commands.get(expected_cmd_name)
        if not webio_cmd:
            _LOGGER.warning("function_plan_add_marker_pairs: M%s — Web-IO '%s' not found", marker_id, expected_cmd_name)
            return f"M{marker_id}: Web-IO '{expected_cmd_name}' not found after config reload"

        web_ref_id = webio_cmd.get("webIoId")
        if web_ref_id is None:
            return f"M{marker_id}: no webIoId for '{expected_cmd_name}'"

        conn_type = "binary" if marker["type"] == "digital" else "analog"
        return await self._function_plan_wire_ref_pair(
            fub_id, 2, marker_id, int(web_ref_id), conn_type, f"M{marker_id}", existing_by_ref, conn_endpoints, pos
        )

    async def function_plan_add_io_pairs(
        self,
        fub_id: int,
        ext_name: str,
        identifiers: list[str],
        column_index: int = 0,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Add IO (type=1) + Web-IO (type=10) element pairs for one extension column.

        identifiers: the IOs to wire in this run. Row slots derive from ALL of the
        extension's HA-relevant IOs (io_column_rows), so a pair retrofitted later lands
        exactly in the slot the initial layout reserved for it — IO plans therefore never
        need a sort pass. Should a column outgrow the canvas anyway, it wraps into a
        sub-column right next to it as a defensive fallback (the plan-creation side is
        expected to pick a canvas tall enough to avoid this).
        Returns (added_identifiers, error_messages).
        """

        def _expected_names(data: dict) -> set[str]:
            ext_idents = {io["identifier"] for io in data.get("io", []) if io["ext_name"] == ext_name}
            return {f"HA IO {ext_name} {ident}" for ident in identifiers if ident in ext_idents}

        fresh_data = await self._reload_config_until_commands_ready(_expected_names)
        webio_commands = fresh_data.get("webio_commands", {})
        ext_ios = {io["identifier"]: io for io in fresh_data.get("io", []) if io["ext_name"] == ext_name}
        rows = io_column_rows(list(ext_ios))

        plan_data = await self.function_plan_load_elements(fub_id)
        existing_by_ref, conn_endpoints = self._function_plan_existing_refs(plan_data)

        _, y_max = self.get_fub_canvas_bounds(fub_id)
        rows_per_col = max(1, int((y_max - FUNCTION_PLAN_LAYOUT_Y_START) / FUNCTION_PLAN_LAYOUT_Y_STEP))

        added: list[str] = []
        errors: list[str] = []
        todo = sorted(identifiers, key=io_sort_key)
        for i, ident in enumerate(todo):
            err = await self._function_plan_add_single_io_pair(
                fub_id,
                ext_name,
                ident,
                ext_ios,
                webio_commands,
                existing_by_ref,
                conn_endpoints,
                (rows.get(ident, 0), column_index, rows_per_col),
            )
            if err is None:
                added.append(ident)
            elif err:
                errors.append(err)
            if progress_cb:
                progress_cb(i + 1, len(todo))

        return added, errors

    async def _function_plan_add_single_io_pair(
        self,
        fub_id: int,
        ext_name: str,
        ident: str,
        ext_ios: dict[str, dict],
        webio_commands: dict,
        existing_by_ref: dict[tuple[int, int], int],
        conn_endpoints: list[set[int]],
        slot: tuple[int, int, int],
    ) -> str | None:
        """Add one IO+Web-IO pair at its deterministic slot=(row, column_index, rows_per_col).

        Return semantics as _function_plan_wire_ref_pair (None = added, "" = already wired).
        """
        label = f"{ext_name} {ident}"
        io_entry = ext_ios.get(ident)
        if not io_entry:
            return f"{label}: not found in fresh config"

        expected_cmd_name = f"HA IO {ext_name} {ident}"
        webio_cmd = webio_commands.get(expected_cmd_name)
        if not webio_cmd:
            _LOGGER.warning("function_plan_add_io_pairs: %s — Web-IO '%s' not found", label, expected_cmd_name)
            return f"{label}: Web-IO '{expected_cmd_name}' not found after config reload"

        web_ref_id = webio_cmd.get("webIoId")
        if web_ref_id is None:
            return f"{label}: no webIoId for '{expected_cmd_name}'"

        try:
            io_ref_id = int(io_entry["id"])
        except (TypeError, ValueError):
            return f"{label}: invalid internal io id '{io_entry.get('id')}'"

        row, column_index, rows_per_col = slot
        sub_col, row_in_col = divmod(row, rows_per_col)
        x_off = (column_index + sub_col) * FUNCTION_PLAN_LAYOUT_COLUMN_WIDTH
        pos = (
            FUNCTION_PLAN_LAYOUT_X_MARKER + x_off,
            FUNCTION_PLAN_LAYOUT_X_WEBIO + x_off,
            FUNCTION_PLAN_LAYOUT_Y_START + row_in_col * FUNCTION_PLAN_LAYOUT_Y_STEP,
        )
        conn_type = "binary" if io_entry.get("is_binary") else "analog"
        return await self._function_plan_wire_ref_pair(
            fub_id, 1, io_ref_id, int(web_ref_id), conn_type, label, existing_by_ref, conn_endpoints, pos
        )

    async def function_plan_rebuild_plan_from_snapshot(
        self, fub_id: int, snapshot: dict[str, Any]
    ) -> tuple[int, int, list[str]]:
        """Recreate every element and connection from a snapshot on a freshly created (empty) plan.

        Snapshot element IDs are plan-local and meaningless on a new plan: pass 1 (re)creates
        every element regardless of type (Marker, WebIO, IO, any catalog function block,
        Comment, Constant), building an old-id -> new-id map; pass 2 redraws every connection
        using that map with its original port positions/polarity (see
        function_plan_catalog.py for why Constants need special handling: their value lives
        in the element's own "name" field, ref_id is always 0).

        Returns (elements_created, connections_created, warnings).
        """
        elements = snapshot.get("elements", {})
        connections = snapshot.get("connections", {})

        id_map, warnings = await self._rebuild_all_elements(fub_id, elements)
        connections_created = 0
        for conn_id, conn in connections.items():
            c_delta, conn_warnings = await self._rebuild_one_connection(fub_id, conn_id, conn, id_map)
            connections_created += c_delta
            warnings.extend(conn_warnings)

        _LOGGER.info(
            "function_plan_rebuild_plan_from_snapshot: fub=%s elements=%d connections=%d warnings=%d",
            fub_id,
            len(id_map),
            connections_created,
            len(warnings),
        )
        return len(id_map), connections_created, warnings

    async def _rebuild_all_elements(self, fub_id: int, elements: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
        """(Re)create every snapshot element on a fresh plan. Returns ({old_id: new_id}, warnings)."""
        id_map: dict[str, int] = {}
        warnings: list[str] = []
        for old_id, elem in elements.items():
            ref = elem.get("reference", {})
            elem_type = ref.get("type")
            ref_id = ref.get("ref_id", 0)
            x, y = elem.get("position_x", 0.0), elem.get("position_y", 0.0)
            if elem_type == FUNCTION_PLAN_COMMENT_TYPE:
                text = (elem.get("name") or "").strip() or FUNCTION_PLAN_MANAGED_PLAN_COMMENT
                new_id = await self.function_plan_add_comment_element(fub_id, text, x=x, y=y)
            elif elem_type == FUNCTION_PLAN_CONSTANT_TYPE:
                new_id = await self.function_plan_add_constant_element(fub_id, elem.get("name", "0"), x=x, y=y)
            else:
                new_id = await self.function_plan_add_element(
                    fub_id=fub_id, ref_id=ref_id, element_type=elem_type, x=x, y=y
                )
            if new_id is None:
                warnings.append(f"element {old_id} (type={elem_type}, ref_id={ref_id}) failed to recreate")
                continue
            id_map[old_id] = new_id
        return id_map, warnings

    async def _rebuild_one_connection(
        self, fub_id: int, conn_id: str, conn: dict[str, Any], id_map: dict[str, int]
    ) -> tuple[int, list[str]]:
        """Recreate one snapshot connection (one source -> one or more sinks) via the id_map.

        Comexio's own semantics: "input" is the source element, "output" is the list of
        sink elements it fans out to (see project memory project_logikplan_api.md). All sinks
        for one source MUST be sent in a single saveconnection call — sending them as
        separate calls silently drops every wire from that source when it's an IO or Constant
        element (confirmed live 2026-08-22; catalog function-block sources tolerated the
        split, IO/Constant sources did not).

        Returns (connections_created, warnings) — created is 0 or 1 (one API call per entry).
        """
        inp = conn.get("input", {})
        old_src = str(inp.get("FubElementId"))
        new_src = id_map.get(old_src)
        if new_src is None:
            return 0, [f"connection {conn_id}: source element {old_src} was not recreated — skipped"]
        conn_type = "analog" if conn.get("type") in (1, "analog") else "binary"

        raw_outputs = conn.get("output", [])
        raw_outputs = list(raw_outputs.values()) if isinstance(raw_outputs, dict) else raw_outputs

        warnings: list[str] = []
        outputs: list[tuple[int, int, bool]] = []
        for out in raw_outputs:
            old_dst = str(out.get("FubElementId"))
            new_dst = id_map.get(old_dst)
            if new_dst is None:
                warnings.append(f"connection {conn_id}: sink element {old_dst} was not recreated — skipped")
                continue
            outputs.append((new_dst, out.get("IOPos", 0), out.get("Inverted", False)))

        if not outputs:
            return 0, warnings

        result = await self.function_plan_save_connection(
            fub_id,
            new_src,
            outputs,
            conn_type,
            input_pos=inp.get("IOPos", 0),
            input_inverted=inp.get("Inverted", False),
        )
        if result is None:
            warnings.append(f"connection {conn_id}: {old_src}->{[o[0] for o in outputs]} save_connection failed")
            return 0, warnings
        return 1, warnings

    def function_plan_name(self, fub_id: int) -> str:
        """Display name of a Function Plan for logging/reporting; falls back to the id."""
        return next(
            (fd.get("Name", str(fub_id)) for fid, fd in self._fub_data.items() if int(fid) == fub_id), str(fub_id)
        )

    @staticmethod
    def _find_marker_element_id(elements: dict[str, Any], marker_id: int) -> str | None:
        """Return the plan-local element id of the given marker, or None if not wired into the plan."""
        for elem_id, elem_data in elements.items():
            ref = elem_data.get("reference", {})
            if ref.get("type") == 2 and int(ref.get("ref_id", -1)) == marker_id:
                return str(elem_id)
        return None

    @staticmethod
    def _connection_output_ids(conn_data: dict[str, Any]) -> list[str]:
        """Normalize a connection's output endpoints (server may serialize as dict or list)."""
        outputs = conn_data.get("output") or []
        if isinstance(outputs, dict):
            outputs = list(outputs.values())
        return [str(o.get("FubElementId")) for o in outputs if isinstance(o, dict)]

    @staticmethod
    def _element_has_any_wiring(elem_id: str, plan_data: dict) -> bool:
        """True if elem_id is a source (input) or sink (output) of any connection in this plan.

        Broader than a WebIO-specific check — used to tell "not wired to a WebIO" apart from
        "not wired at all", since only the latter is safe to delete outright.
        """
        for conn_data in plan_data.get("connections", {}).values():
            if str(conn_data.get("input", {}).get("FubElementId", -1)) == elem_id:
                return True
            if elem_id in ComexioAPI._connection_output_ids(conn_data):
                return True
        return False

    @staticmethod
    def _find_wired_webio_ids_for_marker(marker_id: int, marker_elem_id: str, plan_data: dict) -> list[int]:
        """Return the webIoIds of all WebIO elements directly wired to the given marker element."""
        elements = plan_data.get("elements", {})
        webio_ids: list[int] = []
        for conn_data in plan_data.get("connections", {}).values():
            input_elem = conn_data.get("input", {})
            if str(input_elem.get("FubElementId", -1)) != marker_elem_id:
                continue
            for out_elem_id in ComexioAPI._connection_output_ids(conn_data):
                ref = elements.get(out_elem_id, {}).get("reference", {})
                if ref.get("type") != 10:
                    continue
                raw_ref_id = ref.get("ref_id")
                if raw_ref_id is None:
                    continue
                try:
                    webio_ids.append(int(raw_ref_id))
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "_find_wired_webio_ids_for_marker: malformed WebIO ref_id %r on elem=%s (M%d)",
                        raw_ref_id,
                        out_elem_id,
                        marker_id,
                    )
        return webio_ids

    @staticmethod
    def _find_webio_wiring(webio_id: int, plan_data: dict) -> list[int] | None:
        """Return [webio_elem_id, connected_source_elem_id] if webio_id is wired in this plan.

        The source element is whatever is wired to the WebIO element's input side (marker or
        raw IO — the type doesn't matter here, it's simply the other end of the same wire).
        Returns None if this webIoId has no element in the plan, or the element isn't wired.
        """
        elements = plan_data.get("elements", {})
        webio_elem_id = next(
            (
                eid
                for eid, elem in elements.items()
                if elem.get("reference", {}).get("type") == 10
                and str(elem.get("reference", {}).get("ref_id")) == str(webio_id)
            ),
            None,
        )
        if webio_elem_id is None:
            return None
        for conn_data in plan_data.get("connections", {}).values():
            if webio_elem_id not in ComexioAPI._connection_output_ids(conn_data):
                continue
            input_elem_id = conn_data.get("input", {}).get("FubElementId")
            if input_elem_id is None:
                continue
            return [int(webio_elem_id), int(input_elem_id)]
        return None

    async def _delete_plan_elements_and_restart(
        self, fub_id: int, elem_ids_to_delete: list[int], webio_cmd_ids: list[int], plan_name: str
    ) -> dict:
        """Stop the plan, delete the given elements, restart it, and build the result dict."""
        stop_ok = await self.function_plan_stop_fup(fub_id)
        if not stop_ok:
            _LOGGER.error(
                "_delete_plan_elements_and_restart: failed to stop plan '%s' (fub=%s), aborting cleanup",
                plan_name,
                fub_id,
            )
            return {
                "deleted_elem_count": 0,
                "webio_cmd_ids": [],
                "fub_id": fub_id,
                "plan_stopped": False,
                "plan_name": plan_name,
                "stop_failed": True,
            }

        success = await self.function_plan_delete_elements(elem_ids_to_delete)
        if not success:
            _LOGGER.error("_delete_plan_elements_and_restart: element deletion failed")
            restart_after_failure_ok = await self.function_plan_run_fup(fub_id)
            return {
                "deleted_elem_count": 0,
                "webio_cmd_ids": [],
                "fub_id": fub_id,
                "plan_stopped": not restart_after_failure_ok,
                "plan_name": plan_name,
            }

        restart_ok = await self.function_plan_run_fup(fub_id)
        _LOGGER.info(
            "_delete_plan_elements_and_restart: deleted %d elements, webio_cmd_ids=%s (plan '%s' %s)",
            len(elem_ids_to_delete),
            webio_cmd_ids,
            plan_name,
            "restarted" if restart_ok else "restart failed — left stopped",
        )
        return {
            "deleted_elem_count": len(elem_ids_to_delete),
            "webio_cmd_ids": webio_cmd_ids,
            "fub_id": fub_id,
            "plan_stopped": not restart_ok,
            "plan_name": plan_name,
        }

    async def set_value(
        self,
        target_type: str,
        target_id: str | int,
        value: float | int,
        ext: str | None = None,
        identifier: str | None = None,
    ) -> bool:
        """API write via Basic Auth."""
        auth = aiohttp.BasicAuth(self.api_user, self.api_pass or "") if self.api_user else None

        if auth is not None and not self._auth_warned and not _is_local_address(self.host):
            _LOGGER.warning(
                "Using Basic Auth over plain HTTP on a non-local address. Credentials may be transmitted in clear text."
            )
            self._auth_warned = True

        url = f"{self._base_url}/api/"
        params: dict[str, Any] = {"action": "set", "value": value}

        if target_type == "marker":
            params["marker"] = f"M{target_id}"
        else:
            if ext is None or identifier is None:
                _LOGGER.error("Missing 'ext' or 'identifier' for non-marker API write. Type: %s", target_type)
                return False
            params["ext"] = ext
            params["io"] = identifier

        try:
            async with self.session.get(url, params=params, auth=auth) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Comexio API write failed: HTTP %s for %s with params=%s",
                        resp.status,
                        url,
                        {k: v for k, v in params.items() if k != "value"},
                    )
                    return False
                return True
        except aiohttp.ClientError as err:
            _LOGGER.exception(
                "Comexio API write request error for %s with params=%s: %s",
                url,
                {k: v for k, v in params.items() if k != "value"},
                err,
            )
            return False
        except Exception:
            _LOGGER.exception(
                "Unexpected error during Comexio API write for %s with params=%s",
                url,
                {k: v for k, v in params.items() if k != "value"},
            )
            return False

    async def get_bus_workload(self) -> dict[str, Any]:
        """Fetch the internal bus workload (%) and SD-card presence from the admin interface.

        Called on a fast, independent poll cadence (see coordinator's bus-load loop) —
        much more frequent than the main config audit, so failures are logged at debug
        level only to avoid log spam.
        """
        url = f"{self._base_url}/admin/in_output/inoutputinfo"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/in_output/home"}
        try:
            async with self.session.post(url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Bus workload fetch failed with HTTP status: %s", resp.status)
                    return {}
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.debug("HTTP request error fetching bus workload: %s", err)
            return {}
        except Exception as err:
            _LOGGER.debug("Unexpected error fetching bus workload: %s", err)
            return {}

    async def system_emergency_reboot(self) -> bool:
        """Trigger an IMMEDIATE, unconfirmed full Comexio system reboot.

        Comexio has no confirmation dialog for this and returns no structured result — the
        request itself is the action. Only called by the Bus-Load-Watchdog's emergency path,
        gated behind CONF_BUS_WATCHDOG_AUTO_REBOOT (default off). HTTP 200 only means the
        request was accepted, not that the reboot completed cleanly.
        """
        url = f"{self._base_url}/admin/admin_dashboard/home/"
        try:
            async with self.session.get(url, params={"id": "system", "restart": "1"}) as resp:
                _LOGGER.warning("system_emergency_reboot: request sent, HTTP status %s", resp.status)
                return resp.status == 200
        except aiohttp.ClientError as err:
            _LOGGER.error("system_emergency_reboot: HTTP request error: %s", err)
            return False

    async def check_extension_firmware(self) -> list[dict[str, Any]]:
        """Query the local extension bus for available firmware updates (BASE + all extensions).

        Comexio documents that this can briefly interrupt extension outputs while it runs, so
        it must only be called rarely — see the coordinator's version-gated nightly check, not
        a regular poll. Logged at warning level (not debug) since failures here are infrequent
        enough to matter, unlike the fast bus-workload poll.
        """
        url = f"{self._base_url}/admin/extension/checkextension_fwupdate/"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}/admin/"}
        try:
            async with self.session.post(url, data={"pos": "local"}, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Extension firmware check failed with HTTP status: %s", resp.status)
                    return []
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.warning("HTTP request error checking extension firmware: %s", err)
            return []
        except Exception as e:
            _LOGGER.exception("Unexpected error checking extension firmware: %s", e)
            return []
        if payload.get("ok") != "ok":
            _LOGGER.warning("Extension firmware check returned an error payload: %s", payload)
            return []
        return payload.get("data", [])

    async def close(self) -> None:
        """Close the main session and the dedicated preview session, if one was ever opened.

        The main session is created during config entry setup, so async_create_clientsession
        already registers it for auto-cleanup on entry unload/HA shutdown. The preview session
        (see ensure_preview_session) is created lazily at runtime, outside that setup context,
        so it only gets HA's homeassistant_stop cleanup — not entry-unload cleanup. Closing both
        explicitly here (already called from async_unload_entry and the setup-failure path)
        avoids leaking the preview session's connection across integration reloads.
        """
        await self.session.close()
        if self._preview_session is not None:
            await self._preview_session.close()
            self._preview_session = None
