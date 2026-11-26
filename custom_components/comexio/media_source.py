"""The Doorstation pictures as a gallery in the media browser (Media > Browse media).

Each loaded entry offers its Doorstations as folders in the media source "comexio". A folder lists the newest bell and
motion pictures its Doorstation stored (``DOORSTATION_GALLERY_MAX_PICTURES``), each with its JPEG as thumbnail. The list
and the pictures come from the Doorstation through ComexioAPI, the reads the picture entities use too. Nothing is kept
here: a thumbnail is read when the browser asks for it.

The thumbnail links are signed for the connection that browses (``async_sign_path``): a browser's image request cannot
carry a login, so the signature takes its place. A signed link is a bearer link: whoever holds it can fetch that one
picture until it expires (``DOORSTATION_GALLERY_LINK_EXPIRY_SEC``, one hour). The view itself requires a login for any
request without a signature.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import NamedTuple
import weakref

from aiocomexio import DoorstationPicture, DoorstationPictureKind
from aiocomexio.doorstation_pictures import is_plain_picture_name, picture_of
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    DOORSTATION_GALLERY_CONCURRENT_READS,
    DOORSTATION_GALLERY_LINK_EXPIRY_SEC,
    DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_LIST_TIMEOUT_SEC,
    DOORSTATION_GALLERY_MAX_PICTURES,
    DOORSTATION_GALLERY_MAX_WAITING_READS,
    DOORSTATION_GALLERY_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
    DOORSTATION_PICTURE_VIEW_NAME,
    DOORSTATION_PICTURE_VIEW_PATH,
    one_line,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .doorstation_pictures import PICTURE_KINDS
from .doorstation_stream import LIVE_STREAM_RESPONSE_HEADERS

# The titles of the pictures' kinds in the gallery. They are data, like the picture names: not translated.
_KIND_TITLES: dict[DoorstationPictureKind, str] = {
    DoorstationPictureKind.BELL: "Doorbell",
    DoorstationPictureKind.MOTION: "Motion",
}
_JPEG_MIME = "image/jpeg"
# int() takes at most 4300 digits (ValueError above), a Doorstation extension has far fewer.
_MAX_EXT_ID_DIGITS = 9
_SESSION_GONE_CAUSE = "the integration is unloading or Home Assistant is stopping, try again"
_LOGGER = logging.getLogger(__name__)
# The gallery's places on a config entry's Doorstation lock, kept in hass.data (a semaphore binds to the loop it is
# first used in). One set per entry: each entry has its own lock, so one server must not take another's places.
_GALLERY_READ_SLOTS_KEY = f"{DOMAIN}_gallery_read_slots"


def _gallery_read_slots(hass: HomeAssistant, entry_id: str) -> asyncio.Semaphore:
    """The gallery's places on the lock of one config entry, created on first use."""
    return hass.data.setdefault(
        f"{_GALLERY_READ_SLOTS_KEY}_{entry_id}", asyncio.Semaphore(DOORSTATION_GALLERY_CONCURRENT_READS)
    )


_GALLERY_WAITING_KEY = f"{DOMAIN}_gallery_waiting"


@dataclass
class _Waiting:
    """How many reads of one config entry (thumbnails and folder lists) wait for a gallery place right now, and the
    streaks being logged: a streak of refused reads (one warning, see _refused_for_a_place) and a streak of slow waits
    (one warning, see _warn_slow_place_wait). A streak belongs to the coordinator it started under: a reloaded entry
    starts its own."""

    count: int = 0
    refusing: bool = False
    slow: bool = False
    streak_of: weakref.ref[ComexioCoordinator] | None = None


def _gallery_waiting(hass: HomeAssistant, entry_id: str) -> _Waiting:
    """The waiting count next to the places of one config entry: it outlives an unload like they do."""
    return hass.data.setdefault(f"{_GALLERY_WAITING_KEY}_{entry_id}", _Waiting())


def _kept_gallery_waiting(hass: HomeAssistant, entry_id: str) -> _Waiting | None:
    """The waiting state of an entry that is still kept. A callback that outlives a removed entry (a timer, a wait that
    ends after the removal) must not create the state again: nothing would ever drop it."""
    return hass.data.get(f"{_GALLERY_WAITING_KEY}_{entry_id}")


async def _await_gallery_place(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, number: int, slow_after: float
) -> asyncio.Semaphore | None:
    """Waits for one of the gallery's places, counted against the waiter cap of the entry (thumbnails and browse
    batches alike). None at once when the cap is full; otherwise the caller holds a place and releases that very
    semaphore (an entry removed meanwhile drops its places, a release by key would land on a new set). A wait that runs
    past its own slow-wait mark (slow_after, per kind of read) is logged while it runs."""
    waiting = _gallery_waiting(hass, entry_id)
    if waiting.count >= DOORSTATION_GALLERY_MAX_WAITING_READS:
        return None
    places = _gallery_read_slots(hass, entry_id)
    waiting.count += 1
    slow_warning = asyncio.get_running_loop().call_later(
        slow_after, _warn_slow_place_wait, hass, entry_id, coordinator, number
    )
    try:
        await places.acquire()
    finally:
        slow_warning.cancel()
        waiting.count -= 1
        if waiting.count == 0:
            # No thumbnail waits any more (a cancelled or timed-out wait leaves too): a later stall is a new one.
            waiting.slow = False
    return places


def _is_entry_coordinator(hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator) -> bool:
    """Whether the coordinator is the one the entry runs with now. A request still running under a replaced instance
    (a reload) is not: it must neither take over the streaks nor change the flags of the new instance."""
    return hass.data.get(DOMAIN, {}).get(entry_id) is coordinator


def _streak_of(hass: HomeAssistant, entry_id: str, waiting: _Waiting, coordinator: ComexioCoordinator) -> bool:
    """Hands the streaks of a reloaded entry to its new coordinator: a streak logged under the old instance does not
    silence the first warning of the new one (an unload does not clear the flags, a reload must not inherit them). Only
    the coordinator the entry runs with now takes the streaks, or they would be handed back and forth and warned again
    at every switch. Returns False for any other coordinator. The reference is weak, so an unloaded coordinator is not
    kept alive by the waiting state."""
    if not _is_entry_coordinator(hass, entry_id, coordinator):
        return False
    owner = waiting.streak_of() if waiting.streak_of is not None else None
    if owner is not coordinator:
        waiting.streak_of = weakref.ref(coordinator)
        waiting.refusing = False
        waiting.slow = False
    return True


def _refused_for_a_place(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, number: int, why: str
) -> None:
    """Logs a gallery read (a thumbnail or a folder list) refused for lack of a gallery place: the first of a streak as
    a warning, the others at debug level until a read gets its place again (a stall is then visible once, not once per
    refused request)."""
    waiting = _kept_gallery_waiting(hass, entry_id)
    if waiting is None:
        _LOGGER.debug("Doorstation %s gallery read refused (%s): the entry was removed meanwhile", number, why)
        return
    if not _streak_of(hass, entry_id, waiting, coordinator):
        _LOGGER.debug(
            "Doorstation %s gallery read not answered (%s): its entry was reloaded or unloaded meanwhile", number, why
        )
        return
    if waiting.refusing:
        _LOGGER.debug("Doorstation %s gallery read not answered: %s", number, why)
        return
    waiting.refusing = True
    _LOGGER.warning(
        "Doorstation %s gallery pictures are not answered (%s); further refusals are logged at debug level until one "
        "gets its place",
        number,
        why,
    )


def _warn_slow_place_wait(hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, number: int) -> None:
    """Logs, once per stall, a gallery read (a thumbnail or a folder list) still waiting for a place after its slow-wait
    mark (called by a timer while the wait runs, so the log shows the stall before the wait ends). A stall ends when a
    place is granted in time or the queue runs empty (a browser left, a wait timed out); a later stall warns again. The
    flag is shared by all reads of the entry whatever their mark: a second read may warn inside one stall, but a stall
    is never left unlogged."""
    waiting = _kept_gallery_waiting(hass, entry_id)
    if waiting is None:
        return
    if not _streak_of(hass, entry_id, waiting, coordinator):
        _LOGGER.debug("Doorstation %s still waits for a gallery place, but its entry was reloaded or unloaded", number)
        return
    if waiting.slow:
        return
    waiting.slow = True
    _LOGGER.warning("Doorstation %s gallery read still waits for a gallery place; the places are busy", number)


def _place_granted(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, waited: float, slow_after: float
) -> None:
    """A thumbnail or a list read got its place: the refusal streak ends. The slow streak ends when the places flow
    again: a place that came within the read's own slow mark (slow_after), or a queue that is now empty. A grant after a
    long wait while others still queue belongs to the same stall, and the next one must not warn again. A grant under a
    replaced instance ends the refusal streak only (see below)."""
    waiting = _kept_gallery_waiting(hass, entry_id)
    if waiting is None:
        return
    # A place granted to any read, a replaced one included, shows the places are free again: a later refusal starts a
    # new streak (at worst one more line, never a silenced one). A replaced grant itself leaves the slow flag alone; an
    # emptied queue still clears it in _await_gallery_place, whatever the coordinator.
    waiting.refusing = False
    if not _is_entry_coordinator(hass, entry_id, coordinator):
        return
    if waited < slow_after or waiting.count == 0:
        waiting.slow = False


def forget_gallery_read_slots(hass: HomeAssistant, entry_id: str) -> None:
    """Drops the places of a removed entry. They outlive an unload: a read of the old instance still in flight holds
    one of them, and the reloaded entry must count it against the same places."""
    hass.data.pop(f"{_GALLERY_READ_SLOTS_KEY}_{entry_id}", None)
    hass.data.pop(f"{_GALLERY_WAITING_KEY}_{entry_id}", None)


class _Item(NamedTuple):
    """A parsed identifier of the source: a Doorstation folder (name None) or one of its pictures."""

    entry_id: str
    ext_id: int
    name: str | None


def _is_ext_id(text: str) -> bool:
    """A plain ASCII number of a sane length. str.isdigit() also takes digits such as '²', which int() refuses."""
    return text.isascii() and text.isdecimal() and len(text) <= _MAX_EXT_ID_DIGITS


def _parse(identifier: str) -> _Item | None:
    """The entry, the extension and the picture name of an identifier; None if it names none of these."""
    parts = identifier.split("/")
    if len(parts) not in (2, 3) or not _is_ext_id(parts[1]):
        return None
    return _Item(parts[0], int(parts[1]), parts[2] if len(parts) == 3 else None)


def _loaded_entries(hass: HomeAssistant) -> list[tuple[ConfigEntry, ComexioCoordinator]]:
    """The loaded entries and their coordinators."""
    loaded = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(coordinator, ComexioCoordinator):
            loaded.append((entry, coordinator))
    return loaded


def _loaded(hass: HomeAssistant, entry_id: str) -> tuple[ConfigEntry, ComexioCoordinator] | None:
    """The loaded entry with this id and its coordinator; None if it is not loaded."""
    matches = ((entry, coordinator) for entry, coordinator in _loaded_entries(hass) if entry.entry_id == entry_id)
    return next(matches, None)


def _is_known(loaded: tuple[ConfigEntry, ComexioCoordinator] | None, ext_id: int) -> bool:
    """Whether the loaded entry knows this Doorstation; the gallery offers and serves only those."""
    return loaded is not None and ext_id in loaded[1].doorstations.known


def _view_path(entry_id: str, ext_id: int, name: str) -> str:
    # The callers have checked the name to be a plain file name, which one_line leaves as it is: the wrapper only
    # keeps a path built from a Comexio name on one line by construction.
    return f"{DOORSTATION_PICTURE_VIEW_PATH}/{entry_id}/{ext_id}/{one_line(name)}"


def newest_first(pictures: Iterable[DoorstationPicture]) -> list[DoorstationPicture]:
    """The bell and motion pictures with a plain file name, newest first; a picture without a moment comes last."""
    kept = [picture for picture in pictures if picture.kind in PICTURE_KINDS and is_plain_picture_name(picture.name)]
    return sorted(kept, key=_newest_key, reverse=True)


def _newest_key(picture: DoorstationPicture) -> tuple[bool, datetime]:
    """Sort key: a picture with a moment ranks by it, one without ranks below every one with a moment."""
    return picture.taken_at is not None, picture.taken_at or datetime.min


def _picture_title(picture: DoorstationPicture) -> str:
    label = _KIND_TITLES[picture.kind]
    if picture.taken_at is None:
        return f"{label} ({one_line(picture.name)})"
    return f"{label} {picture.taken_at:%Y-%m-%d %H:%M:%S}"


def _ext_name(coordinator: ComexioCoordinator, ext_id: int) -> str | None:
    """The name of a known Doorstation's extension; None if the Doorstation is not known."""
    doorstation = coordinator.doorstations.known.get(ext_id)
    if doorstation is None:
        return None
    return doorstation_ext_name(doorstation, (coordinator.data or {}).get("io", []))


def _offered(coordinator: ComexioCoordinator, ext_id: int) -> bool:
    """Whether the gallery offers a Doorstation: known, its number one the gallery can read back, and its extension not
    offline (the picture entities' rule)."""
    # _parse reads a folder's number back up to _MAX_EXT_ID_DIGITS digits: a folder beyond that could never be opened.
    if ext_id not in coordinator.doorstations.known or not _is_ext_id(str(ext_id)):
        return False
    name = _ext_name(coordinator, ext_id)
    # Without any extension name the offline set cannot be checked, so nothing is offered. The picture feed keeps the
    # last name it saw; the gallery keeps none, so a gap in both sources hides the Doorstation for that time.
    return name is not None and name not in (coordinator.offline_extensions or ())


def _is_gallery_picture(name: str) -> bool:
    """A plain file name of a bell or motion picture: the kinds the gallery lists. The kind is read off the name, so
    serving one takes no list read (a thumbnail is one request each)."""
    return is_plain_picture_name(name) and picture_of(name).kind in PICTURE_KINDS


def _may_serve(loaded: tuple[ConfigEntry, ComexioCoordinator] | None, ext_id: int, name: str) -> bool:
    """Whether the gallery serves this picture now: the Doorstation is offered and the picture is a listed kind."""
    return loaded is not None and _offered(loaded[1], ext_id) and _is_gallery_picture(name)


def _still_offered(hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, ext_id: int) -> bool:
    """Whether the Doorstation is still offered through the coordinator a read started with. An unload removes the
    entry from hass.data, and a reload brings a new coordinator; the old one keeps its state, so it is not trusted."""
    loaded = _loaded(hass, entry_id)
    return loaded is not None and loaded[1] is coordinator and _offered(coordinator, ext_id)


def _doorstation_title(entry: ConfigEntry, coordinator: ComexioCoordinator, ext_id: int) -> str:
    name = _ext_name(coordinator, ext_id)
    label = one_line(name) if name else f"Doorstation {ext_id}"
    return f"{label} ({entry.title})"


def _folder_item(entry: ConfigEntry, coordinator: ComexioCoordinator, ext_id: int) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=f"{entry.entry_id}/{ext_id}",
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=_doorstation_title(entry, coordinator, ext_id),
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.IMAGE,
    )


def _picture_item(hass: HomeAssistant, entry_id: str, ext_id: int, picture: DoorstationPicture) -> BrowseMediaSource:
    thumbnail = async_sign_path(
        hass, _view_path(entry_id, ext_id, picture.name), timedelta(seconds=DOORSTATION_GALLERY_LINK_EXPIRY_SEC)
    )
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=f"{entry_id}/{ext_id}/{picture.name}",
        media_class=MediaClass.IMAGE,
        media_content_type=_JPEG_MIME,
        title=_picture_title(picture),
        thumbnail=thumbnail,
        can_play=True,
        can_expand=False,
    )


_OFFLINE_OR_UNKNOWN = "Doorstation offline or no longer known"
_TOO_MANY_WAITING = "Too many gallery reads already wait for a place, try again later"


def _list_not_read(detail: str) -> BrowseError:
    """The browse error of a picture list read that did not finish within its budget."""
    return BrowseError(f"Doorstation pictures not read in {DOORSTATION_GALLERY_LIST_TIMEOUT_SEC:g} s {detail}")


async def _folder_children(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, ext_id: int
) -> list[BrowseMediaSource]:
    # The list read takes a gallery place like a thumbnail does: a browse batch must not queue on the Doorstation lock
    # ahead of the camera and the picture entities either. The wait for the place counts into the list's deadline.
    loop = asyncio.get_running_loop()
    started = loop.time()
    placed = False
    try:
        async with asyncio.timeout(DOORSTATION_GALLERY_LIST_TIMEOUT_SEC) as deadline:
            places = await _await_gallery_place(
                hass, entry_id, coordinator, ext_id, DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC
            )
            if places is None:
                _refused_for_a_place(hass, entry_id, coordinator, ext_id, "the waiting reads are at the cap")
                raise BrowseError(_TOO_MANY_WAITING)
            placed = True
            try:
                # A list read that gets its place is a place flowing again, as a thumbnail's is (_place_granted).
                _place_granted(
                    hass,
                    entry_id,
                    coordinator,
                    waited=loop.time() - started,
                    slow_after=DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC,
                )
                # Offline or unloaded while the place was awaited: no list read takes the Doorstation lock for nothing.
                if not _still_offered(hass, entry_id, coordinator, ext_id):
                    raise BrowseError(_OFFLINE_OR_UNKNOWN)
                pictures, cause = await coordinator.api.get_doorstation_pictures(ext_id)
            finally:
                places.release()
    except TimeoutError:
        if not deadline.expired():
            raise
        if not placed:
            # The wait for the place ate the budget: a refusal like a thumbnail's, so a stall shows in the log.
            _refused_for_a_place(
                hass, entry_id, coordinator, ext_id, f"no place within {DOORSTATION_GALLERY_LIST_TIMEOUT_SEC:g} s"
            )
            raise _list_not_read("(the wait for a gallery place included)") from None
        # The place was held and the read did not finish: nothing else logs that, so it is logged here.
        _LOGGER.warning(
            "Doorstation %s picture list read timed out after %g s, with its gallery place",
            ext_id,
            DOORSTATION_GALLERY_LIST_TIMEOUT_SEC,
        )
        raise _list_not_read("(the Doorstation did not answer in time)") from None
    if pictures is None:
        if cause is not None:
            # Any failure cause of the list read is only debug-logged by the API (a read that ran out its own budget is
            # one of them), so the browse logs it here, as the deadline path above does.
            _LOGGER.warning("Doorstation %s picture list not read: %s", ext_id, cause)
        raise BrowseError(f"Doorstation pictures not read: {cause or _SESSION_GONE_CAUSE}")
    newest = newest_first(pictures)[:DOORSTATION_GALLERY_MAX_PICTURES]
    return [_picture_item(hass, entry_id, ext_id, picture) for picture in newest]


class DoorstationGallery(MediaSource):
    """The Doorstation pictures, per Doorstation, newest first."""

    name = "Doorstation pictures"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        if not item.identifier:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.APP,
                media_content_type="",
                title=self.name,
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
                children=[
                    _folder_item(entry, coordinator, ext_id)
                    for entry, coordinator in _loaded_entries(self.hass)
                    for ext_id in coordinator.doorstations.known
                    if _offered(coordinator, ext_id)
                ],
            )
        parsed = _parse(item.identifier)
        if parsed is None or parsed.name is not None:
            raise BrowseError("Unknown item")
        loaded = _loaded(self.hass, parsed.entry_id)
        if loaded is None:
            raise BrowseError("The integration is not loaded, try again")
        if not _is_known(loaded, parsed.ext_id):
            raise BrowseError("Doorstation not known")
        entry, coordinator = loaded
        if not _offered(coordinator, parsed.ext_id):
            raise BrowseError("Doorstation offline")
        children = await _folder_children(self.hass, entry.entry_id, coordinator, parsed.ext_id)
        # The list read may have waited: the entry can have been unloaded or reloaded, or the Doorstation gone offline.
        if not _still_offered(self.hass, entry.entry_id, coordinator, parsed.ext_id):
            raise BrowseError(_OFFLINE_OR_UNKNOWN)
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=item.identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=_doorstation_title(entry, coordinator, parsed.ext_id),
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.IMAGE,
            children=children,
        )

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parsed = _parse(item.identifier)
        if (
            parsed is None
            or parsed.name is None
            or not _may_serve(_loaded(self.hass, parsed.entry_id), parsed.ext_id, parsed.name)
        ):
            raise Unresolvable("Could not resolve this media item")
        return PlayMedia(_view_path(parsed.entry_id, parsed.ext_id, parsed.name), _JPEG_MIME)


# HA awaits async_get_media_source (media_source/__init__.py) — the async signature is the platform contract,
# regardless of body (python:S7503 false positive).
async def async_get_media_source(hass: HomeAssistant) -> DoorstationGallery:  # NOSONAR
    """Set up the Doorstation gallery as a media source (the media_source component asks for it)."""
    return DoorstationGallery(hass)


async def _take_gallery_place(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, number: int
) -> asyncio.Semaphore:
    """Waits for one of the gallery's places, its wait bounded by DOORSTATION_GALLERY_PLACE_WAIT_SEC and the waiter cap.
    A wait that runs past DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC is logged while it runs; a refusal is answered 503."""
    try:
        async with asyncio.timeout(DOORSTATION_GALLERY_PLACE_WAIT_SEC):
            places = await _await_gallery_place(
                hass, entry_id, coordinator, number, DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC
            )
    except TimeoutError:
        _refused_for_a_place(
            hass, entry_id, coordinator, number, f"no place within {DOORSTATION_GALLERY_PLACE_WAIT_SEC:g} s"
        )
        raise web.HTTPServiceUnavailable from None
    if places is None:
        _refused_for_a_place(hass, entry_id, coordinator, number, "the waiting thumbnails are at the cap")
        raise web.HTTPServiceUnavailable
    return places


async def _read_in_a_gallery_place(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: ComexioCoordinator,
    number: int,
    name: str,
) -> tuple[bytes | None, str | None]:
    """One picture read in one of the gallery's places; the wait for a place has its own budget and waiter cap. A
    browser that leaves cancels the handler (Home Assistant's server does so), at the wait or at the read, and the
    place goes back either way (the finally below)."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    places = await _take_gallery_place(hass, entry_id, coordinator, number)
    try:
        _place_granted(
            hass,
            entry_id,
            coordinator,
            waited=loop.time() - started,
            slow_after=DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC,
        )
        return await _read_within_thumbnail_budget(hass, entry_id, coordinator, number, name)
    finally:
        places.release()


async def _read_within_thumbnail_budget(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, number: int, name: str
) -> tuple[bytes | None, str | None]:
    """The read of a taken place, its wait for the Doorstation lock included, within the thumbnail budget."""
    # Offline or unloaded while the place was awaited: no read for a picture that is not handed out.
    if not _still_offered(hass, entry_id, coordinator, number):
        _LOGGER.debug("Doorstation %s went offline or was unloaded while waiting for a gallery place", number)
        raise web.HTTPServiceUnavailable
    try:
        async with asyncio.timeout(DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC) as deadline:
            return await coordinator.api.get_doorstation_picture(
                number, name, held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
            )
    except TimeoutError:
        # A timeout from inside the read (socket or connect) is no budget overrun: not ours to answer as 503.
        if not deadline.expired():
            raise
        _LOGGER.debug(
            "Doorstation %s picture not answered within %s s (the read)",
            number,
            DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC,
        )
        raise web.HTTPServiceUnavailable from None


class DoorstationPictureView(HomeAssistantView):
    """One stored picture of a Doorstation as JPEG: the gallery's thumbnails and its playback."""

    url = DOORSTATION_PICTURE_VIEW_PATH + "/{entry_id}/{ext_id}/{name}"
    name = DOORSTATION_PICTURE_VIEW_NAME
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, ext_id: str, name: str) -> web.Response:
        hass = request.app[KEY_HASS]
        if not _is_ext_id(ext_id):
            raise web.HTTPNotFound
        number = int(ext_id)
        # The name is left out of the logs: it is the picture's, not the Doorstation's.
        loaded = _loaded(hass, entry_id)
        if loaded is None:
            _LOGGER.debug("Doorstation %s picture not served: entry not loaded", number)
            raise web.HTTPNotFound
        _entry, coordinator = loaded
        if not _offered(coordinator, number):
            _LOGGER.debug("Doorstation %s picture not served: Doorstation offline", number)
            raise web.HTTPNotFound
        if not _is_gallery_picture(name):
            _LOGGER.debug("Doorstation %s picture not served: not a bell or motion picture", number)
            raise web.HTTPNotFound
        # The gallery's places come first, so a browser batch queues at most one read ahead of the lock. The wait for a
        # place has DOORSTATION_GALLERY_PLACE_WAIT_SEC of its own: a thumbnail waiting its turn is not cut short.
        # Once a place is taken, the read has DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC in all, its wait for the
        # Doorstation lock included; the read itself holds the lock for at most DOORSTATION_PICTURE_READ_TIMEOUT_SEC.
        data, cause = await _read_in_a_gallery_place(hass, entry_id, coordinator, number, name)
        if not _still_offered(hass, entry_id, coordinator, number):
            # Offline or unloaded while the read waited: no picture is handed out, and 503 says a retry may succeed.
            _LOGGER.debug("Doorstation %s went offline or was unloaded during the picture read", number)
            raise web.HTTPServiceUnavailable
        if data is None:
            # Comexio's answer for a picture rotated out since the listing lands here as well (503, not 404).
            _LOGGER.debug("Doorstation %s picture not read: %s", number, cause or _SESSION_GONE_CAUSE)
            raise web.HTTPServiceUnavailable
        if not data:
            _LOGGER.debug("Doorstation %s picture is empty: answered as not found", number)
            raise web.HTTPNotFound
        # The bytes are a device's: nosniff and the sandbox policy keep them from running on Home Assistant's origin.
        return web.Response(body=data, content_type=_JPEG_MIME, headers=LIVE_STREAM_RESPONSE_HEADERS)
