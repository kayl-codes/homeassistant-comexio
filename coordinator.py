# Version: 0.2.0
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.const import CONF_HOST
import logging

from .api import ComexioAPI
from .const import DOMAIN, CONF_SERVER_ID

_LOGGER = logging.getLogger(__name__)

class ComexioCoordinator(DataUpdateCoordinator):
    """Coordinator to manage data fetching and state updates."""

    def __init__(self, hass, api: ComexioAPI):
        """Initialize the coordinator."""
        # Initial interval is set in __init__.py via entry data
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=15),
        )

        self.api = api
        self.server_id = None
        self.config_entry = None
        self.marker_states = {}
        self.io_states = {}

    async def _async_update_data(self):
        """Fetch the initial configuration and LIVE states from Comexio."""
        _LOGGER.debug("[%s] Periodic sync / Initial load started", self.server_id)

        try:
            raw_config = await self.api.get_raw_config()
            
            # Get max ID for refresh call
            marker_data = raw_config.get("FubModules", {}).get("2", {})
            ids = [int(m.get("Id", 0)) for m in marker_data.values()]
            max_id = max(ids) if ids else 0
            
            # Get live values
            live_states = await self.api.get_live_states(max_id)
            
            # Parse everything
            parsed_data, _ = self.api.parse_config(raw_config, live_states)

            # Filter data based on Config Entry options
            import_markers = self.config_entry.data.get("import_markers", True)
            import_ios = self.config_entry.data.get("import_ios", True)

            final_data = {"markers": [], "io": []}

            if import_markers:
                final_data["markers"] = parsed_data["markers"]
                for m in final_data["markers"]:
                    self.marker_states[m["id"]] = m["value"]

            if import_ios:
                final_data["io"] = parsed_data["io"]
                for io in final_data["io"]:
                    self.io_states[io["id"]] = io["value"]

            return final_data

        except Exception as e:
            _LOGGER.error("Failed to fetch data from Comexio: %s", e)
            raise

    def update_marker(self, marker_id, value):
        """Update marker via Webhook."""
        self.marker_states[str(marker_id)] = value
        self.async_set_updated_data(self.data)

    def update_io_by_name(self, ext_name, identifier, value):
        """Update IO via Webhook."""
        for io in self.data.get("io", []):
            if io["ext_name"].lower() == ext_name.lower() and io["identifier"].lower() == identifier.lower():
                self.io_states[io["id"]] = value
                break
        self.async_set_updated_data(self.data)
