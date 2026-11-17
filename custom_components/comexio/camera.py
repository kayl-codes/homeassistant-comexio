"""Camera platform: one still-image camera per Doorstation of the IO-Server.

The image is the first frame of the camera's MJPEG stream on the IO-Server (aiocomexio's
``get_doorstation_snapshot``), read with the admin session: the stream needs the login, so Home Assistant cannot
hand its URL to a player. There is no live stream here; the frontend asks for a new still image every few seconds.
The Doorstation's own address serves the stream too (reportedly without a login), which is not used: nothing proves
it on a real installation yet, and it would put an unauthenticated stream into Home Assistant.
"""

import asyncio
import logging
import time

from aiocomexio.doorstation import Doorstation
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC,
    doorstation_camera_unique_id,
    extension_device_name,
    one_line,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .entity import ComexioStableEntityIdMixin, build_device_info, extension_device_identifier

_LOGGER = logging.getLogger(__name__)

# Device name of a Doorstation whose extension name is not known (no IO in the data, no name on the page).
_FALLBACK_EXTENSION_NAME = "Doorstation"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up a camera for every Doorstation the polls report, now and later."""
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    sync = _DoorstationCameraSync(coordinator, async_add_entities)
    sync()
    entry.async_on_unload(coordinator.async_add_listener(sync))


class _DoorstationCameraSync:
    """Coordinator listener: a camera for every Doorstation that was not seen before.

    A camera is never removed here: its entity turns unavailable while its Doorstation is not reported (see
    DoorstationTracker for how long a missing one is kept), and the orphan cleanup in async_setup_entry keeps its
    registry entry: the camera of a Doorstation that was really removed is deleted by hand.
    """

    def __init__(self, coordinator: ComexioCoordinator, async_add_entities: AddEntitiesCallback) -> None:
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._added: set[int] = set()

    @callback
    def __call__(self) -> None:
        if new := [ext_id for ext_id in sorted(self._coordinator.doorstations.known) if ext_id not in self._added]:
            self._added.update(new)
            self._async_add_entities(ComexioDoorstationCamera(self._coordinator, ext_id) for ext_id in new)


class ComexioDoorstationCamera(ComexioStableEntityIdMixin, CoordinatorEntity[ComexioCoordinator], Camera):
    """The still image of a Doorstation camera, on the device of the Doorstation's extension."""

    _attr_has_entity_name = True
    _attr_translation_key = "doorstation_camera"

    def __init__(self, coordinator: ComexioCoordinator, ext_id: int) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._ext_id = ext_id
        self._attr_unique_id = doorstation_camera_unique_id(coordinator.server_id, ext_id)
        self._frame: bytes | None = None
        self._frame_at = 0.0
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()
        self._warned = False

    @property
    def _doorstation(self) -> Doorstation | None:
        return self.coordinator.doorstations.known.get(self._ext_id)

    @property
    def _ext_name(self) -> str:
        doorstation = self._doorstation
        ios = (self.coordinator.data or {}).get("io", [])
        known = doorstation_ext_name(doorstation, ios) if doorstation else None
        return known or f"{_FALLBACK_EXTENSION_NAME} {self._ext_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """The extension's device, the one its IOs (PIR1, DI1-DI3, ...) sit on."""
        server_id = self.coordinator.server_id
        return build_device_info(
            self.coordinator,
            identifiers={(DOMAIN, extension_device_identifier(server_id, self._ext_name))},
            name=extension_device_name(server_id, self._ext_name),
            model="Extension Module",
        )

    @property
    def _extension_offline(self) -> bool:
        """The extension reports offline, like the IO entities on its device count it."""
        return self._ext_name in (self.coordinator.offline_extensions or ())

    @property
    def available(self) -> bool:
        return super().available and self._doorstation is not None and not self._extension_offline

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The current JPEG frame; None if the camera cannot be read (HA then reports it to the caller).

        Comexio serves one request at a time and opens the stream anew for each (a read that fails takes up to
        10 s), so requests that arrive together share one read, and a frame younger than
        DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC is handed out again: a failed read is likewise not repeated before
        that interval has passed, or every request queued behind it would start a read of its own. A caller that
        does not look at `available` (the camera.snapshot service) gets no read for a Doorstation that is gone or
        whose extension is offline either.
        """
        async with self._lock:
            doorstation = self._doorstation
            if doorstation is None or self._extension_offline:
                return None
            now = time.monotonic()
            if self._frame is not None and now - self._frame_at < DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC:
                return self._frame
            if self._failed_at is not None and now - self._failed_at < DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC:
                return None
            frame, error = await self.coordinator.api.get_doorstation_snapshot(doorstation.camera_id)
            if not frame:
                self._failed_at = time.monotonic()
                self._report_failure(doorstation, error)
                return None
            self._failed_at = None
            if self._warned:
                self._warned = False
                _LOGGER.info(
                    "[%s] Doorstation camera %d delivers images again", self.coordinator.server_id, self._ext_id
                )
            self._frame, self._frame_at = frame, time.monotonic()
            return frame

    def _report_failure(self, doorstation: Doorstation, error: str | None) -> None:
        """Warn once per failure streak that no image came, debug-log the later ones."""
        # A stale frame would pass for the current picture of a door: none is kept after a failure.
        self._frame = None
        level = logging.DEBUG if self._warned else logging.WARNING
        self._warned = True
        _LOGGER.log(
            level,
            "[%s] No image from the camera of Doorstation %s (camera %d): %s",
            self.coordinator.server_id,
            one_line(self._ext_name),
            doorstation.camera_id,
            one_line(error or "unknown error"),
        )
