"""The Doorstation pictures as a gallery in the media browser (Media > Browse media).

Each loaded entry offers its Doorstations as folders in the media source "comexio". A folder lists the newest bell and
motion pictures its Doorstation stored (``DOORSTATION_GALLERY_MAX_PICTURES``), each with its JPEG as thumbnail. The list
and the pictures come from the Doorstation through ComexioAPI, the reads the picture entities use too. Nothing is kept
here: a thumbnail is read when the browser asks for it.

The thumbnail links are signed for the connection that browses (``async_sign_path``), and the view that serves a
picture needs the login as well: the pictures show only to someone who may browse the media.
"""

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta
import logging
from typing import NamedTuple

from aiocomexio import DoorstationPicture, DoorstationPictureKind
from aiocomexio.doorstation_pictures import is_plain_picture_name
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
    DOORSTATION_GALLERY_LINK_EXPIRY_SEC,
    DOORSTATION_GALLERY_MAX_PICTURES,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
    DOORSTATION_PICTURE_VIEW_NAME,
    DOORSTATION_PICTURE_VIEW_PATH,
    one_line,
)
from .coordinator import ComexioCoordinator
from .doorstation import doorstation_ext_name
from .doorstation_pictures import PICTURE_KINDS

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


def _doorstation_title(entry: ConfigEntry, coordinator: ComexioCoordinator, ext_id: int) -> str:
    doorstation = coordinator.doorstations.known.get(ext_id)
    ios = (coordinator.data or {}).get("io", [])
    name = doorstation_ext_name(doorstation, ios) if doorstation is not None else None
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


async def _folder_children(
    hass: HomeAssistant, entry_id: str, coordinator: ComexioCoordinator, ext_id: int
) -> list[BrowseMediaSource]:
    pictures, cause = await coordinator.api.get_doorstation_pictures(ext_id)
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
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=item.identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=_doorstation_title(entry, coordinator, parsed.ext_id),
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.IMAGE,
            children=await _folder_children(self.hass, entry.entry_id, coordinator, parsed.ext_id),
        )

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parsed = _parse(item.identifier)
        if (
            parsed is None
            or parsed.name is None
            or not is_plain_picture_name(parsed.name)
            or not _is_known(_loaded(self.hass, parsed.entry_id), parsed.ext_id)
        ):
            raise Unresolvable("Could not resolve this media item")
        return PlayMedia(_view_path(parsed.entry_id, parsed.ext_id, parsed.name), _JPEG_MIME)


async def async_get_media_source(hass: HomeAssistant) -> DoorstationGallery:
    """Set up the Doorstation gallery as a media source (the media_source component asks for it)."""
    return DoorstationGallery(hass)


class DoorstationPictureView(HomeAssistantView):
    """One stored picture of a Doorstation as JPEG: the gallery's thumbnails and its playback."""

    url = DOORSTATION_PICTURE_VIEW_PATH + "/{entry_id}/{ext_id}/{name}"
    name = DOORSTATION_PICTURE_VIEW_NAME
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, ext_id: str, name: str) -> web.Response:
        loaded = _loaded(request.app[KEY_HASS], entry_id)
        if loaded is None or not _is_ext_id(ext_id) or not is_plain_picture_name(name):
            raise web.HTTPNotFound
        number = int(ext_id)
        if not _is_known(loaded, number):
            raise web.HTTPNotFound
        _entry, coordinator = loaded
        try:
            async with asyncio.timeout(DOORSTATION_PICTURE_READ_TIMEOUT_SEC) as deadline:
                data, cause = await coordinator.api.get_doorstation_picture(number, name)
        except TimeoutError:
            if not deadline.expired():
                # A timeout from inside the read, not this view's own deadline: it is no slow Doorstation.
                raise
            _LOGGER.debug("Doorstation %s picture not read: timed out", number)
            raise web.HTTPGatewayTimeout from None
        if data is None:
            # Comexio's answer for a picture rotated out since the listing lands here as well (503, not 404).
            _LOGGER.debug("Doorstation %s picture not read: %s", number, cause or _SESSION_GONE_CAUSE)
            raise web.HTTPServiceUnavailable
        if not data:
            raise web.HTTPNotFound
        return web.Response(body=data, content_type=_JPEG_MIME)
