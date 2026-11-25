"""Doorstation pictures: the two image entities, their device, the moment as state, and when the list is read."""

import asyncio
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiocomexio import DoorstationPicture, DoorstationPictureKind
from aiocomexio.doorstation import Doorstation
from homeassistant.components.image import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.comexio import doorstation_pictures
from custom_components.comexio.api import doorstation_read_got_its_turn
from custom_components.comexio.const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    DOORSTATION_PICTURE_PUSH_DELAY_SEC,
    DOORSTATION_PICTURE_PUSH_RETRY_SEC,
    DOORSTATION_PICTURE_PUSH_TRIES,
    DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC,
    DOORSTATION_PICTURE_RETRY_SEC,
    doorstation_pictures_signal,
    io_push_signal,
)
from custom_components.comexio.doorstation_pictures import taken_at_utc

from .conftest import SERVER_ID, device_by_identifier

BELL = DoorstationPictureKind.BELL
MOTION = DoorstationPictureKind.MOTION
BELL_ENTITY_ID = f"image.{SERVER_ID}_doorstation2_bell_picture"
MOTION_ENTITY_ID = f"image.{SERVER_ID}_doorstation2_motion_picture"
BELL_UNIQUE_ID = f"comexio_{SERVER_ID}_doorstation2_bell_picture"
MOTION_UNIQUE_ID = f"comexio_{SERVER_ID}_doorstation2_motion_picture"
EXTENSION = "UD 1"
JPEG = b"\xff\xd8picture\xff\xd9"
CANARY = "canary-text-of-an-error-203.0.113.9"  # an error text that must stay on one line and never be dropped

OLD_BELL = DoorstationPicture(name="bell_old.jpg", kind=BELL, taken_at=datetime(2026, 11, 20, 8, 0, 0))
NEW_BELL = DoorstationPicture(name="bell_new.jpg", kind=BELL, taken_at=datetime(2026, 11, 21, 18, 30, 0))
MOTION_PICTURE = DoorstationPicture(name="motion.jpg", kind=MOTION, taken_at=datetime(2026, 11, 21, 19, 0, 0))


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    """A Doorstation on extension 2 ("UD 1") with a bell button DI1 and two stored pictures (newest first)."""
    extension = api_returns["get_raw_config"]["FubModules"]["1"]["2"]
    extension["extension"]["Identifier"] = "1000-2000-4001"  # a serial number: the extension is online
    extension["inoutput"]["22"] = {
        "Id": 22,
        "Identifier": "DI1",
        "Description": "Klingel DI1",
        "InOutputTypeId": 1,
        "Value": "0",
        "Active": True,
    }
    api_returns["get_doorstations"] = ({2: Doorstation(ext_id=2, camera_id=7, name=EXTENSION)}, None)
    api_returns["get_doorstation_pictures"] = ((MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    api_returns["get_doorstation_picture"] = (JPEG, None)
    return api_returns


@pytest.fixture
def entry(mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    """The fixture's extension "UD 1" reports offline, so its IOs only exist with offline extensions included."""
    return MockConfigEntry(
        domain=DOMAIN,
        minor_version=mock_config_entry.minor_version,
        data=mock_config_entry.data,
        options={CONF_INCLUDE_OFFLINE_EXTENSIONS: True},
    )


@pytest.fixture(autouse=True)
def _no_statistics_unit_fix_delay() -> Generator[None]:
    """The setup's statistics unit fix sleeps 15 s before it starts, and every wait for background tasks waits for it.

    That made each test here take 15 s (the 63 of them ran into the CI job's 15-minute limit); it runs at once instead,
    as in test_statistics_unit_fix.py. Harmless only while no test here loads the recorder: without it the fix returns
    at once and has nothing to do.
    """
    with patch("custom_components.comexio.STATISTICS_UNIT_FIX_START_DELAY_SEC", 0):
        yield


@pytest.fixture(autouse=True)
async def _unload_when_done(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncGenerator[None]:
    """Unload the entry at the end: a read that found nothing new leaves a retry timer, which only the unload drops."""
    yield
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


class _Clock:
    """The monotonic clock of the feed, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    # Only the feed's own `time` name: patching time.monotonic itself would also stop the event loop's clock.
    monkeypatch.setattr(doorstation_pictures, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return hass.data[DOMAIN][entry.entry_id]


async def _online(hass: HomeAssistant, coordinator: Any) -> None:
    """The synthetic extension reports offline, which makes its entities unavailable: bring it back."""
    coordinator.offline_extensions = set()
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup_online(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Set the entry up with its extension online; the list has been read once when this returns."""
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)
    assert coordinator.api.get_doorstation_pictures.await_count == 1
    return coordinator


async def _later(hass: HomeAssistant, coordinator: Any, clock: _Clock, seconds: float) -> None:
    """Let `seconds` pass for the feed's clock and give it a coordinator update, then wait for what that started."""
    clock.now += seconds
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _later_no_wait(hass: HomeAssistant, coordinator: Any, clock: _Clock, seconds: float) -> None:
    """Like _later, but only lets the started read begin (a gated read does not end)."""
    clock.now += seconds
    coordinator.async_update_listeners()
    await hass.async_block_till_done()


async def _fire_timers(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


def _gate(read: AsyncMock, result: Any) -> asyncio.Event:
    """Make the next calls of `read` wait until the returned event is set, then answer `result`."""
    opened = asyncio.Event()

    async def _wait(*_args: Any) -> Any:
        await opened.wait()
        return result

    read.side_effect = _wait
    return opened


def _images(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "image" and "doorstation" in e.unique_id
    ]


async def _image_bytes(hass: HomeAssistant, entity_id: str) -> bytes | None:
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    return await entity.async_image()


# --- the entities ---------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_doorstation_gets_a_bell_and_a_motion_picture_on_its_extension_device(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)

    registry = er.async_get(hass)
    assert {e.entity_id for e in _images(hass, entry)} == {BELL_ENTITY_ID, MOTION_ENTITY_ID}
    device = device_by_identifier(hass, entry.entry_id, {(DOMAIN, f"{SERVER_ID}_ud 1")})
    for entity_id, unique_id in ((BELL_ENTITY_ID, BELL_UNIQUE_ID), (MOTION_ENTITY_ID, MOTION_UNIQUE_ID)):
        picture = registry.async_get(entity_id)
        assert picture is not None
        assert picture.unique_id == unique_id
        assert picture.device_id == device.id
    assert device.name == f"{SERVER_ID} EXT - {EXTENSION}"


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_installation_without_doorstation_gets_no_picture(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)

    coordinator = await _setup(hass, entry)

    assert not _images(hass, entry)
    coordinator.api.get_doorstation_pictures.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_doorstation_without_any_extension_name_gets_no_picture(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({9: Doorstation(ext_id=9, camera_id=3, name=None)}, None)

    coordinator = await _setup(hass, entry)

    assert not _images(hass, entry)
    coordinator.api.get_doorstation_pictures.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_state_is_the_moment_of_the_newest_picture_of_its_kind(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)

    for entity_id, picture in ((BELL_ENTITY_ID, NEW_BELL), (MOTION_ENTITY_ID, MOTION_PICTURE)):
        state = hass.states.get(entity_id)
        assert state is not None
        moment = taken_at_utc(picture)
        assert moment is not None
        assert state.state == moment.isoformat()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_newest_picture_is_served_as_a_jpeg_and_the_list_is_read_once_at_the_start(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)

    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG

    coordinator.api.get_doorstation_pictures.assert_awaited_once_with(2)
    coordinator.api.get_doorstation_picture.assert_awaited_once_with(2, "bell_new.jpg")


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_that_was_read_is_kept_until_a_newer_one_is_listed(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any], clock: _Clock
) -> None:
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)
    await _image_bytes(hass, BELL_ENTITY_ID)
    await _image_bytes(hass, BELL_ENTITY_ID)
    assert coordinator.api.get_doorstation_picture.await_count == 1

    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    coordinator.api.get_doorstation_pictures.return_value = ((newer, MOTION_PICTURE, NEW_BELL), None)
    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)

    await _image_bytes(hass, BELL_ENTITY_ID)
    assert coordinator.api.get_doorstation_picture.await_args_list[-1].args == (2, "bell_newer.jpg")
    assert coordinator.api.get_doorstation_picture.await_count == 2
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    moment = taken_at_utc(newer)
    assert moment is not None
    assert state.state == moment.isoformat()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_no_picture_stored_is_an_unknown_state_and_no_image(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstation_pictures"] = ((), None)
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)

    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    coordinator.api.get_doorstation_picture.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_pictures_of_an_offline_extension_are_unavailable_and_not_read(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup(hass, entry)
    coordinator.offline_extensions = {EXTENSION}
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    coordinator.api.get_doorstation_picture.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_extension_that_is_offline_at_startup_reads_no_list(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_raw_config"]["FubModules"]["1"]["2"]["extension"]["Identifier"] = EXTENSION  # no serial: offline
    coordinator = await _setup(hass, entry)

    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    coordinator.api.get_doorstation_pictures.assert_not_called()
    coordinator.api.get_doorstation_picture.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_page_without_doorstation_keeps_the_registered_pictures_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    api_returns["get_doorstations"] = ({}, None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {e.entity_id for e in _images(hass, entry)} == {BELL_ENTITY_ID, MOTION_ENTITY_ID}
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


# --- when the list is read ------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_coordinator_update_reads_the_list_again_only_after_the_refresh_interval(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    # A webhook push publishes a coordinator update too: any number of them in the interval read nothing.
    for _ in range(5):
        clock.now += 1
        coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads.await_count == 1

    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_bell_push_reads_the_list_after_the_delay_and_not_before(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    coordinator.update_io_by_name(EXTENSION, "DI1", 1, pushed=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads.await_count == 1  # the Doorstation stores the picture a moment after the event

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize("identifier", ["DI1", "DI2", "DI3", "PIR1"])
async def test_a_bell_button_and_the_motion_sensor_each_start_one_delayed_read(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: str
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, identifier), 1)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, identifier), 1)  # a second push inside the pause
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reads.await_count == 1  # nothing is read before the pause is over
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 2  # the one at the start and one for the two pushes


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize("value", [0, -1, None])
async def test_a_button_up_reads_nothing(hass: HomeAssistant, entry: MockConfigEntry, value: float | None) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), value)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 1


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_that_waits_for_its_read_is_dropped_with_the_entry(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)

    assert await hass.config_entries.async_unload(entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 1


# --- failures: one warning per streak -------------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_list_read_warns_once_per_streak_keeps_the_pictures_and_reports_the_end(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    api_returns: dict[str, Any],
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup(hass, entry)
    await _online(hass, coordinator)
    reads = coordinator.api.get_doorstation_pictures
    reads.return_value = (None, f"timed out\n{CANARY}")

    for _ in range(3):
        clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
        coordinator.async_update_listeners()
        await hass.async_block_till_done(wait_background_tasks=True)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "list of its pictures" in r.getMessage()]
    assert len(warnings) == 1
    assert "\n" not in warnings[0].getMessage()
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == taken_at_utc(NEW_BELL).isoformat()  # the failure took nothing away

    reads.return_value = ((MOTION_PICTURE, NEW_BELL), None)
    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sum("is read again" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_picture_read_warns_once_waits_before_the_next_try_and_reports_the_end(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_read.return_value = (None, "not a JPEG")

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # a frontend that asks again does not ask the Doorstation
    assert picture_read.await_count == 1
    assert sum("newest bell picture" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING) == 1

    clock.now += DOORSTATION_PICTURE_RETRY_SEC
    picture_read.side_effect = None
    picture_read.return_value = (JPEG, None)
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert sum("bell pictures are read again" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_newer_picture_is_asked_for_at_once_after_a_failure(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_read.return_value = (None, "not a JPEG")
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None

    # The same picture is not asked for again inside the pause ...
    clock.now += DOORSTATION_PICTURE_RETRY_SEC - 1
    picture_read.return_value = (JPEG, None)
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert picture_read.await_count == 1

    # ... a newer one is, although the pause is not over: the list read comes through a push, inside the interval.
    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    coordinator.api.get_doorstation_pictures.return_value = ((newer, MOTION_PICTURE), None)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)

    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert picture_read.await_args_list[-1].args == (2, "bell_newer.jpg")


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_picture_read_warns_once_per_kind(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    coordinator.api.get_doorstation_picture.return_value = (None, "not a JPEG")

    for entity_id in (BELL_ENTITY_ID, MOTION_ENTITY_ID, BELL_ENTITY_ID, MOTION_ENTITY_ID):
        assert await _image_bytes(hass, entity_id) is None

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "newest" in r.getMessage()]
    assert len(warnings) == 2
    assert "newest bell picture" in warnings[0]
    assert "newest motion picture" in warnings[1]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_empty_picture_is_a_failure_and_never_served_or_kept(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup_online(hass, entry)
    coordinator.api.get_doorstation_picture.return_value = (b"", None)

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert any("empty picture" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_read_that_takes_too_long_ends_with_a_warning_and_waits_before_the_next_try(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_pictures, "DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.05)
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    _gate(picture_read, (JPEG, None))  # never opened

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert picture_read.await_count == 1
    assert sum("not read within" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING) == 1

    clock.now += DOORSTATION_PICTURE_RETRY_SEC
    picture_read.side_effect = None
    picture_read.return_value = (JPEG, None)
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_request_that_waited_out_its_time_behind_other_reads_leaves_no_pause_or_warning(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_pictures, "DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.05)
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture

    async def _behind_another_read(*_args: Any) -> Any:
        doorstation_read_got_its_turn.set(False)  # what ComexioAPI does while it waits for the camera's lock
        await asyncio.Event().wait()

    picture_read.side_effect = _behind_another_read
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # the time ran out before the read had its turn

    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and "could not read its newest" in r.getMessage()
    ]
    picture_read.side_effect = None
    picture_read.return_value = (JPEG, None)
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG  # asked again at once: no pause of a failure held it back
    assert picture_read.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_request_that_never_got_its_turn_is_no_failure_of_the_doorstation_and_a_kept_picture_needs_no_turn(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    clock: _Clock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doorstation_pictures, "DOORSTATION_PICTURE_READ_TIMEOUT_SEC", 0.05)
    coordinator = await _setup_online(hass, entry)
    assert await _image_bytes(hass, MOTION_ENTITY_ID) == JPEG  # kept
    opened = _gate(coordinator.api.get_doorstation_pictures, ((MOTION_PICTURE, NEW_BELL), None))
    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done()  # the list read runs and holds the feed's lock

    assert await _image_bytes(hass, MOTION_ENTITY_ID) == JPEG  # served without waiting for the lock
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # not read yet: its turn did not come in time

    assert coordinator.api.get_doorstation_picture.await_count == 1  # only the motion picture before
    assert not [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.comexio")
        and r.levelno >= logging.WARNING
        and "picture" in r.getMessage()
    ]
    opened.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator.api.get_doorstation_pictures.side_effect = None
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG  # no back-off was left behind


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.logged_exception
async def test_an_unexpected_error_in_a_picture_read_is_a_failure_of_its_kind_with_the_class_only(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_read.side_effect = ValueError("secret-detail-of-the-error")

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # inside the pause

    assert picture_read.await_count == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "newest" in r.getMessage()]
    assert len(warnings) == 1
    assert "ValueError" in warnings[0]
    assert "secret-detail" not in warnings[0]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_read_that_ends_with_the_session_is_no_failure_and_sets_no_pause(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_read.return_value = (None, None)

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None

    assert picture_read.await_count == 2  # asked again at once: nothing was held against the Doorstation
    assert not [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.comexio")
        and r.levelno >= logging.WARNING
        and "picture" in r.getMessage()
    ]


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.logged_exception
async def test_an_unexpected_error_in_the_list_read_is_one_warning_per_streak_and_no_lost_task(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup_online(hass, entry)
    coordinator.api.get_doorstation_pictures.side_effect = ValueError("secret-detail-of-the-error")

    for _ in range(2):
        await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)

    assert coordinator.api.get_doorstation_pictures.await_count == 3  # the failure left no read blocked
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "list of its" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "ValueError" in warnings[0]
    assert "secret-detail" not in warnings[0]  # the class only, like every unexpected error of this integration


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_read_that_ends_with_the_session_is_no_failure_to_report(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await _setup_online(hass, entry)
    coordinator.api.get_doorstation_pictures.return_value = (None, None)  # the session is gone: no cause

    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done(wait_background_tasks=True)

    ours = [r for r in caplog.records if r.name.startswith("custom_components.comexio")]
    assert not [r for r in ours if r.levelno >= logging.WARNING and "pictures" in r.getMessage()]


# --- the interval is the feed's own ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_list_is_read_every_interval_without_any_coordinator_update(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The coordinator's interval can be longer than the advertised five minutes (Sourcery on #338)."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    await _fire_timers(hass, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC + 1)

    assert reads.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_interval_reads_nothing_while_the_extension_is_offline(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    coordinator.offline_extensions = {EXTENSION}

    await _fire_timers(hass, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC + 1)

    assert coordinator.api.get_doorstation_pictures.await_count == 1


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_interval_ends_with_the_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _fire_timers(hass, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC + 1)

    assert reads.await_count == 1


# --- a push that comes while a read runs, and a push whose picture is not stored yet ---------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_during_a_running_read_gets_a_read_that_starts_after_it(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    """The running read may have started before the Doorstation stored the picture."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    opened = _gate(reads, ((MOTION_PICTURE, NEW_BELL, OLD_BELL), None))
    clock.now += DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert reads.await_count == 2  # the interval's read, waiting

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done()
    assert reads.await_count == 2  # not a second one beside it

    opened.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 3


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_whose_read_finds_nothing_new_is_read_again_a_bounded_number_of_times(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # the retry waits its own, longer pause

    for expected in range(3, DOORSTATION_PICTURE_PUSH_TRIES + 2):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)
        assert reads.await_count == expected
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # the start's read and one per try

    # The next press gets its tries again.
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES - 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + 2 * DOORSTATION_PICTURE_PUSH_TRIES


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_press_while_retries_of_its_kind_wait_does_not_renew_them(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A repeated or forged push must not keep the reads going (Sourcery on #338)."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)
    assert reads.await_count == 3  # the start's read and two tries of three: the last one waits

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)  # the bell picture is still awaited
    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES + 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # the tries were not renewed


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_press_before_the_first_read_of_a_push_is_covered_by_it(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)  # the other kind, same waiting read

    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES + 2):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # one cycle awaits both kinds


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_press_before_the_first_read_of_a_push_is_awaited_too(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The bell picture shows up at once, the motion picture does not: the retries go on for the second kind."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    newer_bell = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer_bell, MOTION_PICTURE, NEW_BELL), None)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)

    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES + 2):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # the motion picture was awaited all along


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_read_of_the_interval_that_lists_the_awaited_picture_ends_the_retries_of_a_push(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # nothing new yet, a retry waits

    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer, MOTION_PICTURE, NEW_BELL), None)
    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)  # no push read: a plain update
    assert reads.await_count == 3

    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 4  # the waiting retry ran once, found nothing awaited any more and ended the cycle


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_follow_up_cycle_leaves_out_a_kind_the_cycle_before_it_listed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A read that lists the motion picture ends the wait for it: no cycle of its own to look for it again."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # the bell cycle's first read: no new bell picture yet

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)  # queued for a cycle of its own
    newer_motion = DoorstationPicture(name="motion_new.jpg", kind=MOTION, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer_motion, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)  # lists it, still no new bell
    for _ in range(2 * DOORSTATION_PICTURE_PUSH_TRIES + 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # the bell's tries only


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_read_of_the_interval_that_lists_a_queued_kind_leaves_it_out_of_the_follow_up_cycle(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # the bell cycle's first read: no new bell picture yet

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)  # queued for a cycle of its own
    newer_motion = DoorstationPicture(name="motion_new.jpg", kind=MOTION, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer_motion, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)  # a plain read lists it
    assert reads.await_count == 3

    for _ in range(2 * DOORSTATION_PICTURE_PUSH_TRIES):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 2 + DOORSTATION_PICTURE_PUSH_TRIES  # the bell's remaining tries, no cycle for motion


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_read_that_waited_for_the_lock_asks_no_doorstation_that_went_away_meanwhile(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    picture_opened = _gate(coordinator.api.get_doorstation_picture, (JPEG, None))
    image = asyncio.create_task(_image_bytes(hass, BELL_ENTITY_ID))  # holds the feed's lock
    await hass.async_block_till_done()
    await _later_no_wait(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
    assert reads.await_count == 1  # the interval's read waits for the lock

    coordinator.offline_extensions = {EXTENSION}
    coordinator.async_update_listeners()
    picture_opened.set()
    await image
    await hass.async_block_till_done(wait_background_tasks=True)

    assert reads.await_count == 1  # it did not ask


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_kept_pictures_of_a_doorstation_that_went_away_are_dropped(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    """A removed Doorstation keeps its feed until the entry unloads: its JPEGs (up to 4 MiB each) must not stay."""
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert picture_read.await_count == 1

    coordinator.offline_extensions = {EXTENSION}
    await _later(hass, coordinator, clock, 1)
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # nothing is served for a Doorstation that is away
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)

    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert picture_read.await_count == 2  # read again: the first one was dropped


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_read_that_ends_after_the_doorstation_went_away_serves_and_keeps_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_opened = _gate(picture_read, (JPEG, None))
    image = asyncio.create_task(_image_bytes(hass, BELL_ENTITY_ID))  # the read is under way
    await hass.async_block_till_done()

    coordinator.offline_extensions = {EXTENSION}
    await _later_no_wait(hass, coordinator, clock, 1)
    picture_opened.set()

    assert await image is None  # the picture of a Doorstation that is away is not served
    picture_read.side_effect = None
    picture_read.return_value = (JPEG, None)
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert picture_read.await_count == 2  # nothing of the first read was kept


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_list_that_ends_after_the_doorstation_went_away_is_not_applied(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads: AsyncMock = coordinator.api.get_doorstation_pictures
    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    list_opened = _gate(reads, ((newer, MOTION_PICTURE, NEW_BELL), None))
    told: list[None] = []
    stop = async_dispatcher_connect(hass, doorstation_pictures_signal(SERVER_ID, 2), lambda: told.append(None))
    await _later_no_wait(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
    assert reads.await_count == 2  # the interval's read is under way

    coordinator.offline_extensions = {EXTENSION}
    coordinator.async_update_listeners()
    list_opened.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not told  # the answer of a Doorstation that went away changed nothing

    reads.side_effect = None
    reads.return_value = ((NEW_BELL, MOTION_PICTURE), None)  # what the list held before
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)
    stop()

    assert reads.await_count == 3  # read again on return
    assert not told  # it holds what it held before: had the dropped answer been kept, this read would have changed it
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == taken_at_utc(NEW_BELL).isoformat()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_list_that_ends_after_the_doorstation_went_away_starts_no_failure_streak(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    reads: AsyncMock = coordinator.api.get_doorstation_pictures
    list_opened = _gate(reads, (None, "timed out"))
    await _later_no_wait(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)

    coordinator.offline_extensions = {EXTENSION}
    coordinator.async_update_listeners()
    list_opened.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    def warnings() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno == logging.WARNING and "list of its pictures" in r.getMessage()]

    assert warnings() == []  # a read that failed because the Doorstation went away is no failure to warn about
    coordinator.offline_extensions = set()
    reads.side_effect = None
    reads.return_value = (None, "timed out")
    await _later(hass, coordinator, clock, 1)  # back: the read fails for real

    assert len(warnings()) == 1  # and it did not start the streak either: this one is the first


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_picture_read_that_failed_after_the_doorstation_went_away_sets_no_pause(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_opened = _gate(picture_read, (None, "timed out"))
    image = asyncio.create_task(_image_bytes(hass, BELL_ENTITY_ID))  # the read is under way
    await hass.async_block_till_done()

    coordinator.offline_extensions = {EXTENSION}
    await _later_no_wait(hass, coordinator, clock, 1)
    picture_opened.set()
    assert await image is None
    # The Doorstation going away is no failure to warn about.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING and "newest bell picture" in r.getMessage()]

    picture_read.side_effect = None
    picture_read.return_value = (JPEG, None)
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)  # back well within the pause of a real failure

    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG  # read at once, no pause held it back
    assert picture_read.await_count == 2
    assert all("pictures are read again" not in r.getMessage() for r in caplog.records)  # no streak to end


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failure_streak_of_pictures_ends_when_the_doorstation_goes_away(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    picture_read: AsyncMock = coordinator.api.get_doorstation_picture
    picture_read.return_value = (None, "not a JPEG")
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # a real failure: pause and warning

    coordinator.offline_extensions = {EXTENSION}
    await _later(hass, coordinator, clock, 1)
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)  # back, well within the pause of the failure from before

    assert await _image_bytes(hass, BELL_ENTITY_ID) is None  # asked again: the pause of the old streak is gone
    assert picture_read.await_count == 2
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "newest bell picture" in r.getMessage()]
    assert len(warned) == 2  # and the failure after the return starts a streak of its own


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failure_streak_of_the_list_ends_when_the_doorstation_goes_away(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.comexio.doorstation_pictures")
    coordinator = await _setup_online(hass, entry)
    reads: AsyncMock = coordinator.api.get_doorstation_pictures
    reads.return_value = (None, "timed out")
    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)  # a real failure: the warning

    def warnings() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno == logging.WARNING and "list of its pictures" in r.getMessage()]

    assert len(warnings()) == 1
    coordinator.offline_extensions = {EXTENSION}
    await _later(hass, coordinator, clock, 1)
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)  # back: the first update reads at once, and it fails again

    assert len(warnings()) == 2  # the streak ended with the Doorstation going away, so this one warns afresh


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_that_met_an_offline_doorstation_does_not_block_the_pushes_after_it(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    coordinator.offline_extensions = {EXTENSION}
    await _later(hass, coordinator, clock, 1)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 1  # nothing was read while it was offline

    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)  # the first update after that reads at once
    assert reads.await_count == 2

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)

    assert reads.await_count == 3  # the push after it opened a cycle of its own


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_press_of_the_other_kind_during_the_retries_gets_its_own_tries_afterwards(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # the bell cycle's first read; the motion picture is not awaited yet

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)
    for _ in range(2 * DOORSTATION_PICTURE_PUSH_TRIES + 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + 2 * DOORSTATION_PICTURE_PUSH_TRIES  # the bell's tries, then the motion's


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_burst_of_pushes_starts_no_more_than_one_follow_up_cycle(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The unauthenticated webhook accepts forged pushes while the old id is on: the reads must stay bounded."""
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)

    for _ in range(50):
        async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
        async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)
    for _ in range(3 * DOORSTATION_PICTURE_PUSH_TRIES):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + 2 * DOORSTATION_PICTURE_PUSH_TRIES  # the bell's cycle and one for the motion


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_new_picture_of_the_other_kind_does_not_end_the_retries_of_a_push(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    newer_motion = DoorstationPicture(name="motion_new.jpg", kind=MOTION, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer_motion, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)  # no new bell picture
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)

    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES + 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES  # the bell picture was still missing: all tries


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_motion_push_stops_retrying_once_the_motion_picture_is_listed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    newer_motion = DoorstationPicture(name="motion_new.jpg", kind=MOTION, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer_motion, MOTION_PICTURE, NEW_BELL, OLD_BELL), None)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "PIR1"), 1)

    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 2  # the awaited kind showed up at once


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_push_whose_list_read_fails_is_read_again_like_one_that_found_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    reads.return_value = (None, "timed out")

    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    for _ in range(DOORSTATION_PICTURE_PUSH_TRIES + 1):
        await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 1 + DOORSTATION_PICTURE_PUSH_TRIES


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_plain_update_during_a_running_read_asks_for_no_second_read_and_sets_no_retry(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    opened = _gate(reads, ((MOTION_PICTURE, NEW_BELL, OLD_BELL), None))
    await _later_no_wait(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
    assert reads.await_count == 2

    coordinator.async_update_listeners()  # what every webhook push publishes
    opened.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert reads.await_count == 2  # no read after it, and no push retry: it was no push


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_retries_of_a_push_stop_once_the_new_picture_is_listed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 2  # nothing new yet

    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer, MOTION_PICTURE, NEW_BELL), None)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)
    assert reads.await_count == 3

    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)
    assert reads.await_count == 3
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == taken_at_utc(newer).isoformat()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_unloading_while_a_read_runs_ends_cleanly_and_arms_no_retry(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    _gate(reads, ((MOTION_PICTURE, NEW_BELL), None))
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1))
    await hass.async_block_till_done()
    assert reads.await_count == 2  # the push's read, waiting

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_RETRY_SEC + 1)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert reads.await_count == 2


# --- nothing is read while the Doorstation cannot answer ------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_nothing_is_read_while_the_extension_is_offline_and_the_first_update_after_that_reads_at_once(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    reads = coordinator.api.get_doorstation_pictures
    coordinator.offline_extensions = {EXTENSION}

    await _later(hass, coordinator, clock, 1)
    async_dispatcher_send(hass, io_push_signal(SERVER_ID, 2, "DI1"), 1)
    await _fire_timers(hass, DOORSTATION_PICTURE_PUSH_DELAY_SEC + 1)
    assert reads.await_count == 1

    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    reads.return_value = ((newer, MOTION_PICTURE, NEW_BELL), None)
    coordinator.offline_extensions = set()
    await _later(hass, coordinator, clock, 1)  # far inside the interval: the offline update asked for a read at once

    assert reads.await_count == 2
    state = hass.states.get(BELL_ENTITY_ID)
    assert state is not None
    assert state.state == taken_at_utc(newer).isoformat()
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    assert coordinator.api.get_doorstation_picture.await_args_list[-1].args == (2, "bell_newer.jpg")


# --- what a changed list does to the entities -----------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_list_that_lost_a_kind_makes_that_picture_unknown_and_drops_its_image(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    assert await _image_bytes(hass, BELL_ENTITY_ID) == JPEG
    coordinator.api.get_doorstation_pictures.return_value = ((MOTION_PICTURE,), None)

    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)

    bell = hass.states.get(BELL_ENTITY_ID)
    motion = hass.states.get(MOTION_ENTITY_ID)
    assert bell is not None
    assert bell.state == STATE_UNKNOWN
    assert motion is not None
    assert motion.state == taken_at_utc(MOTION_PICTURE).isoformat()
    assert await _image_bytes(hass, BELL_ENTITY_ID) is None
    assert coordinator.api.get_doorstation_picture.await_count == 1  # the one before the list shrank


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_unchanged_list_does_not_tell_the_entities_and_a_changed_one_does(
    hass: HomeAssistant, entry: MockConfigEntry, clock: _Clock
) -> None:
    coordinator = await _setup_online(hass, entry)
    told: list[bool] = []
    unsubscribe = async_dispatcher_connect(hass, doorstation_pictures_signal(SERVER_ID, 2), lambda: told.append(True))

    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
    assert coordinator.api.get_doorstation_pictures.await_count == 2
    assert not told

    newer = DoorstationPicture(name="bell_newer.jpg", kind=BELL, taken_at=datetime(2026, 11, 22, 7, 0, 0))
    coordinator.api.get_doorstation_pictures.return_value = ((newer, MOTION_PICTURE), None)
    await _later(hass, coordinator, clock, DOORSTATION_PICTURE_REFRESH_INTERVAL_SEC)
    unsubscribe()

    assert told == [True]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_later_polls_add_no_second_set_of_pictures_and_read_nothing_extra(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    coordinator = await _setup_online(hass, entry)

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {e.entity_id for e in _images(hass, entry)} == {BELL_ENTITY_ID, MOTION_ENTITY_ID}
    assert coordinator.api.get_doorstation_pictures.await_count == 1


# --- the frontend ---------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_frontend_gets_the_picture_through_the_image_proxy(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client: Any
) -> None:
    assert await async_setup_component(hass, "http", {})
    coordinator = await _setup_online(hass, entry)  # before the client: its server freezes the router, and the
    assert coordinator is not None  # webhook component the entry depends on registers a view
    client = await hass_client()

    response = await client.get(f"/api/image_proxy/{BELL_ENTITY_ID}")

    assert response.status == 200
    assert response.content_type == "image/jpeg"
    assert await response.read() == JPEG
