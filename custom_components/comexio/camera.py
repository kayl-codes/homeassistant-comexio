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

# Device name of a camera whose Doorstation lost its extension name after the camera was added.
_FALLBACK_EXTENSION_NAME = "Doorstation"


def _known_ext_name(coordinator: ComexioCoordinator, doorstation: Doorstation) -> str | None:
    """The extension name of a Doorstation (its IOs, else the page), None if neither knows it."""
    return doorstation_ext_name(doorstation, (coordinator.data or {}).get("io", []))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up a camera for every Doorstation the polls report, now and later."""
    coordinator: ComexioCoordinator = hass.data[DOMAIN][entry.entry_id]
    sync = _DoorstationCameraSync(coordinator, async_add_entities)
    sync()
    entry.async_on_unload(coordinator.async_add_listener(sync))


class _DoorstationCameraSync:
    """Coordinator listener: a camera for every Doorstation that was not seen before.

    A camera is added only once its extension name is known: the camera joins the device of that extension when it
    is registered and the device is not looked at again, so a camera added under a made-up name would stay on a
    device of its own. A camera is never removed here: its entity turns unavailable while its Doorstation is not
    reported (see DoorstationTracker for how long a missing one is kept), and the orphan cleanup in
    async_setup_entry keeps its registry entry: the camera of a Doorstation that was really removed is deleted by
    hand.
    """

    def __init__(self, coordinator: ComexioCoordinator, async_add_entities: AddEntitiesCallback) -> None:
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._added: set[int] = set()
        self._unnamed_warned: set[int] = set()

    @callback
    def __call__(self) -> None:
        if new := [
            ext_id
            for ext_id, doorstation in sorted(self._coordinator.doorstations.known.items())
            if self._ready(ext_id, doorstation)
        ]:
            self._added.update(new)
            self._async_add_entities(ComexioDoorstationCamera(self._coordinator, ext_id) for ext_id in new)

    def _ready(self, ext_id: int, doorstation: Doorstation) -> bool:
        """True for a Doorstation that has no camera yet and whose extension name is known."""
        if ext_id in self._added:
            return False
        if _known_ext_name(self._coordinator, doorstation) is not None:
            if ext_id in self._unnamed_warned:
                _LOGGER.info(
                    "[%s] Doorstation %d has an extension name now, adding its camera",
                    self._coordinator.server_id,
                    ext_id,
                )
            return True
        if ext_id not in self._unnamed_warned:
            self._unnamed_warned.add(ext_id)
            _LOGGER.warning(
                "[%s] Doorstation %d has no extension name on the IO home page and no IO to take it from: "
                "its camera is added once a name is known",
                self._coordinator.server_id,
                ext_id,
            )
        return False


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
        self._frame_camera_id: int | None = None
        self._failed_at: float | None = None
        self._failed_camera_id: int | None = None
        self._known_name: str | None = None
        self._lock = asyncio.Lock()
        # The camera whose failure streak was warned about: a new camera id starts a streak of its own.
        self._warned_camera_id: int | None = None

    @property
    def _doorstation(self) -> Doorstation | None:
        return self.coordinator.doorstations.known.get(self._ext_id)

    @property
    def _ext_name(self) -> str:
        doorstation = self._doorstation
        if doorstation and (known := _known_ext_name(self.coordinator, doorstation)):
            self._known_name = known
        # The last name seen stays the entity's: the offline state is looked up by it, and a made-up name
        # never is in the offline set.
        return self._known_name or f"{_FALLBACK_EXTENSION_NAME} {self._ext_id}"

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
        whose extension is offline either. The reads of all cameras go one after the other (ComexioAPI): a request
        that waits behind a slow one longer than Home Assistant's own image timeout is cancelled there and gets no
        image.
        """
        async with self._lock:
            doorstation = self._doorstation
            if doorstation is None or self._extension_offline:
                # What was cached belongs to a Doorstation that is not there: a rediscovery within the interval
                # must read afresh, not hand out the picture from before it went. (A failed read stays throttled.)
                self._frame = None
                return None
            now = time.monotonic()
            if self._frame is not None and self._is_fresh(doorstation, now):
                return self._frame
            if self._failed_recently(doorstation, now):
                return None
            frame, error = await self.coordinator.api.get_doorstation_snapshot(doorstation.camera_id)
            if not self._is_current(doorstation):
                # The Doorstation went, changed its camera or its extension went offline while the read was under
                # way: the answer belongs to what was asked, not to what is there now. It starts or ends no
                # failure streak.
                self._frame = None
                _LOGGER.debug(
                    "[%s] Doorstation camera %d: dropped the answer of camera %d (%s), it changed meanwhile",
                    self.coordinator.server_id,
                    self._ext_id,
                    doorstation.camera_id,
                    one_line(error or "image"),
                )
                return None
            if not frame:
                self._failed_at, self._failed_camera_id = time.monotonic(), doorstation.camera_id
                self._report_failure(doorstation, error)
                return None
            self._failed_at = None
            if self._warned_camera_id is not None:
                self._warned_camera_id = None
                _LOGGER.info(
                    "[%s] Doorstation camera %d delivers images again", self.coordinator.server_id, self._ext_id
                )
            self._frame, self._frame_at, self._frame_camera_id = frame, time.monotonic(), doorstation.camera_id
            return frame

    def _is_fresh(self, doorstation: Doorstation, now: float) -> bool:
        """The cached frame is young enough and comes from the camera the Doorstation has now."""
        return (
            self._frame_camera_id == doorstation.camera_id
            and now - self._frame_at < DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC
        )

    def _failed_recently(self, doorstation: Doorstation, now: float) -> bool:
        """The last read of this camera failed less than the interval ago (a new camera id starts afresh)."""
        return (
            self._failed_at is not None
            and self._failed_camera_id == doorstation.camera_id
            and now - self._failed_at < DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC
        )

    def _is_current(self, asked: Doorstation) -> bool:
        """The Doorstation is still known with the camera that was asked, and its extension is not offline."""
        current = self._doorstation
        return current is not None and current.camera_id == asked.camera_id and not self._extension_offline

    def _report_failure(self, doorstation: Doorstation, error: str | None) -> None:
        """Warn once per failure streak of a camera that no image came, debug-log the later ones."""
        # A stale frame would pass for the current picture of a door: none is kept after a failure.
        self._frame = None
        level = logging.DEBUG if self._warned_camera_id == doorstation.camera_id else logging.WARNING
        self._warned_camera_id = doorstation.camera_id
        _LOGGER.log(
            level,
            "[%s] No image from the camera of Doorstation %s (camera %d): %s",
            self.coordinator.server_id,
            one_line(self._ext_name),
            doorstation.camera_id,
            one_line(error or "unknown error"),
        )
