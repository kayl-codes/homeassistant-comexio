"""Doorstation cameras: entity, device, image and what survives a page that shows no Doorstation."""

import asyncio
from collections.abc import Callable
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio.doorstation import Doorstation
import aiohttp
from homeassistant.components.camera import DATA_COMPONENT, Camera, async_get_image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.comexio.camera import ComexioDoorstationCamera
from custom_components.comexio.const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    DOORSTATION_ADDRESS_RECHECK_SEC,
    DOORSTATION_DIRECT_RETRY_SEC,
    DOORSTATION_DIRECT_TIMEOUT_SEC,
)
from custom_components.comexio.doorstation_stream import (
    DIRECT_REQUEST_HEADERS,
    SESSION_CLOSED_CAUSE,
    PrefetchedStream,
)

from .conftest import SERVER_ID, device_by_identifier

FRAME = b"\xff\xd8frame\xff\xd9"
CAMERA_UNIQUE_ID = f"comexio_{SERVER_ID}_doorstation2_camera"
CAMERA_ENTITY_ID = f"camera.{SERVER_ID}_doorstation2_camera"
# The Doorstation's own address (documentation range) and what it serves: a multipart head and a JPEG the frame
# reader of aiocomexio accepts (SOI, one segment, a scan with three bytes, EOI).
ADDRESS = "192.0.2.10"
DIRECT_URL = f"http://{ADDRESS}/proxy/cam/1/"  # the Doorstation's own camera 1, not the IO-Server's camera id
DIRECT_FRAME = bytes.fromhex("ffd8 ffe0 0004 4a46 ffda 0008 01 0100 003f00 123456 ffd9".replace(" ", ""))
DIRECT_STREAM = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + DIRECT_FRAME + b"\r\n--frame\r\n"
DIRECT_HEADERS = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}


@pytest.fixture
def api_returns(api_returns: dict[str, Any]) -> dict[str, Any]:
    """One Doorstation on extension 2 ("UD 1" of the synthetic config) whose camera answers with a frame."""
    api_returns["get_doorstations"] = ({2: Doorstation(ext_id=2, camera_id=7, name="UD 1")}, None)
    api_returns["get_doorstation_snapshot"] = (FRAME, None)
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


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_camera_sits_on_the_extension_device_and_serves_the_frame(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)

    registry = er.async_get(hass)
    camera = registry.async_get(CAMERA_ENTITY_ID)
    assert camera is not None
    assert camera.unique_id == CAMERA_UNIQUE_ID
    device = device_by_identifier(hass, entry.entry_id, {(DOMAIN, f"{SERVER_ID}_ud 1")})
    assert camera.device_id == device.id
    assert device.name == f"{SERVER_ID} EXT - UD 1"
    # The synthetic extension reports offline (no image then, see the next test): bring it back online.
    hass.data[DOMAIN][entry.entry_id].offline_extensions = set()
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_camera_of_an_offline_extension_serves_no_image(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert "UD 1" in coordinator.offline_extensions
    # The camera exists and is unavailable: without this, a setup that never created it would pass below as well.
    state = hass.states.get(CAMERA_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, CAMERA_ENTITY_ID)

    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_installation_without_doorstation_gets_no_camera(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({}, None)

    await _setup(hass, entry)

    registry = er.async_get(hass)
    assert not [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "camera"]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_page_without_doorstation_keeps_the_registered_camera_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    api_returns["get_doorstations"] = ({}, None)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(CAMERA_ENTITY_ID) is not None
    state = hass.states.get(CAMERA_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_doorstation_without_any_extension_name_gets_no_camera(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any]
) -> None:
    api_returns["get_doorstations"] = ({9: Doorstation(ext_id=9, camera_id=3, name=None)}, None)

    await _setup(hass, entry)

    registry = er.async_get(hass)
    assert not [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "camera"]


@pytest.fixture
def direct_address(api_returns: dict[str, Any]) -> None:
    """The extension of the Doorstation is configured with an address of its own."""
    api_returns["get_doorstation_address"] = (ADDRESS, None)


async def _online_camera(hass: HomeAssistant, entry: MockConfigEntry) -> ComexioDoorstationCamera:
    """Set the entry up and return its camera entity, the extension counted online."""
    await _setup(hass, entry)
    hass.data[DOMAIN][entry.entry_id].offline_extensions = set()
    camera = hass.data[DATA_COMPONENT].get_entity(CAMERA_ENTITY_ID)
    assert isinstance(camera, ComexioDoorstationCamera)
    return camera


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_image_comes_from_the_doorstation_itself_when_it_has_an_address(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    await _online_camera(hass, entry)

    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == DIRECT_FRAME

    api = hass.data[DOMAIN][entry.entry_id].api
    api.get_doorstation_address.assert_awaited_once_with(2)
    api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_doorstation_without_an_address_stays_on_the_io_server(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    await _online_camera(hass, entry)

    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    assert aioclient_mock.call_count == 0
    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_snapshot.assert_awaited_once_with(7)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_failed_direct_read_falls_back_and_pauses_the_direct_way(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    # The image still comes (over the IO-Server) and the failure is warned about once, with no address in it.
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME
    camera._frame = None  # the next request must read, not take the cached frame
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    assert aioclient_mock.call_count == 1  # the second read did not try the Doorstation again
    assert api.get_doorstation_address.await_count == 1  # nor did it ask the IO-Server for the address
    assert api.get_doorstation_snapshot.await_count == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Doorstation itself" in r.getMessage()]
    assert len(warnings) == 1
    assert "HTTP 503" in warnings[0].getMessage()
    assert ADDRESS not in warnings[0].getMessage()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_direct_way_is_tried_again_after_the_pause_with_a_fresh_address(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api
    await async_get_image(hass, CAMERA_ENTITY_ID)
    assert api.get_doorstation_address.await_count == 1

    # The pause is over, and the Doorstation answers now.
    aioclient_mock.clear_requests()
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    assert camera._direct_failed_at is not None
    camera._direct_failed_at -= DOORSTATION_DIRECT_RETRY_SEC + 1
    camera._frame = None
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == DIRECT_FRAME

    assert api.get_doorstation_address.await_count == 2  # read afresh: the address may have changed
    assert api.get_doorstation_snapshot.await_count == 1  # only the read of the failed attempt
    assert "works again" in caplog.text


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_direct_image_is_kept_for_the_minimum_interval(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    await _online_camera(hass, entry)

    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == DIRECT_FRAME
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == DIRECT_FRAME

    assert aioclient_mock.call_count == 1  # the second request got the frame read a moment ago
    assert hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.await_count == 1


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_after_the_recheck_the_image_is_read_from_the_new_address(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    new_url = "http://192.0.2.11/proxy/cam/1/"
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    aioclient_mock.get(new_url, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api
    await async_get_image(hass, CAMERA_ENTITY_ID)

    # The extension was given another address, and the recheck interval has passed.
    api.get_doorstation_address.return_value = ("192.0.2.11", None)
    assert camera._address_checked_at is not None
    camera._address_checked_at -= DOORSTATION_ADDRESS_RECHECK_SEC + 1
    camera._frame = None
    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == DIRECT_FRAME

    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [DIRECT_URL, new_url]


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_camera_that_went_during_the_address_lookup_asks_the_io_server_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]

    async def lookup_while_the_entry_unloads(_ext_id: int) -> tuple[None, None]:
        coordinator.unloading = True
        return None, None

    coordinator.api.get_doorstation_address.side_effect = lookup_while_the_entry_unloads

    assert await camera._read_frame(doorstation) == (None, None)

    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_address_that_was_not_found_is_looked_for_again_only_after_the_recheck_interval(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    lookup = hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address

    assert await camera._direct_address() is None
    assert await camera._direct_address() is None
    assert lookup.await_count == 1  # not at every image: Comexio serves one request at a time

    assert camera._address_checked_at is not None
    camera._address_checked_at -= DOORSTATION_ADDRESS_RECHECK_SEC - 5
    assert await camera._direct_address() is None
    assert lookup.await_count == 1

    # One was configured meanwhile.
    lookup.return_value = (ADDRESS, None)
    camera._address_checked_at -= 10
    assert await camera._direct_address() == ADDRESS
    assert lookup.await_count == 2

    # Within the interval a known address is not looked up again.
    assert await camera._direct_address() == ADDRESS
    assert lookup.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_known_address_is_looked_up_again_after_the_recheck_interval(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    lookup = hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address
    assert await camera._direct_address() == ADDRESS
    assert lookup.await_count == 1

    # The extension was given another address meanwhile: the next read goes there, not to the old one.
    lookup.return_value = ("192.0.2.11", None)
    assert camera._address_checked_at is not None
    camera._address_checked_at -= DOORSTATION_ADDRESS_RECHECK_SEC - 5
    assert await camera._direct_address() == ADDRESS  # not due yet
    camera._address_checked_at -= 10
    assert await camera._direct_address() == "192.0.2.11"
    assert lookup.await_count == 2

    # An address that was taken away is dropped as well (the camera stays on the IO-Server from then on).
    lookup.return_value = (None, None)
    camera._address_checked_at -= DOORSTATION_ADDRESS_RECHECK_SEC + 1
    assert await camera._direct_address() is None
    assert lookup.await_count == 3


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_failed_address_lookup_beside_a_known_address_keeps_using_that_one(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)
    lookup = hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address
    assert await camera._direct_address() == ADDRESS

    camera._address_checked_at = None  # the next call looks again
    lookup.return_value = (None, "timed out")
    assert await camera._direct_address() == ADDRESS

    # Nothing says the address is wrong: no pause, no warning (the cause is only debug-logged), no new lookup at the
    # next image.
    assert camera._direct_failed_at is None
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "custom_components.comexio.camera"
    ]
    assert "address lookup failed (timed out), keeping the known address" in caplog.text
    assert await camera._direct_address() == ADDRESS
    assert lookup.await_count == 2


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_address_lookup_without_a_known_address_pauses_the_direct_way_and_says_why(
    hass: HomeAssistant, entry: MockConfigEntry, api_returns: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    api_returns["get_doorstation_address"] = (None, "timed out")
    camera = await _online_camera(hass, entry)

    # Not "no address" for ten minutes, but a failure of the direct way: warned about once, paused.
    assert await camera._direct_address() is None

    assert camera._direct_failed_at is not None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "address not read" in r.getMessage()]
    assert len(warnings) == 1
    assert "timed out" in warnings[0].getMessage()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_direct_way_stays_paused_until_the_retry_interval_has_passed(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    await async_get_image(hass, CAMERA_ENTITY_ID)
    assert camera._direct_failed_at is not None

    camera._direct_failed_at = time.monotonic() - (DOORSTATION_DIRECT_RETRY_SEC - 5)
    assert await camera._direct_address() is None
    camera._direct_failed_at = time.monotonic() - (DOORSTATION_DIRECT_RETRY_SEC + 5)
    assert await camera._direct_address() == ADDRESS


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failure_streak_is_warned_about_once_and_its_end_reported_once(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)

    def warnings() -> int:
        return len(
            [r for r in caplog.records if r.levelno == logging.WARNING and "Doorstation itself" in r.getMessage()]
        )

    def recoveries() -> int:
        return caplog.text.count("works again")

    camera._direct_worked()  # nothing failed before: nothing to report
    assert recoveries() == 0
    camera._direct_failed("first")
    camera._direct_failed("second")
    assert warnings() == 1
    camera._direct_worked()
    camera._direct_worked()
    assert recoveries() == 1
    camera._direct_failed("third")  # a new streak is warned about again
    assert warnings() == 2


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("answer", [(None, "HTTP 503"), (DIRECT_FRAME, None)], ids=["failed", "delivered"])
async def test_a_direct_read_that_ends_after_the_extension_went_offline_starts_or_ends_no_streak(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: tuple[bytes | None, str | None],
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # A streak is under way (its pause is over): neither answer may end it, nor start a new one.
    streak_began = camera._direct_failed_at = time.monotonic() - DOORSTATION_DIRECT_RETRY_SEC - 1
    camera._direct_warned = True

    async def read_while_the_extension_goes(*_args: object, **_kwargs: object) -> tuple[bytes | None, str | None]:
        coordinator.offline_extensions = {"UD 1"}
        return answer

    with (
        patch("custom_components.comexio.camera.read_direct_frame", read_while_the_extension_goes),
        pytest.raises(HomeAssistantError),
    ):
        await async_get_image(hass, CAMERA_ENTITY_ID)

    assert camera._direct_failed_at == streak_began
    assert camera._direct_warned
    assert "Doorstation itself" not in caplog.text
    assert "works again" not in caplog.text
    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_two_requests_at_once_look_the_address_up_once(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    camera = await _online_camera(hass, entry)
    lookup = hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(0.05)
        return ADDRESS, None

    lookup.side_effect = slow_lookup

    assert await asyncio.gather(camera._direct_address(), camera._direct_address()) == [ADDRESS, ADDRESS]
    assert lookup.await_count == 1


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize("gone", ["extension offline", "doorstation gone", "camera changed", "entry unloading"])
async def test_a_failed_address_lookup_of_a_camera_that_is_gone_starts_no_failure_streak(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture, gone: str
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def lookup_while_the_extension_goes(_ext_id: int) -> tuple[None, str]:
        if gone == "extension offline":
            coordinator.offline_extensions = {"UD 1"}
        elif gone == "entry unloading":
            coordinator.unloading = True
        elif gone == "camera changed":
            coordinator.doorstations.known[2] = Doorstation(ext_id=2, camera_id=8, name="UD 1")
        else:
            coordinator.doorstations.known.pop(2)
        return None, "timed out"

    coordinator.api.get_doorstation_address.side_effect = lookup_while_the_extension_goes

    assert await camera._direct_address() is None

    assert camera._direct_failed_at is None
    assert "Doorstation itself" not in caplog.text


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_read_that_fails_while_the_address_is_looked_up_keeps_the_pause(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)

    async def lookup_while_a_read_fails(_ext_id: int) -> tuple[str, None]:
        camera._direct_failed("HTTP 503")  # a stream or still read failed meanwhile
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = lookup_while_a_read_fails

    # The address read before the failure is not trusted, and no read is opened inside the pause.
    assert await camera._direct_address() is None

    assert camera._direct_failed_at is not None
    assert camera._address_checked_at is None
    assert camera._address is None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_an_address_lookup_that_uses_up_the_budget_of_a_still_read_blames_the_io_server(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    api = hass.data[DOMAIN][entry.entry_id].api
    api.get_doorstation_address.side_effect = slow_lookup

    with patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05):
        assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    # The pause is set inside Home Assistant's own image timeout, and the IO-Server was asked after it.
    assert camera._direct_failed_at is not None
    # The line says what was missing (the IO-Server's answer for the address), not that a read failed.
    assert "no image from the Doorstation itself (the IO-Server did not answer in time)" in caplog.text
    assert "reading from the Doorstation itself failed" not in caplog.text
    api.get_doorstation_snapshot.assert_awaited_once_with(7)
    assert not camera._address_lock.locked()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_the_read_of_a_still_gets_what_the_address_lookup_left_of_the_budget(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]

    async def lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(0.15)
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = lookup
    read = AsyncMock(return_value=(None, "no frame in time"))

    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.5),
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_MIN_READ_SEC", 0.1),
        patch("custom_components.comexio.camera.read_direct_frame", read),
    ):
        await camera._read_direct_frame(doorstation)

    (budget,) = (call.kwargs["budget_sec"] for call in read.await_args_list)
    assert 0.1 <= budget <= 0.35  # 0.5 s in all, 0.15 s of it gone in the lookup
    assert camera._direct_failed_at is not None
    assert not camera._address_lock.locked()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_lookup_that_runs_out_of_time_for_a_camera_that_went_starts_no_streak_and_asks_nobody(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]

    async def lookup_while_the_entry_unloads(_ext_id: int) -> tuple[str, None]:
        coordinator.unloading = True
        await asyncio.sleep(30)
        return ADDRESS, None

    coordinator.api.get_doorstation_address.side_effect = lookup_while_the_entry_unloads

    with patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05):
        # The camera went: nothing to report for it (the caller drops the answer), neither a cause nor a streak.
        assert await camera._read_frame(doorstation) == (None, None)

    assert camera._direct_failed_at is None
    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_still_read_that_ends_while_the_entry_unloads_starts_no_streak_and_asks_nobody(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]

    async def read_while_the_entry_unloads(*_args: object, **_kwargs: object) -> tuple[None, str]:
        coordinator.unloading = True
        return None, "HTTP 500"

    with patch("custom_components.comexio.camera.read_direct_frame", read_while_the_entry_unloads):
        assert await camera._read_frame(doorstation) == (None, "HTTP 500")

    assert camera._direct_failed_at is None
    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_camera_that_went_during_a_successful_address_lookup_sends_no_request_to_the_doorstation(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]

    async def lookup_while_the_entry_unloads(_ext_id: int) -> tuple[str, None]:
        coordinator.unloading = True
        return ADDRESS, None

    coordinator.api.get_doorstation_address.side_effect = lookup_while_the_entry_unloads
    read = AsyncMock(return_value=(None, "HTTP 500"))

    with patch("custom_components.comexio.camera.read_direct_frame", read):
        assert await camera._read_frame(doorstation) == (None, None)
        assert await camera.async_camera_image() is None  # the caller drops it: no cache, no streak, lock free

    read.assert_not_called()  # the address was found, but nobody asks for this image any more
    assert camera._address == ADDRESS
    assert camera._address_checked_at is not None
    assert camera._frame is None
    assert camera._failed_at is None
    assert camera._direct_failed_at is None
    assert not camera._lock.locked()
    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_read_that_ends_with_home_assistant_stopping_starts_no_failure_streak(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def read_while_home_assistant_stops(*_args: object, **_kwargs: object) -> tuple[None, str]:
        hass.set_state(CoreState.stopping)  # its shared web session is closed under the read
        return None, SESSION_CLOSED_CAUSE

    try:
        with patch("custom_components.comexio.camera.read_direct_frame", read_while_home_assistant_stops):
            assert await camera.async_camera_image() is None  # dropped like the answer of a camera that went
    finally:
        hass.set_state(CoreState.running)

    assert camera._direct_failed_at is None  # no pause, no warning for a Doorstation that is fine
    assert camera._warned_camera_id is None
    assert camera._failed_at is None
    assert camera._frame is None
    assert not camera._lock.locked()
    coordinator.api.get_doorstation_snapshot.assert_not_called()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_still_read_that_is_cancelled_in_the_address_lookup_lets_the_lock_go(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    in_lookup = asyncio.Event()

    async def lookup(_ext_id: int) -> tuple[str, None]:
        in_lookup.set()
        await asyncio.sleep(30)
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = lookup
    read = asyncio.create_task(camera._read_direct_frame(doorstation))
    await in_lookup.wait()
    assert camera._address_lock.locked()  # the lookup is under way

    read.cancel()  # Home Assistant's own image timeout
    with pytest.raises(asyncio.CancelledError):
        await read

    assert not camera._address_lock.locked()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_lookup_that_runs_out_of_time_beside_a_known_address_asks_the_io_server_and_pauses_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    api.get_doorstation_address.side_effect = slow_lookup
    camera._address = ADDRESS  # known; the lookup is only the refresh after a failed read
    read = AsyncMock(return_value=(DIRECT_FRAME, None))

    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05),
        patch("custom_components.comexio.camera.read_direct_frame", read),
    ):
        assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    read.assert_not_called()  # the budget went in the lookup, the Doorstation was not asked
    api.get_doorstation_snapshot.assert_awaited_once_with(7)
    assert camera._direct_failed_at is None
    assert "Doorstation itself" not in caplog.text
    assert camera._address == ADDRESS


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_lookup_that_leaves_too_little_of_the_budget_starts_no_read_and_no_streak(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    async def lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(0.12)
        return ADDRESS, None

    api.get_doorstation_address.side_effect = lookup
    read = AsyncMock(return_value=(DIRECT_FRAME, None))

    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.15),
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_MIN_READ_SEC", 0.1),
        patch("custom_components.comexio.camera.read_direct_frame", read),
    ):
        assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    read.assert_not_called()
    assert camera._direct_failed_at is None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_refresh_of_a_known_address_that_times_out_is_not_repeated_by_every_read(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    api.get_doorstation_address.side_effect = slow_lookup
    camera._address, camera._address_checked_at = ADDRESS, None  # known, refresh due after a failed read

    with patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05):
        assert await camera._address_within_budget() == (ADDRESS, True)
        assert not camera._address_is_due()  # the next read does not ask again
        asked = api.get_doorstation_address.await_count
        assert await camera._address_within_budget() == (ADDRESS, False)

    assert api.get_doorstation_address.await_count == asked
    assert camera._address == ADDRESS
    assert camera._direct_failed_at is None  # a slow IO-Server is no failure of the Doorstation


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_failed_read_during_a_timed_out_refresh_keeps_its_reset_of_the_address_check(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        camera._direct_failed("stream failed meanwhile")  # a failure lands while the lookup is pending
        await asyncio.sleep(30)
        return ADDRESS, None

    api.get_doorstation_address.side_effect = slow_lookup
    camera._address, camera._address_checked_at = ADDRESS, None

    with patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05):
        assert await camera._address_within_budget() == (ADDRESS, True)

    assert camera._direct_paused()  # the failure inside the lookup started the pause
    assert camera._address_checked_at is None  # ... and its reset was not undone by the timeout
    assert not camera._address_lock.locked()  # the cancelled lookup let go of the lock


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_lookup_that_times_out_with_no_address_known_is_asked_again(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = slow_lookup
    camera._address, camera._address_checked_at = None, None

    with patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05):
        assert await camera._address_within_budget() == (None, True)

    assert camera._address_is_due()  # nothing is known: the next read looks again


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_closed_session_while_home_assistant_runs_is_an_ordinary_failure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    # Without HA stopping nobody explains a closed session away: the direct way pauses and the IO-Server answers.
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with patch(
        "custom_components.comexio.camera.read_direct_frame", AsyncMock(return_value=(None, SESSION_CLOSED_CAUSE))
    ):
        assert await camera.async_camera_image() == FRAME

    assert camera._direct_failed_at is not None
    coordinator.api.get_doorstation_snapshot.assert_awaited_once()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_nothing_is_read_or_handed_out_once_home_assistant_is_stopping(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]
    camera._frame, camera._frame_at, camera._frame_camera_id = FRAME, time.monotonic(), doorstation.camera_id
    read = AsyncMock()

    hass.set_state(CoreState.stopping)
    try:
        with patch("custom_components.comexio.camera.read_direct_frame", read):
            assert await camera.async_camera_image() is None
    finally:
        hass.set_state(CoreState.running)

    read.assert_not_called()
    coordinator.api.get_doorstation_snapshot.assert_not_called()
    assert camera._frame is None
    assert camera._direct_failed_at is None
    assert camera._failed_at is None


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_cached_frame_is_not_handed_out_while_the_entry_unloads(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]
    camera._frame, camera._frame_at, camera._frame_camera_id = FRAME, time.monotonic(), doorstation.camera_id
    assert await camera.async_camera_image() == FRAME  # fresh, as long as the entry is up

    coordinator.unloading = True
    try:
        assert await camera.async_camera_image() is None
        assert camera._frame is None
        # Nothing cached either: no read starts, so no failure streak can start.
        assert await camera.async_camera_image() is None
        coordinator.api.get_doorstation_snapshot.assert_not_called()
        assert camera._failed_at is None
        assert camera._warned_camera_id is None
    finally:
        coordinator.unloading = False


@pytest.fixture(autouse=True)
def one_stream_per_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resp.content` is one stream, as in aiohttp: the mock builds a fresh one at every access, which would hand the
    live stream's first bytes out twice (once as the prefetch, once as the rest)."""
    original = AiohttpClientMockResponse.content

    def content(self: AiohttpClientMockResponse) -> Any:
        if (stream := getattr(self, "_one_stream", None)) is None:
            stream = self._one_stream = original.fget(self)  # type: ignore[attr-defined]
        return stream

    monkeypatch.setattr(AiohttpClientMockResponse, "content", property(content))


@pytest.fixture(autouse=True)
def mock_streams_end_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock streams hold a few bytes and end: that is no device dropping the connection (the tests that mean it
    set the minimum run time back)."""
    monkeypatch.setattr("custom_components.comexio.camera.DOORSTATION_LIVE_STREAM_MIN_RUN_SEC", 0.0)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_live_stream_is_passed_on_from_the_doorstation(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    await _online_camera(hass, entry)
    client = await hass_client()

    response = await client.get(f"/api/camera_proxy_stream/{CAMERA_ENTITY_ID}")

    assert response.status == 200
    # Rebuilt from the media type and the boundary, never the Doorstation's header as it came; the response keeps a
    # browser from running anything in the stream of a device on the network.
    assert response.headers["Content-Type"] == 'multipart/x-mixed-replace;boundary="frame"'
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox"
    assert await response.read() == DIRECT_STREAM
    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_snapshot.assert_not_called()
    # Asked for unencoded: the byte cap of the still image counts bytes of the stream itself.
    assert aioclient_mock.mock_calls[0][3]["Accept-Encoding"] == "identity"


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("failure", [aiohttp.ClientError, TimeoutError])
async def test_a_live_stream_that_cannot_connect_falls_back_to_the_default_and_pauses_the_direct_way(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, failure: type[Exception]
) -> None:
    aioclient_mock.get(DIRECT_URL, exc=failure)
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    default_stream.assert_awaited_once()
    assert camera._direct_failed_at is not None
    assert await camera._direct_address() is None  # paused: the stills go over the IO-Server meanwhile


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_answered_with_an_error_status_falls_back_and_pauses_the_direct_way(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Home Assistant's own proxy helper would hand this error page on as the stream: it is checked first.
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    default_stream.assert_awaited_once()
    assert camera._direct_failed_at is not None
    assert "HTTP 503" in caplog.text
    assert ADDRESS not in caplog.text
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_of_another_type_is_not_passed_on_to_the_frontend(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The type reaches the browser as the Doorstation sent it, over plain HTTP: text/html would run on HA's origin.
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers={"Content-Type": "text/html"})
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    default_stream.assert_awaited_once()
    assert camera._direct_failed_at is not None
    assert "the answer is not an MJPEG stream (Content-Type text/html)" in caplog.text
    assert not camera._direct_gate.locked()
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api")
@pytest.mark.parametrize(("case", "lookups"), [("no address", 1), ("extension offline", 0), ("no Doorstation", 0)])
async def test_the_live_stream_stays_on_still_images_without_a_direct_way(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    api_returns: dict[str, Any],
    case: str,
    lookups: int,
) -> None:
    if case != "no address":
        api_returns["get_doorstation_address"] = (ADDRESS, None)
    await _setup(hass, entry)
    camera = hass.data[DATA_COMPONENT].get_entity(CAMERA_ENTITY_ID)
    assert isinstance(camera, ComexioDoorstationCamera)
    if case != "extension offline":
        hass.data[DOMAIN][entry.entry_id].offline_extensions = set()
    default_stream = AsyncMock(return_value="the still stream")

    if case == "no Doorstation":
        # The page no longer shows it (the camera is unavailable then, which is what keeps the direct way shut).
        hass.data[DOMAIN][entry.entry_id].doorstations.known.pop(2)
        assert not camera.available
    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    default_stream.assert_awaited_once()
    assert aioclient_mock.call_count == 0
    assert hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.await_count == lookups


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_answered_with_200_and_no_data_falls_back_and_pauses_the_direct_way(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=b"", headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert camera._direct_failed_at is not None
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_no_second_connection_is_opened_to_the_doorstation_beside_a_running_live_stream(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    camera = await _online_camera(hass, entry)
    camera._streams_open = 1

    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    assert aioclient_mock.call_count == 0
    api = hass.data[DOMAIN][entry.entry_id].api
    api.get_doorstation_address.assert_not_called()
    api.get_doorstation_snapshot.assert_awaited_once_with(7)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_that_works_again_ends_the_failure_streak(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    await async_get_image(hass, CAMERA_ENTITY_ID)  # the failure, warned about
    assert camera._direct_warned

    aioclient_mock.clear_requests()
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    assert camera._direct_failed_at is not None
    camera._direct_failed_at -= DOORSTATION_DIRECT_RETRY_SEC + 1
    client = await hass_client()
    assert await (await client.get(f"/api/camera_proxy_stream/{CAMERA_ENTITY_ID}")).read() == DIRECT_STREAM

    assert camera._direct_failed_at is None
    assert not camera._direct_warned
    assert "works again" in caplog.text
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_doorstation_that_does_not_answer_the_live_stream_request_is_given_up_on(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)

    class _Hanging:
        async def get(self, *_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(30)

    default_stream = AsyncMock(return_value="the still stream")
    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05),
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Hanging()),
        patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
    ):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert camera._direct_failed_at is not None
    assert "no answer in time" in caplog.text
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_answer_of_a_live_stream_that_is_not_passed_on_is_closed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    resp = MagicMock(status=503, headers={})

    class _Session:
        async def get(self, *_args: object, **_kwargs: object) -> MagicMock:
            return resp

    default_stream = AsyncMock(return_value="the still stream")
    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()),
        patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
    ):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    resp.close.assert_called_once_with()
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("body", [b"--frame\r\nContent-Type: image/jpeg\r\n\r\n", b"--frame\r\n" + DIRECT_FRAME[:-1]])
async def test_a_live_stream_answered_with_200_and_data_but_no_whole_frame_falls_back(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    """Bytes alone do not make a stream: it counts once a whole picture came."""
    aioclient_mock.get(DIRECT_URL, content=body, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert camera._direct_failed_at is not None
    assert "the stream ended without a frame" in caplog.text
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize(
    ("content_type", "cause"),
    [
        ("multipart/x-mixed-replace", "no boundary"),
        ("multipart/x-mixed-replace; boundary=a<b>", "unsupported characters"),
    ],
    ids=["no boundary", "unsupported characters"],
)
async def test_a_live_stream_without_a_usable_boundary_falls_back_and_pauses_the_direct_way(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    content_type: str,
    cause: str,
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers={"Content-Type": content_type})
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert camera._direct_failed_at is not None
    assert cause in caplog.text
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_forwarder_gets_the_clean_content_type_and_a_callback_for_the_end(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    seen: list[tuple[object, ...]] = []

    async def proxy(*args: object) -> str:
        seen.append(args)
        return "ended"

    request = MagicMock()
    with patch("custom_components.comexio.camera.forward_live_stream", proxy):
        assert await camera._proxy_direct_stream(request, DIRECT_URL, doorstation) == "ended"

    ((got_hass, got_request, _stream, content_type, on_ended),) = seen
    assert (got_hass, got_request) == (hass, request)
    assert content_type == 'multipart/x-mixed-replace;boundary="frame"'
    with patch.object(camera, "_live_stream_ended") as ended:
        on_ended("why", 1.5, True)
    ended.assert_called_once_with(doorstation, "why", 1.5, True)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize(
    ("delivered_sec", "blame", "failure"),
    [
        (1.0, True, True),
        (1.0, False, False),
        (5.0, True, False),
        (6.0, True, False),
    ],
    ids=[
        "device cut it off after one second of data",
        "viewer left after one second of data",
        "exactly the minimum is no failure",
        "device cut it off after a while",
    ],
)
async def test_only_a_stream_the_doorstation_cut_off_early_counts_as_a_failure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    delivered_sec: float,
    blame: bool,
    failure: bool,
) -> None:
    monkeypatch.setattr("custom_components.comexio.camera.DOORSTATION_LIVE_STREAM_MIN_RUN_SEC", 5.0)
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]

    camera._live_stream_ended(doorstation, "the Doorstation ended the stream", delivered_sec, blame)

    assert (camera._direct_failed_at is not None) is failure
    assert ("live stream: the Doorstation ended the stream after 1 s of data" in caplog.text) is (
        failure and delivered_sec == 1.0
    )


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize(
    ("delivered_sec", "blame", "recovers"),
    [
        (1.0, False, False),
        (1.0, True, False),
        (5.0, False, True),
        (6.0, True, True),
    ],
    ids=[
        "viewer left after one second: proves nothing",
        "device cut it off after one second",
        "viewer left after the minimum: the stream worked",
        "device cut it off after a while: the stream worked",
    ],
)
async def test_only_a_stream_that_delivered_for_a_while_ends_a_failure_streak(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    delivered_sec: float,
    blame: bool,
    recovers: bool,
) -> None:
    """A viewer that leaves after a second must not wipe a streak the Doorstation has earned."""
    monkeypatch.setattr("custom_components.comexio.camera.DOORSTATION_LIVE_STREAM_MIN_RUN_SEC", 5.0)
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    camera._direct_failed("earlier")
    earlier = camera._direct_failed_at

    camera._live_stream_ended(doorstation, "the stream ended", delivered_sec, blame)

    assert ("works again" in caplog.text) is recovers
    assert (camera._direct_failed_at is None) is recovers
    # Neutral (not blamed, too short) is told from a failure by the streak: a failure renews its time, neutral keeps it.
    renewed = not recovers and camera._direct_failed_at != earlier
    assert renewed is (blame and not recovers)
    assert ("the stream ended after 1 s of data" in caplog.text) is renewed


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_device_that_cuts_every_stream_short_keeps_one_warning_and_never_works_again(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start of a stream must not clear the streak the end of that stream continues."""
    monkeypatch.setattr("custom_components.comexio.camera.DOORSTATION_LIVE_STREAM_MIN_RUN_SEC", 5.0)
    camera = await _online_camera(hass, entry)
    client = await hass_client()

    for _ in range(2):
        aioclient_mock.clear_requests()  # a mock answer's stream can be read once
        aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
        assert await (await client.get(f"/api/camera_proxy_stream/{CAMERA_ENTITY_ID}")).read() == DIRECT_STREAM
        assert camera._direct_failed_at is not None
        camera._direct_failed_at -= DOORSTATION_DIRECT_RETRY_SEC + 1  # the pause is over

    assert "works again" not in caplog.text
    assert caplog.text.count("no image from the Doorstation itself") == 2
    assert [r.levelno for r in caplog.records if "no image from the Doorstation itself" in r.message] == [
        logging.WARNING,
        logging.DEBUG,
    ]
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_answer_without_a_content_type_that_got_through_the_check_fails_loudly(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    request = MagicMock()

    with (
        patch("custom_components.comexio.camera.live_stream_content_type", return_value=None),
        pytest.raises(RuntimeError, match="no usable content type"),
    ):
        await camera._proxy_direct_stream(request, DIRECT_URL, doorstation)

    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_stream_that_ends_at_once_for_a_camera_that_changed_starts_no_failure_streak(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.comexio.camera.DOORSTATION_LIVE_STREAM_MIN_RUN_SEC", 5.0)
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]
    coordinator.offline_extensions = {"UD 1"}

    camera._live_stream_ended(doorstation, "the Doorstation ended the stream", 0.2, True)

    assert camera._direct_failed_at is None


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("ending", ["extension offline", "entry unloading", "camera changed", "doorstation gone"])
async def test_a_running_live_stream_ends_with_the_camera_it_was_started_for(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, ending: str
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    alive_calls: list[Callable[[], bool]] = []

    def spy(first: bytes, rest: object, alive: Callable[[], bool]) -> PrefetchedStream:
        alive_calls.append(alive)
        return PrefetchedStream(first, rest, alive)  # type: ignore[arg-type]

    async def proxy(_hass: object, _request: object, _stream: object, _content_type: object, _on_ended: object) -> str:
        return "ended"

    with (
        patch("custom_components.comexio.camera.PrefetchedStream", spy),
        patch("custom_components.comexio.camera.forward_live_stream", proxy),
    ):
        doorstation = coordinator.doorstations.known[2]
        assert await camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation) == "ended"

    (alive,) = alive_calls
    assert alive()  # the camera it was started for is there
    if ending == "extension offline":
        coordinator.offline_extensions = {"UD 1"}
    elif ending == "entry unloading":
        coordinator.unloading = True
    elif ending == "camera changed":
        coordinator.doorstations.known[2] = Doorstation(ext_id=2, camera_id=8, name="UD 1")
    else:
        coordinator.doorstations.known.pop(2)
    assert not alive()  # the stream ends at its next read, the connection is let go
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("failing", [False, True], ids=["delivering", "failing"])
async def test_a_camera_that_goes_while_the_live_stream_starts_gets_no_stream_and_no_failure_streak(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    failing: bool,
) -> None:
    camera = await _online_camera(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    doorstation = coordinator.doorstations.known[2]
    resp = MagicMock(status=503 if failing else 200, headers={} if failing else DIRECT_HEADERS)
    resp.content.read = AsyncMock(side_effect=[DIRECT_STREAM, b""])

    class _Session:
        async def get(self, *_args: object, **_kwargs: object) -> MagicMock:
            # The camera was fine when the request went out (the check at the gate passed) and is gone by the time
            # the Doorstation has answered.
            coordinator.offline_extensions = {"UD 1"}
            return resp

    async def proxy(*_args: object) -> str:
        raise AssertionError("nothing is passed on for a camera that is gone")

    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()),
        patch("custom_components.comexio.camera.forward_live_stream", proxy),
    ):
        assert await camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation) is None

    assert camera._direct_failed_at is None
    assert not camera._direct_warned
    assert "Doorstation itself" not in caplog.text
    assert camera._streams_open == 0
    resp.close.assert_called_once()


async def _settled(condition: Callable[[], bool]) -> None:
    """Let the event loop run until `condition` holds, then a bounded number of further loop turns.

    No fixed sleep: a loaded test runner only takes longer, as the turns after it are loop iterations, not time.
    The further turns are a heuristic, not proof of quiescence: a test that asserts something did NOT happen also
    needs a positive check afterwards.
    """
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
@pytest.mark.parametrize("hang", ["gate", "connect", "first frame", "stream"])
async def test_a_viewer_that_goes_away_gives_back_the_stream_count_the_gate_and_the_connection(
    hass: HomeAssistant, entry: MockConfigEntry, hang: str
) -> None:
    """The browser closes the tab: the viewer's task is cancelled wherever it waits."""
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    never = asyncio.Event()
    waiting = asyncio.Event()  # set where the viewer hangs (all but "gate"), so it is cancelled there and not before
    resp = MagicMock(status=200, headers=DIRECT_HEADERS)

    async def read_never(*_args: object) -> bytes:
        waiting.set()
        await never.wait()
        return b""

    resp.content.read = read_never if hang == "first frame" else AsyncMock(side_effect=[DIRECT_STREAM, b""])

    class _Session:
        async def get(self, *_args: object, **_kwargs: object) -> MagicMock:
            if hang == "connect":
                waiting.set()
                await never.wait()
            return resp

    async def proxy(*_args: object) -> str:
        waiting.set()
        await never.wait()
        return "never"

    if hang == "gate":
        await camera._direct_gate.acquire()  # a still read is under way
    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()),
        patch("custom_components.comexio.camera.forward_live_stream", proxy),
    ):
        viewer = asyncio.create_task(camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation))
        if hang == "gate":
            await _settled(lambda: camera._streams_open == 1)  # counted, and queued at the gate that is held
        else:
            async with asyncio.timeout(5):
                await waiting.wait()
        assert not viewer.done()
        assert camera._streams_open == 1
        viewer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await viewer
    if hang == "gate":
        camera._direct_gate.release()

    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()
    assert camera._direct_failed_at is None  # a viewer that left is no failure of the Doorstation
    if hang in {"first frame", "stream"}:
        resp.close.assert_called_once()
    else:
        resp.close.assert_not_called()  # nothing was opened (the request did not answer / was not sent)


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_still_read_that_starts_while_the_live_stream_connects_opens_no_second_connection(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    counted: list[int] = []

    class _Session:
        async def get(self, *_args: object, **_kwargs: object) -> MagicMock:
            counted.append(camera._streams_open)  # the request is under way
            return MagicMock(status=503, headers={})

    with patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()):
        await camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation)

    assert counted == [1]
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_live_stream_that_starts_while_the_address_is_looked_up_gets_no_second_connection(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    camera = await _online_camera(hass, entry)

    async def lookup_while_a_stream_starts(_ext_id: int) -> tuple[str, None]:
        camera._streams_open = 1
        return ADDRESS, None

    api = hass.data[DOMAIN][entry.entry_id].api
    api.get_doorstation_address.side_effect = lookup_while_a_stream_starts

    assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    assert aioclient_mock.call_count == 0
    api.get_doorstation_snapshot.assert_awaited_once_with(7)
    assert camera._direct_failed_at is None  # the IO-Server answered because of the stream, nothing failed


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_live_stream_request_does_not_follow_a_redirect(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    seen: list[dict[str, Any]] = []

    class _Session:
        async def get(self, *_args: object, **kwargs: Any) -> MagicMock:
            seen.append(kwargs)
            return MagicMock(status=302, headers={})

    with patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()):
        await camera._connect_direct_stream(DIRECT_URL, doorstation)

    assert [kwargs.get("allow_redirects") for kwargs in seen] == [False]


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_the_live_stream_request_is_limited_only_at_the_connect_and_asks_for_the_stream_unencoded(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The shared session cuts a whole request off after 300 s: a live stream must not run into that."""
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    seen: list[dict[str, Any]] = []

    class _Session:
        async def get(self, *_args: object, **kwargs: Any) -> MagicMock:
            seen.append(kwargs)
            return MagicMock(status=302, headers={})

    with patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()):
        await camera._connect_direct_stream(DIRECT_URL, doorstation)

    (kwargs,) = seen
    assert kwargs["timeout"].total is None
    assert kwargs["timeout"].sock_connect == DOORSTATION_DIRECT_TIMEOUT_SEC
    assert kwargs["auto_decompress"] is False  # the check of Content-Encoding sees what the Doorstation sent
    assert kwargs["headers"] == DIRECT_REQUEST_HEADERS


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_live_stream_failures_warn_once_per_streak_name_the_live_stream_and_hold_the_pause(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.get(DIRECT_URL, status=503)
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        await camera.handle_async_mjpeg_stream(MagicMock())
        failed_at = camera._direct_failed_at
        await camera.handle_async_mjpeg_stream(MagicMock())  # inside the pause: nobody is asked
        assert aioclient_mock.call_count == 1
        assert camera._direct_failed_at == failed_at
        camera._direct_failed_at = time.monotonic() - DOORSTATION_DIRECT_RETRY_SEC - 1
        await camera.handle_async_mjpeg_stream(MagicMock())  # after the pause: asked again, fails again

    assert aioclient_mock.call_count == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Doorstation camera" in r.message]
    assert len(warnings) == 1  # one per streak
    assert "live stream: HTTP 503" in warnings[0].message
    assert default_stream.await_count == 3


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_whose_address_lookup_runs_out_of_time_pauses_the_direct_way(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)
    camera._address = None
    camera._address_checked_at = None
    default_stream = AsyncMock(return_value="the still stream")

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = slow_lookup
    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05),
        patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
    ):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert aioclient_mock.call_count == 0  # no address, nobody asked
    assert camera._direct_failed_at is not None  # as for a still read: the next viewer does not wait for it again
    assert "the IO-Server did not answer in time" in caplog.text


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_second_viewer_is_turned_away_before_any_address_lookup(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")
    lookups = hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.await_count
    camera._streams_open = 1  # a live stream runs
    caplog.set_level(logging.DEBUG)

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.await_count == lookups
    assert "another live stream runs" in caplog.text
    assert camera._direct_failed_at is None  # a second viewer is no failure


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_still_read_does_not_overlap_a_live_stream_that_is_starting(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    camera = await _online_camera(hass, entry)

    async with camera._direct_gate:  # the live stream is connecting
        assert (await async_get_image(hass, CAMERA_ENTITY_ID)).content == FRAME

    assert aioclient_mock.call_count == 0  # no second connection to the Doorstation
    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_snapshot.assert_awaited_once_with(7)
    assert camera._direct_failed_at is None  # nothing failed, the IO-Server answered because of the stream


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_live_stream_waits_for_a_still_read_that_is_under_way(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    release = asyncio.Event()
    in_lookup = asyncio.Event()
    connects: list[str] = []

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        in_lookup.set()
        await release.wait()
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = slow_lookup

    class _Session:
        async def get(self, url: str, **_kwargs: object) -> MagicMock:
            connects.append(url)
            return MagicMock(status=503, headers={})

    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()),
    ):
        still = asyncio.create_task(camera._read_direct_frame(doorstation))
        await in_lookup.wait()
        live = asyncio.create_task(camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation))
        await _settled(lambda: camera._streams_open == 1)  # the live stream is counted and queued at the gate
        assert not connects  # the live stream has not opened its connection beside the still read
        release.set()
        await live
        # The stream is counted from its start on: the still read, back from its lookup, leaves the Doorstation to it.
        assert await still is None

    assert DIRECT_URL in connects
    assert camera._streams_open == 0


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_still_read_that_is_cancelled_in_the_address_lookup_lets_the_gate_go(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    in_lookup = asyncio.Event()

    async def lookup(_ext_id: int) -> tuple[str, None]:
        in_lookup.set()
        await asyncio.sleep(30)
        return ADDRESS, None

    hass.data[DOMAIN][entry.entry_id].api.get_doorstation_address.side_effect = lookup
    read = asyncio.create_task(camera._read_direct_frame(doorstation))
    await in_lookup.wait()
    assert camera._direct_gate.locked()  # a live stream that starts now waits for it

    read.cancel()  # Home Assistant's own image timeout
    with pytest.raises(asyncio.CancelledError):
        await read

    assert not camera._direct_gate.locked()
    assert not camera._address_lock.locked()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_second_live_viewer_opens_no_second_connection_and_pauses_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    camera._streams_open = 1  # another viewer's stream runs

    class _Session:
        async def get(self, *_args: object, **_kwargs: object) -> MagicMock:
            raise AssertionError("a second connection to the Doorstation was opened")

    with patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()):
        assert await camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation) is None

    assert camera._streams_open == 1  # the other stream's count is left alone
    assert camera._direct_failed_at is None
    assert not camera._direct_warned
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_second_live_viewer_gets_the_default_stream_and_no_address_lookup(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api
    camera._streams_open = 1  # another viewer's stream runs
    default = AsyncMock(return_value="default stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "default stream"

    default.assert_awaited_once()
    api.get_doorstation_address.assert_not_called()  # no lookup for a viewer that is turned away anyway
    assert camera._direct_failed_at is None


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_queued_behind_a_failing_still_read_honours_the_pause(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    doorstation = hass.data[DOMAIN][entry.entry_id].doorstations.known[2]
    connects: list[str] = []

    class _Session:
        async def get(self, url: str, **_kwargs: object) -> MagicMock:
            connects.append(url)
            return MagicMock(status=503, headers={})

    with patch("custom_components.comexio.camera.async_get_clientsession", return_value=_Session()):
        async with camera._direct_gate:  # a still read is under way
            live = asyncio.create_task(camera._proxy_direct_stream(MagicMock(), DIRECT_URL, doorstation))
            await _settled(lambda: camera._streams_open == 1)
            assert camera._streams_open == 1  # counted and waiting at the gate
            camera._direct_failed("HTTP 500")  # ... and fails, the pause starts
            failed_at = camera._direct_failed_at
        assert await live is None

    assert not connects  # the Doorstation was not asked during the pause
    assert camera._direct_failed_at == failed_at  # the turned-away viewer neither extends nor ends the pause
    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()


def _closed_session(on_get: Callable[[], None] | None = None, *, closed: bool = True) -> MagicMock:
    """A shared web session that refuses the live stream request the way aiohttp refuses one on a closed session."""

    async def get(*_args: object, **_kwargs: object) -> None:
        if on_get is not None:
            on_get()
        raise RuntimeError("Session is closed")

    session = MagicMock()
    session.closed = closed
    session.get = get
    return session


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_closed_session_while_home_assistant_runs_is_an_ordinary_live_stream_failure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    # Without HA stopping nobody explains a closed session away: the direct way pauses, the default stream answers.
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_closed_session()),
        patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
    ):
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert camera._direct_failed_at is not None
    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_stream_refused_while_home_assistant_stops_starts_no_failure_streak(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    camera = await _online_camera(hass, entry)
    default_stream = AsyncMock(return_value="the still stream")

    try:
        with (
            patch(
                "custom_components.comexio.camera.async_get_clientsession",
                # Its shared web session is closed under the request: Home Assistant is stopping.
                return_value=_closed_session(lambda: hass.set_state(CoreState.stopping)),
            ),
            patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
        ):
            assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"
    finally:
        hass.set_state(CoreState.running)

    assert camera._direct_failed_at is None  # no pause, no warning for a Doorstation that is fine
    assert camera._warned_camera_id is None
    assert "no image from the Doorstation itself" not in caplog.text
    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_any_other_runtime_error_of_the_live_stream_request_is_not_swallowed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    camera = await _online_camera(hass, entry)
    request = MagicMock()

    # The session is open: this RuntimeError is not aiohttp's refusal of a closed one, so it is a bug and visible.
    with (
        patch("custom_components.comexio.camera.async_get_clientsession", return_value=_closed_session(closed=False)),
        pytest.raises(RuntimeError, match="Session is closed"),
    ):
        await camera.handle_async_mjpeg_stream(request)

    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api", "direct_address")
async def test_a_live_viewer_waiting_at_the_gate_for_a_camera_that_goes_sends_no_request(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(DIRECT_URL, content=DIRECT_STREAM, headers=DIRECT_HEADERS)
    camera = await _online_camera(hass, entry)
    doorstations = hass.data[DOMAIN][entry.entry_id].doorstations
    default_stream = AsyncMock(return_value="the still stream")

    with patch.object(Camera, "handle_async_mjpeg_stream", default_stream):
        async with camera._direct_gate:  # a still read is under way
            viewer = asyncio.create_task(camera.handle_async_mjpeg_stream(MagicMock()))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert camera._streams_open == 1  # queued behind the read, counted from its connect on
            doorstations.known.pop(2)  # the page no longer shows the Doorstation
        assert await viewer == "the still stream"

    assert aioclient_mock.call_count == 0  # no request to a Doorstation nobody asks for
    assert camera._direct_failed_at is None  # and no failure streak
    assert camera._streams_open == 0
    assert not camera._direct_gate.locked()


@pytest.mark.usefixtures("mock_comexio_api")
async def test_a_live_viewer_whose_address_lookup_runs_out_of_time_gets_the_default_stream(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    camera = await _online_camera(hass, entry)
    api = hass.data[DOMAIN][entry.entry_id].api

    async def slow_lookup(_ext_id: int) -> tuple[str, None]:
        await asyncio.sleep(30)
        return ADDRESS, None

    api.get_doorstation_address.side_effect = slow_lookup
    default_stream = AsyncMock(return_value="the still stream")

    with (
        patch("custom_components.comexio.camera.DOORSTATION_DIRECT_TIMEOUT_SEC", 0.05),
        patch.object(Camera, "handle_async_mjpeg_stream", default_stream),
    ):
        # The lookup is bounded like the still read's: the viewer is not held until the IO-Server answers.
        assert await camera.handle_async_mjpeg_stream(MagicMock()) == "the still stream"

    assert aioclient_mock.call_count == 0
    assert camera._streams_open == 0
    assert not camera._address_lock.locked()
