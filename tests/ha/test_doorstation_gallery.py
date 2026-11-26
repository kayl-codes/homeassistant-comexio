"""The Doorstation gallery in the media browser: folders, newest pictures, signed thumbnails and the view."""

import asyncio
from collections.abc import AsyncGenerator, Generator
import copy
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from aiocomexio import DoorstationPicture, DoorstationPictureKind
from aiocomexio.doorstation import Doorstation
from homeassistant.components import media_source
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comexio.const import (
    DOMAIN,
    DOORSTATION_GALLERY_CONCURRENT_READS,
    DOORSTATION_GALLERY_MAX_PICTURES,
    DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
    DOORSTATION_PICTURE_VIEW_PATH,
)
from custom_components.comexio.media_source import _GALLERY_READ_SLOTS_KEY, _gallery_read_slots

pytestmark = pytest.mark.usefixtures("mock_comexio_api")

EXTENSION = "UD 1"
JPEG = b"\xff\xd8gallery\xff\xd9"
MEDIA_ROOT = f"media-source://{DOMAIN}"
TOO_LONG_EXT_ID = "1" * 5000  # more digits than int() takes (4300)
OTHER_PICTURE = DoorstationPicture(
    name="26-11-22-07-00-00_door.jpg", kind=DoorstationPictureKind.OTHER, taken_at=datetime(2026, 11, 22, 7, 0, 0)
)
MOTION_PICTURE = DoorstationPicture(
    name="26-11-21-19-00-00_pir.jpg", kind=DoorstationPictureKind.MOTION, taken_at=datetime(2026, 11, 21, 19, 0, 0)
)
NEW_BELL = DoorstationPicture(
    name="26-11-21-18-30-00_bell.jpg", kind=DoorstationPictureKind.BELL, taken_at=datetime(2026, 11, 21, 18, 30, 0)
)
OLD_BELL = DoorstationPicture(
    name="26-11-20-08-00-00_bell.jpg", kind=DoorstationPictureKind.BELL, taken_at=datetime(2026, 11, 20, 8, 0, 0)
)


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    """A Doorstation on extension 2 with three stored pictures of its kinds and one of another (listed newest first)."""
    api_returns["get_doorstations"] = ({2: Doorstation(ext_id=2, camera_id=7, name=EXTENSION)}, None)
    api_returns["get_doorstation_pictures"] = ((OTHER_PICTURE, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    api_returns["get_doorstation_picture"] = (JPEG, None)
    return api_returns


@pytest.fixture(autouse=True)
def _no_statistics_unit_fix_delay() -> Generator[None]:
    """The statistics unit fix sleeps before it starts; no test here needs that wait (as in the picture tests)."""
    with patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0):
        yield


@pytest.fixture(autouse=True)
async def _unload_when_done(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> AsyncGenerator[None]:
    """Unload the entry at the end, so no timer of the test outlives it."""
    yield
    if mock_config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, *, online: bool = True) -> Any:
    assert await async_setup_component(hass, "media_source", {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if online:
        # The fixture's IO data reports the Doorstation's extension offline; the gallery tests start with it online.
        coordinator.offline_extensions = set()
    return coordinator


def _folder_id(entry: MockConfigEntry) -> str:
    return f"{entry.entry_id}/2"


def _picture_id(entry: MockConfigEntry, name: str) -> str:
    return f"{entry.entry_id}/2/{name}"


def _picture_path(entry: MockConfigEntry, name: str) -> str:
    return f"{DOORSTATION_PICTURE_VIEW_PATH}/{entry.entry_id}/2/{name}"


class _SignallingPlaces(asyncio.Semaphore):
    """The gallery's places; `waiting` is set once a read has to wait for one, so a test knows the read stands there."""

    def __init__(self, waiting: asyncio.Event) -> None:
        super().__init__(DOORSTATION_GALLERY_CONCURRENT_READS)
        self.waiting = waiting

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


async def _take_all_places(hass: HomeAssistant, entry_id: str) -> tuple[_SignallingPlaces, asyncio.Event]:
    """Puts the entry's gallery places under the test's control: all taken, so the next read waits for one."""
    waiting = asyncio.Event()
    places = _SignallingPlaces(waiting)
    hass.data[f"{_GALLERY_READ_SLOTS_KEY}_{entry_id}"] = places
    for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
        await places.acquire()
    return places, waiting


def _release_places(places: asyncio.Semaphore) -> None:
    for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
        places.release()


# --- the browser ----------------------------------------------------------------------------------------------------


async def test_the_media_browser_lists_the_doorstation_as_a_folder(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    root = await media_source.async_browse_media(hass, MEDIA_ROOT)

    assert [child.identifier for child in root.children] == [_folder_id(mock_config_entry)]
    assert root.children[0].title == f"{EXTENSION} ({mock_config_entry.title})"


async def test_a_folder_lists_the_newest_bell_and_motion_pictures_first(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert [child.identifier for child in folder.children] == [
        _picture_id(mock_config_entry, "26-11-21-19-00-00_pir.jpg"),
        _picture_id(mock_config_entry, "26-11-21-18-30-00_bell.jpg"),
        _picture_id(mock_config_entry, "26-11-20-08-00-00_bell.jpg"),
    ]


async def test_a_picture_is_titled_with_its_kind_and_its_moment(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert [child.title for child in folder.children] == [
        "Motion 2026-11-21 19:00:00",
        "Doorbell 2026-11-21 18:30:00",
        "Doorbell 2026-11-20 08:00:00",
    ]


async def test_a_folder_keeps_to_the_cap_of_pictures(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)

    with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_MAX_PICTURES", 2):
        folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert [child.identifier for child in folder.children] == [
        _picture_id(mock_config_entry, "26-11-21-19-00-00_pir.jpg"),
        _picture_id(mock_config_entry, "26-11-21-18-30-00_bell.jpg"),
    ]


async def test_a_thumbnail_is_a_signed_link_to_the_picture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    thumbnail = folder.children[0].thumbnail
    assert thumbnail.startswith(f"{_picture_path(mock_config_entry, '26-11-21-19-00-00_pir.jpg')}?")
    assert "authSig=" in thumbnail


async def test_a_picture_resolves_to_its_view(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)

    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, '26-11-21-18-30-00_bell.jpg')}"

    play = await media_source.async_resolve_media(hass, media_id, None)

    assert play.url == _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    assert play.mime_type == "image/jpeg"


async def test_a_name_that_is_no_picture_does_not_resolve(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, 'x.png')}"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


@pytest.mark.parametrize(("cause", "reason"), [("timed out", "timed out"), (None, "unloading")])
async def test_a_folder_whose_pictures_cannot_be_read_is_an_error_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, cause: str | None, reason: str
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = (None, cause)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with pytest.raises(BrowseError, match=reason):
        await media_source.async_browse_media(hass, folder_id)


async def test_the_gallery_is_empty_once_the_entry_is_unloaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)

    root = await media_source.async_browse_media(hass, MEDIA_ROOT)

    assert root.children == []


@pytest.mark.parametrize("digit", ["²", "٢", "２"])
async def test_a_non_ascii_digit_is_no_folder_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, digit: str
) -> None:
    await _setup(hass, mock_config_entry)
    folder_id = f"{MEDIA_ROOT}/{mock_config_entry.entry_id}/{digit}"

    with pytest.raises(BrowseError, match="Unknown item"):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_number_too_long_for_an_extension_is_no_folder_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    folder_id = f"{MEDIA_ROOT}/{mock_config_entry.entry_id}/{TOO_LONG_EXT_ID}"

    with pytest.raises(BrowseError, match="Unknown item"):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_doorstation_that_is_not_known_is_no_folder(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    folder_id = f"{MEDIA_ROOT}/{mock_config_entry.entry_id}/9"

    with pytest.raises(BrowseError, match="not known"):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_folder_of_an_unloaded_entry_is_an_error_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with pytest.raises(BrowseError, match="not loaded"):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_root_without_a_known_doorstation_is_empty(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)
    await _setup(hass, mock_config_entry)

    root = await media_source.async_browse_media(hass, MEDIA_ROOT)

    assert root.children == []


async def test_a_picture_without_a_moment_comes_last_and_is_titled_by_its_name(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    # A name with an impossible moment: still a bell picture by its name, but no moment to sort by.
    undated = DoorstationPicture(name="26-13-40-25-61-61_bell.jpg", kind=DoorstationPictureKind.BELL, taken_at=None)
    api_returns["get_doorstation_pictures"] = ((undated, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    last = folder.children[-1]
    assert last.identifier == _picture_id(mock_config_entry, "26-13-40-25-61-61_bell.jpg")
    assert last.title == "Doorbell (26-13-40-25-61-61_bell.jpg)"


async def test_a_name_that_is_no_plain_picture_name_is_not_listed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    moment = datetime(2026, 11, 22, 9, 0, 0)
    tricky = (
        DoorstationPicture(name="a/b.jpg", kind=DoorstationPictureKind.BELL, taken_at=moment),
        DoorstationPicture(name="../z.jpg", kind=DoorstationPictureKind.BELL, taken_at=moment),
        NEW_BELL,
    )
    api_returns["get_doorstation_pictures"] = (tricky, None)
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert [child.identifier for child in folder.children] == [
        _picture_id(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    ]


async def test_a_folder_shows_only_the_cap_of_the_newest_pictures_of_a_longer_list(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    count = DOORSTATION_GALLERY_MAX_PICTURES + 5
    moments = [datetime(2026, 11, 1) + timedelta(hours=hour) for hour in range(count)]
    oldest_first = tuple(
        DoorstationPicture(
            name=f"{moment:%y-%m-%d-%H-%M-%S}_bell.jpg", kind=DoorstationPictureKind.BELL, taken_at=moment
        )
        for moment in moments
    )
    api_returns["get_doorstation_pictures"] = (oldest_first, None)
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    newest_first = [f"{moment:%y-%m-%d-%H-%M-%S}_bell.jpg" for moment in reversed(moments)]
    assert [child.identifier for child in folder.children] == [
        _picture_id(mock_config_entry, name) for name in newest_first[:DOORSTATION_GALLERY_MAX_PICTURES]
    ]


async def test_a_signed_thumbnail_is_served_without_a_login(
    hass: HomeAssistant, hass_client_no_auth: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")
    client = await hass_client_no_auth()

    response = await client.get(folder.children[0].thumbnail)

    assert response.status == HTTPStatus.OK
    assert await response.read() == JPEG


async def test_a_resolve_for_an_unloaded_entry_is_unresolvable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, '26-11-21-18-30-00_bell.jpg')}"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


async def test_a_picture_of_a_doorstation_that_is_not_known_does_not_resolve(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    media_id = f"{MEDIA_ROOT}/{mock_config_entry.entry_id}/9/26-11-21-18-30-00_bell.jpg"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


# --- the view -------------------------------------------------------------------------------------------------------


async def test_the_view_serves_a_picture_to_a_logged_in_client(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.OK
    assert await response.read() == JPEG
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox"
    coordinator.api.get_doorstation_picture.assert_awaited_with(
        2, "26-11-21-18-30-00_bell.jpg", held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
    )


async def test_the_view_refuses_a_client_that_is_not_logged_in(
    hass: HomeAssistant, hass_client_no_auth: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client_no_auth()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("name", ["26-11-22-07-00-00_door.jpg", "26-11-22-07-00-00_shot.jpg", "bell_new.jpg"])
async def test_a_picture_of_another_kind_does_not_resolve(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, name: str
) -> None:
    await _setup(hass, mock_config_entry)

    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, name)}"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


@pytest.mark.parametrize("name", ["26-11-22-07-00-00_door.jpg", "26-11-22-07-00-00_shot.jpg", "bell_new.jpg"])
async def test_the_view_answers_not_found_for_a_picture_of_another_kind(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, name: str
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, name))

    assert response.status == HTTPStatus.NOT_FOUND
    coordinator.api.get_doorstation_picture.assert_not_awaited()


@pytest.mark.parametrize(
    "name", ["26-11-21-19-00-00_pir.jpg", "26-11-21-19-00-00_PIR.jpg", "26-13-40-25-61-61_bell.jpg"]
)
async def test_a_motion_picture_and_an_undated_bell_picture_resolve_and_are_served(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, name: str
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    client = await hass_client()
    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, name)}"

    play = await media_source.async_resolve_media(hass, media_id, None)
    response = await client.get(_picture_path(mock_config_entry, name))

    assert play.url == _picture_path(mock_config_entry, name)
    assert response.status == HTTPStatus.OK
    assert await response.read() == JPEG
    coordinator.api.get_doorstation_picture.assert_awaited_with(
        2, name, held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
    )


async def test_every_picture_the_folder_lists_resolves_and_is_served(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    client = await hass_client()
    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert folder.children
    names = [child.identifier.rsplit("/", 1)[-1] for child in folder.children]
    # Distinct listed names, so the per-picture asserts below cannot all pass on one and the same picture.
    assert len(set(names)) == len(names)
    for child, name in zip(folder.children, names, strict=True):
        play = await media_source.async_resolve_media(hass, f"{MEDIA_ROOT}/{child.identifier}", None)
        # Each listed picture must resolve to its own view path, not merely to some served one.
        assert play.url == _picture_path(mock_config_entry, name)
        response = await client.get(play.url)
        assert response.status == HTTPStatus.OK
        # The view read this picture's own name from the Doorstation (the fake serves the same bytes for every name).
        coordinator.api.get_doorstation_picture.assert_any_await(
            2, name, held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
        )


async def test_an_offline_doorstation_is_neither_resolved_nor_served(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.offline_extensions = {EXTENSION}
    client = await hass_client()
    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, '26-11-21-18-30-00_bell.jpg')}"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)
    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.NOT_FOUND
    coordinator.api.get_doorstation_picture.assert_not_awaited()


async def test_a_folder_whose_extension_goes_offline_during_the_list_read_is_not_listed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _goes_offline(*_args: Any) -> tuple[tuple[DoorstationPicture, ...], None]:
        coordinator.offline_extensions = {EXTENSION}
        return (NEW_BELL,), None

    coordinator.api.get_doorstation_pictures.side_effect = _goes_offline
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with pytest.raises(BrowseError, match="offline or no longer known"):
        await media_source.async_browse_media(hass, folder_id)
    coordinator.api.get_doorstation_pictures.assert_awaited_once()


async def test_a_folder_whose_entry_is_unloaded_during_the_list_read_is_not_listed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _unloaded(*_args: Any) -> tuple[tuple[DoorstationPicture, ...], None]:
        # What an unload does: the entry leaves hass.data while the list read is under way.
        hass.data[DOMAIN].pop(mock_config_entry.entry_id)
        return (NEW_BELL,), None

    coordinator.api.get_doorstation_pictures.side_effect = _unloaded
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with pytest.raises(BrowseError, match="no longer known"):
        await media_source.async_browse_media(hass, folder_id)
    coordinator.api.get_doorstation_pictures.assert_awaited_once()


async def test_the_view_answers_unavailable_when_the_doorstation_goes_offline_during_the_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _goes_offline(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        coordinator.offline_extensions = {EXTENSION}
        return b"\xff\xd8", None

    coordinator.api.get_doorstation_picture.side_effect = _goes_offline
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_answers_unavailable_when_the_entry_is_reloaded_during_the_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _reloaded(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        # A reload brings a new coordinator under the same entry id; the read's own coordinator no longer counts.
        hass.data[DOMAIN][mock_config_entry.entry_id] = copy.copy(coordinator)
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _reloaded
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_answers_unavailable_when_the_entry_is_unloaded_during_the_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _unloaded(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        # What an unload does: the entry leaves hass.data while the read is under way; the picture read before stays.
        hass.data[DOMAIN].pop(mock_config_entry.entry_id)
        return b"\xff\xd8", None

    coordinator.api.get_doorstation_picture.side_effect = _unloaded
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_answers_not_found_for_a_name_that_is_no_picture(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "not_a_picture.png"))

    assert response.status == HTTPStatus.NOT_FOUND


async def test_the_view_answers_unavailable_when_the_picture_cannot_be_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (None, "timed out")
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_answers_not_found_for_an_empty_picture(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (b"", None)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.NOT_FOUND


async def test_the_view_bounds_the_lock_hold_of_its_read_and_answers_what_comes_back(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (None, "not read within 8 s")
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    coordinator.api.get_doorstation_picture.assert_awaited_once_with(
        2, "26-11-21-18-30-00_bell.jpg", held_for_sec=DOORSTATION_PICTURE_READ_TIMEOUT_SEC
    )
    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


def test_the_thumbnail_budget_outlasts_the_picture_read() -> None:
    # The thumbnail budget covers the wait for the lock as well as the read, so it must exceed the read's own bound.
    assert DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC > DOORSTATION_PICTURE_READ_TIMEOUT_SEC


async def test_the_view_serves_a_read_that_finishes_within_its_budget(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _slow_read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        await asyncio.sleep(0.2)
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _slow_read
    client = await hass_client()

    # The read outlasts the picture budget (0.05 s) but not the thumbnail budget (0.5 s): only the latter applies.
    with (
        patch("custom_components.comexio.media_source.DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.05),
        patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 0.5),
    ):
        response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.OK
    assert await response.read() == JPEG


async def test_a_thumbnail_queued_past_the_budget_answers_unavailable(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _stuck_read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        # Stands for the wait behind a read that holds the lock: the budget covers that wait too.
        await asyncio.sleep(60)
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _stuck_read
    client = await hass_client()

    with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 0.05):
        response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    coordinator.api.get_doorstation_picture.assert_awaited_once()
    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_a_browser_batch_takes_only_a_couple_of_places_on_the_doorstation_lock(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    running = 0
    most_at_once = 0
    places_full = asyncio.Event()

    async def _read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        if running == DOORSTATION_GALLERY_CONCURRENT_READS:
            places_full.set()
        # The first reads wait until all places run, then hold theirs: the overlap does not depend on timing.
        async with asyncio.timeout(5):
            await places_full.wait()
        await asyncio.sleep(0.05)
        running -= 1
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _read
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")

    responses = await asyncio.gather(*(client.get(path) for _ in range(5)))

    assert [response.status for response in responses] == [HTTPStatus.OK] * 5
    assert most_at_once == DOORSTATION_GALLERY_CONCURRENT_READS


async def test_a_browse_batch_takes_only_a_couple_of_places_on_the_doorstation_lock(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    running = 0
    most_at_once = 0
    places_full = asyncio.Event()

    async def _list(*_args: Any, **_kwargs: Any) -> tuple[tuple[DoorstationPicture, ...], None]:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        if running == DOORSTATION_GALLERY_CONCURRENT_READS:
            places_full.set()
        # As in the thumbnail batch: the first reads wait until all places run; the overlap does not depend on timing.
        async with asyncio.timeout(5):
            await places_full.wait()
        await asyncio.sleep(0.05)
        running -= 1
        return (OTHER_PICTURE, MOTION_PICTURE, NEW_BELL, OLD_BELL), None

    coordinator.api.get_doorstation_pictures.side_effect = _list
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    folders = await asyncio.gather(*(media_source.async_browse_media(hass, folder) for _ in range(5)))

    assert all(len(folder_item.children) == 3 for folder_item in folders)
    assert most_at_once == DOORSTATION_GALLERY_CONCURRENT_READS


async def test_unloading_the_entry_drops_its_gallery_places(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    _gallery_read_slots(hass, mock_config_entry.entry_id)

    await hass.config_entries.async_unload(mock_config_entry.entry_id)

    assert f"{_GALLERY_READ_SLOTS_KEY}_{mock_config_entry.entry_id}" not in hass.data


async def test_a_thumbnail_waiting_for_a_gallery_place_past_the_budget_answers_unavailable(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places = _gallery_read_slots(hass, mock_config_entry.entry_id)
    for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
        await places.acquire()
    client = await hass_client()
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 0.05):
            # Every place is taken: the request must still answer within its budget, not wait for a place forever.
            async with asyncio.timeout(5):
                response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))
    finally:
        for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
            places.release()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    coordinator.api.get_doorstation_picture.assert_not_awaited()


async def test_a_thumbnail_whose_doorstation_goes_offline_while_waiting_for_a_gallery_place_is_not_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    request = asyncio.ensure_future(client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")))
    try:
        async with asyncio.timeout(5):
            await waiting.wait()
        coordinator.offline_extensions = {EXTENSION}
        _release_places(places)
        async with asyncio.timeout(5):
            response = await request
    finally:
        request.cancel()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    coordinator.api.get_doorstation_picture.assert_not_awaited()


async def test_a_thumbnail_whose_entry_is_reloaded_while_waiting_for_a_gallery_place_is_not_read(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    request = asyncio.ensure_future(client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")))
    try:
        async with asyncio.timeout(5):
            await waiting.wait()
        # A reload brings a new coordinator under the same entry id; the waiting read's own one no longer counts.
        hass.data[DOMAIN][mock_config_entry.entry_id] = copy.copy(coordinator)
        _release_places(places)
        async with asyncio.timeout(5):
            response = await request
    finally:
        request.cancel()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    coordinator.api.get_doorstation_picture.assert_not_awaited()


async def test_a_timeout_from_inside_the_read_is_no_budget_overrun(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    # A socket or connect timeout of the read itself: the view's budget did not run out, so it is not a 503. aiohttp
    # answers an unhandled TimeoutError with 504 and logs its traceback.
    coordinator.api.get_doorstation_picture.side_effect = TimeoutError
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.GATEWAY_TIMEOUT


async def test_an_offline_doorstation_is_neither_listed_nor_browsed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry, online=False)

    # The fixture's own IO data reports the extension offline: the gallery goes by that, not by a test override.
    assert EXTENSION in coordinator.offline_extensions
    root = await media_source.async_browse_media(hass, MEDIA_ROOT)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    assert root.children == []
    with pytest.raises(BrowseError, match="offline"):
        await media_source.async_browse_media(hass, folder_id)
    coordinator.api.get_doorstation_pictures.assert_not_awaited()


async def test_an_extension_whose_offline_state_is_not_known_yet_is_offered(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.offline_extensions = None

    root = await media_source.async_browse_media(hass, MEDIA_ROOT)

    assert [child.identifier for child in root.children] == [_folder_id(mock_config_entry)]


async def test_a_doorstation_without_a_known_extension_name_is_neither_listed_nor_browsed_nor_served(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    client = await hass_client()

    # No name from the IO data or the Doorstation: the offline set cannot be checked, so the gallery offers nothing.
    with patch("custom_components.comexio.media_source.doorstation_ext_name", return_value=None):
        root = await media_source.async_browse_media(hass, MEDIA_ROOT)
        with pytest.raises(BrowseError, match="offline"):
            await media_source.async_browse_media(hass, folder_id)
        response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert root.children == []
    assert response.status == HTTPStatus.NOT_FOUND
    coordinator.api.get_doorstation_pictures.assert_not_awaited()
    coordinator.api.get_doorstation_picture.assert_not_awaited()


async def test_a_timeout_raised_by_the_list_read_itself_is_not_taken_for_the_folders_deadline(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.side_effect = TimeoutError
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with pytest.raises(TimeoutError):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_folder_whose_list_takes_too_long_is_an_error_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _never_ends(*_args: Any) -> None:
        await asyncio.sleep(60)

    coordinator.api.get_doorstation_pictures.side_effect = _never_ends
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    with (
        patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_TIMEOUT_SEC", 0.01),
        pytest.raises(BrowseError, match="not read in"),
    ):
        await media_source.async_browse_media(hass, folder_id)


async def test_a_folder_waiting_for_a_gallery_place_past_the_budget_is_an_error_in_the_browser(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places = _gallery_read_slots(hass, mock_config_entry.entry_id)
    for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
        await places.acquire()
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_TIMEOUT_SEC", 0.05):
            # Every place is taken: the folder must answer within its deadline, not wait for a place forever.
            async with asyncio.timeout(5):
                with pytest.raises(BrowseError, match="not read in"):
                    await media_source.async_browse_media(hass, folder_id)
    finally:
        for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
            places.release()

    coordinator.api.get_doorstation_pictures.assert_not_awaited()


async def test_a_folder_that_goes_offline_while_waiting_for_a_gallery_place_is_not_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    browse = asyncio.ensure_future(media_source.async_browse_media(hass, folder_id))
    try:
        async with asyncio.timeout(5):
            await waiting.wait()
        coordinator.offline_extensions = {EXTENSION}
        _release_places(places)
        async with asyncio.timeout(5):
            with pytest.raises(BrowseError, match="offline or no longer known"):
                await browse
    finally:
        browse.cancel()

    coordinator.api.get_doorstation_pictures.assert_not_awaited()


async def test_a_folder_whose_entry_is_unloaded_while_waiting_for_a_gallery_place_is_not_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    browse = asyncio.ensure_future(media_source.async_browse_media(hass, folder_id))
    try:
        async with asyncio.timeout(5):
            await waiting.wait()
        # What an unload does: the entry leaves hass.data while the browse waits for its place.
        hass.data[DOMAIN].pop(mock_config_entry.entry_id)
        _release_places(places)
        async with asyncio.timeout(5):
            with pytest.raises(BrowseError, match="no longer known"):
                await browse
    finally:
        browse.cancel()

    coordinator.api.get_doorstation_pictures.assert_not_awaited()


@pytest.mark.parametrize("digit", ["²", "٢", "２", TOO_LONG_EXT_ID])
async def test_the_view_answers_not_found_for_a_digit_that_is_no_extension(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, digit: str
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(
        f"{DOORSTATION_PICTURE_VIEW_PATH}/{mock_config_entry.entry_id}/{digit}/26-11-21-18-30-00_bell.jpg"
    )

    assert response.status == HTTPStatus.NOT_FOUND


async def test_the_view_answers_not_found_for_a_doorstation_that_is_not_known(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(
        f"{DOORSTATION_PICTURE_VIEW_PATH}/{mock_config_entry.entry_id}/9/26-11-21-18-30-00_bell.jpg"
    )

    assert response.status == HTTPStatus.NOT_FOUND
