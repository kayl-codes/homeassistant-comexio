# Version: 0.6.0
import re
import json
import aiohttp
import logging
import random
import base64
import time
import io
import asyncio
import socket
from urllib.parse import urlparse
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from multidict import MultiDict
from homeassistant.helpers.network import get_url

# Mandatory DOMAIN import for Audit logic
from .const import DOMAIN, KNOWN_DOMAINS
from .const import DOMAIN

class SafeDict(dict):
    """Safe dictionary for string formatting that doesn't crash on missing keys."""
    def __missing__(self, key):
        return '{' + key + '}'

class SafeDict(dict):
    """Safe dictionary for string formatting that doesn't crash on missing keys."""
    def __missing__(self, key):
        return '{' + key + '}'

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

    def __init__(self, hass, host, username, password, api_user=None, api_pass=None):
        """Initialize the API class with all required credentials."""
        self.hass = hass
        self.host = host
        self.username = username
        self.password = password
        self.api_user = api_user
        self.api_pass = api_pass
        
        # This context is vital for the Audit logic to find the configured webio_name
        self.config_entry = None
        
        # mandatory cookie management for session persistence
        jar = aiohttp.CookieJar(unsafe=True)
        self.session = aiohttp.ClientSession(cookie_jar=jar)

        self.io_types = {}


    async def _request(self, method, url, **kwargs):
        """Wrapper for HTTP requests with error handling."""
        try:
            async with self.session.request(method, url, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            _LOGGER.error("HTTP request failed: %s", e)
            return None

    def _clean_value(self, val):
        """Standardizes values: replaces German comma with dot and converts to numbers."""
        if val is None:
            return 0
        if isinstance(val, str):
            val = val.replace(",", ".")
        try:
            return float(val)
        except ValueError:
            _LOGGER.warning("Failed to clean value: %s", val)
            return 0

    async def get_ha_address(self):
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
                    except socket.error:
                        continue
                return None

            hostname = await self.hass.async_add_executor_job(resolve_dns)
            
            if not hostname:
                if not fallback_ip or fallback_ip in ["localhost", "127.0.0.1", "::1"]:
                    def get_local_ip():
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        try:
                            s.connect(("8.8.8.8", 80))
                            return s.getsockname()[0]
                        except Exception: return "127.0.0.1"
                        finally: s.close()
                    hostname = await self.hass.async_add_executor_job(get_local_ip)
                else:
                    hostname = fallback_ip

            return f"{hostname}:{port}"
        except Exception as e:
            _LOGGER.error("Failed to determine HA address: %s", e)
            return "127.0.0.1:8123"

    def _encrypt_block(self, data_str, mod, exp):
        """RSA encryption logic matching Comexio v11 (PKCS1v15)."""
        try:
            pub_key = rsa.RSAPublicNumbers(exp, mod).public_key()
            encrypted = pub_key.encrypt(
                data_str.encode('iso-8859-1'), 
                padding.PKCS1v15()
            )
            return encrypted.hex().zfill(512)
        except Exception as e:
            _LOGGER.error("RSA Block encryption failed: %s", e)
            raise


    async def login(self):
        """Performs the RSA login procedure for admin access."""
        _LOGGER.debug("Starting v11 RSA login procedure for host: %s", self.host)
        url = f"http://{self.host}/board/home/login/"
        
        try:
            self.session.cookie_jar.update_cookies({"comexio-client-time": str(int(time.time()))})
            async with self.session.post(url, data={'login_keys': 'true'}) as resp:
                keys = await resp.json(content_type=None)
            
            salt_str = base64.b64decode(keys['salt']).decode('iso-8859-1')
            mod, exp = int(keys['modulus'], 16), int(keys['exponent'], 16)
            nonce = ''.join(random.choice("0123456789ABCDEF") for _ in range(20))
            
            pw = f"{self._encrypt_block(salt_str + nonce + self.password, mod, exp)} {self._encrypt_block(salt_str + nonce + '', mod, exp)}"
            payload = MultiDict([('target', '/board/home/login'), ('username', self.username), ('password', pw), ('loginsubmit', 'Anmelden'), ('encryption', 'rsa')])
            
            async with self.session.post(url, data=payload, headers={"Referer": url}) as resp:
                async with self.session.get(f"http://{self.host}/admin/") as v_resp:
                    html = await v_resp.text()
                    if "Anmeldung" not in html and html != "":
                        _LOGGER.info("Successfully logged into Comexio Admin interface")
                        return True
            return False
        except Exception as e:
            _LOGGER.error("Critical error during Comexio login: %s", e)
            return False


    async def get_raw_config(self):
        """
        Downloads JS config objects and global IO types from the admin interface.
        This provides the source of truth for all device properties and units.
        """
        # 1. Fetch the main admin page to get global variables like $ioTypes
        url_main = f"http://{self.host}/admin/"
        async with self.session.get(url_main) as resp:
            main_html = await resp.text()
            
            # Extract $ioTypes for dynamic unit and type mapping
            type_match = re.search(r'var\s+\$ioTypes\s*=\s*({.*?});', main_html, re.DOTALL)
            if type_match:
                try:
                    self.io_types = json.loads(type_match.group(1))
                    _LOGGER.debug("Successfully loaded %d Comexio IO types", len(self.io_types))
                except json.JSONDecodeError:
                    _LOGGER.error("Failed to decode $ioTypes JSON")
                    self.io_types = {}

        # 2. Fetch the function module page for the technical device configuration
        url_conf = f"http://{self.host}/admin/function_function_module/home"
        async with self.session.get(url_conf) as resp:
            html = await resp.text()
            
        matches = re.finditer(r'var\s+\$([\w\d_]+)\s*=\s*(\{.*?\});', html, re.DOTALL)
        result = {}
        for m in matches:
            try:
                result[m.group(1)] = json.loads(m.group(2))
            except json.JSONDecodeError:
                continue
        return result


    async def get_live_states(self, marker_count):
        """Fetches current live values for markers from the dashboard refresh endpoint."""
        url = f"http://{self.host}/board/dashboard/refresh/"
        markers_dict = {str(i): {"action": "get", "MarkerName": f"M{i}"} for i in range(1, marker_count + 1)}
        markers_dict["messages"] = {"action": "messages"}
        
        form_data = aiohttp.FormData()
        form_data.add_field('json', json.dumps(markers_dict))
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/", 
            "User-Agent": "Mozilla/5.0"
        }
        
        try:
            async with self.session.post(url, data=form_data, headers=headers) as resp:
                data = await resp.json(content_type=None)
                return data.get("result", {})
        except Exception as e:
            _LOGGER.error("Error fetching live states: %s", e)
            return {}


    def parse_config(self, conf, live_states=None):
        """
        Processes the raw configuration and performs a technical audit.
        Uses dynamic IO type mapping to determine binary vs analog states and units.
        """
        data = {"markers": [], "io": [], "webio_commands": {}, "device_id": None, "device_ip": None}
        live_states = live_states or {}
                
        # 1. Determine the configured Web-IO name for this instance
        webio_name = "HomeAssistant"
        schema_marker = "M{MarkerId} {MarkerTitle}"
        schema_io = "{ExtName} {IoId} {IoTitle}"
        server_alias = "comexio"
        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", "HomeAssistant")
            schema_marker = conf_data.get("schema_marker", "M{MarkerId} {MarkerTitle}")
            schema_io = conf_data.get("schema_io", "{ExtName} {IoId} {IoTitle}")
            server_alias = conf_data.get("server_id", "comexio")

        # 2. Map Webhooks (Web-IO Commands) from Comexio for Audit
        web_devices = conf.get("WebDevices", {})
        fub_10 = conf.get("FubModules", {}).get("10", {})
        target_dev_id = None
        
        for d_id, d_data in web_devices.items():
            if d_data.get("Name") == webio_name:
                target_dev_id = str(d_id)
                data["device_id"] = target_dev_id
                data["device_ip"] = d_data.get("Ip")
                
                # Fix: Do not treat 0 as False
                raw_base_id = d_data.get("BaseId")
                data["base_id"] = str(raw_base_id) if raw_base_id is not None else None
                break
        
        if target_dev_id and target_dev_id in fub_10:
            for w_id, w_obj in fub_10[target_dev_id].items():
                raw_type = w_obj.get("TypeId")
                try:
                    val_type = int(raw_type) if raw_type is not None else 1
                except (ValueError, TypeError):
                    val_type = 1

                data["webio_commands"][w_obj.get("Name")] = {
                    "webIoId": w_id, 
                    "cmdId": w_obj.get("WebCommandId"),
                    "typeId": val_type
                }
        
        # 3. Process Markers (Digital, Analog, or Interval)
        for m in conf.get("FubModules", {}).get("2", {}).values():
            if not m.get("Name"): 
                continue
                
            m_id = str(m.get("Id"))
            m_type_raw = m.get("Type", 1)
            
            # Map type: 1=Digital, 2/3=Analog
            m_type_str = "analog" if m_type_raw in [2, 3] else "digital"
            m_title = m.get('Name', '')
            
            # Apply User Naming Schema
            ha_name = schema_marker.format_map(SafeDict(
                ServerAlias=server_alias, 
                MarkerId=m_id, 
                MarkerTitle=m_title
            ))
            
            data["markers"].append({
                "id": m_id,
                "ha_name": " ".join(ha_name.split()),
                "name": f"M{m_id} {m.get('Name')}",
                "type": m_type_str,
                "type_raw": m_type_raw,
                "value": self._clean_value(live_states.get(m_id, 0))
            })

        # 4. Process IOs using dynamic type mapping from $ioTypes
        for ext_id, ext_content in conf.get("FubModules", {}).get("1", {}).items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")
            
            for io_item in ext_content.get("inoutput", {}).values():
                if not io_item or not io_item.get("Active"): 
                    continue
                
                io_type_id = str(io_item.get("InOutputTypeId"))
                # Get technical properties from global type list
                type_info = self.io_types.get(io_type_id, {})

                is_binary = type_info.get("binary", False)
                v_min = type_info.get("min", 0)
                v_max = type_info.get("max", 1) # Default 1 for binary
                unit = type_info.get("unit", "")
                
                # Cleanup unit strings (e.g. encoded Celsius)
                if "\\u00b0C" in unit or "C" in unit:
                    unit = "°C"

                desc = io_item.get("Description")
                ident = io_item.get("Identifier")
                if desc and desc.strip() and desc != ident:
                    io_name = f"{ext_name} {ident} {desc.strip()}"
                else:
                    io_name = f"{ext_name} {ident}"
                    
                # Apply User Naming Schema
                ha_name = schema_io.format_map(SafeDict(
                    ServerAlias=server_alias,
                    ExtName=ext_name,
                    IoId=ident,
                    IoTitle=desc if desc else ""
                ))

                data["io"].append({
                    "id": str(io_item.get("Id")), 
                    "ext_name": ext_name,
                    "identifier": ident,
                    "ha_name": " ".join(ha_name.split()),
                    "name": io_name,
                    "is_binary": is_binary,
                    "unit": unit,
                    "min": v_min,
                    "max": v_max,
                    "type_id_raw": int(io_type_id),
                    "value": self._clean_value(io_item.get("Value", 0))
                })
                
        _LOGGER.info("Audit: %d Markers, %d IOs, %d Webhooks in Comexio for %s", 
                    len(data["markers"]), len(data["io"]), len(data["webio_commands"]), webio_name)
        return data


    # --- WEB-IO MANAGEMENT ---
    async def get_webio_base_info(self, webio_name):
        """Scans classes via add-page."""
        url_add = f"http://{self.host}/admin/web_io/add"
        async with self.session.get(url_add) as resp:
            html = await resp.text()
            pattern = fr'<option value="(\d+)"[^>]*>{re.escape(webio_name)}</option>'
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                b_id = match.group(1)
                url_win = f"http://{self.host}/admin/web_io/baseDeviceWindow/"
                async with self.session.get(url_win) as win_resp:
                    win_html = await win_resp.text()
                    
                    return (b_id, f"delete_web_device_base/?id={b_id}" in win_html)
        return None


    async def get_webio_device_info(self, device_name):
        """Checks instance existence via HTML tabs."""
        url_home = f"http://{self.host}/admin/web_io/home"
        async with self.session.get(url_home) as resp:
            html = await resp.text()
            pattern = fr'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
            match = re.search(pattern, html, re.IGNORECASE)
            return match.group(1) if match else None

    async def delete_webio_device(self, device_id):
        """
        Tries to delete the device instance. 
        Returns True if successful, False if blocked by Comexio logic.
        """
        url = f"http://{self.host}/admin/web_io/delete_device/?id={device_id}"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        _LOGGER.debug("Deleting Web-IO device %s via GET: %s", device_id, url)
        async with self.session.get(url, headers=headers) as resp:
            html = await resp.text()
            
            # Check for jQuery UI error state which indicates the device is in use
            if 'class="ui-state-error' in html or "ui-state-error" in html:
                _LOGGER.warning("Device %s is in use within Comexio logic and cannot be deleted.", device_id)
                return False
            return resp.status == 200


    async def delete_webio_base(self, base_id):
        """Sends DELETE command for device class template."""
        url = f"http://{self.host}/admin/web_io/delete_web_device_base/?id={base_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"http://{self.host}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            return resp.status == 200

    async def update_webio_device_ip(self, device_id, ha_address):
        """
        Updates the server address (IP:Port) of an existing device.
        Uses the specific POST format required by Comexio's main save handler.
        """
        _LOGGER.info("Updating Web-IO device %s address to %s", device_id, ha_address)
        url = f"http://{self.host}/admin/web_io/save"
        
        webio_name = "HomeAssistant"
        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", "HomeAssistant")
            schema_marker = conf_data.get("schema_marker", "{ServerAlias} M{MarkerId} {MarkerTitle}")
            schema_io = conf_data.get("schema_io", "{ServerAlias} {ExtName} {IoId} {IoTitle}")
            server_alias = conf_data.get("server_id", "comexio")
        else:
            schema_marker = "{ServerAlias} M{MarkerId} {MarkerTitle}"
            schema_io = "{ServerAlias} {ExtName} {IoId} {IoTitle}"
            server_alias = "comexio"

        # Construct the payload based on user observations.
        device_data = {
            "web_device_id": str(device_id),
            f"name_{device_id}": webio_name,
            f"ip_{device_id}": ha_address,
            f"username_{device_id}": "",
            f"password_{device_id}": "",
            f"checkca_{device_id}": "0",
            f"pinnedpubkey_{device_id}": "",
            f"form_login_{device_id}": "2"
        }
        
        payload = {
            "no_reload": "true",
            "JSON": json.dumps(device_data)
        }
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        
        async with self.session.post(url, data=payload, headers=headers) as resp:
            if resp.status == 200:
                result = await resp.json(content_type=None)
                return result.get("save") == 1
            return False

    async def delete_single_command(self, cmd_id, device_id):
        """Removes an individual command instance (Delta Sync)."""
        _LOGGER.info("Deleting individual Web-IO command ID: %s", cmd_id)
        url = f"http://{self.host}/admin/web_io/delete_web_command/?id={cmd_id}&dev={device_id}"
        async with self.session.get(url) as resp:
            return resp.status == 200


    async def save_single_command(self, base_id, device_id, cmd_payload, existing_cmd_id=None):
        """
        Adds or updates a single command in an existing device (Delta Sync).
        If existing_cmd_id is provided, an UPDATE is performed.
        """
        _LOGGER.info("Applying command: %s (Update: %s)", cmd_payload.get("Name"), existing_cmd_id is not None)
        url = f"http://{self.host}/admin/web_io/save_command"
        
        # Base64 identifier for Comexio
        if existing_cmd_id:
            # Update format exactly like original Comexio trace: {"src":"command","id":1324}
            cmd_ref = json.dumps({"src":"command","id": int(existing_cmd_id)}, separators=(',', ':'))
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
            "DefaultActive": 1
        }
        
        if existing_cmd_id:
            payload["id"] = str(existing_cmd_id)
        else:
            payload["deviceBaseId"] = str(base_id) if base_id is not None else "0"
        
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        
        #_LOGGER.debug("Sending save_single_command payload: %s", payload)
        
        async with self.session.post(url, data=payload, headers=headers) as resp:
            resp_text = await resp.text()
            #_LOGGER.debug("save_single_command response [%s]: %s", resp.status, resp_text)
            return resp.status == 200

    def generate_webio_json(self, server_id, webio_name, parsed_data):
        """Generates JSON for import with instant activation flag."""
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands = []
        
        # 1. Create Web-IO for markers
        for m in parsed_data.get("markers", []):
            is_ana = m['type'] == "analog"
            lua = f"function data(a)\r\n  local d = {{ id=\"{m['id']}\", value=a, type=\"marker\" }}\r\n  return json_stringify(d)\r\nend"
            
            commands.append({
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
                "io": []
            })

        # 2. Create Web-IO for IOs
        for io_item in parsed_data.get("io", []):
            # check data type
            is_ana = not io_item.get("is_binary", False)
            #unit = io_item.get("unit", "")
            
            # Use the authentic min/max from the Comexio type definition
            v_min = io_item.get("min", 0)
            v_max = io_item.get("max", 1 if not is_ana else 100)
            
            lua = f"function data(a)\r\n  local d = {{ ext=\"{io_item['ext_name']}\", io=\"{io_item['identifier']}\", value=a, type=\"io\" }}\r\n  return json_stringify(d)\r\nend"
            
            commands.append({
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
                "io": []
            })
            
        return json.dumps({
            "data": "web_io", 
            "format": 1, 
            "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 2, "BaseId": 0}, 
            "commands": commands
        })


    async def upload_web_io(self, server_id, webio_name, web_io_json):
        """Uploads JSON class template."""
        url = f"http://{self.host}/admin/web_io/upload_device_settings"
        file_data = io.BytesIO(web_io_json.encode('utf-8'))
        form = aiohttp.FormData()
        form.add_field('file', file_data, filename=f"ha_{server_id}.json", content_type='application/json')
        form.add_field('set_name', webio_name)
        async with self.session.post(url, data=form, headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"http://{self.host}/admin/web_io/home"}) as resp:
            if resp.status == 200:
                result = await resp.json(content_type=None)
                if result.get("ok"): return True, result.get("base_id")
            return False, await resp.text()


    async def create_webio_device(self, name, base_id, ha_address=None):
        """Creates a device instance. Automatically determines HA address if not provided."""
        if not ha_address:
            ha_address = await self.get_ha_address()
            
        url = f"http://{self.host}/admin/web_io/saveDeviceWindow"
        
        payload = {
            "name": name, 
            "ip": ha_address, 
            "web_device_base": base_id, 
            "username": "", 
            "password": "", 
            "web_device_base_sample": "none", 
            "identifier": "", 
            "form_login": "2"
        }
        
        async with self.session.post(url, data=payload, headers={"X-Requested-With": "XMLHttpRequest"}) as resp:
            return resp.status == 200


    async def set_value(self, target_type, target_id, value, ext=None, identifier=None):
        """API write via Basic Auth."""
        auth = aiohttp.BasicAuth(self.api_user, self.api_pass) if self.api_user else None
        url = f"http://{self.host}/api/"
        params = {"action": "set", "value": value}
        if target_type == "marker": params["marker"] = f"M{target_id}"
        else: params["ext"], params["io"] = ext, identifier
        try:
            async with self.session.get(url, params=params, auth=auth) as resp:
                return resp.status == 200
        except Exception: return False


    async def close(self):
        """Clean up session."""
        await self.session.close()
