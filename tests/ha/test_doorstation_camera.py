"""Doorstation cameras: entity, device, image and what survives a page that shows no Doorstation."""

import asyncio
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from aiocomexio.doorstation import Doorstation
from homeassistant.components.camera import DATA_COMPONENT, async_get_image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.comexio.camera import ComexioDoorstationCamera
from custom_components.comexio.const import (
    CONF_INCLUDE_OFFLINE_EXTENSIONS,
    DOMAIN,
    DOORSTATION_ADDRESS_RECHECK_SEC,
    DOORSTATION_DIRECT_RETRY_SEC,
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
    assert "the IO-Server did not answer in time" in caplog.text
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
        assert await camera._read_frame(doorstation) == (None, "the IO-Server did not answer in time")

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
