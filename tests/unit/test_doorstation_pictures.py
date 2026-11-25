"""Doorstation pictures: which picture is the newest, its moment in UTC, and the two reads of ComexioAPI."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import (
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioDataError,
    DoorstationPicture,
    DoorstationPictureKind,
)
from aiocomexio.doorstation import Doorstation
from homeassistant.util import dt as dt_util
import pytest

from custom_components.comexio import _doorstation_entity_uids
from custom_components.comexio.api import SYSTEM_INFO_CERTIFICATE_CHANGED, ComexioAPI, doorstation_read_got_its_turn
from custom_components.comexio.const import (
    DOMAIN,
    DOORSTATION_EMPTY_POLLS_BEFORE_DROP,
    doorstation_picture_unique_id,
    doorstation_pictures_signal,
)
from custom_components.comexio.doorstation import DoorstationTracker
from custom_components.comexio.doorstation_pictures import DoorstationPictureFeed, newest_of_kind, taken_at_utc

BELL = DoorstationPictureKind.BELL
MOTION = DoorstationPictureKind.MOTION
JPEG = b"\xff\xd8picture\xff\xd9"


def _picture(name: str, kind: DoorstationPictureKind, taken_at: datetime | None) -> DoorstationPicture:
    return DoorstationPicture(name=name, kind=kind, taken_at=taken_at)


# --- the newest picture of a kind ---------------------------------------------------------------------------------


def test_the_newest_picture_of_a_kind_is_the_one_with_the_latest_moment_not_the_first_listed() -> None:
    older = _picture("bell_old.jpg", BELL, datetime(2026, 11, 20, 8, 0, 0))
    newer = _picture("bell_new.jpg", BELL, datetime(2026, 11, 21, 8, 0, 0))

    assert newest_of_kind([older, newer], BELL) is newer
    assert newest_of_kind([newer, older], BELL) is newer


def test_pictures_of_the_other_kind_are_not_counted() -> None:
    bell = _picture("bell.jpg", BELL, datetime(2026, 11, 20, 8, 0, 0))
    motion = _picture("motion.jpg", MOTION, datetime(2026, 11, 22, 8, 0, 0))

    assert newest_of_kind([bell, motion], BELL) is bell
    assert newest_of_kind([bell, motion], MOTION) is motion


def test_a_picture_without_a_moment_is_never_the_newest() -> None:
    dated = _picture("bell.jpg", BELL, datetime(2026, 11, 20, 8, 0, 0))
    undated = _picture("bell_odd.jpg", BELL, None)

    assert newest_of_kind([undated, dated], BELL) is dated
    assert newest_of_kind([undated], BELL) is None


def test_no_picture_of_the_kind_is_none() -> None:
    assert newest_of_kind([], BELL) is None
    assert newest_of_kind([_picture("m.jpg", MOTION, datetime(2026, 11, 20, 8, 0, 0))], BELL) is None


def test_two_pictures_of_the_same_moment_pick_the_one_listed_first() -> None:
    """The library lists the newest first (equal moments by name), so the first listed is the one it ranks highest."""
    moment = datetime(2026, 11, 20, 8, 0, 0)
    b = _picture("bell_b.jpg", BELL, moment)
    a = _picture("bell_a.jpg", BELL, moment)

    assert newest_of_kind([b, a], BELL) is b
    assert newest_of_kind([a, b], BELL) is a


# --- the moment as UTC ----------------------------------------------------------------------------------------------


@pytest.fixture
def berlin_time_zone() -> Any:
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Berlin"))
    yield
    dt_util.set_default_time_zone(previous)


@pytest.mark.usefixtures("berlin_time_zone")
@pytest.mark.parametrize(
    ("wall_clock", "utc"),
    [
        (datetime(2026, 11, 20, 8, 0, 0), datetime(2026, 11, 20, 7, 0, 0, tzinfo=UTC)),  # winter time, UTC+1
        (datetime(2026, 7, 20, 8, 0, 0), datetime(2026, 7, 20, 6, 0, 0, tzinfo=UTC)),  # summer time, UTC+2
    ],
)
def test_the_moment_is_read_as_the_local_time_of_the_installation(wall_clock: datetime, utc: datetime) -> None:
    assert taken_at_utc(_picture("bell.jpg", BELL, wall_clock)) == utc


def test_a_picture_without_a_moment_has_no_utc_time() -> None:
    assert taken_at_utc(_picture("bell.jpg", BELL, None)) is None


@pytest.mark.usefixtures("berlin_time_zone")
@pytest.mark.parametrize(
    ("wall_clock", "utc"),
    [
        # The hour that does not exist (clocks go 02:00 -> 03:00): read with the offset before the change.
        (datetime(2026, 3, 29, 2, 30, 0), datetime(2026, 3, 29, 1, 30, 0, tzinfo=UTC)),
        # The hour that exists twice (03:00 -> 02:00): the first one, summer time.
        (datetime(2026, 10, 25, 2, 30, 0), datetime(2026, 10, 25, 0, 30, 0, tzinfo=UTC)),
    ],
)
def test_a_moment_in_the_hour_of_a_clock_change_is_read_with_the_offset_before_the_change(
    wall_clock: datetime, utc: datetime
) -> None:
    """Pinned, not claimed to be right: the Doorstation's name carries no offset, so one hour a year is a guess."""
    assert taken_at_utc(_picture("bell.jpg", BELL, wall_clock)) == utc


# --- names ------------------------------------------------------------------------------------------------------------


def test_the_unique_id_of_a_picture_carries_only_the_technical_address() -> None:
    assert doorstation_picture_unique_id("IOSrv1", 2, "bell") == "comexio_iosrv1_doorstation2_bell_picture"
    assert doorstation_picture_unique_id("iosrv1", "14", "motion") == "comexio_iosrv1_doorstation14_motion_picture"


def test_the_signal_of_the_pictures_is_per_server_and_doorstation() -> None:
    assert doorstation_pictures_signal("iosrv1", 2) != doorstation_pictures_signal("iosrv1", 3)
    assert doorstation_pictures_signal("iosrv1", 2) != doorstation_pictures_signal("iosrv2", 2)


# --- the feed's extension name and device ---------------------------------------------------------------------------


def _feed(known: dict[int, Doorstation], ios: list[dict[str, Any]] | None = None, offline: Any = None) -> Any:
    coordinator = MagicMock()
    coordinator.server_id = "iosrv1"
    coordinator.data = {"io": ios or []}
    coordinator.offline_extensions = offline
    tracker = DoorstationTracker()
    tracker.update(known)
    coordinator.doorstations = tracker
    return DoorstationPictureFeed(MagicMock(), MagicMock(), coordinator, 14), tracker


def test_the_feed_keeps_the_last_extension_name_while_its_doorstation_is_unknown() -> None:
    feed, tracker = _feed({14: Doorstation(ext_id=14, camera_id=1, name="UD 1")})

    assert feed.ext_name == "UD 1"
    for _ in range(DOORSTATION_EMPTY_POLLS_BEFORE_DROP):
        tracker.update({})
    assert feed.doorstation is None
    assert feed.ext_name == "UD 1"
    assert not feed.available


def test_a_feed_that_never_saw_a_name_falls_back_to_the_extension_id_and_stays_unavailable() -> None:
    feed, _ = _feed({})

    assert feed.ext_name == "Doorstation 14"
    assert not feed.available
    assert feed.device_info["name"] == "iosrv1 EXT - Doorstation 14"


def test_the_feed_is_unavailable_while_its_extension_is_offline() -> None:
    known = {14: Doorstation(ext_id=14, camera_id=1, name="UD 1")}

    assert _feed(known, offline={"UD 1"})[0].available is False
    assert _feed(known, offline={"other"})[0].available is True
    assert _feed(known, offline=None)[0].available is True


def test_the_feed_device_is_the_one_of_the_extension() -> None:
    feed, _ = _feed({14: Doorstation(ext_id=14, camera_id=1, name="UD 1")})

    info = feed.device_info

    assert info["identifiers"] == {(DOMAIN, "iosrv1_ud 1")}
    assert info["name"] == "iosrv1 EXT - UD 1"


# --- the orphan cleanup keeps the pictures --------------------------------------------------------------------------


def _registry_with(*entries: tuple[str, str]) -> MagicMock:
    registry = MagicMock()
    registry.entities.get_entries_for_config_entry_id.return_value = [
        SimpleNamespace(domain=domain, unique_id=unique_id) for domain, unique_id in entries
    ]
    return registry


@pytest.mark.parametrize("import_ios", [True, False])
def test_the_orphan_cleanup_keeps_the_pictures_whether_or_not_ios_are_imported(import_ios: bool) -> None:
    pictures = [
        ("image", doorstation_picture_unique_id("iosrv1", 2, "bell")),
        ("image", doorstation_picture_unique_id("iosrv1", 2, "motion")),
    ]
    registry = _registry_with(*pictures, ("image", "comexio_iosrv1_fub5_preview"), ("sensor", "comexio_iosrv1_m1"))

    kept = _doorstation_entity_uids(registry, MagicMock(entry_id="e1"), "iosrv1", import_ios=import_ios)

    assert kept == {unique_id for _, unique_id in pictures}  # another image entity of the entry is not ours to keep


# --- the two reads of ComexioAPI ------------------------------------------------------------------------------------


def _api_with(comexio_api: ComexioAPI, **client_methods: AsyncMock) -> ComexioAPI:
    comexio_api._client = MagicMock(**client_methods)
    comexio_api._client_credentials = comexio_api._credentials()
    return comexio_api


def test_the_list_read_returns_what_the_client_read(comexio_api: ComexioAPI) -> None:
    pictures = (_picture("bell.jpg", BELL, datetime(2026, 11, 20, 8, 0, 0)),)
    api = _api_with(comexio_api, get_doorstation_pictures=AsyncMock(return_value=pictures))

    assert asyncio.run(api.get_doorstation_pictures(14)) == (pictures, None)
    api.client.get_doorstation_pictures.assert_awaited_once_with(14)


def test_an_empty_list_is_no_failure(comexio_api: ComexioAPI) -> None:
    api = _api_with(comexio_api, get_doorstation_pictures=AsyncMock(return_value=()))

    assert asyncio.run(api.get_doorstation_pictures(14)) == ((), None)


def test_the_picture_read_returns_the_jpeg(comexio_api: ComexioAPI) -> None:
    api = _api_with(comexio_api, get_doorstation_picture=AsyncMock(return_value=JPEG))

    assert asyncio.run(api.get_doorstation_picture(14, "bell.jpg")) == (JPEG, None)
    api.client.get_doorstation_picture.assert_awaited_once_with(14, "bell.jpg")


@pytest.mark.parametrize("method", ["get_doorstation_pictures", "get_doorstation_picture"])
@pytest.mark.parametrize("error", [ComexioConnectionError("timed out"), ComexioDataError("timed out")])
def test_a_failed_read_is_a_cause_not_an_empty_result(comexio_api: ComexioAPI, method: str, error: Exception) -> None:
    api = _api_with(comexio_api, **{method: AsyncMock(side_effect=error)})
    args = (14,) if method == "get_doorstation_pictures" else (14, "bell.jpg")

    assert asyncio.run(getattr(api, method)(*args)) == (None, "timed out")


@pytest.mark.parametrize("method", ["get_doorstation_pictures", "get_doorstation_picture"])
def test_a_changed_certificate_is_reported_for_the_repair_and_not_raised(comexio_api: ComexioAPI, method: str) -> None:
    err = ComexioCertificateMismatchError("changed", expected="a" * 64, actual="b" * 64)
    api = _api_with(comexio_api, **{method: AsyncMock(side_effect=err)})
    args = (14,) if method == "get_doorstation_pictures" else (14, "bell.jpg")

    assert asyncio.run(getattr(api, method)(*args)) == (None, SYSTEM_INFO_CERTIFICATE_CHANGED)
    assert api.certificate_mismatch is err


@pytest.mark.parametrize("method", ["get_doorstation_pictures", "get_doorstation_picture"])
def test_nothing_is_asked_once_the_session_is_gone(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """At unload or while Home Assistant stops: no request, and no failure to report either."""
    fetch = AsyncMock(return_value=())
    api = _api_with(comexio_api, **{method: fetch})
    monkeypatch.setattr(api, "_closed", True)
    args = (14,) if method == "get_doorstation_pictures" else (14, "bell.jpg")

    assert asyncio.run(getattr(api, method)(*args)) == (None, None)
    fetch.assert_not_called()


@pytest.mark.parametrize("method", ["get_doorstation_pictures", "get_doorstation_picture"])
def test_a_session_closed_under_the_read_is_no_failure(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    fetch = AsyncMock(side_effect=RuntimeError("Session is closed"))
    api = _api_with(comexio_api, **{method: fetch})
    # Open before the request, gone when the error is looked at.
    monkeypatch.setattr(api, "_session_gone_reason", MagicMock(side_effect=[None, "closed"]))
    args = (14,) if method == "get_doorstation_pictures" else (14, "bell.jpg")

    assert asyncio.run(getattr(api, method)(*args)) == (None, None)
    fetch.assert_awaited_once()  # the request was made: this is not the skip of a session that was gone before


def test_another_runtime_error_is_a_bug_and_raised(comexio_api: ComexioAPI) -> None:
    api = _api_with(comexio_api, get_doorstation_pictures=AsyncMock(side_effect=RuntimeError("something else")))
    read = api.get_doorstation_pictures(14)

    with pytest.raises(RuntimeError, match="something else"):
        asyncio.run(read)


def test_the_picture_reads_run_one_after_the_other_with_the_other_doorstation_reads(comexio_api: ComexioAPI) -> None:
    """Comexio serves one request at a time: the list, a picture and a frame never run beside each other."""
    running = 0
    most = 0

    async def _slow(*_args: Any) -> Any:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return ()

    api = _api_with(
        comexio_api,
        get_doorstation_pictures=AsyncMock(side_effect=_slow),
        get_doorstation_picture=AsyncMock(side_effect=_slow),
        get_doorstation_snapshot=AsyncMock(side_effect=_slow),
    )

    async def _together() -> None:
        await asyncio.gather(
            api.get_doorstation_pictures(1), api.get_doorstation_picture(1, "a.jpg"), api.get_doorstation_snapshot(1)
        )

    asyncio.run(_together())

    assert most == 1


def test_a_doorstation_read_notes_whether_it_got_its_turn_on_the_camera_lock(comexio_api: ComexioAPI) -> None:
    """A caller whose time ran out tells a wait behind other reads from a Doorstation that did not answer."""
    api = _api_with(comexio_api, get_doorstation_picture=AsyncMock(return_value=JPEG))

    async def _bounded_read() -> None:
        async with asyncio.timeout(0.05):
            await api.get_doorstation_picture(1, "a.jpg")

    async def _scenario() -> tuple[bool, bool]:
        async with api._doorstation_snapshot_lock:  # another read holds the camera
            with pytest.raises(TimeoutError):
                await _bounded_read()
            waited = doorstation_read_got_its_turn.get()
        await api.get_doorstation_picture(1, "a.jpg")
        return waited, doorstation_read_got_its_turn.get()

    waited, then_read = asyncio.run(_scenario())

    assert waited is False  # it never held the lock
    assert then_read is True  # the next read did


def test_a_doorstation_read_that_holds_the_camera_lock_when_its_time_runs_out_has_had_its_turn(
    comexio_api: ComexioAPI,
) -> None:
    """A read that ran out of time with the lock in hand is a failure of the Doorstation, not a wait."""

    async def _never_answers(*_args: object) -> None:
        await asyncio.Event().wait()

    api = _api_with(comexio_api, get_doorstation_picture=AsyncMock(side_effect=_never_answers))

    async def _bounded_read() -> None:
        async with asyncio.timeout(0.05):
            await api.get_doorstation_picture(1, "a.jpg")

    async def _scenario() -> bool:
        with pytest.raises(TimeoutError):
            await _bounded_read()
        return doorstation_read_got_its_turn.get()

    assert asyncio.run(_scenario()) is True


def test_a_stalled_list_read_ends_at_its_deadline_and_frees_the_lock(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it the list would hold the camera's lock for the whole HTTP timeout (Sourcery on #338)."""
    monkeypatch.setattr("custom_components.comexio.api.DOORSTATION_PICTURE_LIST_TIMEOUT_SEC", 0.05)
    calls = 0

    async def _stalled_then_fine(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()  # the first read never answers
        return ()

    api = _api_with(comexio_api, get_doorstation_pictures=AsyncMock(side_effect=_stalled_then_fine))

    async def _twice() -> tuple[Any, Any]:
        return await api.get_doorstation_pictures(1), await api.get_doorstation_pictures(1)

    stalled, next_read = asyncio.run(asyncio.wait_for(_twice(), 5))  # a missing deadline fails, it does not hang

    assert stalled == (None, "not read within 0.05 s")
    assert next_read == ((), None)  # the lock was released: the next read ran


def test_a_picture_read_given_a_hold_budget_ends_at_it_with_its_cause(comexio_api: ComexioAPI) -> None:
    calls = 0

    async def _stalled_then_fine(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()  # the first read never answers
        return b"\xff\xd8"

    api = _api_with(comexio_api, get_doorstation_picture=AsyncMock(side_effect=_stalled_then_fine))

    async def _twice() -> tuple[Any, Any]:
        first = await api.get_doorstation_picture(1, "a.jpg", held_for_sec=0.05)
        second = await api.get_doorstation_picture(1, "a.jpg", held_for_sec=0.05)
        return first, second

    stalled, next_read = asyncio.run(asyncio.wait_for(_twice(), 5))  # a missing deadline fails, it does not hang

    assert stalled == (None, "not read within 0.05 s")
    assert next_read == (b"\xff\xd8", None)  # the lock was released: the next read ran
    assert api.client.get_doorstation_picture.await_count == 2  # both reads reached the client


def test_the_wait_for_the_lock_does_not_count_into_the_list_deadline(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.comexio.api.DOORSTATION_PICTURE_LIST_TIMEOUT_SEC", 0.05)

    async def _slow_frame(*_args: Any) -> Any:
        await asyncio.sleep(0.15)  # longer than the list's deadline: the list only waits behind it
        return b""

    api = _api_with(
        comexio_api,
        get_doorstation_pictures=AsyncMock(return_value=()),
        get_doorstation_snapshot=AsyncMock(side_effect=_slow_frame),
    )

    async def _together() -> Any:
        frame = asyncio.create_task(api.get_doorstation_snapshot(1))
        await asyncio.sleep(0.01)  # the frame holds the lock now
        return await api.get_doorstation_pictures(1), await frame

    listed, _ = asyncio.run(_together())

    assert listed == ((), None)


def test_a_timeout_of_the_request_itself_is_not_taken_for_the_list_deadline(comexio_api: ComexioAPI) -> None:
    """Only the deadline of the read is mapped to a cause; any other TimeoutError is not this method's to hide."""
    api = _api_with(comexio_api, get_doorstation_pictures=AsyncMock(side_effect=TimeoutError("inner")))
    read = api.get_doorstation_pictures(1)

    with pytest.raises(TimeoutError, match="inner"):
        asyncio.run(read)
