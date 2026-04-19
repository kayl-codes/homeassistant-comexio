# Version: 0.2.5
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

_LOGGER = logging.getLogger(__name__)

class ComexioAPI:
    """
    Detailed interface to communicate with the Comexio API.
    Handles RSA Login for Admin tasks, Dashboard Refresh for live values,
    and comprehensive Web-IO Management for automated synchronization.
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
            _LOGGER.error("Error fetching dashboard live states: %s", e)
            return {}

    def parse_config(self, conf, live_states=None):
        """Processes config and performs a deep audit of Web-IO commands."""
        data = {"markers": [], "io": [], "webio_commands": 0}
        live_states = live_states or {}
        max_marker_id = 0
        
        # --- Deep Audit Web-IO Commands ---
        # We look into FubModules -> 10 (Web-IO)
        webio_modules = conf.get("FubModules", {}).get("10", {})
        command_count = 0
        
        for device_id, device_data in webio_modules.items():
            # Check for different possible keys where Comexio stores the command list
            # Based on your trace, it might be in 'inoutput' or 'commands'
            cmds = device_data.get("inoutput", {})
            if not cmds:
                cmds = device_data.get("commands", {})
            
            if isinstance(cmds, dict):
                command_count += len(cmds)
            elif isinstance(cmds, list):
                command_count += len(cmds)

        data["webio_commands"] = command_count

        # --- Process Markers ---
        fub_markers = conf.get("FubModules", {}).get("2", {})
        for m in fub_markers.values():
            label = m.get("Name")
            if not label: continue
            m_id = str(m.get("Id"))
            max_marker_id = max(max_marker_id, int(m_id))
            data["markers"].append({
                "id": m_id, "name": label,
                "type": "digital" if m.get("Type") == 1 else "analog",
                "value": self._clean_value(live_states.get(m_id, 0))
            })
        
        # --- Process IOs ---
        fub_extensions = conf.get("FubModules", {}).get("1", {})
        for ext_id, ext_content in fub_extensions.items():
            ext_meta = ext_content.get("extension", {})
            ext_name = ext_meta.get("Name", f"Ext{ext_id}")
            for io_item in ext_content.get("inoutput", {}).values():
                if not io_item or not io_item.get("Active"): continue
                data["io"].append({
                    "id": str(io_item.get("Id")), "ext_name": ext_name,
                    "identifier": io_item.get("Identifier"),
                    "name": io_item.get("Description") or io_item.get("Identifier"),
                    "value": self._clean_value(io_item.get("Value", 0))
                })
                
        _LOGGER.info(
            "Parsing complete: %d Markers, %d IOs, and %d existing Web-IO Commands found", 
            len(data["markers"]), len(data["io"]), data["webio_commands"]
        )
        return data, max_marker_id

    #
    # --- WEB-IO MANAGEMENT SECTION ---
    #
    async def get_web_io_base_info(self, webio_name):
        """
        Robust scan for base_id and deletable status using the add-device page.
        This is the most reliable way to find all classes in Comexio v11.
        """
        base_id = None
        is_deletable = False
        
        _LOGGER.debug("Searching for Web-IO base named: %s using the add-page", webio_name)

        # Step 1: Request the 'Add Device' page which contains a full list of classes
        url_add = f"http://{self.host}/admin/web_io/add"
        async with self.session.get(url_add) as resp:
            html = await resp.text()
            # Regex to find the ID in the option list
            pattern = fr'<option value="(\d+)"[^>]*>{re.escape(webio_name)}</option>'
            match = re.search(pattern, html, re.IGNORECASE)
            
            if match:
                base_id = match.group(1)
                _LOGGER.debug("Found base_id %s for class '%s'", base_id, webio_name)
            else:
                _LOGGER.debug("Class '%s' not found on add-device page", webio_name)

        # CRITICAL: Return None if not found, otherwise the button loop won't stop
        if base_id is None:
            return None

        # Step 2: Check if the class is deletable via the baseDeviceWindow
        url_window = f"http://{self.host}/admin/web_io/baseDeviceWindow/"
        async with self.session.get(url_window) as win_resp:
            win_html = await win_resp.text()
            # If the delete link for this ID exists, it's deletable
            if f"delete_web_device_base/?id={base_id}" in win_html:
                is_deletable = True
                _LOGGER.debug("Class %s is currently deletable", base_id)
            else:
                _LOGGER.debug("Class %s is in use and NOT deletable", base_id)
                
        return (base_id, is_deletable)

    async def get_web_io_device_info(self, device_name):
        """
        Scans the Web-IO home page for device tabs in the HTML.
        This is the most reliable way to verify if a device instance exists.
        """
        _LOGGER.debug("Searching for device '%s' in Web-IO HTML tabs", device_name)
        url_home = f"http://{self.host}/admin/web_io/home"
        
        async with self.session.get(url_home) as resp:
            html = await resp.text()
            
            # We look for the device name inside the <a> tags of the tab navigation
            # Example: <a id="tab-link-18" href="#tabs-18">HomeAssistant_v1</a>
            pattern = fr'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
            match = re.search(pattern, html, re.IGNORECASE)
            
            if match:
                device_id = match.group(1)
                _LOGGER.debug("Found device '%s' with ID %s in HTML tabs", device_name, device_id)
                return device_id
                
            # Fallback: Check JS variable $WebDevices if HTML tab is not yet rendered
            match_js = re.search(r'var\s+\$WebDevices\s*=\s*({.*?});', html)
            if match_js:
                try:
                    devices = json.loads(match_js.group(1))
                    for d_id, d_data in devices.items():
                        if d_data.get("Name", "").lower() == device_name.lower():
                            _LOGGER.debug("Found device '%s' in JS fallback (ID: %s)", device_name, d_id)
                            return d_id
                except: pass

        _LOGGER.debug("Device '%s' not found in Comexio Web-IO list", device_name)
        return None

    async def delete_web_io_device(self, device_id):
        """Sends the command to delete a specific device instance."""
        _LOGGER.info("Executing deletion of device instance ID: %s", device_id)
        url = f"http://{self.host}/admin/web_io/delete_device/?id={device_id}"
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                _LOGGER.debug("Deletion request for device %s sent successfully", device_id)
                return True
            return False

    async def delete_web_io_base(self, base_id):
        """Sends the command to delete a specific Web-IO device class template."""
        _LOGGER.info("Executing deletion of device class ID: %s", base_id)
        url = f"http://{self.host}/admin/web_io/delete_web_device_base/?id={base_id}"
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home"
        }
        
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 200:
                _LOGGER.debug("Deletion request for class %s sent successfully", base_id)
                return True
            return False

    def generate_web_io_json(self, server_id, webio_name, parsed_data):
        """Generates JSON and ensures ALL commands are marked as 'Active'."""
        _LOGGER.debug("Generating Web-IO JSON. Forcing 'Active': 1 for all commands.")
        webhook_path = f"/api/webhook/comexio_{server_id}"
        commands = []
        
        # Helper to create a command entry
        def create_cmd(name, type_id, max_v, lua_data):
            return {
                "Name": name,
                "TypeId": type_id,
                "Min": 0, "Max": max_v,
                "Parameter": webhook_path,
                "HeaderModifier": "Content-Type: application/json",
                "Data": lua_data,
                "Protocol": 0, "PostGet": 1, "Authentication": 0,
                "Input": 1, 
                "Active": 1, # MANDATORY FOR WEBHOOKS
                "BaseId": 0, "io": []
            }

        for m in parsed_data.get("markers", []):
            is_ana = (m['type'] == "analog")
            lua = f"function data(a)\r\n  local d = {{ id=\"{m['id']}\", value=a, type=\"marker\" }}\r\n  return json_stringify(d)\r\nend"
            commands.append(create_cmd(f"HA M{m['id']} {m['name']}", 2 if is_ana else 1, 100 if is_ana else 1, lua))
            
        for io_item in parsed_data.get("io", []):
            is_ana = any(x in io_item['identifier'].upper() for x in ["AI", "AO", "TL", "OL", "QI"])
            lua = f"function data(a)\r\n  local d = {{ ext=\"{io_item['ext_name']}\", io=\"{io_item['identifier']}\", value=a, type=\"io\" }}\r\n  return json_stringify(d)\r\nend"
            commands.append(create_cmd(f"HA IO {io_item['ext_name']} {io_item['identifier']}", 2 if is_ana else 1, 100 if is_ana else 1, lua))

        return json.dumps({
            "data": "web_io", "format": 1,
            "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 0, "BaseId": 0},
            "commands": commands
        })

    async def upload_web_io(self, server_id, webio_name, web_io_json):
        """
        Uploads the generated JSON as a device class to Comexio.
        Uses a binary stream to ensure high-volume command transfers (e.g. 386 entities).
        """
        _LOGGER.info("Uploading Web-IO device class '%s' to Comexio", webio_name)
        url = f"http://{self.host}/admin/web_io/upload_device_settings"
        
        # Prepare binary stream for multipart upload
        file_data = io.BytesIO(web_io_json.encode('utf-8'))
        
        form = aiohttp.FormData()
        # Field 'file' must contain the JSON content as a virtual file
        form.add_field('file', 
                        file_data, 
                        filename=f"ha_{server_id}.json", 
                        content_type='application/json')
        
        # The 'set_name' field is used by Comexio to override the identifier
        form.add_field('set_name', webio_name)
        
        headers = {
            "X-Requested-With": "XMLHttpRequest", 
            "Referer": f"http://{self.host}/admin/web_io/home",
            "User-Agent": "Mozilla/5.0 HA-Integration"
        }
        
        try:
            async with self.session.post(url, data=form, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json(content_type=None)
                    if result.get("ok"):
                        _LOGGER.info("Web-IO upload successful. Assigned base_id: %s", result.get("base_id"))
                        return True, result.get("base_id")
                    else:
                        _LOGGER.error("Comexio rejected the upload: %s", result)
                        return False, f"Rejection: {result}"
                
                error_text = await resp.text()
                _LOGGER.error("Upload failed with HTTP status: %s. Response: %s", resp.status, error_text[:200])
                return False, f"HTTP Error {resp.status}"
                
        except Exception as e:
            _LOGGER.error("Exception during Web-IO upload: %s", e)
            return False, str(e)


    async def create_web_io_device(self, name, base_id, ha_ip):
        """Creates a device instance in Comexio linked to the specific device class ID."""
        _LOGGER.info("Creating device instance '%s' using base_id %s", name, base_id)
        url = f"http://{self.host}/admin/web_io/saveDeviceWindow"
        
        payload = {
            "name": name, 
            "ip": ha_ip, 
            "web_device_base": base_id, 
            "username": "", "password": "", 
            "web_device_base_sample": "none", 
            "identifier": "", "form_login": "2" # 2 = No Login
        }
        
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.host}/admin/web_io/home",
            "User-Agent": "Mozilla/5.0 HA-Integration"
        }
        
        try:
            async with self.session.post(url, data=payload, headers=headers) as resp:
                if resp.status == 200:
                    _LOGGER.info("Device instance created successfully")
                    return True
                _LOGGER.error("Failed to create device instance. Status: %s", resp.status)
                return False
        except Exception as e:
            _LOGGER.error("Error during device creation: %s", e)
            return False

    async def set_value(self, target_type, target_id, value, ext=None, identifier=None):
        """Fast write command via basic API."""
        auth = aiohttp.BasicAuth(self.api_user, self.api_pass) if self.api_user else None
        url = f"http://{self.host}/api/"
        params = {"action": "set", "value": value}
        if target_type == "marker": params["marker"] = f"M{target_id}"
        else: params["ext"], params["io"] = ext, identifier
        
        try:
            async with self.session.get(url, params=params, auth=auth) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self):
        """Correctly close the aiohttp session."""
        await self.session.close()
