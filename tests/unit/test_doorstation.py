"""Doorstation cameras: what the tracker keeps, how the camera fetches its image, how the poll reads the page."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioCertificateMismatchError, ComexioConnectionError, ComexioDataError
from aiocomexio.doorstation import Doorstation
from homeassistant.exceptions import ConfigEntryAuthFailed
import pytest

from custom_components.comexio import _doorstation_camera_uids
from custom_components.comexio.api import SYSTEM_INFO_CERTIFICATE_CHANGED, ComexioAPI
from custom_components.comexio.camera import ComexioDoorstationCamera, _DoorstationCameraSync
from custom_components.comexio.const import (
    DOORSTATION_EMPTY_POLLS_BEFORE_DROP,
    DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC,
    doorstation_camera_unique_id,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.doorstation import DoorstationTracker, doorstation_ext_name

SERVER = "iosrv1"
FRAME = b"\xff\xd8frame\xff\xd9"


def _doorstation(ext_id: int = 14, camera_id: int = 1, name: str | None = "Tuer") -> Doorstation:
    return Doorstation(ext_id=ext_id, camera_id=camera_id, name=name)


# --- the tracker --------------------------------------------------------------------------------------------------


def test_a_found_doorstation_becomes_known_and_reports_the_change() -> None:
    tracker = DoorstationTracker()

    assert tracker.update({14: _doorstation()}) is True
    assert tracker.known == {14: _doorstation()}
    assert tracker.update({14: _doorstation()}) is False


def test_empty_polls_keep_the_known_doorstations_until_the_limit() -> None:
    tracker = DoorstationTracker()
    tracker.update({14: _doorstation()})

    for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP - 1):
        assert tracker.update({}) is False
        assert 14 in tracker.known

    assert tracker.update({}) is True
    assert tracker.known == {}


def test_a_poll_that_finds_the_doorstation_again_restarts_the_count() -> None:
    tracker = DoorstationTracker()
    tracker.update({14: _doorstation()})
    for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP - 1):
        tracker.update({})

    tracker.update({14: _doorstation()})
    for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP - 1):
        tracker.update({})

    assert 14 in tracker.known


def test_empty_polls_without_known_doorstations_change_nothing() -> None:
    tracker = DoorstationTracker()

    assert [tracker.update({}) for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP + 1)] == [False] * (
        DOORSTATION_EMPTY_POLLS_BEFORE_DROP + 1
    )
    assert tracker.known == {}


# --- the extension name -------------------------------------------------------------------------------------------


def test_the_extension_name_comes_from_the_ios_of_the_same_extension() -> None:
    ios = [{"ext_id": "3", "ext_name": "OTHER"}, {"ext_id": "14", "ext_name": "DOOR"}]

    assert doorstation_ext_name(_doorstation(name="From page"), ios) == "DOOR"


def test_the_extension_name_falls_back_to_the_name_on_the_page_then_to_none() -> None:
    assert doorstation_ext_name(_doorstation(name="From page"), [{"ext_id": "3", "ext_name": "OTHER"}]) == "From page"
    assert doorstation_ext_name(_doorstation(name=None), []) is None


# --- the camera ---------------------------------------------------------------------------------------------------


def _camera(
    tracker: DoorstationTracker | None = None, snapshot: AsyncMock | None = None
) -> tuple[ComexioDoorstationCamera, MagicMock]:
    coordinator = MagicMock(spec=ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.data = {"io": [{"ext_id": "14", "ext_name": "DOOR"}]}
    coordinator.offline_extensions = set()
    coordinator.doorstations = tracker or DoorstationTracker()
    if tracker is None:
        coordinator.doorstations.update({14: _doorstation()})
    coordinator.api = MagicMock(get_doorstation_snapshot=snapshot or AsyncMock(return_value=(FRAME, None)))
    with patch("custom_components.comexio.camera.Camera.__init__", lambda _self: None):
        camera = ComexioDoorstationCamera(coordinator, 14)
    return camera, coordinator


def test_the_camera_id_is_the_extension_id_only() -> None:
    camera, _ = _camera()

    assert camera.unique_id == doorstation_camera_unique_id(SERVER, 14) == "comexio_iosrv1_doorstation14_camera"


def test_the_camera_joins_the_device_of_its_extension() -> None:
    camera, _ = _camera()

    with patch("custom_components.comexio.camera.build_device_info", side_effect=lambda *_a, **kw: kw) as build:
        info = camera.device_info

    assert info["identifiers"] == {("comexio", "iosrv1_door")}
    assert info["name"] == "iosrv1 EXT - DOOR"
    assert build.call_count == 1


def test_a_camera_whose_extension_is_unknown_gets_a_device_of_its_own() -> None:
    coordinator_less, _ = _camera()
    coordinator_less.coordinator.data = {"io": []}
    coordinator_less.coordinator.doorstations.update({14: _doorstation(name=None)})

    assert coordinator_less._ext_name == "Doorstation 14"


def test_the_camera_is_unavailable_once_its_doorstation_is_dropped() -> None:
    tracker = DoorstationTracker()
    tracker.update({14: _doorstation()})
    camera, coordinator = _camera(tracker)
    coordinator.last_update_success = True

    with patch("custom_components.comexio.camera.CoordinatorEntity.available", True):
        assert camera.available is True
        for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP):
            tracker.update({})
        assert camera.available is False


def test_the_camera_is_unavailable_while_its_extension_is_offline() -> None:
    camera, coordinator = _camera()
    coordinator.last_update_success = True

    with patch("custom_components.comexio.camera.CoordinatorEntity.available", True):
        assert camera.available is True
        coordinator.offline_extensions = {"DOOR"}
        assert camera.available is False


def test_the_image_is_read_with_the_camera_id_of_the_doorstation() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    camera, _ = _camera(snapshot=snapshot)

    assert asyncio.run(camera.async_camera_image()) == FRAME
    snapshot.assert_awaited_once_with(1)


def test_a_young_frame_is_handed_out_again_without_a_new_read() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    camera, _ = _camera(snapshot=snapshot)

    async def _twice() -> tuple[bytes | None, bytes | None]:
        return await camera.async_camera_image(), await camera.async_camera_image()

    assert asyncio.run(_twice()) == (FRAME, FRAME)
    assert snapshot.await_count == 1


def test_an_old_frame_is_read_again() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    camera, _ = _camera(snapshot=snapshot)

    async def _later() -> None:
        await camera.async_camera_image()
        camera._frame_at -= DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC + 1
        await camera.async_camera_image()

    asyncio.run(_later())

    assert snapshot.await_count == 2


def test_requests_that_arrive_together_share_one_read() -> None:
    async def _slow(_camera_id: int) -> tuple[bytes, None]:
        await asyncio.sleep(0.01)
        return FRAME, None

    snapshot = AsyncMock(side_effect=_slow)
    camera, _ = _camera(snapshot=snapshot)

    async def _together() -> list[bytes | None]:
        return list(await asyncio.gather(*(camera.async_camera_image() for _ in range(3))))

    assert asyncio.run(_together()) == [FRAME] * 3
    assert snapshot.await_count == 1


def test_a_failed_read_gives_no_image_and_drops_the_old_frame(caplog: pytest.LogCaptureFixture) -> None:
    snapshot = AsyncMock(side_effect=[(FRAME, None), (None, "no frame within 10 s")])
    camera, _ = _camera(snapshot=snapshot)

    async def _fail() -> bytes | None:
        await camera.async_camera_image()
        camera._frame_at -= DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC + 1
        return await camera.async_camera_image()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_fail()) is None

    assert camera._frame is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "no frame within 10 s" in caplog.records[0].getMessage()


def test_a_failing_camera_warns_once_per_streak_and_says_when_it_works_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = AsyncMock(side_effect=[(None, "timeout"), (None, "timeout"), (FRAME, None), (None, "timeout")])
    camera, _ = _camera(snapshot=snapshot)

    async def _run() -> None:
        for _ in range(4):
            camera._frame_at = -DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC - 1
            camera._failed_at = None
            await camera.async_camera_image()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())

    levels = [r.levelno for r in caplog.records if "camera" in r.getMessage().lower()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.INFO, logging.WARNING]


def test_requests_queued_behind_a_failed_read_do_not_read_again() -> None:
    async def _slow_failure(_camera_id: int) -> tuple[None, str]:
        await asyncio.sleep(0.01)
        return None, "no frame within 10 s"

    snapshot = AsyncMock(side_effect=_slow_failure)
    camera, _ = _camera(snapshot=snapshot)

    async def _together() -> list[bytes | None]:
        return list(await asyncio.gather(*(camera.async_camera_image() for _ in range(3))))

    assert asyncio.run(_together()) == [None] * 3
    assert snapshot.await_count == 1


def test_a_failed_read_is_repeated_once_the_interval_has_passed() -> None:
    snapshot = AsyncMock(side_effect=[(None, "timeout"), (FRAME, None)])
    camera, _ = _camera(snapshot=snapshot)

    async def _later() -> bytes | None:
        await camera.async_camera_image()
        assert camera._failed_at is not None
        camera._failed_at -= DOORSTATION_SNAPSHOT_MIN_INTERVAL_SEC + 1
        return await camera.async_camera_image()

    assert asyncio.run(_later()) == FRAME
    assert camera._failed_at is None


def test_no_read_goes_out_for_a_doorstation_that_is_gone() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    tracker = DoorstationTracker()
    camera, _ = _camera(tracker, snapshot)

    assert asyncio.run(camera.async_camera_image()) is None
    snapshot.assert_not_awaited()


def test_no_read_goes_out_for_an_extension_that_reports_offline() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    camera, coordinator = _camera(snapshot=snapshot)
    coordinator.offline_extensions = {"DOOR"}

    assert asyncio.run(camera.async_camera_image()) is None
    snapshot.assert_not_awaited()


def test_an_empty_frame_is_a_failed_read_not_an_image() -> None:
    snapshot = AsyncMock(return_value=(b"", None))
    camera, _ = _camera(snapshot=snapshot)

    assert asyncio.run(camera.async_camera_image()) is None
    assert camera._frame is None
    assert camera._failed_at is not None


def test_a_young_frame_is_not_handed_out_for_a_doorstation_that_is_gone() -> None:
    snapshot = AsyncMock(return_value=(FRAME, None))
    camera, coordinator = _camera(snapshot=snapshot)

    async def _drop_then_ask() -> bytes | None:
        await camera.async_camera_image()
        for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP):
            coordinator.doorstations.update({})
        return await camera.async_camera_image()

    assert asyncio.run(_drop_then_ask()) is None
    assert snapshot.await_count == 1


# --- the platform listener ----------------------------------------------------------------------------------------


def test_a_camera_is_added_once_per_doorstation_also_when_it_comes_later() -> None:
    coordinator = MagicMock(spec=ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.doorstations = DoorstationTracker()
    added: list[ComexioDoorstationCamera] = []
    sync = _DoorstationCameraSync(coordinator, added.extend)

    with patch("custom_components.comexio.camera.Camera.__init__", lambda _self: None):
        sync()
        assert not added
        coordinator.doorstations.update({14: _doorstation()})
        sync()
        sync()
        coordinator.doorstations.update({14: _doorstation(), 15: _doorstation(15, 2)})
        sync()
        for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP):
            coordinator.doorstations.update({})
        assert not coordinator.doorstations.known
        sync()
        coordinator.doorstations.update({14: _doorstation()})
        sync()

    assert [camera.unique_id for camera in added] == [
        doorstation_camera_unique_id(SERVER, 14),
        doorstation_camera_unique_id(SERVER, 15),
    ]


@pytest.mark.parametrize("server_id", [SERVER, "IOSrv1"])
def test_the_orphan_cleanup_keeps_every_registered_camera_of_the_server(server_id: str) -> None:
    registered = [
        SimpleNamespace(domain="camera", unique_id=doorstation_camera_unique_id(server_id, ext_id))
        for ext_id in (14, 15)
    ]
    others = [
        SimpleNamespace(domain="sensor", unique_id=f"comexio_{server_id}_doorstation14_other".lower()),
        SimpleNamespace(domain="camera", unique_id="comexio_other_doorstation14_camera"),
    ]

    with patch("custom_components.comexio.er.async_entries_for_config_entry", return_value=registered + others):
        uids = _doorstation_camera_uids(MagicMock(), MagicMock(entry_id="entry"), server_id)

    assert uids == {doorstation_camera_unique_id(server_id, ext_id) for ext_id in (14, 15)}


# --- api and poll -------------------------------------------------------------------------------------------------


def test_the_doorstation_fetch_returns_what_the_client_read(comexio_api: ComexioAPI) -> None:
    found = {14: _doorstation()}
    comexio_api._client = MagicMock(get_doorstations=AsyncMock(return_value=found))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_doorstations()) == (found, None)


def test_the_doorstation_fetch_reports_a_changed_certificate_without_raising(comexio_api: ComexioAPI) -> None:
    err = ComexioCertificateMismatchError("changed", expected="a" * 64, actual="b" * 64)
    comexio_api._client = MagicMock(get_doorstations=AsyncMock(side_effect=err))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_doorstations()) == (None, SYSTEM_INFO_CERTIFICATE_CHANGED)
    assert comexio_api.certificate_mismatch is err


def test_the_snapshot_fetch_returns_the_frame(comexio_api: ComexioAPI) -> None:
    comexio_api._client = MagicMock(get_doorstation_snapshot=AsyncMock(return_value=FRAME))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_doorstation_snapshot(1)) == (FRAME, None)
    comexio_api._client.get_doorstation_snapshot.assert_awaited_once_with(1)


def test_the_snapshot_fetch_reports_a_changed_certificate_without_raising(comexio_api: ComexioAPI) -> None:
    err = ComexioCertificateMismatchError("changed", expected="a" * 64, actual="b" * 64)
    comexio_api._client = MagicMock(get_doorstation_snapshot=AsyncMock(side_effect=err))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_doorstation_snapshot(1)) == (None, SYSTEM_INFO_CERTIFICATE_CHANGED)
    assert comexio_api.certificate_mismatch is err


@pytest.mark.parametrize("method", ["get_doorstations", "get_doorstation_snapshot"])
@pytest.mark.parametrize("error", [ComexioConnectionError("timed out"), ComexioDataError("timed out")])
def test_a_failed_read_is_a_cause_not_an_empty_result(comexio_api: ComexioAPI, method: str, error: Exception) -> None:
    client = MagicMock(**{method: AsyncMock(side_effect=error)})
    comexio_api._client = client
    comexio_api._client_credentials = comexio_api._credentials()
    call = getattr(comexio_api, method)

    result = asyncio.run(call() if method == "get_doorstations" else call(1))

    assert result == (None, "timed out")


def _poll_coordinator(fetch: AsyncMock) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.doorstations = DoorstationTracker()
    coordinator._doorstations_warned = False
    coordinator.api = MagicMock(get_doorstations=fetch)
    return coordinator


def test_a_poll_hands_the_doorstations_to_the_tracker() -> None:
    coordinator = _poll_coordinator(AsyncMock(return_value=({14: _doorstation()}, None)))

    asyncio.run(coordinator._async_refresh_doorstations())

    assert 14 in coordinator.doorstations.known


def test_a_failed_poll_keeps_the_cameras_and_warns_once_until_it_works_again(caplog: pytest.LogCaptureFixture) -> None:
    fetch = AsyncMock(
        side_effect=[
            ({14: _doorstation()}, None),
            (None, "timeout"),
            (None, "timeout"),
            ({14: _doorstation()}, None),
        ]
    )
    coordinator = _poll_coordinator(fetch)

    with caplog.at_level(logging.INFO):
        for _ in range(4):
            asyncio.run(coordinator._async_refresh_doorstations())
            assert 14 in coordinator.doorstations.known

    messages = [(r.levelno, r.getMessage()) for r in caplog.records if "Doorstations" in r.getMessage()]
    assert [level for level, _ in messages] == [logging.WARNING, logging.INFO]
    assert "timeout" in messages[0][1]


def test_a_changed_certificate_keeps_the_cameras_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _poll_coordinator(AsyncMock(return_value=(None, SYSTEM_INFO_CERTIFICATE_CHANGED)))
    coordinator.doorstations.update({14: _doorstation()})

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_refresh_doorstations())

    assert 14 in coordinator.doorstations.known
    assert not caplog.records


def test_an_unexpected_error_does_not_break_the_poll(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _poll_coordinator(AsyncMock(side_effect=RuntimeError("boom")))
    coordinator.doorstations.update({14: _doorstation()})

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_refresh_doorstations())
        asyncio.run(coordinator._async_refresh_doorstations())

    assert coordinator._doorstations_warned is True
    assert 14 in coordinator.doorstations.known
    assert sum(1 for r in caplog.records if r.exc_info) == 2  # the traceback every time
    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING and not r.exc_info) == 1  # the warning once


def test_empty_pages_drop_the_cameras_only_after_the_limit() -> None:
    coordinator = _poll_coordinator(AsyncMock(return_value=({}, None)))
    coordinator.doorstations.update({14: _doorstation()})

    for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP - 1):
        asyncio.run(coordinator._async_refresh_doorstations())
        assert 14 in coordinator.doorstations.known

    asyncio.run(coordinator._async_refresh_doorstations())

    assert coordinator.doorstations.known == {}


def test_a_failed_read_neither_counts_as_an_empty_page_nor_interrupts_the_series() -> None:
    coordinator = _poll_coordinator(AsyncMock(side_effect=[({}, None), (None, "timeout"), ({}, None), ({}, None)]))
    coordinator.doorstations.update({14: _doorstation()})
    assert DOORSTATION_EMPTY_POLLS_BEFORE_DROP == 3

    for _ in range(3):
        asyncio.run(coordinator._async_refresh_doorstations())
    assert 14 in coordinator.doorstations.known  # two empty pages so far, the failed read counted for nothing

    asyncio.run(coordinator._async_refresh_doorstations())
    assert coordinator.doorstations.known == {}  # the third empty page, the failed read did not restart the count


def test_a_refused_login_ends_the_poll() -> None:
    coordinator = _poll_coordinator(AsyncMock(side_effect=ConfigEntryAuthFailed("refused")))

    refresh = coordinator._async_refresh_doorstations()

    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(refresh)


def test_the_poll_reads_the_page_without_asking_the_caller_for_anything() -> None:
    fetch: Any = AsyncMock(return_value=({}, None))
    coordinator = _poll_coordinator(fetch)

    asyncio.run(coordinator._async_refresh_doorstations())

    fetch.assert_awaited_once_with()
