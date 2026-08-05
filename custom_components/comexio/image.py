# Version: 0.8.2
"""Image platform exposing the last generated Function Plan preview SVG.

Unlike the Plan Preview sensor (entity_picture + /local/ file + cache-buster), an image
entity is a first-class picture source for the frontend: the built-in picture-entity card
accepts it natively and swaps the image URL in place on every state change — no DOM
rebuild, no flicker, no polling. The bytes are served straight from the coordinator's
in-memory SVG cache; the config/www file is only used as a fallback after a restart.
"""

import pathlib
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ComexioCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio plan preview image entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanPreviewImage(hass, coordinator, coordinator.server_id)])


class ComexioPlanPreviewImage(CoordinatorEntity, ImageEntity):
    """Serves the last rendered Function Plan preview SVG as an image entity.

    Fed by coordinator.async_generate_plan_preview (Preview button, function_plan_visualize
    with format=svg, or the debounced live-value refresh) — image_last_updated follows
    last_plan_preview['generated_at'], so the frontend re-fetches exactly when a new
    render exists.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_content_type = "image/svg+xml"
    _attr_translation_key = "plan_preview"

    def __init__(self, hass: HomeAssistant, coordinator: ComexioCoordinator, server_id: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"comexio_{server_id}_plan_preview_image"
        self._preview_path = pathlib.Path(hass.config.path("www", f"comexio_{server_id}_plan_preview.svg"))
        self._sync_timestamp()

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self.coordinator.server_id)},
            "name": self.coordinator.server_id,
            "manufacturer": "Comexio",
            "model": "IO-Server",
        }

    def _sync_timestamp(self) -> None:
        preview = self.coordinator.last_plan_preview
        if preview and (generated_at := preview.get("generated_at")):
            self._attr_image_last_updated = dt_util.parse_datetime(generated_at)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_timestamp()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Fall back to the persisted SVG file's mtime so the preview survives HA restarts."""
        await super().async_added_to_hass()
        if self._attr_image_last_updated is None:
            self._attr_image_last_updated = await self.hass.async_add_executor_job(self._preview_file_mtime)

    def _preview_file_mtime(self) -> Any:
        try:
            return dt_util.utc_from_timestamp(self._preview_path.stat().st_mtime)
        except OSError:
            return None

    async def async_image(self) -> bytes | None:
        """Return the preview SVG bytes (in-memory cache, file fallback after restart)."""
        if (svg := self.coordinator.last_plan_preview_svg) is not None:
            return svg.encode("utf-8")
        return await self.hass.async_add_executor_job(self._read_preview_file)

    def _read_preview_file(self) -> bytes | None:
        try:
            return self._preview_path.read_bytes()
        except OSError:
            return None
