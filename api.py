# Version: 0.1.17
import re
import json
import aiohttp
import logging
import random
import base64
import time
import io
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from multidict import MultiDict

_LOGGER = logging.getLogger(__name__)

class ComexioAPI:
    """
    Detailed interface to communicate with the Comexio API.
    Handles RSA Login for Admin tasks and Basic Auth for fast control.
    """

    def __init__(self, hass, host, username, password, api_user=None, api_pass=None):
        """Initialize the API class with all required credentials."""
        self.hass = hass
        self.host = host
        self.username = username # UI Login Username
        self.password = password # UI Login Password
        self.api_user = api_user # API Basic Auth Username
        self.api_pass = api_pass # API Basic Auth Password
        
        # CookieJar is essential for maintaining the session across multiple requests.
        # unsafe=True is required for Comexio v11 when using IP addresses.
        jar = aiohttp.CookieJar(unsafe=True)
        self.session = aiohttp.ClientSession(cookie_jar=jar)

    def _clean_value(self, val):
        """
        Cleans and normalizes values from Comexio.
        Replaces German decimal separators (comma) with dots and converts to numeric types.
        """
        if val is None:
            _LOGGER.debug("Cleaning value: Received None, returning 0")
            return 0
            
        # Convert everything to string first to handle mixed types
        str_val = str(val).replace(",", ".").strip()
        
        try:
            if "." in str_val:
                return float(str_val)
            return int(str_val)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not convert value '%s' to number, returning 0", val)
            return 0

    def _encrypt_block(self, data_str, mod, exp):
        """
        Performs RSA encryption using PKCS1v15 padding.
        This matches the logic used in the Comexio v11 web interface.
        """
        try:
            pub_key = rsa.RSAPublicNumbers(exp, mod).public_key()
            # Encryption requires bytes, Comexio uses iso-8859-1 for the salt/password string
            encrypted = pub_key.encrypt(
                data_str.encode('iso-8859-1'), 
                padding.PKCS1v15()
            )
            # Return as 512 character hex string padded with zeros
            return encrypted.hex().zfill(512)
        except Exception as e:
            _LOGGER.error("RSA Block encryption failed: %s", e)
            raise

    async def login(self):
        """
        Performs the complex multi-step RSA login required for admin access.
        Necessary for reading full configuration and uploading Web-IO classes.
        """
        _LOGGER.debug("Starting v11 RSA login procedure for host: %s", self.host)
        url = f"http://{self.host}/board/home/login/"
        
        try:
            # Step 1: Set the required comexio-client-time cookie
            client_time = str(int(time.time()))
            self.session.cookie_jar.update_cookies({"comexio-client-time": client_time})

            # Step 2: Fetch RSA keys (modulus, exponent) and the salt from the server
            _LOGGER.debug("Fetching login keys and salt from Comexio")
            async with self.session.post(url, data={'login_keys': 'true'}) as resp:
                keys = await resp.json(content_type=None)
                salt_b64 = keys['salt']
                mod = int(keys['modulus'], 16)
                exp = int(keys['exponent'], 16)
            
            salt_str = base64.b64decode(salt_b64).decode('iso-8859-1')
            
            # Step 3: Generate a random nonce and prepare the two-block password
            chars = "0123456789ABCDEF"
            nonce = ''.join(random.choice(chars) for _ in range(20))
            
            # Block 1 contains salt + nonce + password
            enc1 = self._encrypt_block(salt_str + nonce + self.password, mod, exp)
            # Block 2 contains salt + nonce + empty string
            enc2 = self._encrypt_block(salt_str + nonce + "", mod, exp)
            
            # Comexio expects the blocks separated by a space
            final_pw = f"{enc1} {enc2}"
            
            # Step 4: Submit the login form
            payload = MultiDict([
                ('target', '/board/home/login'), 
                ('username', self.username), 
                ('password', final_pw), 
                ('loginsubmit', 'Anmelden'), 
                ('encryption', 'rsa')
            ])
            
            headers = {
                "Referer": url, 
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HA-Integration"
            }

            _LOGGER.debug("Submitting login form for user: %s", self.username)
            async with self.session.post(url, data=payload, headers=headers) as resp:
                # Step 5: Verification - Check if we can reach the admin page
                async with self.session.get(f"http://{self.host}/admin/", headers=headers) as v_resp:
                    html = await v_resp.text()
                    
                    # If "Anmeldung" is NOT in the response, we are successfully logged in
                    if "Anmeldung" not in html and html != "":
                        _LOGGER.info("Successfully logged into Comexio Admin interface")
                        return True
            
            _LOGGER.error("Comexio login failed. Verification page still shows login form.")
            return False
            
        except Exception as e:
            _LOGGER.error("Critical error during Comexio login: %s", e)
            return False

    async def get_raw_config(self):
        """Downloads the full JavaScript configuration objects from the logic page."""
        _LOGGER.debug("Downloading raw configuration from Comexio admin page")
        url = f"http://{self.host}/admin/function_function_module/home"
        
        async with self.session.get(url) as resp:
            html = await resp.text()

        # Extract variables like $FubModules or $WebDevices using Regex
        matches = re.finditer(r'var\s+\$([\w\d_]+)\s*=\s*(\{.*?\});', html, re.DOTALL)
        result = {}
        for m in matches:
            var_name = m.group(1)
            try:
                result[var_name] = json.loads(m.group(2))
            except json.JSONDecodeError:
                _LOGGER.warning("Could not parse JSON for JS variable: %s", var_name)
                continue
        return result

    async def get_live_states(self, marker_count):
        """
        Fetches the actual current values of all markers.
        Config data only contains static info; live values require the dashboard/refresh endpoint.
        """
        _LOGGER.debug("Requesting live states for %s markers from dashboard refresh", marker_count)
        url = f"http://{self.host}/board/dashboard/refresh/"
        
        # Build the JSON payload as expected by Comexio v11
        markers_dict = {}
        for i in range(1, marker_count + 1):
            markers_dict[str(i)] = {"action": "get", "MarkerName": f"M{i}"}
        
        markers_dict["messages"] = {"action": "messages"}
        
        # Prepare multipart/form-data with a single field named 'json'
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
                _LOGGER.debug("Successfully received live states from dashboard")
                return data.get("result", {})
        except Exception as e:
            _LOGGER.error("Failed to fetch dashboard live states: %s", e)
            return {}

    def parse_config(self, conf, live_states=None):
        """
        Processes the raw JS objects into a structured format for Home Assistant.
        Merges configuration with live status values.
        """
        data = {"markers": [], "io": []}
        live_states = live_states or {}
        max_marker_id = 0
        
        _LOGGER.debug("Starting to parse configuration modules")
        
        # --- Process Markers (Type 2) ---
        fub_markers = conf.get("FubModules", {}).get("2", {})
        for m in fub_markers.values():
            label = m.get("Name")
            # Only import markers that have a description/name assigned
            if not label:
                continue
            
            m_id = str(m.get("Id"))
            # Track highest ID for later live-state calls
            max_marker_id = max(max_marker_id, int(m_id))
            
            # Map Comexio types to our internal strings
            m_type_raw = m.get("Type")
            m_type = "analog"
            if m_type_raw == 1:
                m_type = "digital"
            elif m_type_raw == 3:
                m_type = "interval"
            
            data["markers"].append({
                "id": m_id,
                "name": label,
                "type": m_type,
                "value": self._clean_value(live_states.get(m_id, 0))
            })
        
        # --- Process Extensions and IOs (Type 1) ---
        fub_extensions = conf.get("FubModules", {}).get("1", {})
        for ext_id, ext_content in fub_extensions.items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")
            
            ios = ext_content.get("inoutput", {})
            for io_item in ios.values():
                # Only import IOs that are marked as active
                if not io_item or not io_item.get("Active"):
                    continue
                
                data["io"].append({
                    "id": str(io_item.get("Id")),
                    "ext_name": ext_name,
                    "identifier": io_item.get("Identifier"),
                    "name": io_item.get("Description") or io_item.get("Identifier"),
                    "value": self._clean_value(io_item.get("Value", 0))
                })
                
        _LOGGER.info("Parsing complete: %d Markers and %d IOs found", len(data["markers"]), len(data["io"]))
        return data, max_marker_id

    # --- WEB-IO MANAGEMENT ---

    async def get_web_io_base_info(self, webio_name):
        """
        Scans for an existing device class and checks if it can be deleted.
        Returns a tuple: (base_id_string, is_deletable_boolean)
        """
        base_id = None
        is_deletable = False
        
        _LOGGER.debug("Checking Comexio for existing Web-IO class: %s", webio_name)

        # 1. Look up the ID in the $WebDeviceBase variable on the home page
        url_home = f"http://{self.host}/admin/web_io/home"
        async with self.session.get(url_home) as resp:
            html = await resp.text()
            match = re.search(r'var\s+\$WebDeviceBase\s*=\s*({.*?});', html)
            if match:
                bases = json.loads(match.group(1))
                for b_id, b_data in bases.items():
                    if b_data.get("Identifier") == webio_name:
                        base_id = b_id
                        _LOGGER.debug("Found existing base_id %s for class %s", base_id, webio_name)
                        break
        
        if base_id is None:
            _LOGGER.debug("No existing Web-IO class found with name: %s", webio_name)
            return None, False

        # 2. Check if the class is deletable (only unassigned classes show up in the baseDeviceWindow)
        url_window = f"http://{self.host}/admin/web_io/baseDeviceWindow/"
        async with self.session.get(url_window) as resp:
            html = await resp.text()
            # If the specific delete link for our ID is found in the HTML, it's safe to delete
            if f"delete_web_device_base/?id={base_id}" in html:
                is_deletable = True
                _LOGGER.debug("Web-IO class %s is NOT in use and CAN be deleted", base_id)
            else:
                _LOGGER.debug("Web-IO class %s IS in use and CANNOT be deleted", base_id)
                
        return base_id, is_deletable

    async def delete_web_io_base(self, base_id):
        """Sends the command to delete a specific Web-IO device class template."""
        _LOGGER.info("Attempting to delete Web-IO class ID: %s", base_id)
        url = f"http://{self.host}/admin/web_io/delete_web_device_base/?id={base_id}"
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                _LOGGER.debug("Delete request for class %s sent successfully", base_id)
                return True
            _LOGGER.error("Failed to delete Web-IO class. Server returned status: %s", resp.status)
            return False

    def generate_web_io_json(self, server_id, webio_name, parsed_data):
        """
        Creates a JSON string compatible with Comexio Web-IO Import.
        Sets correct TypeID and Min/Max based on the entity type.
        """
        _LOGGER.debug("Generating Web-IO JSON for server: %s", server_id)
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands = []
        
        # --- Create Commands for Markers ---
        for m in parsed_data.get("markers", []):
            is_analog = (m['type'] == "analog")
            
            # Comexio TypeId: 1 = Digital, 2 = Analog
            type_id = 2 if is_analog else 1
            max_val = 100 if is_analog else 1
            
            commands.append({
                "Name": f"HA M{m['id']} {m['name']}",
                "TypeId": type_id,
                "Min": 0,
                "Max": max_val,
                "Parameter": webhook_path,
                "HeaderModifier": "Content-Type: application/json",
                "Data": f"function data(a)\r\n  local d = {{ id=\"{m['id']}\", value=a, type=\"marker\" }}\r\n  return json_stringify(d)\r\nend",
                "Protocol": 0, "PostGet": 1, "Authentication": 0, "Input": 1, "Active": 1, "BaseId": 0, "io": []
            })
            
        # --- Create Commands for IOs ---
        for io_item in parsed_data.get("io", []):
            ident_upper = io_item['identifier'].upper()
            # Detect analog IOs by their identifier prefix
            is_analog_io = any(prefix in ident_upper for prefix in ["AI", "AO", "TL", "OL", "QI"])
            
            type_id = 2 if is_analog_io else 1
            max_val = 100 if is_analog_io else 1
            
            commands.append({
                "Name": f"HA IO {io_item['ext_name']} {io_item['identifier']}",
                "TypeId": type_id,
                "Min": 0,
                "Max": max_val,
                "Parameter": webhook_path,
                "HeaderModifier": "Content-Type: application/json",
                "Data": f"function data(a)\r\n  local d = {{ ext=\"{io_item['ext_name']}\", io=\"{io_item['identifier']}\", value=a, type=\"io\" }}\r\n  return json_stringify(d)\r\nend",
                "Protocol": 0, "PostGet": 1, "Authentication": 0, "Input": 1, "Active": 1, "BaseId": 0, "io": []
            })

        # Wrap everything in the Comexio Device Class structure
        web_io_class = {
            "data": "web_io", 
            "format": 1,
            "base": {
                "Identifier": webio_name, 
                "UseCookies": 0, 
                "Login": 0, 
                "BaseId": 0
            },
            "commands": commands
        }
        
        _LOGGER.debug("JSON generation complete. Command count: %d", len(commands))
        return json.dumps(web_io_class)

    async def upload_web_io(self, server_id, webio_name, web_io_json):
        """Uploads the generated JSON as a device class to the Comexio server."""
        _LOGGER.info("Uploading fresh Web-IO device class '%s' to Comexio", webio_name)
        url = f"http://{self.host}/admin/web_io/upload_device_settings"
        
        # Prepare binary stream for multipart upload
        file_data = io.BytesIO(web_io_json.encode('utf-8'))
        
        form = aiohttp.FormData()
        form.add_field('file', 
                        file_data, 
                        filename=f"ha_{server_id}.json", 
                        content_type='application/json')
        
        # The 'set_name' field is used by Comexio to override the identifier if needed
        form.add_field('set_name', webio_name)
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home",
            "User-Agent": "Mozilla/5.0 HA-Integration"
        }
        
        try:
            async with self.session.post(url, data=form, headers=headers) as resp:
                if resp.status == 200:
                    # Comexio returns JSON like {"ok": true, "base_id": 20}
                    result = await resp.json(content_type=None)
                    if result.get("ok"):
                        _LOGGER.info("Web-IO upload successful. Assigned base_id: %s", result.get("base_id"))
                        return True, result.get("base_id")
                    else:
                        _LOGGER.error("Comexio rejected the upload: %s", result)
                        return False, f"Rejection: {result}"
                
                _LOGGER.error("Upload failed with HTTP status: %s", resp.status)
                return False, f"HTTP Error {resp.status}"
                
        except Exception as e:
            _LOGGER.error("Exception during Web-IO upload: %s", e)
            return False, str(e)

    async def get_web_io_device_info(self, device_name):
        """
        Scans for an existing device instance and returns its ID.
        Looks into the $WebDevices JS variable on the home page.
        """
        _LOGGER.debug("Searching for existing Web-IO device named: %s", device_name)
        url_home = f"http://{self.host}/admin/web_io/home"
        
        async with self.session.get(url_home) as resp:
            html = await resp.text()
            match = re.search(r'var\s+\$WebDevices\s*=\s*({.*?});', html)
            if match:
                devices = json.loads(match.group(1))
                for d_id, d_data in devices.items():
                    if d_data.get("Name") == device_name:
                        _LOGGER.debug("Found existing device_id: %s", d_id)
                        return d_id
        return None

    async def delete_web_io_device(self, device_id):
        """Deletes a specific device instance."""
        _LOGGER.info("Deleting Web-IO device instance with ID: %s", device_id)
        url = f"http://{self.host}/admin/web_io/delete_device/?id={device_id}"
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"http://{self.host}/admin/web_io/home"}
        async with self.session.get(url, headers=headers) as resp:
            return resp.status == 200

    async def create_web_io_device(self, device_name, base_id, ha_ip):
        """
        Creates a new device instance based on a device class (base_id).
        ha_ip should be 'ha-ip-or-host:8123'
        """
        _LOGGER.info("Creating new device instance '%s' using base_id %s", device_name, base_id)
        url = f"http://{self.host}/admin/web_io/saveDeviceWindow"
        
        # We simulate the exact payload from your trace
        payload = {
            "name": device_name,
            "ip": ha_ip,
            "web_device_base": base_id,
            "username": "",
            "password": "",
            "web_device_base_sample": "none",
            "identifier": "",
            "form_login": "2" # 2 = No Login
        }
        
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.host}/admin/web_io/home",
            "User-Agent": "Mozilla/5.0"
        }
        
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                return resp.status == 200
        except Exception as e:
            _LOGGER.error("Failed to create device instance: %s", e)
            return False

    async def set_value(self, target_type, target_id, value, ext=None, identifier=None):
        """
        Performs a fast write command using the Comexio API (Basic Auth).
        This is used for switches and sliders in the Home Assistant UI.
        """
        # Build basic auth if credentials are provided
        auth = None
        if self.api_user and self.api_pass:
            auth = aiohttp.BasicAuth(self.api_user, self.api_pass)
        
        url = f"http://{self.host}/api/"
        
        # Prepare query parameters
        params = {"action": "set", "value": value}
        
        if target_type == "marker":
            # For markers, Comexio expects 'marker=M123'
            params["marker"] = f"M{target_id}"
            _LOGGER.debug("Sending API command: Set Marker M%s to %s", target_id, value)
        else:
            # For physical IOs, Comexio expects 'ext=LD1&io=Q1'
            params["ext"] = ext
            params["io"] = identifier
            _LOGGER.debug("Sending API command: Set IO %s/%s to %s", ext, identifier, value)
        
        try:
            async with self.session.get(url, params=params, auth=auth) as resp:
                if resp.status == 200:
                    return True
                _LOGGER.error("API command failed with status: %s", resp.status)
                return False
        except Exception as e:
            _LOGGER.error("Connection error during API command: %s", e)
            return False

    async def close(self):
        """Closes the aiohttp session and cleans up resources."""
        _LOGGER.debug("Closing Comexio API session")
        await self.session.close()
