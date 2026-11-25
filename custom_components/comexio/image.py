# Version: 0.8.2
"""Image platform: the Function Plan preview SVG, and the last bell and motion picture of every Doorstation.

The Doorstation pictures are in the second half of this file (feed and reads: doorstation_pictures.py).

The Function Plan preview SVG:

An image entity is a first-class picture source for the frontend: the built-in picture-entity
card accepts it natively and swaps the image URL in place on every state change — no DOM
rebuild, no flicker, no polling. The bytes are served straight from the coordinator's SVG
(restored from .storage after a restart, see coordinator.async_load_plan_preview) and only
through HA's authenticated image proxy: the preview names markers, IOs, wiring and live values,
so it is never written to config/www, which HA serves without authentication under /local/.
"""

from typing import Any

from aiocomexio import DoorstationPictureKind
from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, doorstation_picture_unique_id, doorstation_pictures_signal
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .doorstation_pictures import PICTURE_KINDS, DoorstationPictureFeed, taken_at_utc
from .entity import ComexioFunctionPlanEntityMixin, ComexioStableEntityIdMixin


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Comexio plan preview image entity and the picture entities of every Doorstation, now and later."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ComexioPlanPreviewImage(hass, coordinator, coordinator.server_id)])
    sync = _DoorstationPictureSync(hass, entry, coordinator, async_add_entities)
    sync()
    entry.async_on_unload(coordinator.async_add_listener(sync))


class ComexioPlanPreviewImage(ComexioFunctionPlanEntityMixin, CoordinatorEntity[ComexioCoordinator], ImageEntity):
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


class _DoorstationPictureSync:
    """Coordinator listener: a feed and two picture entities for every Doorstation that was not seen before.

    Like the cameras (camera.py) the entities join the extension's device, so they are added only once its name is
    known (the camera's own listener warns about a Doorstation that has none). Nothing is removed here: an entity turns
    unavailable with its Doorstation, and the orphan cleanup keeps its registry entry. Every later update asks each
    feed whether its list is due for a new read.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: ComexioCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._feeds: dict[int, DoorstationPictureFeed] = {}

    @callback
    def __call__(self) -> None:
        for feed in self._feeds.values():
            feed.poll()
        for ext_id in sorted(self._coordinator.doorstations.known):
            if ext_id not in self._feeds and self._named(ext_id):
                self._add(ext_id)

    def _named(self, ext_id: int) -> bool:
        """Whether the extension name of the Doorstation is known (its IOs, else the page)."""
        doorstation = self._coordinator.doorstations.known[ext_id]
        return doorstation_ext_name(doorstation, (self._coordinator.data or {}).get("io", [])) is not None

    def _add(self, ext_id: int) -> None:
        feed = DoorstationPictureFeed(self._hass, self._entry, self._coordinator, ext_id)
        self._feeds[ext_id] = feed
        self._async_add_entities(
            ComexioDoorstationPicture(self._hass, self._coordinator, feed, kind) for kind in PICTURE_KINDS
        )
        feed.start()


class ComexioDoorstationPicture(ComexioStableEntityIdMixin, CoordinatorEntity[ComexioCoordinator], ImageEntity):
    """The newest picture a Doorstation stored for a bell press or for motion, on the device of its extension.

    The state is the moment of the picture (its file name's, read as the installation's local time), so an automation
    triggers on a new picture and the frontend fetches it then. The JPEG is read from the Doorstation when it is
    asked for and kept until a newer one is listed. No picture yet is an unknown state and no image.
    """

    _attr_has_entity_name = True
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ComexioCoordinator,
        feed: DoorstationPictureFeed,
        kind: DoorstationPictureKind,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._feed = feed
        self._kind = kind
        self._attr_unique_id = doorstation_picture_unique_id(coordinator.server_id, feed.ext_id, kind.value)
        self._attr_translation_key = f"doorstation_{kind.value}_picture"

    @property
    def device_info(self) -> DeviceInfo:
        """The extension's device, the one its IOs sit on."""
        return self._feed.device_info

    @property
    def available(self) -> bool:
        return super().available and self._feed.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                doorstation_pictures_signal(self.coordinator.server_id, self._feed.ext_id),
                self._handle_pictures_changed,
            )
        )
        self._sync_timestamp()

    def _sync_timestamp(self) -> None:
        picture = self._feed.newest(self._kind)
        self._attr_image_last_updated = taken_at_utc(picture) if picture is not None else None

    @callback
    def _handle_pictures_changed(self) -> None:
        self._sync_timestamp()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """The JPEG of the newest picture of this kind; None if there is none, or it cannot be read."""
        return await self._feed.async_picture(self._kind)
