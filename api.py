# Version: 0.4.0
import re
import json
import aiohttp
import logging
import random
import base64
import time
import io
import asyncio
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from multidict import MultiDict

# Mandatory DOMAIN import for Audit logic
from .const import DOMAIN

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
        url_conf = f"http://{self.host}/admin/function_module/home"
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
        data = {"markers": [], "io": [], "webio_commands": {}}
        live_states = live_states or {}
                
        # 1. Determine the configured Web-IO name for this instance
        webio_name = "HomeAssistant" 
        if self.config_entry:
            conf_data = {**self.config_entry.data, **self.config_entry.options}
            webio_name = conf_data.get("webio_name", "HomeAssistant")
        elif DOMAIN in self.hass.data:
            for coord in self.hass.data[DOMAIN].values():
                if coord.api == self:
                    webio_name = coord.config_entry.data.get("webio_name", "HomeAssistant")
                    break

        # 2. Map Webhooks (Web-IO Commands) from Comexio for Audit
        web_devices = conf.get("WebDevices", {})
        fub_10 = conf.get("FubModules", {}).get("10", {})
        target_dev_id = None
        
        for d_id, d_data in web_devices.items():
            if d_data.get("Name") == webio_name:
                target_dev_id = str(d_id)
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
            
            data["markers"].append({
                "id": m_id,
                "name": m.get("Name"),
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

                data["io"].append({
                    "id": str(io_item.get("Id")), 
                    "ext_name": ext_name,
                    "identifier": io_item.get("Identifier"),
                    "name": io_item.get("Description") or io_item.get("Identifier"),
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
    async def get_web_io_base_info(self, webio_name):
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
                    
                    # debug state
                    deletable = f"delete_web_device_base/?id={b_id}" in win_html
                    _LOGGER.debug("Audit Web-IO Base: ID %s, HTML contains delete-link: %s", b_id, deletable)
                    
                    return (b_id, f"delete_web_device_base/?id={b_id}" in win_html)
        return None


    async def get_web_io_device_info(self, device_name):
        """Checks instance existence via HTML tabs."""
        url_home = f"http://{self.host}/admin/web_io/home"
        async with self.session.get(url_home) as resp:
            html = await resp.text()
            pattern = fr'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
            match = re.search(pattern, html, re.IGNORECASE)
            return match.group(1) if match else None


    async def delete_web_io_device(self, device_id):
        """Sends DELETE command for device instance."""
        url = f"http://{self.host}/admin/web_io/delete_device/?id={device_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"http://{self.host}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            return resp.status == 200


    async def delete_web_io_base(self, base_id):
        """Sends DELETE command for device class template."""
        url = f"http://{self.host}/admin/web_io/delete_web_device_base/?id={base_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"http://{self.host}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            return resp.status == 200


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
            # Update format: {"src":"command","id":"879"}
            cmd_ref = json.dumps({"src":"command","id": str(existing_cmd_id)})
            cmd_id_b64 = base64.b64encode(cmd_ref.encode()).decode()
        else:
            # New format: {"src":"command","id":null}
            cmd_id_b64 = "eyJzcmMiOiJjb21tYW5kIiwiaWQiOm51bGx9"
        
        payload = {
            "deviceBaseId": base_id,
            "dlg_web_device_id": device_id,
            "protocol": 0,
            "parameter": cmd_payload["Parameter"],
            "header_modifier": "Content-Type: application/json",
            "data": cmd_payload["Data"],
            "post_get": 1,
            "authentication": 0,
            "id_cmd_io_0": cmd_id_b64,
            "name_cmd_io_0": cmd_payload["Name"],
            "type_cmd_io_0": cmd_payload["TypeId"],
            "min_cmd_io_0": 0,
            "max_cmd_io_0": cmd_payload["Max"],
            "input_cmd_io_0": 1,
            "DefaultActive": 1
        }
        async with self.session.post(url, data=payload) as resp:
            return resp.status == 200

    async def perform_delta_sync(self, base_id, device_id, parsed_data, com_commands):
        """Perform a targeted update of commands with wrong types or missing entries."""
        _LOGGER.info("Starting Delta Sync for Web-IO device %s", device_id)
        
        # Get server_id and webio_name from config_entry for correct JSON generation
        conf_data = {**self.config_entry.data, **self.config_entry.options}
        server_id = conf_data.get("server_id", "unknown")
        webio_name = conf_data.get("webio_name", "HomeAssistant")

        # Pass the correct parameters
        correct_json_str = self.generate_web_io_json(server_id, webio_name, parsed_data)
        correct_data = json.loads(correct_json_str)
        
        for correct_cmd in correct_data.get("commands", []):
            cmd_name = correct_cmd["Name"]
            existing = com_commands.get(cmd_name)
            
            # Fall 1: Der Befehl existiert bereits
            if existing:
                # Wenn der Typ nicht stimmt, machen wir ein Update
                if int(existing.get("typeId", 1)) != correct_cmd["TypeId"]:
                    _LOGGER.info("Fixing type for command %s", cmd_name)
                    await self.save_single_command(
                        base_id, 
                        device_id, 
                        correct_cmd, 
                        existing_cmd_id=existing.get("cmdId")
                    )
            # Fall 2: Der Befehl fehlt komplett
            else:
                _LOGGER.info("Adding missing command %s", cmd_name)
                await self.save_single_command(base_id, device_id, correct_cmd)
                
        return True

    def generate_web_io_json(self, server_id, webio_name, parsed_data):
        """Generates JSON for import with instant activation flag."""
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands = []
        
        # 1. Web-IO für Merker erzeugen
        for m in parsed_data.get("markers", []):
            is_ana = m['type'] == "analog"
            lua = f"function data(a)\r\n  local d = {{ id=\"{m['id']}\", value=a, type=\"marker\" }}\r\n  return json_stringify(d)\r\nend"
            
            commands.append({
                "Name": f"HA M{m['id']} {m['name']}", 
                "TypeId": 2 if is_ana else 1, 
                "Min": 0, 
                "Max": 100 if is_ana else 1, 
                "Parameter": webhook_path, 
                "HeaderModifier": "Content-Type: application/json", 
                "Data": lua, 
                "Protocol": 0, 
                "PostGet": 1, 
                "Authentication": 0, 
                "Input": 1, 
                "DefaultActive": 1, 
                "BaseId": 0, 
                "io": []
            })
            
        # 2. Web-IO für IOs erzeugen
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
                "TypeId": 2 if is_ana else 1,      # 1 == Digital   2 == Analog
                "Min": v_min, 
                "Max": v_max,
                "Parameter": webhook_path, 
                "HeaderModifier": "Content-Type: application/json", 
                "Data": lua, 
                "Protocol": 0, 
                "PostGet": 1, 
                "Authentication": 0, 
                "Input": 1, 
                "DefaultActive": 1, 
                "BaseId": 0, 
                "io": []
            })
            
        return json.dumps({
            "data": "web_io", 
            "format": 1, 
            "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 0, "BaseId": 0}, 
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


    async def create_web_io_device(self, name, base_id, ha_ip):
        """Creates a device instance."""
        url = f"http://{self.host}/admin/web_io/saveDeviceWindow"
        payload = {"name": name, "ip": ha_ip, "web_device_base": base_id, "username": "", "password": "", "web_device_base_sample": "none", "identifier": "", "form_login": "2"}
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
