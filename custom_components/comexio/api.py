# Version: 0.7.5
import base64
import binascii
from contextlib import suppress
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
from .const import KNOWN_DOMAINS


class SafeDict(dict):
    """Safe dictionary for string formatting that doesn't crash on missing keys."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


LOCAL_HOSTNAME_RE = re.compile(
    r"^(?:localhost|"
    r"(?:[a-zA-Z0-9_-]+\.local)|"
    r"(?:[a-zA-Z0-9_-]+\.lan)|"
    r"(?:[a-zA-Z0-9_-]+\.home))\.?$"
)

# Module-level compiled patterns for get_raw_config (used on every coordinator refresh).
_IO_TYPES_DECL_RE = re.compile(r"var\s+\$ioTypes\s*=\s*")
_SCRIPT_BLOCK_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_VAR_DECL_RE = re.compile(r"var\s+\$([\w\d_]+)\s*=\s*", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


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
        session_kwargs = {}
        if _is_local_address(self.host):
            session_kwargs["cookie_jar"] = aiohttp.CookieJar(unsafe=True)

        self.session: aiohttp.ClientSession = async_create_clientsession(hass, **session_kwargs)

        # io_types holds mapping of IO type metadata. Default to {} for safe fallbacks.
        self.io_types: dict[str, Any] = {}
        self._auth_warned: bool = False
        self._login_warned: bool = False

    @property
    def _base_url(self) -> str:
        """Return the base URL for the Comexio IO-Server."""
        return f"http://{self.host}"

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
            _LOGGER.error("Failed to determine HA address: %s", e)
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

    async def login(self) -> bool:
        """Performs the RSA login procedure for admin access."""
        if not _is_local_address(self.host) and not self._login_warned:
            _LOGGER.warning(
                "Logging into Comexio over plain HTTP on a non-local address (%s). "
                "Credentials may be transmitted in clear text.",
                self.host,
            )
            self._login_warned = True

        _LOGGER.debug("Starting v11 RSA login procedure for host: %s", self.host)
        url = f"{self._base_url}/board/home/login/"

        self.session.cookie_jar.update_cookies({"comexio-client-time": str(int(time.time()))})
        try:
            async with self.session.post(url, data={"login_keys": "true"}) as resp:
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
                self.session.post(url, data=payload, headers={"Referer": url}) as resp,
                self.session.get(f"{self._base_url}/admin/") as v_resp,
            ):
                html = await v_resp.text()
                if "Anmeldung" not in html and html != "":
                    _LOGGER.info("Successfully logged into Comexio Admin interface")
                    return True
            return False
        except (aiohttp.ClientError, json.JSONDecodeError, KeyError, binascii.Error, ValueError) as e:
            _LOGGER.error("Critical error during Comexio login: %s", e)
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

        # Extract $ioTypes for dynamic unit and type mapping
        if assign_match := _IO_TYPES_DECL_RE.search(main_html):
            brace_index = main_html.find("{", assign_match.end())
            if brace_index != -1:
                raw_object, _ = _extract_js_object_literal(main_html, brace_index)
                if raw_object:
                    try:
                        clean_json = _normalize_js_like_object(raw_object)
                        self.io_types = json.loads(clean_json)
                        _LOGGER.debug("Successfully loaded %d Comexio IO types", len(self.io_types))
                    except json.JSONDecodeError as exc:
                        _LOGGER.error("Failed to decode $ioTypes JSON: %s. Falling back to generic IOs.", exc)
                        self.io_types = {}
                else:
                    _LOGGER.warning("Failed to parse $ioTypes object. Falling back to generic IOs.")
                    self.io_types = {}
            else:
                _LOGGER.warning("Failed to locate opening brace for $ioTypes object. Falling back to generic IOs.")
                self.io_types = {}
        else:
            _LOGGER.warning("Global IO types variable $ioTypes not found. Falling back to generic IOs.")
            self.io_types = {}

        # 2. Fetch the function module page for the technical device configuration
        url_conf = f"{self._base_url}/admin/function_function_module/home"
        async with self.session.get(url_conf) as resp:
            if resp.status != 200:
                _LOGGER.error("Failed to fetch function module page (HTTP %s)", resp.status)
                return {}
            html = await resp.text()

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
                except Exception as json_error:
                    raw_text = await resp.text()
                    _LOGGER.error(
                        "Failed to parse live states response as JSON: %s; raw response: %s",
                        json_error,
                        raw_text,
                    )
                    return {}
        except aiohttp.ClientError as err:
            _LOGGER.error("HTTP request error fetching live states: %s", err)
            return {}
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching live states: %s", e)
            return {}

    def parse_config(self, conf: dict[str, Any], live_states: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Processes the raw configuration and performs a technical audit.
        Uses dynamic IO type mapping to determine binary vs analog states and units.
        """
        data = {"markers": [], "io": [], "webio_commands": {}, "device_id": None, "device_ip": None}
        live_states = live_states or {}

        # Load configuration
        config_names = self._load_config_names()
        webio_name, schema_marker, schema_io, server_alias = config_names

        # Extract FubModules once
        fub_modules = conf.get("FubModules", {})

        # 2. Map Webhooks and device info
        self._process_device_info(conf, data, webio_name, fub_modules)

        # 3. Process Markers
        self._process_markers(data, live_states, schema_marker, server_alias, fub_modules)

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

    def _process_device_info(
        self,
        conf: dict[str, Any],
        data: dict[str, Any],
        webio_name: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process device info and webhooks."""
        web_devices = conf.get("WebDevices", {})
        fub_10 = fub_modules.get("10", {})
        target_dev_id = None

        for d_id, d_data in web_devices.items():
            if d_data.get("Name") == webio_name:
                target_dev_id = str(d_id)
                data["device_id"] = target_dev_id
                data["device_ip"] = d_data.get("Ip")
                raw_base_id = d_data.get("BaseId")
                data["base_id"] = str(raw_base_id) if raw_base_id is not None else None
                break

        if target_dev_id and target_dev_id in fub_10:
            for w_id, w_obj in fub_10[target_dev_id].items():
                self._add_webhook_command(data, w_id, w_obj)

    def _add_webhook_command(self, data: dict[str, Any], w_id: str, w_obj: dict[str, Any]) -> None:
        """Add a webhook command to data."""
        raw_type = w_obj.get("TypeId")
        try:
            val_type = int(raw_type) if raw_type is not None else 1
        except (ValueError, TypeError):
            val_type = 1

        data["webio_commands"][w_obj.get("Name")] = {
            "webIoId": w_id,
            "cmdId": w_obj.get("WebCommandId"),
            "typeId": val_type,
        }

    def _process_markers(
        self,
        data: dict[str, Any],
        live_states: dict[str, Any],
        schema_marker: str,
        server_alias: str,
        fub_modules: dict[str, Any],
    ) -> None:
        """Process markers from config."""
        for m in fub_modules.get("2", {}).values():
            if not m.get("Name") or m.get("Id") is None:
                continue

            m_id = str(m.get("Id"))
            m_type_raw = m.get("Type", 1)
            m_type_str = "analog" if m_type_raw in [2, 3] else "digital"
            m_title = m.get("Name", "")

            ha_name = schema_marker.format_map(SafeDict(ServerAlias=server_alias, MarkerId=m_id, MarkerTitle=m_title))

            data["markers"].append(
                {
                    "id": m_id,
                    "ha_name": " ".join(ha_name.split()),
                    "name": f"M{m_id} {m.get('Name')}",
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
        """Process IOs from config."""
        for ext_id, ext_content in fub_modules.get("1", {}).items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")

            for io_item in ext_content.get("inoutput", {}).values():
                if not io_item or not io_item.get("Active"):
                    continue

                io_type_id = str(io_item.get("InOutputTypeId"))
                type_info = self.io_types.get(io_type_id, {})

                ident = io_item.get("Identifier") or str(io_item.get("Id", "unknown"))
                desc = io_item.get("Description") or ident

                self._add_io_entry(data, io_item, ext_name, ident, desc, type_info, schema_io, server_alias)

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
    ) -> None:
        """Add an IO entry to data."""
        is_binary = type_info.get("binary", False)
        v_min = type_info.get("min", 0)
        v_max = type_info.get("max", 1)
        unit = type_info.get("unit", "")

        # Cleanup unit strings
        if unit in ("\\u00b0C", "°C", "C"):
            unit = "°C"

        if desc and desc.strip() and desc != ident:
            io_name = f"{ext_name} {ident} {desc.strip()}"
        else:
            io_name = f"{ext_name} {ident}"

        ha_name = schema_io.format_map(
            SafeDict(ServerAlias=server_alias, ExtName=ext_name, IoId=ident, IoTitle=desc or "")
        )

        try:
            type_id_raw = int(io_item.get("InOutputTypeId", 1))
        except (ValueError, TypeError):
            type_id_raw = 1

        data["io"].append(
            {
                "id": str(io_item.get("Id")),
                "ext_name": ext_name,
                "identifier": ident,
                "ha_name": " ".join(ha_name.split()),
                "name": io_name,
                "is_binary": is_binary,
                "unit": unit,
                "min": v_min,
                "max": v_max,
                "type_id_raw": type_id_raw,
                "value": self._clean_value(io_item.get("Value", 0)),
            }
        )

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

    async def update_webio_device_ip(self, device_id: str | int, ha_address: str) -> bool:
        """
        Updates the server address (IP:Port) of an existing device.
        Uses the specific POST format required by Comexio's main save handler.
        """
        _LOGGER.info("Updating Web-IO device %s address to %s", device_id, ha_address)
        url = f"{self._base_url}/admin/web_io/save"

        webio_name = "HomeAssistant"
        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", "HomeAssistant")

        # Construct the payload based on user observations.
        device_data = {
            "web_device_id": str(device_id),
            f"name_{device_id}": webio_name,
            f"ip_{device_id}": ha_address,
            f"username_{device_id}": "",
            f"password_{device_id}": "",
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
                except Exception as json_error:
                    raw_text = await resp.text()
                    _LOGGER.error(
                        "Failed to parse Web-IO device IP update response as JSON: %s; raw response: %s",
                        json_error,
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
            "header_modifier": "Content-Type: application/json",
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

    def build_webio_commands(self, server_id: str, parsed_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the list of Web-IO command dicts for the given parsed configuration.

        Returns the list directly so callers can use it without a json.dumps/json.loads roundtrip.
        """
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands: list[dict[str, Any]] = []

        # 1. Create Web-IO for markers
        for m in parsed_data.get("markers", []):
            is_ana = m["type"] == "analog"
            safe_id = str(m["id"]).replace('"', '\\"')
            lua = (
                f"function data(a)\r\n"
                f'  local d = {{ id="{safe_id}", value=a, type="marker" }}\r\n'
                f"  return json_stringify(d)\r\n"
                f"end"
            )

            commands.append(
                {
                    "Name": f"HA {m['name']}",
                    "TypeId": 2 if is_ana else 1,
                    "Min": 0,
                    "Max": 100 if is_ana else 1,
                    "Parameter": webhook_path,
                    "HeaderModifier": "Content-Type: application/json",
                    "Data": lua,
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
            )

        # 2. Create Web-IO for IOs
        for io_item in parsed_data.get("io", []):
            # check data type
            is_ana = not io_item.get("is_binary", False)

            # Use the authentic min/max from the Comexio type definition
            v_min = io_item.get("min", 0)
            v_max = io_item.get("max", 100 if is_ana else 1)

            safe_ext = str(io_item["ext_name"]).replace('"', '\\"')
            safe_io_id = str(io_item["identifier"]).replace('"', '\\"')

            lua = (
                f"function data(a)\r\n"
                f'  local d = {{ ext="{safe_ext}", io="{safe_io_id}", '
                f'value=a, type="io" }}\r\n'
                f"  return json_stringify(d)\r\n"
                f"end"
            )

            commands.append(
                {
                    "Name": f"HA IO {io_item['ext_name']} {io_item['identifier']}",
                    "TypeId": 2 if is_ana else 1,
                    "Min": v_min,
                    "Max": v_max,
                    "Parameter": webhook_path,
                    "HeaderModifier": "Content-Type: application/json",
                    "Data": lua,
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
            )

        return commands

    def generate_webio_json(self, server_id: str, webio_name: str, parsed_data: dict[str, Any]) -> str:
        """Generate the upload-ready JSON string for the Comexio Web-IO importer."""
        return json.dumps(
            {
                "data": "web_io",
                "format": 1,
                "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 2, "BaseId": 0},
                "commands": self.build_webio_commands(server_id, parsed_data),
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
            "password": "",
            "web_device_base_sample": "none",
            "identifier": "",
            "form_login": "2",
        }

        async with self.session.post(url, data=payload, headers={"X-Requested-With": "XMLHttpRequest"}) as resp:
            return resp.status == 200

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
            _LOGGER.error(
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

    async def close(self) -> None:
        """Clean up session."""
        if not self.session.closed:
            try:
                await self.session.close()
            except Exception as err:
                _LOGGER.debug("Error closing Comexio API session: %s", err)
