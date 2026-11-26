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
from datetime import datetime, timedelta
import logging
from typing import NamedTuple

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
    DOORSTATION_GALLERY_LIST_TIMEOUT_SEC,
    DOORSTATION_GALLERY_MAX_PICTURES,
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


def forget_gallery_read_slots(hass: HomeAssistant, entry_id: str) -> None:
    """Drops the places of an unloaded entry. A request that still waits keeps its own reference to them; a new one
    finds the entry unloaded before it reaches for a place (the views and the browser check that first)."""
    hass.data.pop(f"{_GALLERY_READ_SLOTS_KEY}_{entry_id}", None)


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
    """Whether the gallery offers a Doorstation: known, and its extension not offline (the picture entities' rule)."""
    if ext_id not in coordinator.doorstations.known:
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


async def _folder_children(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, ext_id: int
) -> list[BrowseMediaSource]:
    # The list read takes a gallery place like a thumbnail does: a browse batch must not queue on the Doorstation lock
    # ahead of the camera and the picture entities either. The wait for the place counts into the list's deadline.
    try:
        async with (
            asyncio.timeout(DOORSTATION_GALLERY_LIST_TIMEOUT_SEC) as deadline,
            _gallery_read_slots(hass, entry_id),
        ):
            # Offline or unloaded while the place was awaited: no list read takes the Doorstation lock for nothing.
            if not _still_offered(hass, entry_id, coordinator, ext_id):
                raise BrowseError(_OFFLINE_OR_UNKNOWN)
            pictures, cause = await coordinator.api.get_doorstation_pictures(ext_id)
    except TimeoutError:
        if not deadline.expired():
            raise
        raise BrowseError(f"Doorstation pictures not read in {DOORSTATION_GALLERY_LIST_TIMEOUT_SEC:g} s") from None
    if pictures is None:
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
        # The read, its wait for the Doorstation lock included, has DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC in all: a
        # thumbnail queued behind a stuck read must not keep its request open. The gallery's own places come first, so a
        # browser batch queues on the lock two reads deep at most. Once the lock is taken, the read holds it for at most
        # DOORSTATION_PICTURE_READ_TIMEOUT_SEC (held_for_sec); the budget ends the read earlier if the wait used it up.
        slots = _gallery_read_slots(hass, entry_id)
        try:
            async with asyncio.timeout(DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC) as deadline, slots:
                # Offline or unloaded while the place was awaited: no read for a picture that is not handed out.
                if not _still_offered(hass, entry_id, coordinator, number):
                    _LOGGER.debug(
                        "Doorstation %s went offline or was unloaded while waiting for a gallery place", number
                    )
                    raise web.HTTPServiceUnavailable
                data, cause = await coordinator.api.get_doorstation_picture(
                    number, name, held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
                )
        except TimeoutError:
            # A timeout from inside the read (socket or connect) is no budget overrun: not ours to answer as 503.
            if not deadline.expired():
                raise
            # The wait for a gallery place and the read both count: the line does not tell which one ran out.
            _LOGGER.debug(
                "Doorstation %s picture not answered within %s s (a place or the read)",
                number,
                DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC,
            )
            raise web.HTTPServiceUnavailable from None
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
