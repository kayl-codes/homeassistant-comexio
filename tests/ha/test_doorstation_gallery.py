"""The Doorstation gallery in the media browser: folders, newest pictures, signed thumbnails and the view."""

import asyncio
from collections.abc import AsyncGenerator, Generator
import contextlib
import copy
from datetime import datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import PropertyMock, patch

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

from custom_components.comexio.api import doorstation_read_lock, forget_doorstation_read_lock
from custom_components.comexio.const import (
    DOMAIN,
    DOORSTATION_GALLERY_CONCURRENT_READS,
    DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_LIST_TIMEOUT_SEC,
    DOORSTATION_GALLERY_MAX_PICTURES,
    DOORSTATION_GALLERY_MAX_WAITING_READS,
    DOORSTATION_GALLERY_PLACE_WAIT_SEC,
    DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC,
    DOORSTATION_PICTURE_READ_TIMEOUT_SEC,
    DOORSTATION_PICTURE_VIEW_PATH,
)
from custom_components.comexio.media_source import (
    _GALLERY_READ_SLOTS_KEY,
    _gallery_read_slots,
    _gallery_waiting,
    _kept_gallery_waiting,
    _place_granted,
    _refused_for_a_place,
    _warn_slow_place_wait,
)

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
    hass: HomeAssistant, api_returns: dict[str, Any], mock_config_entry: MockConfigEntry
) -> None:
    # Not the fixture's newest-first order: the expected order must come from the gallery's own sort.
    api_returns["get_doorstation_pictures"] = ((OLD_BELL, OTHER_PICTURE, NEW_BELL, MOTION_PICTURE), None)
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


async def test_the_view_gives_up_at_the_thumbnail_budget_not_at_the_picture_budget(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _endless_read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        await asyncio.sleep(1)
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _endless_read
    client = await hass_client()

    # The read would be allowed the picture budget (5 s), so only the thumbnail budget (0.05 s) can end it here.
    with (
        patch("custom_components.comexio.media_source.DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 5),
        patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 0.05),
    ):
        response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_serves_a_read_that_finishes_within_its_budget(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _slow_read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        await asyncio.sleep(0.2)
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _slow_read
    client = await hass_client()

    # The read outlasts the picture budget (0.05 s) but not the thumbnail budget (5 s): only the latter applies.
    # The thumbnail budget leaves wide room over the 0.2 s read, so a loaded event loop cannot turn it into a 503.
    with (
        patch("custom_components.comexio.media_source.DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.05),
        patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 5),
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
    # The place is free again once the budget ran out: DOORSTATION_GALLERY_PLACE_WAIT_SEC counts on that.
    assert not _gallery_read_slots(hass, mock_config_entry.entry_id).locked()


async def test_a_thumbnail_blocked_on_the_doorstation_lock_past_the_budget_answers_unavailable(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    doorstation_lock = asyncio.Lock()
    # Set as the read reaches the lock: a 503 that came earlier than the lock wait never gets here.
    at_the_lock = asyncio.Event()

    async def _read_behind_the_lock(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        # The Doorstation lock is what the real read waits for; another read holds it for as long as the test says.
        at_the_lock.set()
        async with doorstation_lock:
            return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _read_behind_the_lock
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")

    await doorstation_lock.acquire()
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 0.05):
            request = asyncio.ensure_future(client.get(path))
            try:
                async with asyncio.timeout(5):
                    await at_the_lock.wait()
                async with asyncio.timeout(10):
                    response = await request
            finally:
                request.cancel()
    finally:
        doorstation_lock.release()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    coordinator.api.get_doorstation_picture.assert_awaited_once()


async def test_a_browser_batch_reads_one_picture_at_a_time_on_the_doorstation_lock(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    running = 0
    most_at_once = 0

    async def _read(*_args: Any, **_kwargs: Any) -> tuple[bytes, None]:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        await asyncio.sleep(0.05)
        running -= 1
        return JPEG, None

    coordinator.api.get_doorstation_picture.side_effect = _read
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")

    responses = await asyncio.gather(*(client.get(path) for _ in range(5)))

    assert [response.status for response in responses] == [HTTPStatus.OK] * 5
    # One place: the camera waits behind one read at most, so the batch never runs two reads on the lock at once.
    assert most_at_once == 1


async def test_a_browse_batch_reads_one_list_at_a_time_on_the_doorstation_lock(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    running = 0
    most_at_once = 0

    async def _list(*_args: Any, **_kwargs: Any) -> tuple[tuple[DoorstationPicture, ...], None]:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        await asyncio.sleep(0.05)
        running -= 1
        return (OTHER_PICTURE, MOTION_PICTURE, NEW_BELL, OLD_BELL), None

    coordinator.api.get_doorstation_pictures.side_effect = _list
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"

    folders = await asyncio.gather(*(media_source.async_browse_media(hass, folder) for _ in range(5)))

    assert all(len(folder_item.children) == 3 for folder_item in folders)
    assert most_at_once == 1


async def test_a_list_read_that_gets_its_place_at_once_ends_the_slow_streak(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A place that comes within the slow mark is the places flowing again, for a list read as for a thumbnail. The
    # other thumbnail is only counted here: it stays queued, so the empty-queue rule cannot end the streak instead.
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = ((OTHER_PICTURE,), None)
    waiting = _gallery_waiting(hass, mock_config_entry.entry_id)
    waiting.slow = True
    waiting.refusing = True
    waiting.count = 1

    with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC", 0.05):
        await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")
        # The place came at once: a timer left running would warn after its mark, so wait past it before looking.
        await asyncio.sleep(0.2)

    assert not waiting.slow
    assert not waiting.refusing
    assert not [r for r in caplog.records if "still waits" in r.getMessage()]


async def test_a_list_read_waiting_past_the_slow_mark_is_warned_while_it_waits(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The folder list waits for a place like a thumbnail does: the stall is logged while the list still waits, and only
    # once its own mark has passed (a warning before it would be a thumbnail's mark leaking into the list).
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = ((OTHER_PICTURE,), None)
    places, _waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    loop = asyncio.get_running_loop()
    browse = asyncio.ensure_future(
        media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")
    )
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC", 0.2):
            started = loop.time()
            async with asyncio.timeout(5):
                while not any(r.levelname == "WARNING" and "still waits" in r.getMessage() for r in caplog.records):
                    await asyncio.sleep(0.01)
            # The mark is 0.2 s; the loop's clock may run up to one tick (about 16 ms on Windows) early: the margin.
            assert loop.time() - started >= 0.17
            assert not browse.done()
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        await browse


def test_the_slow_marks_lie_within_the_waits_they_warn_in() -> None:
    # A mark past its wait never warns: the wait ends (a refusal or a timeout) first. The folder list had such a mark
    # while it shared the thumbnails' one, so its browse warning could not come.
    assert DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC < DOORSTATION_GALLERY_LIST_TIMEOUT_SEC
    assert DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC < DOORSTATION_GALLERY_PLACE_WAIT_SEC


async def test_a_browse_cancelled_while_it_waits_leaves_no_slow_timer_behind(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A browser that leaves while its folder list waits must take the slow-wait timer with it: a leaked timer would warn
    # about a wait that no longer exists. The mark is short here, so a leaked timer fires inside the test.
    await _setup(hass, mock_config_entry)
    places, _waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    waiting = _gallery_waiting(hass, mock_config_entry.entry_id)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    browse = asyncio.ensure_future(media_source.async_browse_media(hass, folder))
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC", 0.2):
            async with asyncio.timeout(5):
                while waiting.count < 1:
                    await asyncio.sleep(0.01)
            browse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await browse
            await asyncio.sleep(0.3)
    finally:
        _release_places(places)
    assert waiting.count == 0
    assert not waiting.slow
    assert not [r for r in caplog.records if "still waits" in r.getMessage()]


async def test_a_folder_list_that_stalls_with_its_place_is_logged_and_told_apart(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The place was held, so the wait was not the cause: the read itself ran out of time, and that is logged once.
    coordinator = await _setup(hass, mock_config_entry)

    async def _stall(_ext_id: int) -> None:
        await asyncio.Event().wait()

    coordinator.api.get_doorstation_pictures.side_effect = _stall
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    with (
        patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_TIMEOUT_SEC", 0.2),
        pytest.raises(BrowseError, match=r"not read in .* s \(the Doorstation did not answer in time\)"),
    ):
        await media_source.async_browse_media(hass, folder)
    coordinator.api.get_doorstation_pictures.assert_awaited()
    assert any(r.levelname == "WARNING" and "timed out after" in r.getMessage() for r in caplog.records)
    assert all("no place within" not in r.getMessage() for r in caplog.records)


async def test_a_folder_list_grant_is_judged_by_the_list_mark(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    # A stall the folder list sees ends only when the places flow again within the list's own mark. A list read granted
    # after a longer wait, while another read still queues, keeps the stall: judged by the thumbnails' much longer mark,
    # the grant would end it, and the next slow wait would warn again.
    coordinator = await _setup(hass, mock_config_entry)
    granted_read = asyncio.Event()

    async def _held_read(_ext_id: int) -> tuple[tuple[DoorstationPicture, ...], None]:
        granted_read.set()
        await asyncio.Event().wait()
        return (OTHER_PICTURE,), None

    coordinator.api.get_doorstation_pictures.side_effect = _held_read
    places, _queued = await _take_all_places(hass, mock_config_entry.entry_id)
    waiting = _gallery_waiting(hass, mock_config_entry.entry_id)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    first = asyncio.ensure_future(media_source.async_browse_media(hass, folder))
    second = asyncio.ensure_future(media_source.async_browse_media(hass, folder))
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_SLOW_PLACE_WAIT_SEC", 0.05):
            async with asyncio.timeout(5):
                while waiting.count < 2 or not waiting.slow:
                    await asyncio.sleep(0.01)
            # Both marks have passed by now: the grant comes after a wait well past the list mark, not on its edge.
            await asyncio.sleep(0.1)
            _release_places(places)
            async with asyncio.timeout(5):
                await granted_read.wait()
        assert waiting.count == 1
        assert waiting.slow
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_a_failed_folder_list_read_is_logged_by_the_browse(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The read fails without the browse's deadline expiring (here: the read's own budget, as the API words it) and
    # returns only its cause: the API only debug-logs that, so the browse logs it.
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = (None, "not read within 10 s")
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    with pytest.raises(BrowseError, match="not read within 10 s"):
        await media_source.async_browse_media(hass, folder)
    assert any(r.levelname == "WARNING" and "picture list not read" in r.getMessage() for r in caplog.records)


async def test_a_folder_list_read_with_the_session_gone_is_not_logged(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A session that is gone (unload, Home Assistant stopping) answers None without a cause: nothing to log.
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = (None, None)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    with pytest.raises(BrowseError, match="not read"):
        await media_source.async_browse_media(hass, folder)
    assert all("picture list not read" not in r.getMessage() for r in caplog.records)


async def test_a_folder_list_that_gets_no_place_in_time_is_logged_as_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass, mock_config_entry)
    places, _waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    try:
        with (
            patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_LIST_TIMEOUT_SEC", 0.2),
            pytest.raises(BrowseError, match=r"not read in .* s \(the wait for a gallery place included\)"),
        ):
            await media_source.async_browse_media(hass, folder)
    finally:
        _release_places(places)
    assert any(r.levelname == "WARNING" and "no place within" in r.getMessage() for r in caplog.records)


async def test_a_folder_list_at_the_waiter_cap_is_logged_as_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass, mock_config_entry)
    waiting = _gallery_waiting(hass, mock_config_entry.entry_id)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    waiting.count = DOORSTATION_GALLERY_MAX_WAITING_READS
    try:
        with pytest.raises(BrowseError, match="Too many gallery reads"):
            await media_source.async_browse_media(hass, folder)
    finally:
        waiting.count = 0
    assert any(r.levelname == "WARNING" and "at the cap" in r.getMessage() for r in caplog.records)


async def test_the_gallery_places_outlive_a_reload_and_go_with_the_entry_on_removal(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    key = f"{_GALLERY_READ_SLOTS_KEY}_{mock_config_entry.entry_id}"
    places = _gallery_read_slots(hass, mock_config_entry.entry_id)

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert hass.data[key] is places

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    assert _gallery_read_slots(hass, mock_config_entry.entry_id) is places

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    assert key not in hass.data


async def test_a_reloaded_entry_keeps_the_doorstation_lock_of_its_predecessor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    lock = coordinator.api._doorstation_snapshot_lock

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)

    reloaded = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert reloaded.api is not coordinator.api
    assert reloaded.api._doorstation_snapshot_lock is lock


def test_each_entry_has_its_own_doorstation_read_lock_until_it_is_forgotten() -> None:
    hass = cast(HomeAssistant, SimpleNamespace(data={}))
    lock = doorstation_read_lock(hass, "entry_a")
    assert doorstation_read_lock(hass, "entry_a") is lock
    assert doorstation_read_lock(hass, "entry_b") is not lock
    forget_doorstation_read_lock(hass, "entry_a")
    assert doorstation_read_lock(hass, "entry_a") is not lock
    forget_doorstation_read_lock(hass, "never_seen")


async def test_a_thumbnail_waiting_for_a_gallery_place_past_the_budget_answers_unavailable(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    request = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_PLACE_WAIT_SEC", 0.05):
            request = asyncio.ensure_future(client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")))
            # Every place is taken: the request must stand in the queue and still answer within its place budget.
            async with asyncio.timeout(5):
                await waiting.wait()
            async with asyncio.timeout(5):
                response = await request
    finally:
        _release_places(places)
        if request is not None:
            request.cancel()

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    coordinator.api.get_doorstation_picture.assert_not_awaited()
    # The timed-out wait gave its count back, or the cap would refuse every later thumbnail of the entry.
    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0


async def test_a_thumbnail_past_the_waiting_limit_is_answered_at_once(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    queued = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_MAX_WAITING_READS", 1):
            queued = asyncio.ensure_future(client.get(path))
            async with asyncio.timeout(5):
                await waiting.wait()
            # One thumbnail stands in the queue for a place; the next is refused without waiting behind it.
            async with asyncio.timeout(5):
                refused = await client.get(path)
            assert not queued.done()
    except BaseException:
        if queued is not None:
            queued.cancel()
        raise
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        served = await queued

    assert refused.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert served.status == HTTPStatus.OK
    coordinator.api.get_doorstation_picture.assert_awaited_once()
    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0


async def test_a_stall_at_the_places_is_warned_once_per_streak(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    queued = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_MAX_WAITING_READS", 1):
            queued = asyncio.ensure_future(client.get(path))
            async with asyncio.timeout(5):
                await waiting.wait()
            # Two refusals in a row make one streak: one warning, the second is only logged at debug level.
            async with asyncio.timeout(5):
                first = await client.get(path)
                second = await client.get(path)
    except BaseException:
        if queued is not None:
            queued.cancel()
        raise
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        served = await queued

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "not answered" in r.getMessage()]
    assert (first.status, second.status, served.status) == (HTTPStatus.SERVICE_UNAVAILABLE, 503, HTTPStatus.OK)
    assert len(warnings) == 1
    # The thumbnail that got its place ended the streak.
    assert _gallery_waiting(hass, mock_config_entry.entry_id).refusing is False


class _Instance:
    """A stand-in for a coordinator instance: a plain object the streak can hold weakly."""


def _make_current(hass: HomeAssistant, entry_id: str, instance: Any) -> None:
    """Makes the stand-in the coordinator the entry runs with now, as setup does (a reload replaces it)."""
    hass.data.setdefault(DOMAIN, {})[entry_id] = instance


async def test_a_reloaded_entry_starts_its_own_refusal_streak(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The streak of the instance before the reload must not silence the first refusal of the instance after it.
    entry_id = mock_config_entry.entry_id
    _gallery_waiting(hass, entry_id)  # the entry is kept while its thumbnails wait
    before, after = cast(Any, _Instance()), cast(Any, _Instance())
    _make_current(hass, entry_id, before)
    _refused_for_a_place(hass, entry_id, before, 2, "first")
    _refused_for_a_place(hass, entry_id, before, 2, "second")
    _make_current(hass, entry_id, after)
    _refused_for_a_place(hass, entry_id, after, 2, "third")

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "not answered" in r.getMessage()]
    assert len(warnings) == 2


async def test_a_reloaded_entry_starts_its_own_slow_streak(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    entry_id = mock_config_entry.entry_id
    _gallery_waiting(hass, entry_id)
    before, after = cast(Any, _Instance()), cast(Any, _Instance())
    _make_current(hass, entry_id, before)
    _warn_slow_place_wait(hass, entry_id, before, 2)
    _warn_slow_place_wait(hass, entry_id, before, 2)
    _make_current(hass, entry_id, after)
    _warn_slow_place_wait(hass, entry_id, after, 2)

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "still waits" in r.getMessage()]
    assert len(warnings) == 2


async def test_a_grant_of_a_replaced_instance_ends_only_the_refusal_streak(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A read that started before the reload still runs to its end: its refusal and its slow mark must not warn again or
    # end the streaks the new instance has logged. Its grant does show the places are free, so the refusal streak ends
    # (a later refusal of the new instance warns again), while the slow streak, the new instance's own, is left alone.
    entry_id = mock_config_entry.entry_id
    waiting = _gallery_waiting(hass, entry_id)
    old, new = cast(Any, _Instance()), cast(Any, _Instance())
    _make_current(hass, entry_id, new)
    _refused_for_a_place(hass, entry_id, new, 2, "first")
    _warn_slow_place_wait(hass, entry_id, new, 2)
    _refused_for_a_place(hass, entry_id, old, 2, "late")
    _warn_slow_place_wait(hass, entry_id, old, 2)
    _place_granted(hass, entry_id, old, waited=0.0, slow_after=DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC)

    assert waiting.refusing is False
    assert waiting.slow is True
    assert waiting.streak_of() is new
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2

    _refused_for_a_place(hass, entry_id, new, 2, "second")
    _warn_slow_place_wait(hass, entry_id, new, 2)

    assert waiting.refusing is True
    assert waiting.slow is True
    assert waiting.streak_of() is new
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 3


async def test_a_request_after_the_entry_was_unloaded_changes_no_streak_and_warns_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # No coordinator is registered for the entry any more (unloaded, not yet reloaded): a late request of the old
    # instance neither warns nor clears what is kept for the entry.
    entry_id = mock_config_entry.entry_id
    waiting = _gallery_waiting(hass, entry_id)
    old = cast(Any, _Instance())
    _refused_for_a_place(hass, entry_id, old, 2, "late")
    _warn_slow_place_wait(hass, entry_id, old, 2)

    assert waiting.refusing is False
    assert waiting.slow is False
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_cancelled_wait_while_another_still_waits_keeps_the_slow_flag(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    # Only the queue running empty ends a stall: a browser that leaves while another thumbnail still waits must not
    # reset the flag, or the stall would warn a second time.
    entry_id = mock_config_entry.entry_id
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, _ = await _take_all_places(hass, entry_id)
    waiting = _gallery_waiting(hass, entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    first = asyncio.ensure_future(client.get(path))
    second = asyncio.ensure_future(client.get(path))
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC", 0.01):
            # Both must be queued before the cancel, or the queue would run empty and end the stall by itself.
            async with asyncio.timeout(5):
                while waiting.count < 2:
                    await asyncio.sleep(0.01)
            async with asyncio.timeout(5):
                while not waiting.slow:
                    await asyncio.sleep(0.01)
            first.cancel()
            async with asyncio.timeout(5):
                while waiting.count != 1:
                    await asyncio.sleep(0.01)
        assert waiting.slow is True
    finally:
        first.cancel()
        second.cancel()
        _release_places(places)


async def test_a_slow_wait_that_outlives_its_entry_creates_no_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The entry was removed while its thumbnail still waited: the timer must not bring the waiting state back.
    _warn_slow_place_wait(hass, mock_config_entry.entry_id, cast(Any, _Instance()), 2)

    assert _kept_gallery_waiting(hass, mock_config_entry.entry_id) is None
    assert not [r for r in caplog.records if "still waits" in r.getMessage()]


async def test_a_thumbnail_granted_before_the_slow_mark_is_not_warned(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # A free place is taken at once: the timer that would warn at the mark must be cancelled by the grant.
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    client = await hass_client()
    with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC", 0.05):
        response = await client.get(_picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg"))
        await asyncio.sleep(0.1)

    assert response.status == HTTPStatus.OK
    assert not [r for r in caplog.records if "still waits" in r.getMessage()]


async def test_a_thumbnail_whose_browser_left_while_it_waited_takes_no_place_and_reads_nothing(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # Home Assistant's server cancels the handler when the browser disconnects: the queued thumbnail gives its count
    # back, no read starts when the place comes free, and the slow-wait timer it had set is cancelled with the wait.
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    queued = asyncio.ensure_future(client.get(path))
    # The mark is well above the time the cancel takes under load, so the wait is always cancelled before it.
    with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC", 1.0):
        try:
            async with asyncio.timeout(5):
                await waiting.wait()
            queued.cancel()
            async with asyncio.timeout(5):
                while _gallery_waiting(hass, mock_config_entry.entry_id).count:
                    await asyncio.sleep(0.01)
        finally:
            _release_places(places)
        # Past the slow mark: a timer that the cancelled wait had not cleared would warn now.
        await asyncio.sleep(1.2)

    coordinator.api.get_doorstation_picture.assert_not_awaited()
    assert not _gallery_read_slots(hass, mock_config_entry.entry_id).locked()
    assert not [r for r in caplog.records if "still waits" in r.getMessage()]


@pytest.mark.parametrize(
    ("count", "waited", "still_slow"),
    [
        # A long wait while others still queue: the same stall goes on.
        (1, DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC, True),
        # A long wait and the queue is empty now: the stall is over.
        (0, DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC, False),
        # A place within the mark: the places flow again, whatever is still queued.
        (1, 0.0, False),
    ],
)
async def test_a_grant_ends_the_slow_streak_only_when_the_places_flow_again(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, count: int, waited: float, still_slow: bool
) -> None:
    waiting = _gallery_waiting(hass, mock_config_entry.entry_id)
    waiting.slow = True
    waiting.refusing = True
    waiting.count = count
    coordinator = cast(Any, _Instance())
    _make_current(hass, mock_config_entry.entry_id, coordinator)

    _place_granted(
        hass, mock_config_entry.entry_id, coordinator, waited=waited, slow_after=DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC
    )

    assert waiting.slow is still_slow
    assert not waiting.refusing


async def test_a_grant_after_the_unload_ends_the_refusal_streak_and_leaves_the_slow_one(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    # No coordinator is registered for the entry (unloaded, not yet reloaded): a grant still shows the places are free,
    # so the refusal streak ends; the slow streak is left to the next instance that takes the entry.
    entry_id = mock_config_entry.entry_id
    waiting = _gallery_waiting(hass, entry_id)
    waiting.refusing = True
    waiting.slow = True
    _place_granted(
        hass, entry_id, cast(Any, _Instance()), waited=0.0, slow_after=DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC
    )

    assert not waiting.refusing
    assert waiting.slow


async def test_a_refusal_and_a_grant_after_the_removal_of_the_entry_create_no_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    instance = cast(Any, _Instance())
    _refused_for_a_place(hass, mock_config_entry.entry_id, instance, 2, "the cap")
    _place_granted(
        hass, mock_config_entry.entry_id, instance, waited=0.0, slow_after=DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC
    )

    assert _kept_gallery_waiting(hass, mock_config_entry.entry_id) is None
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_thumbnail_waiting_past_the_slow_mark_is_warned_before_its_wait_ends(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    queued = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC", 0.2):
            queued = asyncio.ensure_future(client.get(path))
            async with asyncio.timeout(5):
                await waiting.wait()
            # Well before the mark there is no warning yet: a warning at once would pass the checks below as well.
            await asyncio.sleep(0.05)
            assert not any(r.levelname == "WARNING" and "still waits" in r.getMessage() for r in caplog.records)
            # The warning comes while the thumbnail still stands in the queue, not when its wait has ended.
            async with asyncio.timeout(5):
                while not any(r.levelname == "WARNING" and "still waits" in r.getMessage() for r in caplog.records):
                    await asyncio.sleep(0.01)
            assert not queued.done()
    except BaseException:
        if queued is not None:
            queued.cancel()
        raise
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        served = await queued

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "still waits" in r.getMessage()]
    assert served.status == HTTPStatus.OK
    assert len(warnings) == 1


async def test_a_stall_whose_queue_drains_by_browsers_leaving_is_warned_again_by_the_next_one(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    # The queue empties when the browser leaves: no thumbnail waits, so the stall is over and the next one gets its own
    # warning at its mark. A flag left set by the first stall would silence it.
    entry_id = mock_config_entry.entry_id
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    queued: list[asyncio.Future[Any]] = []

    def stall_warnings() -> int:
        return len([r for r in caplog.records if r.levelname == "WARNING" and "still waits" in r.getMessage()])

    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_SLOW_PLACE_WAIT_SEC", 0.01):
            for expected in (1, 2):
                queued.append(asyncio.ensure_future(client.get(path)))
                async with asyncio.timeout(5):
                    while stall_warnings() < expected:
                        await asyncio.sleep(0.01)
                queued[-1].cancel()
                async with asyncio.timeout(5):
                    while _gallery_waiting(hass, entry_id).count:
                        await asyncio.sleep(0.01)
    finally:
        for request in queued:
            request.cancel()
        _release_places(places)

    coordinator.api.get_doorstation_picture.assert_not_awaited()
    assert stall_warnings() == 2


async def test_a_folder_browse_past_the_waiting_limit_is_refused_at_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_pictures.return_value = ([], None)
    folder = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    queued = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_MAX_WAITING_READS", 1):
            queued = asyncio.ensure_future(media_source.async_browse_media(hass, folder))
            async with asyncio.timeout(5):
                await waiting.wait()
            # One browse batch stands in the queue for a place; the next is refused without waiting behind it.
            async with asyncio.timeout(5):
                with pytest.raises(BrowseError, match="Too many gallery reads"):
                    await media_source.async_browse_media(hass, folder)
            assert not queued.done()
    except BaseException:
        if queued is not None:
            queued.cancel()
        raise
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        await queued

    coordinator.api.get_doorstation_pictures.assert_awaited_once()
    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0


async def test_a_thumbnail_queued_behind_the_places_is_served_within_its_own_read_budget(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    # The one place is held by another read; the test decides when it is free again.
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")

    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_THUMBNAIL_TIMEOUT_SEC", 1.0):
            request = asyncio.ensure_future(client.get(path))
            # The request must stand in the queue for a place before the wait starts counting: only then does the
            # sleep past the read budget test that the place wait is no part of that budget.
            async with asyncio.timeout(5):
                await waiting.wait()
            await asyncio.sleep(1.5)
            places.release()
            async with asyncio.timeout(10):
                response = await request
    finally:
        places.release()

    assert response.status == HTTPStatus.OK
    coordinator.api.get_doorstation_picture.assert_awaited_once()


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


async def test_a_doorstation_whose_number_the_gallery_cannot_read_back_is_not_listed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    doorstation = coordinator.doorstations.known[2]
    # Ten digits: _parse reads nine at most, so the folder could be listed but never opened.
    too_long = 1_234_567_890
    with patch.object(
        type(coordinator.doorstations),
        "known",
        new_callable=PropertyMock,
        return_value={2: doorstation, too_long: doorstation},
    ):
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
        pytest.raises(BrowseError, match=r"not read in .* s \(the Doorstation did not answer in time\)"),
    ):
        await media_source.async_browse_media(hass, folder_id)
    # The list gave its place back when its deadline ran out: DOORSTATION_GALLERY_PLACE_WAIT_SEC counts on that.
    assert not _gallery_read_slots(hass, mock_config_entry.entry_id).locked()


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
                with pytest.raises(BrowseError, match=r"not read in .* s \(the wait for a gallery place included\)"):
                    await media_source.async_browse_media(hass, folder_id)
    finally:
        for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
            places.release()

    coordinator.api.get_doorstation_pictures.assert_not_awaited()
    # The timed-out wait gave its count back, or the cap would refuse every later browse of the entry.
    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0


async def test_a_browse_cancelled_while_waiting_for_a_gallery_place_gives_its_count_back(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    browse = asyncio.ensure_future(media_source.async_browse_media(hass, folder_id))
    try:
        async with asyncio.timeout(5):
            await waiting.wait()
        assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 1
        browse.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browse
    finally:
        _release_places(places)

    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0
    # The cancelled browse took no place: all of them are free again.
    async with asyncio.timeout(1):
        for _ in range(DOORSTATION_GALLERY_CONCURRENT_READS):
            await _gallery_read_slots(hass, mock_config_entry.entry_id).acquire()


async def test_a_browse_past_the_waiting_limit_is_refused_while_a_thumbnail_queues(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (JPEG, None)
    places, waiting = await _take_all_places(hass, mock_config_entry.entry_id)
    client = await hass_client()
    path = _picture_path(mock_config_entry, "26-11-21-18-30-00_bell.jpg")
    folder_id = f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}"
    queued = None
    try:
        with patch("custom_components.comexio.media_source.DOORSTATION_GALLERY_MAX_WAITING_READS", 1):
            queued = asyncio.ensure_future(client.get(path))
            async with asyncio.timeout(5):
                await waiting.wait()
            # The cap counts the queued thumbnail too: the browse is refused rather than queued behind it.
            async with asyncio.timeout(5):
                with pytest.raises(BrowseError, match="Too many gallery reads"):
                    await media_source.async_browse_media(hass, folder_id)
            assert not queued.done()
    except BaseException:
        if queued is not None:
            queued.cancel()
        raise
    finally:
        _release_places(places)
    async with asyncio.timeout(10):
        served = await queued

    assert served.status == HTTPStatus.OK
    assert _gallery_waiting(hass, mock_config_entry.entry_id).count == 0


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
