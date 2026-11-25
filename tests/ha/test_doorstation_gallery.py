"""The Doorstation gallery in the media browser: folders, newest pictures, signed thumbnails and the view."""

import asyncio
from collections.abc import AsyncGenerator, Generator
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
    DOORSTATION_GALLERY_MAX_PICTURES,
    DOORSTATION_PICTURE_VIEW_PATH,
)

pytestmark = pytest.mark.usefixtures("mock_comexio_api")

EXTENSION = "UD 1"
JPEG = b"\xff\xd8gallery\xff\xd9"
MEDIA_ROOT = f"media-source://{DOMAIN}"
TOO_LONG_EXT_ID = "1" * 5000  # more digits than int() takes (4300)
OTHER_PICTURE = DoorstationPicture(
    name="other.jpg", kind=DoorstationPictureKind.OTHER, taken_at=datetime(2026, 11, 22, 7, 0, 0)
)
MOTION_PICTURE = DoorstationPicture(
    name="motion.jpg", kind=DoorstationPictureKind.MOTION, taken_at=datetime(2026, 11, 21, 19, 0, 0)
)
NEW_BELL = DoorstationPicture(
    name="bell_new.jpg", kind=DoorstationPictureKind.BELL, taken_at=datetime(2026, 11, 21, 18, 30, 0)
)
OLD_BELL = DoorstationPicture(
    name="bell_old.jpg", kind=DoorstationPictureKind.BELL, taken_at=datetime(2026, 11, 20, 8, 0, 0)
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


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    assert await async_setup_component(hass, "media_source", {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return hass.data[DOMAIN][entry.entry_id]


def _folder_id(entry: MockConfigEntry) -> str:
    return f"{entry.entry_id}/2"


def _picture_id(entry: MockConfigEntry, name: str) -> str:
    return f"{entry.entry_id}/2/{name}"


def _picture_path(entry: MockConfigEntry, name: str) -> str:
    return f"{DOORSTATION_PICTURE_VIEW_PATH}/{entry.entry_id}/2/{name}"


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
        _picture_id(mock_config_entry, "motion.jpg"),
        _picture_id(mock_config_entry, "bell_new.jpg"),
        _picture_id(mock_config_entry, "bell_old.jpg"),
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
        _picture_id(mock_config_entry, "motion.jpg"),
        _picture_id(mock_config_entry, "bell_new.jpg"),
    ]


async def test_a_thumbnail_is_a_signed_link_to_the_picture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    thumbnail = folder.children[0].thumbnail
    assert thumbnail.startswith(f"{_picture_path(mock_config_entry, 'motion.jpg')}?")
    assert "authSig=" in thumbnail


async def test_a_picture_resolves_to_its_view(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await _setup(hass, mock_config_entry)

    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, 'bell_new.jpg')}"

    play = await media_source.async_resolve_media(hass, media_id, None)

    assert play.url == _picture_path(mock_config_entry, "bell_new.jpg")
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
    undated = DoorstationPicture(name="bell_nodate.jpg", kind=DoorstationPictureKind.BELL, taken_at=None)
    api_returns["get_doorstation_pictures"] = ((undated, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    last = folder.children[-1]
    assert last.identifier == _picture_id(mock_config_entry, "bell_nodate.jpg")
    assert last.title == "Doorbell (bell_nodate.jpg)"


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

    assert [child.identifier for child in folder.children] == [_picture_id(mock_config_entry, "bell_new.jpg")]


async def test_a_folder_shows_only_the_cap_of_the_newest_pictures_of_a_longer_list(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    count = DOORSTATION_GALLERY_MAX_PICTURES + 5
    oldest_first = tuple(
        DoorstationPicture(
            name=f"bell_{hour:02d}.jpg",
            kind=DoorstationPictureKind.BELL,
            taken_at=datetime(2026, 11, 1) + timedelta(hours=hour),
        )
        for hour in range(count)
    )
    api_returns["get_doorstation_pictures"] = (oldest_first, None)
    await _setup(hass, mock_config_entry)

    folder = await media_source.async_browse_media(hass, f"{MEDIA_ROOT}/{_folder_id(mock_config_entry)}")

    assert len(folder.children) == DOORSTATION_GALLERY_MAX_PICTURES
    assert folder.children[0].identifier == _picture_id(mock_config_entry, f"bell_{count - 1:02d}.jpg")


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
    media_id = f"{MEDIA_ROOT}/{_picture_id(mock_config_entry, 'bell_new.jpg')}"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


async def test_a_picture_of_a_doorstation_that_is_not_known_does_not_resolve(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    media_id = f"{MEDIA_ROOT}/{mock_config_entry.entry_id}/9/bell_new.jpg"

    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, media_id, None)


# --- the view -------------------------------------------------------------------------------------------------------


async def test_the_view_serves_a_picture_to_a_logged_in_client(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    assert response.status == HTTPStatus.OK
    assert await response.read() == JPEG
    coordinator.api.get_doorstation_picture.assert_awaited_with(2, "bell_new.jpg")


async def test_the_view_refuses_a_client_that_is_not_logged_in(
    hass: HomeAssistant, hass_client_no_auth: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client_no_auth()

    response = await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    assert response.status == HTTPStatus.UNAUTHORIZED


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

    response = await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_the_view_answers_not_found_for_an_empty_picture(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.return_value = (b"", None)
    client = await hass_client()

    response = await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    assert response.status == HTTPStatus.NOT_FOUND


async def test_the_view_answers_gateway_timeout_when_the_read_takes_too_long(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)

    async def _never_ends(*_args: Any) -> None:
        await asyncio.sleep(60)

    coordinator.api.get_doorstation_picture.side_effect = _never_ends
    client = await hass_client()

    with patch("custom_components.comexio.media_source.DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.01):
        response = await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    assert response.status == HTTPStatus.GATEWAY_TIMEOUT
    coordinator.api.get_doorstation_picture.assert_awaited_with(2, "bell_new.jpg")


async def test_a_timeout_from_inside_the_read_is_not_taken_for_a_slow_doorstation(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, mock_config_entry)
    coordinator.api.get_doorstation_picture.side_effect = TimeoutError
    client = await hass_client()

    with patch("custom_components.comexio.media_source._LOGGER") as logger:
        await client.get(_picture_path(mock_config_entry, "bell_new.jpg"))

    logger.debug.assert_not_called()


@pytest.mark.parametrize("digit", ["²", "٢", "２", TOO_LONG_EXT_ID])
async def test_the_view_answers_not_found_for_a_digit_that_is_no_extension(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry, digit: str
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(f"{DOORSTATION_PICTURE_VIEW_PATH}/{mock_config_entry.entry_id}/{digit}/bell_new.jpg")

    assert response.status == HTTPStatus.NOT_FOUND


async def test_the_view_answers_not_found_for_a_doorstation_that_is_not_known(
    hass: HomeAssistant, hass_client: Any, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)
    client = await hass_client()

    response = await client.get(f"{DOORSTATION_PICTURE_VIEW_PATH}/{mock_config_entry.entry_id}/9/bell_new.jpg")

    assert response.status == HTTPStatus.NOT_FOUND
