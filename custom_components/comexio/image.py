# Version: 0.8.2
"""Image platform exposing the last generated Function Plan preview SVG.

An image entity is a first-class picture source for the frontend: the built-in picture-entity
card accepts it natively and swaps the image URL in place on every state change — no DOM
rebuild, no flicker, no polling. The bytes are served straight from the coordinator's SVG
(restored from .storage after a restart, see coordinator.async_load_plan_preview) and only
through HA's authenticated image proxy: the preview names markers, IOs, wiring and live values,
so it is never written to config/www, which HA serves without authentication under /local/.
"""

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
from .entity import ComexioFunctionPlanEntityMixin


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio plan preview image entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanPreviewImage(hass, coordinator, coordinator.server_id)])


class ComexioPlanPreviewImage(ComexioFunctionPlanEntityMixin, CoordinatorEntity, ImageEntity):
    """Serves the last rendered Function Plan preview SVG as an image entity.

    Fed by coordinator.async_generate_plan_preview (plan card and 'Plan'/'Backup' selection, see
    plan_preview; function_plan_visualize with format=svg, or the debounced live-value refresh) —
    image_last_updated follows last_plan_preview['generated_at'], so the frontend re-fetches
    exactly when a new render exists.
    """

    _attr_has_entity_name = True
    _hub_era_name = "Plan Preview"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_content_type = "image/svg+xml"
    _attr_translation_key = "plan_preview"

    def __init__(self, hass: HomeAssistant, coordinator: ComexioCoordinator, server_id: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"comexio_{server_id}_plan_preview_image"
        self._sync_timestamp()

    def _sync_timestamp(self) -> None:
        preview = self.coordinator.last_plan_preview
        if preview and (generated_at := preview.get("generated_at")):
            self._attr_image_last_updated = dt_util.parse_datetime(generated_at)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_timestamp()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which plan and source the served SVG shows.

        Read by comexio-plan-card.js: in the orphaned-plans view it compares the image on screen
        (a trigger-only card without its own image: this entity's newest render) with the backup
        selector's choice before a delete/keep action. captured_at names an orphan render's
        snapshot (its slot shifts after a delete); None for every other source. Only with an SVG on hand
        (rendered in this run or restored with last_plan_preview from the same store entry): a
        last_plan_preview without one names no image, so the identity stays None — the card then
        warns — until the next render.
        """
        has_svg = self.coordinator.last_plan_preview_svg is not None
        preview = (self.coordinator.last_plan_preview if has_svg else None) or {}
        return {
            "fub_id": preview.get("fub_id"),
            "plan_name": preview.get("plan_name"),
            "source": preview.get("source"),
            "captured_at": preview.get("captured_at"),
        }

    async def async_image(self) -> bytes | None:
        """Return the preview SVG bytes; None before the first preview."""
        svg = self.coordinator.last_plan_preview_svg
        return svg.encode("utf-8") if svg is not None else None
