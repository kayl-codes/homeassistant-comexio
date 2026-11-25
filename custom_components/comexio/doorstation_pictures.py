"""The pictures a Doorstation stores for a bell press or motion: the newest of each kind, kept up to date.

aiocomexio reads the list of stored pictures and a picture (``get_doorstation_pictures`` / ``get_doorstation_picture``);
this module decides when to read them and what to keep. One ``DoorstationPictureFeed`` per Doorstation serves its two
``image`` entities (image.py):

* The list is read when the feed starts, then every ``DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC`` on a timer of the
  feed's own (whatever the coordinator's interval is; a coordinator update reads too once the interval has passed, and
  at once after the Doorstation was unreachable), and ``DOORSTATION_PICTURE_PUSH_DELAY_SEC`` after a push of a bell
  button or the motion sensor. The Doorstation stores the picture a moment after the event; how long that takes is not
  measured, so a push whose read lists nothing new of its kind is read again (``DOORSTATION_PICTURE_PUSH_RETRY_SEC``,
  ``..._PUSH_TRIES`` reads in all), and a push that comes while another read runs gets a read that starts after it.
  A push that comes while those reads are under way renews nothing (a forged or repeated push would keep the reads
  going); it gets reads of its own once they are done, so the reads pushes start come in groups of at most
  ``..._PUSH_TRIES``, a pause apart. A push reaches the feed only through an IO the integration wires (a Web-IO command
  exists for it); without that the timer finds the picture. Nothing is read while the Doorstation is unknown or its
  extension offline.
* Only the newest picture of each kind is kept (name and, once an entity asked for it, the JPEG): the list itself can
  hold thousands of names and is dropped after reading.
* A picture is read when something asks for it (the frontend, a notification) and again only when a newer one is
  listed. A read that failed is not repeated for ``DOORSTATION_PICTURE_RETRY_SEC`` (a newer picture is asked for at
  once), and none takes longer than ``DOORSTATION_PICTURE_READ_TIMEOUT_SEC``: Home Assistant cancels an image request
  after about 10 s without a trace.
* The moment of a picture is the one in its file name, the Doorstation's own clock without a time zone: it is taken
  as the local time of the Home Assistant installation (the installation's own, which a Doorstation set up right
  shows). A picture whose name carries no valid moment is not counted as the newest of its kind, as the list's order
  puts those last.
"""

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from functools import partial
import logging
import time

from aiocomexio import DoorstationPicture, DoorstationPictureKind
from aiocomexio.doorstation import Doorstation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DOORSTATION_BELL_IDENTIFIERS,
    DOORSTATION_MOTION_IDENTIFIER,
    DOORSTATION_PICTURE_PUSH_DELAY_SEC,
    DOORSTATION_PICTURE_PUSH_RETRY_SEC,
    DOORSTATION_PICTURE_PUSH_TRIES,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
    DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC,
    DOORSTATION_PICTURE_RETRY_SEC,
    doorstation_pictures_signal,
    extension_device_name,
    io_push_signal,
    one_line,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .entity import build_device_info, extension_device_identifier

_LOGGER = logging.getLogger(__name__)

# The kinds an entity is made for: what the bell button and the motion sensor store.
PICTURE_KINDS = (DoorstationPictureKind.BELL, DoorstationPictureKind.MOTION)

# Device name of an entity whose Doorstation lost its extension name after the entity was added.
_FALLBACK_EXTENSION_NAME = "Doorstation"

# What the push of an IO says was taken: a bell button a bell picture, the motion sensor a motion picture.
_PUSH_KIND_BY_IDENTIFIER: dict[str, DoorstationPictureKind] = {
    **dict.fromkeys(sorted(DOORSTATION_BELL_IDENTIFIERS), DoorstationPictureKind.BELL),
    DOORSTATION_MOTION_IDENTIFIER: DoorstationPictureKind.MOTION,
}


def newest_of_kind(pictures: Iterable[DoorstationPicture], kind: DoorstationPictureKind) -> DoorstationPicture | None:
    """The picture of `kind` with the latest moment in its name; None if there is none with a moment.

    Chosen by the moment here, not by the list's order: the order is the library's and says nothing for a name
    without a moment. Two pictures of the same moment: the one listed first (the library lists the newest first).
    """
    best: tuple[datetime, DoorstationPicture] | None = None
    for picture in pictures:
        moment = picture.taken_at
        if picture.kind is not kind or moment is None:
            continue
        if best is None or moment > best[0]:
            best = (moment, picture)
    return best[1] if best else None


def taken_at_utc(picture: DoorstationPicture) -> datetime | None:
    """The moment of a picture as an aware UTC time, reading its wall-clock time as Home Assistant's local time."""
    if picture.taken_at is None:
        return None
    return dt_util.as_utc(picture.taken_at.replace(tzinfo=dt_util.get_default_time_zone()))


class DoorstationPictureFeed:
    """The newest bell and motion picture of one Doorstation, and the reads that keep them current."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: ComexioCoordinator, ext_id: int) -> None:
        self._hass = hass
        # Its background tasks end when the entry unloads and never delay its setup.
        self._entry = entry
        self._coordinator = coordinator
        self.ext_id = ext_id
        self._newest: dict[DoorstationPictureKind, DoorstationPicture] = {}
        self._jpegs: dict[DoorstationPictureKind, tuple[str, bytes]] = {}
        self._lock = asyncio.Lock()
        self._last_read: float | None = None
        self._pending_read = False
        # A push came while a read ran: that read may have started before the picture was stored.
        self._read_again = False
        self._push_timer: Callable[[], None] | None = None
        # The reads of a push cycle: from the first push on idle until the awaited pictures are listed or the tries are
        # used up. _awaited: the kinds whose new picture is still missing; _next_awaited: kinds of pushes that came
        # after the cycle's first read began (a kind already awaited is covered), they get a cycle of their own.
        self._push_open = False
        self._push_tries = 0
        self._awaited: set[DoorstationPictureKind] = set()
        self._next_awaited: set[DoorstationPictureKind] = set()
        self._stopped = False
        self._known_name: str | None = None
        self._list_failed = False
        # Per kind: the picture whose read failed last and when; a kind in it is in a failure streak.
        self._picture_failures: dict[DoorstationPictureKind, tuple[str, float]] = {}

    # --- what the entities show ---

    @property
    def doorstation(self) -> Doorstation | None:
        """The Doorstation as the polls report it now; None while they report none."""
        return self._coordinator.doorstations.known.get(self.ext_id)

    @property
    def ext_name(self) -> str:
        """The extension's name: the last one seen stays while the Doorstation is unknown (the offline set uses it)."""
        doorstation = self.doorstation
        if doorstation and (known := doorstation_ext_name(doorstation, (self._coordinator.data or {}).get("io", []))):
            self._known_name = known
        return self._known_name or f"{_FALLBACK_EXTENSION_NAME} {self.ext_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """The extension's device, the one its IOs, its camera and its doorbell events sit on."""
        server_id = self._coordinator.server_id
        return build_device_info(
            self._coordinator,
            identifiers={(DOMAIN, extension_device_identifier(server_id, self.ext_name))},
            name=extension_device_name(server_id, self.ext_name),
            model="Extension Module",
        )

    @property
    def available(self) -> bool:
        """The Doorstation is reported and its extension is not offline."""
        return self.doorstation is not None and self.ext_name not in (self._coordinator.offline_extensions or ())

    def newest(self, kind: DoorstationPictureKind) -> DoorstationPicture | None:
        """The newest picture of `kind` the last list read showed."""
        return self._newest.get(kind)

    async def async_picture(self, kind: DoorstationPictureKind) -> bytes | None:
        """The JPEG of the newest picture of `kind`; None if there is none or it cannot be read.

        Read from the Doorstation (through the IO-Server) once per picture and kept until a newer one is listed. The
        read goes one after the other with the list reads of this Doorstation and, in ComexioAPI, with every other
        read of a Doorstation. The wait for those counts into DOORSTATION_PICTURE_READ_TIMEOUT_SEC, but only a read
        that ran out of time is a failure of the Doorstation: a request that never got its turn returns None and
        leaves no mark (the reads that held it report their own failures).
        """
        if (cached := self._cached_jpeg(kind)) is not None:
            return cached
        deadline = asyncio.get_running_loop().time() + DOORSTATION_PICTURE_READ_TIMEOUT_SEC
        if not await self._acquire_by(deadline):
            return None
        budget = asyncio.timeout_at(deadline)
        try:
            async with budget:
                return await self._async_read_picture(kind)
        except Exception as err:  # noqa: BLE001 - the image request must end in None and never in an error page
            expired = isinstance(err, TimeoutError) and budget.expired()
            if not expired:
                _LOGGER.debug("Doorstation %d picture read failed", self.ext_id, exc_info=True)
            cause = f"not read within {DOORSTATION_PICTURE_READ_TIMEOUT_SEC:g} s" if expired else type(err).__name__
            self._report_picture_failure(kind, self._newest.get(kind), cause)
            return None
        finally:
            self._lock.release()

    async def _acquire_by(self, deadline: float) -> bool:
        """Take the feed's lock; False if `deadline` (loop time) came first."""
        try:
            async with asyncio.timeout_at(deadline):
                await self._lock.acquire()
        except TimeoutError:
            return False
        return True

    def _cached_jpeg(self, kind: DoorstationPictureKind) -> bytes | None:
        """The kept JPEG if it is the newest picture's and the Doorstation can be reached; read-only, no lock."""
        picture = self._newest.get(kind)
        cached = self._jpegs.get(kind)
        if picture is not None and cached is not None and cached[0] == picture.name and self.available:
            return cached[1]
        return None

    async def _async_read_picture(self, kind: DoorstationPictureKind) -> bytes | None:
        """The body of async_picture, under the lock and inside its time budget."""
        picture = self._newest.get(kind)
        if picture is None or not self.available:
            return None
        if (cached := self._cached_jpeg(kind)) is not None:
            return cached  # an earlier request in the queue read it meanwhile
        failed = self._picture_failures.get(kind)
        if (
            failed is not None
            and failed[0] == picture.name
            and time.monotonic() - failed[1] < DOORSTATION_PICTURE_RETRY_SEC
        ):
            return None
        data, cause = await self._coordinator.api.get_doorstation_picture(self.ext_id, picture.name)
        if not data:
            # No data and no cause: the session is gone, nothing to report. Empty data is a failure of its own.
            self._report_picture_failure(kind, picture, cause or (None if data is None else "empty picture"))
            return None
        if not self.available:
            # It went away while this one was on its way: poll() dropped the kept pictures, and nothing is kept or
            # served for a Doorstation that cannot be reached (as `_cached_jpeg` does). Nor does the answer end a
            # failure streak: a picture that was never kept is not "read again".
            return None
        if self._picture_failures.pop(kind, None) is not None:
            _LOGGER.info(
                "[%s] Doorstation %d: its %s pictures are read again",
                self._coordinator.server_id,
                self.ext_id,
                kind.value,
            )
        # No size check of its own: aiocomexio reads at most 4 MiB of a picture (more is a failure, not data), so the
        # memory kept is two pictures per Doorstation at most.
        self._jpegs[kind] = (picture.name, data)
        return data

    # --- when to read the list ---

    @callback
    def start(self) -> None:
        """Listen for the pushes of the bell buttons and the motion sensor and read the list once.

        Everything started here ends with the config entry.
        """
        server_id = self._coordinator.server_id
        for identifier, kind in sorted(_PUSH_KIND_BY_IDENTIFIER.items()):
            self._entry.async_on_unload(
                async_dispatcher_connect(
                    self._hass, io_push_signal(server_id, self.ext_id, identifier), partial(self._handle_push, kind)
                )
            )
        # The interval is the feed's own: the coordinator's can be longer, and its updates are no clock for this.
        self._entry.async_on_unload(
            async_track_time_interval(
                self._hass, self._interval_tick, timedelta(seconds=DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
            )
        )
        self._entry.async_on_unload(self.stop)
        self.poll()

    @callback
    def stop(self) -> None:
        """Drop the waiting read of a push and take no new one."""
        self._stopped = True
        if self._push_timer is not None:
            self._push_timer()
            self._push_timer = None

    @callback
    def poll(self) -> None:
        """A coordinator update came: read the list again if the last read is older than the refresh interval.

        Nothing is read while the Doorstation is unknown or its extension offline; the first update after that reads
        at once.
        """
        if not self.available:
            self._last_read = None
            # A removed Doorstation keeps its feed until the entry unloads: the pictures it held (up to 4 MiB each) go,
            # the names stay for the entities. The first image request after it is back reads the JPEG again.
            self._jpegs = {}
            # A streak of failed picture reads ends with it too: after the return the first failure warns again, and
            # the pause of a failure from before does not hold a picture back.
            self._picture_failures = {}
            return
        if self._last_read is None or time.monotonic() - self._last_read >= DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC:
            self._read_soon()

    @callback
    def _interval_tick(self, _now: datetime) -> None:
        """The refresh interval has passed: read the list, whatever the coordinator has been doing."""
        if self.available:
            self._read_soon()

    @callback
    def _handle_push(self, kind: DoorstationPictureKind, value: float | None) -> None:
        """A bell button or the motion sensor reports a value above zero: read the list after a short pause.

        The reads of a push (a cycle) are not renewed by further pushes, or a forged or repeated one would keep them
        going: one that comes before the cycle's first read is covered by it, one for a kind still awaited is covered
        by the retries, and any other gets a cycle of its own when this one is done.
        """
        if value is None or value <= 0:
            return
        if not self._push_open:
            self._open_push_cycle({kind})
        elif self._push_tries == 0:
            self._awaited.add(kind)
        elif kind not in self._awaited:
            self._next_awaited.add(kind)

    def _open_push_cycle(self, kinds: set[DoorstationPictureKind]) -> None:
        self._push_open = True
        self._push_tries = 0
        self._awaited = set(kinds)
        self._next_awaited = set()
        self._arm_push_timer(DOORSTATION_PICTURE_PUSH_DELAY_SEC)

    def _close_push_cycle(self) -> None:
        self._push_open = False
        self._awaited = set()
        self._next_awaited = set()

    def _after_push_read(self, changed: frozenset[DoorstationPictureKind]) -> None:
        """A read of the cycle ended: read again while an awaited picture is missing and tries are left, else end it.

        Only the kinds the pushes asked for count: a new picture of the other kind says nothing about them.
        """
        self._awaited -= changed
        self._next_awaited -= changed  # this read lists them already: a cycle of their own would read for nothing
        if self._awaited and self._push_tries < DOORSTATION_PICTURE_PUSH_TRIES:
            if self._push_timer is None:
                self._arm_push_timer(DOORSTATION_PICTURE_PUSH_RETRY_SEC)
            return
        follow_up = self._next_awaited
        self._close_push_cycle()
        if follow_up:
            self._open_push_cycle(follow_up)

    def _arm_push_timer(self, delay: float) -> None:
        if not self._stopped:
            self._push_timer = async_call_later(self._hass, delay, self._push_read)

    @callback
    def _push_read(self, _now: datetime) -> None:
        self._push_timer = None
        if not self.available:
            # The first update after the Doorstation is back reads the list (poll), so the push is not lost.
            _LOGGER.debug("Doorstation %d: no picture read after a push, it cannot be reached", self.ext_id)
            self._close_push_cycle()
            return
        self._push_tries += 1
        self._read_soon(after_push=True)

    def _read_soon(self, *, after_push: bool = False) -> None:
        """Start a read of the list unless one runs already (a second one would read the same).

        A push that finds one running asks for another after it: the running one may have started before the
        Doorstation stored the picture.
        """
        if self._pending_read:
            self._read_again = self._read_again or after_push
            return
        self._pending_read = True
        self._entry.async_create_background_task(
            self._hass,
            self._async_read(after_push),
            f"comexio_{self._coordinator.server_id}_doorstation_{self.ext_id}_pictures",
        )

    async def _async_read(self, after_push: bool) -> None:
        changed: frozenset[DoorstationPictureKind] = frozenset()
        try:
            changed = await self._refresh_safely()
            while self._read_again:
                self._read_again = False
                after_push = True
                changed |= await self._refresh_safely()
        finally:
            # Stamped after the request: a slow IO-Server does not shorten the interval, and a failed read waits it out.
            # Unless the Doorstation became unreachable meanwhile: then poll() has asked for a read at once on return.
            self._last_read = time.monotonic() if self.available else None
            self._pending_read = False
        if after_push:
            self._after_push_read(changed)
        elif self._push_open:
            # A timer or coordinator read that lists a picture a push waits for: its retries need not look for it again.
            self._awaited -= changed
            self._next_awaited -= changed

    async def _refresh_safely(self) -> frozenset[DoorstationPictureKind]:
        """async_refresh, but an error nobody expected is a warning of the streak instead of a lost task."""
        try:
            return await self.async_refresh()
        except Exception as err:  # noqa: BLE001 - a background task: nobody else would see it
            _LOGGER.debug("Doorstation %d: reading the list of pictures failed", self.ext_id, exc_info=True)
            self._report_list_failure(type(err).__name__)
            return frozenset()

    async def async_refresh(self) -> frozenset[DoorstationPictureKind]:
        """Read the list of stored pictures and note the newest of each kind; the kinds whose newest one changed.

        The entities are told when one changed. A list that could not be read changes nothing (no kind).
        """
        async with self._lock:
            if not self.available:
                # It went away while this read waited for the lock: no request for a Doorstation that cannot answer.
                _LOGGER.debug("Doorstation %d: list read skipped, it cannot be reached", self.ext_id)
                return frozenset()
            pictures, cause = await self._coordinator.api.get_doorstation_pictures(self.ext_id)
            if not self.available:
                # It went away while the request was on its way: nothing of this answer is kept (as for a picture, see
                # `_async_read_picture`), and the first update after it is back reads the list again (poll).
                _LOGGER.debug("Doorstation %d: list read dropped, it went away meanwhile", self.ext_id)
                return frozenset()
            if pictures is None:
                self._report_list_failure(cause)
                return frozenset()
            if self._list_failed:
                self._list_failed = False
                _LOGGER.info(
                    "[%s] Doorstation %d: the list of its pictures is read again",
                    self._coordinator.server_id,
                    self.ext_id,
                )
            before = {kind: picture.name for kind, picture in self._newest.items()}
            self._newest = {
                kind: picture for kind in PICTURE_KINDS if (picture := newest_of_kind(pictures, kind)) is not None
            }
            self._jpegs = {
                kind: kept
                for kind, kept in self._jpegs.items()
                if kind in self._newest and kept[0] == self._newest[kind].name
            }
            after = {kind: picture.name for kind, picture in self._newest.items()}
            changed = frozenset(kind for kind in PICTURE_KINDS if before.get(kind) != after.get(kind))
        if changed:
            async_dispatcher_send(self._hass, doorstation_pictures_signal(self._coordinator.server_id, self.ext_id))
        return changed

    # --- failures: one warning per streak ---

    def _report_list_failure(self, cause: str | None) -> None:
        """Warn once per streak about a list that could not be read; no cause means the session is gone (no failure)."""
        if cause is None or self._list_failed:
            return
        self._list_failed = True
        _LOGGER.warning(
            "[%s] Doorstation %d: could not read the list of its pictures: %s",
            self._coordinator.server_id,
            self.ext_id,
            one_line(cause),
        )

    def _report_picture_failure(
        self, kind: DoorstationPictureKind, picture: DoorstationPicture | None, cause: str | None
    ) -> None:
        """Note a picture that could not be read and warn once per streak of its kind; no cause means session gone.

        A read that failed because the Doorstation went away is no failure of its picture (as for the list, see
        `async_refresh`): no pause and no streak, or it would hold the picture back for the pause after the return.
        """
        if cause is None or picture is None:
            return
        if not self.available:
            _LOGGER.debug(
                "Doorstation %d: %s picture read failure not counted, it went away meanwhile: %s",
                self.ext_id,
                kind.value,
                one_line(cause),
            )
            return
        streak = kind in self._picture_failures
        self._picture_failures[kind] = (picture.name, time.monotonic())
        if streak:
            return
        _LOGGER.warning(
            "[%s] Doorstation %d: could not read its newest %s picture: %s",
            self._coordinator.server_id,
            self.ext_id,
            kind.value,
            one_line(cause),
        )
