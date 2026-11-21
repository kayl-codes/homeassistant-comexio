"""Camera platform: one camera per Doorstation of the IO-Server.

The Doorstation serves its MJPEG stream under its own address, with no login (confirmed on a real installation,
#180). The camera prefers that way for its still image: the address comes from the extension's settings on the
IO-Server (``ComexioAPI.get_doorstation_address``), and the image is the first frame of the Doorstation's stream.
Without a usable address (`local`, not readable), or while a direct read fails, the camera falls back to the
IO-Server: there the stream needs the admin session, and the image is the first frame of the stream (aiocomexio's
``get_doorstation_snapshot``). The frontend asks for a new still image every few seconds (Home Assistant's default
stream of a camera that has no stream of its own).
"""

import asyncio
import logging
import time

from aiocomexio.doorstation import Doorstation
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DOORSTATION_ADDRESS_RECHECK_SEC,
    DOORSTATION_DIRECT_MIN_READ_SEC,
    DOORSTATION_DIRECT_RETRY_SEC,
    DOORSTATION_DIRECT_TIMEOUT_SEC,
    DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC,
    doorstation_camera_unique_id,
    extension_device_name,
    one_line,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .doorstation_stream import doorstation_direct_url, read_direct_frame
from .entity import ComexioStableEntityIdMixin, build_device_info, extension_device_identifier

_LOGGER = logging.getLogger(__name__)

_LOOKUP_TIMED_OUT = "the IO-Server did not answer in time"

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
        # The Doorstation's own address (None: none usable, the IO-Server is used), when it was looked for (None: look
        # again), when a read from the Doorstation last failed (None: none since it worked) and whether that was warned.
        self._address: str | None = None
        self._address_checked_at: float | None = None
        self._direct_failed_at: float | None = None
        self._direct_warned = False
        # Defence for a second reader of the address (reads of one camera are serialised by `_lock` today).
        self._address_lock = asyncio.Lock()

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
            if doorstation is None or not self._is_current(doorstation):
                # What was cached belongs to a Doorstation that is not there (or an entry that unloads): a
                # rediscovery within the interval must read afresh, not hand out the picture from before it went.
                # (A failed read stays throttled.)
                self._frame = None
                return None
            now = time.monotonic()
            if self._frame is not None and self._is_fresh(doorstation, now):
                return self._frame
            if self._failed_recently(doorstation, now):
                return None
            frame, error = await self._read_frame(doorstation)
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

    async def _read_frame(self, doorstation: Doorstation) -> tuple[bytes | None, str | None]:
        """One frame from the Doorstation itself if it has a usable address and answers, else from the IO-Server."""
        if (direct := await self._read_direct_frame(doorstation)) is not None:
            return direct
        if not self._is_current(doorstation):
            return None, None  # the camera went while the direct way was tried: nobody wants this answer
        return await self.coordinator.api.get_doorstation_snapshot(doorstation.camera_id)

    async def _read_direct_frame(self, doorstation: Doorstation) -> tuple[bytes | None, str | None] | None:
        """(frame, None) read from the Doorstation itself; None when the IO-Server is to be asked.

        None for no usable address, a pause, and for a failed read (noted here). A camera that is gone meanwhile gives
        what was read as it is (see async_camera_image) and starts or ends no failure streak. Reads of one camera
        never overlap (`async_camera_image` holds `_lock`), so the Doorstation is asked by one request at a time.

        The address lookup and the read share DOORSTATION_DIRECT_TIMEOUT_SEC: Home Assistant gives an image request
        10 s in all, so the IO-Server, asked after a failed attempt, must keep time to answer, and the pause must be
        set before Home Assistant cancels the request.
        """
        started = asyncio.get_running_loop().time()
        address, lookup_timed_out = await self._address_within_budget()
        left = DOORSTATION_DIRECT_TIMEOUT_SEC - (asyncio.get_running_loop().time() - started)
        if address is None:
            if not lookup_timed_out:
                return None
            frame, error = None, _LOOKUP_TIMED_OUT  # no address known and none read in time
        elif left < DOORSTATION_DIRECT_MIN_READ_SEC:
            return None  # the budget went in the lookup: the IO-Server answers, the Doorstation was not asked
        else:
            # The read gets what is left of the budget and names its own cause (no answer / no frame in time).
            frame, error = await read_direct_frame(
                async_get_clientsession(self.hass), doorstation_direct_url(address), budget_sec=left
            )
        if not self._is_current(doorstation):
            return frame, error
        if frame is not None:
            self._direct_worked()
            return frame, None
        self._direct_failed(error)
        return None

    async def _address_within_budget(self) -> tuple[str | None, bool]:
        """(address, False) as `_direct_address` has it; (the address known, True) if the lookup used up the budget.

        The IO-Server did not answer in time (it is asked behind its other requests): not the Doorstation's fault. A
        known address (the lookup was a refresh after a failed read) stays, as in `_look_up_address`.
        """
        try:
            async with asyncio.timeout(DOORSTATION_DIRECT_TIMEOUT_SEC):
                return await self._direct_address(), False
        except TimeoutError:
            _LOGGER.debug(
                "[%s] Doorstation camera %d: address lookup used up the budget, the IO-Server answers",
                self.coordinator.server_id,
                self._ext_id,
            )
            if self._address is not None and not self._direct_paused():
                # The known address stays (as in `_look_up_address`) and is not asked for again by every read
                # that follows: each would spend its budget on the same slow answer. Should the address have
                # changed, the next read fails, which starts the refresh afresh (one failed read and the pause).
                # Not during the pause: a read that failed meanwhile reset the check, and that stays.
                self._address_checked_at = time.monotonic()
            return self._address, True

    async def _direct_address(self) -> str | None:
        """The Doorstation's own address while reading from it is worth a try, else None (use the IO-Server).

        None during the pause after a failed read, and for an extension with no usable address. The address is
        looked up again after DOORSTATION_ADDRESS_RECHECK_SEC, a known one too (so one configured later is found, and
        one that changed is noticed before a read fails), and after a failed read. A failed lookup beside a known
        address keeps that one, without a known one it counts as no address.
        """
        async with self._address_lock:
            if self._direct_paused():
                return None
            if self._address_is_due() and not await self._look_up_address():
                return None
            return self._address

    def _address_is_due(self) -> bool:
        """The address was never looked for (or a read failed), or DOORSTATION_ADDRESS_RECHECK_SEC has passed."""
        if self._address_checked_at is None:
            return True
        return time.monotonic() - self._address_checked_at >= DOORSTATION_ADDRESS_RECHECK_SEC

    def _direct_paused(self) -> bool:
        """A read from the Doorstation itself failed less than DOORSTATION_DIRECT_RETRY_SEC ago."""
        return (
            self._direct_failed_at is not None
            and time.monotonic() - self._direct_failed_at < DOORSTATION_DIRECT_RETRY_SEC
        )

    async def _look_up_address(self) -> bool:
        """Look the address up (under `_address_lock`); False when the direct way is not to be used now."""
        asked = self._doorstation
        # No time limit of its own: `_address_within_budget` bounds the lookup together with the read.
        address, cause = await self.coordinator.api.get_doorstation_address(self._ext_id)
        if self._direct_paused():
            # A read failed while the address was looked up: the pause holds, and the next lookup is not held off
            # by this one (the failure reset it, the address read before it is not trusted).
            _LOGGER.debug(
                "[%s] Doorstation camera %d: address lookup dropped, a read failed meanwhile",
                self.coordinator.server_id,
                self._ext_id,
            )
            return False
        if cause is not None and self._address is None:
            # The lookup failed (not: there is no usable address) and there is none known: pause the direct way like
            # after a failed read and look again after the pause. A camera that is gone or changed meanwhile, or an
            # entry that unloads, starts no streak.
            if asked is not None and self._is_current(asked):
                self._direct_failed(f"address not read: {cause}")
            return False
        if cause is None:
            self._address = address
        else:
            # A failed lookup beside a known address keeps that one: nothing says it is wrong.
            _LOGGER.debug(
                "[%s] Doorstation camera %d: address lookup failed (%s), keeping the known address",
                self.coordinator.server_id,
                self._ext_id,
                one_line(cause),
            )
        self._address_checked_at = time.monotonic()
        return True

    def _direct_worked(self) -> None:
        """A read from the Doorstation itself delivered: the direct way is in use again."""
        self._direct_failed_at = None
        if self._direct_warned:
            self._direct_warned = False
            _LOGGER.info(
                "[%s] Doorstation camera %d: reading from the Doorstation itself works again",
                self.coordinator.server_id,
                self._ext_id,
            )

    def _direct_failed(self, error: str | None) -> None:
        """A read from the Doorstation itself failed: pause the direct way and look the address up afresh later.

        Warns once per failure streak, the camera goes on over the IO-Server meanwhile.
        """
        self._direct_failed_at = time.monotonic()
        self._address_checked_at = None
        _LOGGER.log(
            logging.DEBUG if self._direct_warned else logging.WARNING,
            "[%s] Doorstation camera %d: reading from the Doorstation itself failed (%s), using the IO-Server for %d s",
            self.coordinator.server_id,
            self._ext_id,
            one_line(error or "unknown error"),
            DOORSTATION_DIRECT_RETRY_SEC,
        )
        self._direct_warned = True

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
        """The Doorstation is still known with the camera that was asked, its extension is not offline and the entry
        is not unloading: whatever was asked belongs to a camera that is there.

        The unloading flag is set while the entry waits before it unloads, so for that time no image is handed out.
        """
        current = self._doorstation
        return (
            current is not None
            and current.camera_id == asked.camera_id
            and not self._extension_offline
            and not self.coordinator.unloading
        )

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
