"""1-Wire sensors: config parsing fallback, status poll, state keeping, entities and the orphan cleanup."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioAuthenticationError, ComexioCertificateMismatchError, ComexioDataError
from aiocomexio.config import TYPE_ANALOG, TYPE_DIGITAL
from aiocomexio.onewire import OneWireSensor, OneWireStatus
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
import pytest

import custom_components.comexio as comexio_init
from custom_components.comexio import api as api_module, binary_sensor, coordinator as coordinator_module
from custom_components.comexio.api import SYSTEM_INFO_CERTIFICATE_CHANGED, ComexioAPI, OneWireReading
from custom_components.comexio.binary_sensor import (
    ComexioOneWireBinarySensor,
    ComexioOneWireProblemSensor,
    _onewire_binary_sensors,
)
from custom_components.comexio.const import (
    DOMAIN,
    ONEWIRE_DATA_KEY,
    ONEWIRE_FAIL_STREAK_THRESHOLD,
    ONEWIRE_POLL_INTERVAL_SEC,
    onewire_problem_unique_id,
    onewire_signal,
    onewire_unique_id,
    stable_object_id,
)
from custom_components.comexio.coordinator import ComexioCoordinator, _imported_data
from custom_components.comexio.entity import onewire_sensors_for_platform
from custom_components.comexio.onewire import (
    PROBLEM_MISSING,
    PROBLEM_NOT_FOUND,
    PROBLEM_READ_ERROR,
    TRANSITION_DROPPED,
    TRANSITION_RECOVERED,
    OneWireState,
    onewire_entity_name,
    onewire_platform,
)
from custom_components.comexio.sensor import ComexioOneWireSensor, _onewire_sensors

PINNED = "a" * 64
OTHER = "b" * 64


def _sensor(address: str = "OT1", *, kind: str | None = TYPE_ANALOG, name: str = "Kitchen") -> OneWireSensor:
    prefix = address.rstrip("0123456789")
    return OneWireSensor(
        address=address,
        config_id=int(address[len(prefix) :]),
        prefix=prefix,
        name=name,
        hw_id="28-0000000001",
        kind=kind,
    )


def _status(
    address: str = "OT1", *, online: bool = True, visible: bool = True, value: float | None = 21.5
) -> OneWireStatus:
    return OneWireStatus(
        address=address, device_id="28-0000000001", name="", online=online, visible=visible, value=value
    )


def _fub_modules(*entries: dict[str, Any]) -> dict[str, Any]:
    """$FubModules with a marker group and the 1-Wire group 12 holding ``entries``."""
    return {"2": {}, "12": {str(i): entry for i, entry in enumerate(entries, 1)}}


def _entry(config_id: int, prefix: str = "OT", *, binary: int = 2) -> dict[str, Any]:
    return {
        "Id": config_id,
        "Prefix": prefix,
        "Name": f"Sensor {config_id}",
        "HwId": "28-1",
        "Binary": binary,
        "Active": 1,
    }


# --- api: sensor list from the config ---------------------------------------------------------------------------


def test_config_parses_the_sensors(comexio_api: ComexioAPI) -> None:
    sensors = comexio_api._parse_onewire(_fub_modules(_entry(1), _entry(2, "OIB", binary=1)))

    assert sensors is not None
    assert list(sensors) == ["OT1", "OIB2"]
    assert sensors["OIB2"].kind == TYPE_DIGITAL


@pytest.mark.parametrize("fub_modules", [None, {}, "text"], ids=["missing", "empty", "not a mapping"])
def test_a_failed_config_fetch_keeps_the_last_sensors(comexio_api: ComexioAPI, fub_modules: object) -> None:
    """A failed fetch is no installation without 1-Wire sensors: their entities must not go."""
    known = comexio_api._parse_onewire(_fub_modules(_entry(1)))

    assert comexio_api._parse_onewire(fub_modules) is known


def test_the_sensor_list_is_unknown_before_any_config_parsed(comexio_api: ComexioAPI) -> None:
    assert comexio_api._parse_onewire(None) is None


def test_a_changed_config_group_keeps_the_last_sensors_and_warns(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    known = comexio_api._parse_onewire(_fub_modules(_entry(1)))
    monkeypatch.setattr(api_module, "parse_onewire_config", MagicMock(side_effect=ComexioDataError("changed group")))

    with caplog.at_level(logging.WARNING):
        assert comexio_api._parse_onewire(_fub_modules(_entry(2))) is known

    assert "1-Wire sensors not read from the Comexio config" in caplog.text
    assert "changed group" in caplog.text


def test_a_lasting_config_group_change_warns_once_per_cause(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Not on every config poll; a new cause, and the same one after the group parsed again, warn anew."""
    parse = MagicMock(side_effect=ComexioDataError("changed group"))
    monkeypatch.setattr(api_module, "parse_onewire_config", parse)

    def warnings_after(*causes: Exception | dict[str, OneWireSensor]) -> int:
        caplog.clear()
        for cause in causes:
            parse.side_effect = cause if isinstance(cause, Exception) else None
            parse.return_value = cause
            comexio_api._parse_onewire(_fub_modules(_entry(1)))
        return sum("1-Wire sensors not read" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    with caplog.at_level(logging.DEBUG):
        assert warnings_after(ComexioDataError("changed group"), ComexioDataError("changed group")) == 1
        assert warnings_after(ComexioDataError("other shape")) == 1
        assert warnings_after({"OT1": _sensor()}, ComexioDataError("other shape")) == 1


def test_an_installation_without_1wire_sensors_has_an_empty_list(comexio_api: ComexioAPI) -> None:
    assert comexio_api._parse_onewire({"2": {}}) == {}


def test_the_imported_data_carries_the_sensor_list_whatever_the_import_options() -> None:
    sensors = {"OT1": _sensor()}
    parsed = {"markers": [], "io": [], ONEWIRE_DATA_KEY: sensors}
    conf = {"import_markers": False, "import_ios": False, "import_knx": False, "import_quickbus": False}

    assert _imported_data(parsed, conf)[ONEWIRE_DATA_KEY] is sensors
    assert _imported_data({"markers": [], "io": []}, {})[ONEWIRE_DATA_KEY] is None


# --- api: status poll -------------------------------------------------------------------------------------------


@pytest.fixture
def client(comexio_api: ComexioAPI) -> MagicMock:
    """Replace the main-session client with a mock (kept as long as the settings stay)."""
    fake = MagicMock()
    comexio_api._client = fake
    comexio_api._client_credentials = comexio_api._credentials()
    return fake


def test_status_poll_returns_the_statuses(comexio_api: ComexioAPI, client: MagicMock) -> None:
    statuses = {"OT1": _status()}
    client.get_onewire_status = AsyncMock(return_value=statuses)

    assert asyncio.run(comexio_api.get_onewire_status()) == OneWireReading(statuses, None)


def test_a_failed_status_poll_carries_its_cause(comexio_api: ComexioAPI, client: MagicMock) -> None:
    client.get_onewire_status = AsyncMock(side_effect=ComexioDataError("changed answer"))

    assert asyncio.run(comexio_api.get_onewire_status()) == OneWireReading(None, "changed answer")


def test_a_lapsed_session_is_logged_in_again_once(comexio_api: ComexioAPI, client: MagicMock) -> None:
    statuses = {"OT1": _status()}
    client.get_onewire_status = AsyncMock(side_effect=[ComexioAuthenticationError("login form"), statuses])
    comexio_api._full_login = AsyncMock(return_value=True)

    assert asyncio.run(comexio_api.get_onewire_status()) == OneWireReading(statuses, None)
    comexio_api._full_login.assert_awaited_once()


def test_a_status_poll_meeting_another_certificate_raises_the_repair_quietly(
    comexio_api: ComexioAPI, client: MagicMock
) -> None:
    hook = MagicMock()
    comexio_api.on_certificate_mismatch = hook
    client.get_onewire_status = AsyncMock(
        side_effect=ComexioCertificateMismatchError("foreign certificate", expected=PINNED, actual=OTHER)
    )

    assert asyncio.run(comexio_api.get_onewire_status()) == OneWireReading(None, SYSTEM_INFO_CERTIFICATE_CHANGED)
    hook.assert_called_once_with(OTHER)


# --- state ------------------------------------------------------------------------------------------------------


def test_a_poll_replaces_the_statuses() -> None:
    state = OneWireState()

    assert state.apply_status({"OT1": _status()}) is None
    assert state.status("OT1") == _status()
    assert state.status("OT2") is None


def test_one_failed_poll_keeps_the_last_statuses() -> None:
    state = OneWireState()
    state.apply_status({"OT1": _status()})

    assert state.apply_status(None) is None
    assert state.status("OT1") == _status()


def test_the_statuses_turn_unknown_once_after_the_threshold() -> None:
    state = OneWireState()
    state.apply_status({"OT1": _status()})

    dropped = [state.apply_status(None) for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD + 2)]

    assert dropped.count(TRANSITION_DROPPED) == 1
    assert dropped[ONEWIRE_FAIL_STREAK_THRESHOLD - 1] == TRANSITION_DROPPED
    assert state.statuses is None


def test_a_poll_that_never_worked_reports_the_threshold_too() -> None:
    """Without it, a status poll that fails from the start would never be warned about."""
    state = OneWireState()

    assert [state.apply_status(None) for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD)][-1] == TRANSITION_DROPPED


def test_a_good_poll_resets_the_streak() -> None:
    state = OneWireState()
    for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD - 1):
        state.apply_status(None)
    state.apply_status({"OT1": _status()})

    assert state.apply_status(None) is None
    assert state.status("OT1") is not None


def test_the_first_good_poll_after_an_outage_reports_the_recovery_once() -> None:
    """A second outage after the recovery is reported again: each outage is logged when it starts and ends."""
    state = OneWireState()
    state.apply_status({"OT1": _status()})
    failed = [None] * ONEWIRE_FAIL_STREAK_THRESHOLD
    good = {"OT1": _status()}

    transitions = [state.apply_status(statuses) for statuses in [*failed, good, good, *failed]]

    assert [t for t in transitions if t is not None] == [TRANSITION_DROPPED, TRANSITION_RECOVERED, TRANSITION_DROPPED]
    assert transitions[ONEWIRE_FAIL_STREAK_THRESHOLD] == TRANSITION_RECOVERED


def test_a_good_poll_after_a_long_outage_reports_the_recovery() -> None:
    """Failures past the threshold still end in a recovery: the streak keeps counting after the drop."""
    state = OneWireState()

    for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD + 3):
        state.apply_status(None)

    assert state.apply_status({"OT1": _status()}) == TRANSITION_RECOVERED


def test_a_good_poll_after_a_single_failure_is_no_recovery() -> None:
    state = OneWireState()
    state.apply_status({"OT1": _status()})
    state.apply_status(None)

    assert state.apply_status({"OT1": _status()}) is None


def test_problems_are_unknown_while_the_statuses_are() -> None:
    assert OneWireState().problems({"OT1": _sensor()}) is None


def test_problems_list_each_kind_sorted() -> None:
    state = OneWireState()
    state.apply_status(
        {
            "OT1": _status("OT1"),
            "OT2": _status("OT2", online=False),
            "OT3": _status("OT3", visible=False),
            "OT9": _status("OT9", online=False, visible=False),
        }
    )
    sensors = {address: _sensor(address) for address in ("OT9", "OT1", "OT4", "OT3", "OT2")}

    assert state.problems(sensors) == {
        PROBLEM_READ_ERROR: ["OT2"],
        PROBLEM_NOT_FOUND: ["OT3", "OT9"],
        PROBLEM_MISSING: ["OT4"],
    }


def test_a_status_without_a_configured_sensor_is_no_problem() -> None:
    state = OneWireState()
    state.apply_status({"OT1": _status(), "OT7": _status("OT7", online=False)})

    assert state.problems({"OT1": _sensor()}) == {PROBLEM_READ_ERROR: [], PROBLEM_NOT_FOUND: [], PROBLEM_MISSING: []}


@pytest.mark.parametrize(
    ("kind", "platform"), [(TYPE_DIGITAL, "binary_sensor"), (TYPE_ANALOG, "sensor"), (None, "sensor")]
)
def test_platform_follows_the_kind(kind: str | None, platform: str) -> None:
    assert onewire_platform(_sensor(kind=kind)) == platform


@pytest.mark.parametrize(
    ("name", "entity_name"), [("Kitchen", "OT1 Kitchen"), ("", "OT1"), ("  ", "OT1"), ("Two\nlines", "OT1 Two lines")]
)
def test_entity_name(name: str, entity_name: str) -> None:
    assert onewire_entity_name(_sensor(name=name)) == entity_name


def test_unique_and_entity_id_carry_only_the_address() -> None:
    unique_id = onewire_unique_id("srv", "OT12")

    assert unique_id == "comexio_srv_onewire_ot12"
    assert stable_object_id(unique_id) == "srv_onewire_ot12"


# --- coordinator ------------------------------------------------------------------------------------------------


def _coordinator(reading: OneWireReading | None = None, data: dict[str, Any] | None = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.hass = MagicMock()
    coordinator.server_id = "srv"
    coordinator.api = MagicMock()
    coordinator.api.get_onewire_status = AsyncMock(return_value=reading or OneWireReading({"OT1": _status()}))
    coordinator.onewire = OneWireState()
    coordinator.unloading = False
    coordinator._onewire_task = None
    coordinator._onewire_outage_warned = False
    coordinator._onewire_traceback_logged = False
    coordinator.data = data
    return coordinator


def test_tick_stores_the_statuses_and_notifies_the_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator()

    asyncio.run(coordinator._async_onewire_tick())

    assert coordinator.onewire.status("OT1") == _status()
    sent.assert_called_once_with(coordinator.hass, onewire_signal("srv"))


def test_tick_warns_once_when_the_states_turn_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator(OneWireReading(None, "HTTP 500"))

    async def ticks() -> None:
        for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD + 1):
            await coordinator._async_onewire_tick()

    with caplog.at_level(logging.WARNING):
        asyncio.run(ticks())

    warnings = [r for r in caplog.records if "1-Wire status poll failed" in r.getMessage()]
    assert len(warnings) == 1
    assert "HTTP 500" in warnings[0].getMessage()
    assert sent.call_count == ONEWIRE_FAIL_STREAK_THRESHOLD + 1


def test_tick_logs_the_recovery_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator(OneWireReading(None, "HTTP 500"))

    async def ticks() -> None:
        for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD):
            await coordinator._async_onewire_tick()
        coordinator.api.get_onewire_status.return_value = OneWireReading({"OT1": _status()})
        for _ in range(2):
            await coordinator._async_onewire_tick()

    with caplog.at_level(logging.INFO):
        asyncio.run(ticks())

    assert sum("1-Wire status poll works again" in r.getMessage() for r in caplog.records) == 1
    assert coordinator.onewire.status("OT1") == _status()


def test_an_unexpected_error_counts_as_a_failed_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It must neither end the task unseen nor leave the entities on their last states."""
    sent = MagicMock()
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", sent)
    coordinator = _coordinator()
    asyncio.run(coordinator._async_onewire_tick())
    coordinator.api.get_onewire_status.side_effect = KeyError("value")

    async def ticks() -> None:
        for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD):
            await coordinator._async_onewire_tick()

    with caplog.at_level(logging.WARNING):
        asyncio.run(ticks())

    assert "1-Wire status poll failed unexpectedly" in caplog.text
    assert "1-Wire sensors unavailable: unexpected error (KeyError)" in caplog.text
    assert coordinator.onewire.statuses is None
    assert sent.call_count == ONEWIRE_FAIL_STREAK_THRESHOLD + 1


def test_one_unexpected_error_keeps_the_last_statuses_until_a_good_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator()
    asyncio.run(coordinator._async_onewire_tick())
    coordinator.api.get_onewire_status.side_effect = KeyError("value")

    asyncio.run(coordinator._async_onewire_tick())
    assert coordinator.onewire.status("OT1") == _status()

    coordinator.api.get_onewire_status.side_effect = None
    coordinator.api.get_onewire_status.return_value = OneWireReading({"OT1": _status(value=7.0)})
    asyncio.run(coordinator._async_onewire_tick())
    assert coordinator.onewire.status("OT1") == _status(value=7.0)


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info and r.levelno >= logging.ERROR]


def test_a_lasting_unexpected_error_logs_its_traceback_once_per_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Once, not every minute: a later failure goes to debug, only a good poll arms the traceback again."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator()
    poll = coordinator.api.get_onewire_status

    async def ticks(count: int) -> None:
        for _ in range(count):
            await coordinator._async_onewire_tick()

    with caplog.at_level(logging.DEBUG):
        poll.side_effect = KeyError("value")
        asyncio.run(ticks(ONEWIRE_FAIL_STREAK_THRESHOLD + 2))
        # A failed poll without an exception is no good poll: the outage goes on.
        poll.side_effect = None
        poll.return_value = OneWireReading(None, "HTTP 500")
        asyncio.run(ticks(1))
        poll.side_effect = KeyError("value")
        asyncio.run(ticks(1))
        assert len(_tracebacks(caplog)) == 1
        assert any("still fails unexpectedly" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)

        poll.side_effect = None
        poll.return_value = OneWireReading({"OT1": _status()})
        asyncio.run(ticks(1))
        poll.side_effect = KeyError("value")
        asyncio.run(ticks(1))

    assert len(_tracebacks(caplog)) == 2


def test_tick_does_not_warn_about_a_changed_certificate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The certificate repair is already raised; the states still turn unknown."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator(OneWireReading(None, SYSTEM_INFO_CERTIFICATE_CHANGED))

    async def ticks() -> None:
        for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD):
            await coordinator._async_onewire_tick()

    with caplog.at_level(logging.WARNING):
        asyncio.run(ticks())

    assert "1-Wire status poll failed" not in caplog.text
    assert coordinator.onewire.statuses is None


def test_the_recovery_is_logged_only_after_a_warned_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A certificate outage is not warned about, so its end is not reported either, not even after a warned one."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    coordinator = _coordinator()
    poll = coordinator.api.get_onewire_status
    good = OneWireReading({"OT1": _status()})

    async def outage_then_good(error: str) -> None:
        poll.return_value = OneWireReading(None, error)
        for _ in range(ONEWIRE_FAIL_STREAK_THRESHOLD):
            await coordinator._async_onewire_tick()
        poll.return_value = good
        await coordinator._async_onewire_tick()

    with caplog.at_level(logging.INFO):
        asyncio.run(outage_then_good("HTTP 500"))
        asyncio.run(outage_then_good(SYSTEM_INFO_CERTIFICATE_CHANGED))

    assert sum("1-Wire status poll failed" in r.getMessage() for r in caplog.records) == 1
    assert sum("works again" in r.getMessage() for r in caplog.records) == 1


def _scheduling_coordinator(data: dict[str, Any] | None) -> ComexioCoordinator:
    coordinator = _coordinator(data=data)
    coordinator.hass.async_create_task = MagicMock(side_effect=lambda coro, **_kw: coro.close() or MagicMock())
    return coordinator


def _configured_data() -> dict[str, Any]:
    """Coordinator data with one known sensor: the only state in which the poll has entities to feed."""
    return {ONEWIRE_DATA_KEY: {"OT1": _sensor()}}


def test_schedule_polls_configured_sensors() -> None:
    coordinator = _scheduling_coordinator(_configured_data())

    coordinator._async_schedule_onewire_tick()

    coordinator.hass.async_create_task.assert_called_once()


@pytest.mark.parametrize("data", [None, {}, {ONEWIRE_DATA_KEY: None}, {ONEWIRE_DATA_KEY: {}}], ids=str)
def test_schedule_skips_without_a_known_sensor(data: dict[str, Any] | None) -> None:
    """No sensor configured, or the list unknown: either way no 1-Wire entity exists to feed."""
    coordinator = _scheduling_coordinator(data)

    coordinator._async_schedule_onewire_tick()

    coordinator.hass.async_create_task.assert_not_called()


def test_schedule_skips_while_unloading() -> None:
    coordinator = _scheduling_coordinator(_configured_data())
    coordinator.unloading = True

    coordinator._async_schedule_onewire_tick()

    coordinator.hass.async_create_task.assert_not_called()


def test_schedule_skips_while_a_poll_runs() -> None:
    coordinator = _scheduling_coordinator(_configured_data())
    coordinator._onewire_task = MagicMock(done=MagicMock(return_value=False))

    coordinator._async_schedule_onewire_tick()

    coordinator.hass.async_create_task.assert_not_called()


def test_schedule_starts_a_new_poll_once_the_last_one_is_done() -> None:
    coordinator = _scheduling_coordinator(_configured_data())
    coordinator._onewire_task = MagicMock(done=MagicMock(return_value=True))

    coordinator._async_schedule_onewire_tick()

    coordinator.hass.async_create_task.assert_called_once()
    assert coordinator.hass.async_create_task.call_args.kwargs["name"] == "comexio_srv_onewire_tick"


def test_poll_start_ticks_at_once_and_then_every_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the immediate tick, the entities would stay unavailable for the first interval."""
    coordinator = _scheduling_coordinator(_configured_data())
    track = MagicMock(return_value="cancel")
    monkeypatch.setattr(coordinator_module, "async_track_time_interval", track)

    assert coordinator.async_start_onewire_poll() == "cancel"

    coordinator.hass.async_create_task.assert_called_once()
    track.assert_called_once_with(
        coordinator.hass, coordinator._async_schedule_onewire_tick, timedelta(seconds=ONEWIRE_POLL_INTERVAL_SEC)
    )


def test_poll_start_with_an_unknown_list_polls_once_the_list_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interval runs even while the sensor list is unknown, so a list parsed later is polled from then on."""
    coordinator = _scheduling_coordinator(None)
    track = MagicMock(return_value="cancel")
    monkeypatch.setattr(coordinator_module, "async_track_time_interval", track)

    coordinator.async_start_onewire_poll()

    coordinator.hass.async_create_task.assert_not_called()
    tick = track.call_args.args[1]
    coordinator.data = _configured_data()
    tick()
    coordinator.hass.async_create_task.assert_called_once()


@pytest.mark.parametrize(
    ("system_info", "onewire"), [(False, True), (True, False), (True, True)], ids=["1-wire", "system info", "both"]
)
def test_shutdown_cancels_the_running_polls(monkeypatch: pytest.MonkeyPatch, system_info: bool, onewire: bool) -> None:
    """A poll in flight must not outlive the API session async_unload_entry closes."""
    coordinator = _coordinator()
    for helper in ("_stop_connection_poll", "_cancel_preview_card_sweep", "_disarm_preview_cache"):
        monkeypatch.setattr(coordinator, helper, MagicMock(), raising=False)
    monkeypatch.setattr(coordinator, "_async_close_plan_preview_store", AsyncMock(), raising=False)
    coordinator._knx_dpt_reload_cancel = None
    coordinator._preview_refresh_cancel = None
    coordinator._system_info_task = None
    monkeypatch.setattr(coordinator_module.DataUpdateCoordinator, "async_shutdown", AsyncMock())

    async def scenario() -> list[asyncio.Task[None]]:
        if system_info:
            coordinator._system_info_task = asyncio.create_task(asyncio.sleep(60))
        if onewire:
            coordinator._onewire_task = asyncio.create_task(asyncio.sleep(60))
        await coordinator.async_shutdown()
        return [task for task in (coordinator._system_info_task, coordinator._onewire_task) if task is not None]

    tasks = asyncio.run(scenario())

    assert len(tasks) == system_info + onewire
    assert all(task.cancelled() for task in tasks)


# --- entities ---------------------------------------------------------------------------------------------------


def _entity_coordinator(sensors: dict[str, OneWireSensor] | None, statuses: dict[str, OneWireStatus] | None) -> Any:
    state = OneWireState()
    if statuses is not None:
        state.apply_status(statuses)
    return SimpleNamespace(server_id="srv", onewire=state, data={ONEWIRE_DATA_KEY: sensors})


def test_analog_sensor_shows_the_reading() -> None:
    entity = ComexioOneWireSensor(_entity_coordinator({"OT1": _sensor()}, {"OT1": _status()}), _sensor())

    assert entity.available is True
    assert entity.native_value == pytest.approx(21.5)
    assert entity.unique_id == "comexio_srv_onewire_ot1"
    assert entity.name == "OT1 Kitchen"
    assert entity.extra_state_attributes == {"address": "OT1", "hw_id": "28-0000000001"}


@pytest.mark.parametrize(
    "statuses",
    [None, {}, {"OT1": _status(online=False)}, {"OT1": _status(visible=False)}],
    ids=["unknown", "missing", "read error", "not found"],
)
def test_sensor_is_unavailable_without_a_good_status(statuses: dict[str, OneWireStatus] | None) -> None:
    """A read error can still carry a value: it must not show as a reading."""
    sensor, digital = _sensor(), _sensor("OT1", kind=TYPE_DIGITAL)
    coordinator = _entity_coordinator({"OT1": sensor}, statuses)

    assert ComexioOneWireSensor(coordinator, sensor).available is False
    assert ComexioOneWireBinarySensor(coordinator, digital).available is False


def test_sensor_without_a_status_has_no_value() -> None:
    entity = ComexioOneWireSensor(_entity_coordinator({"OT1": _sensor()}, {}), _sensor())

    assert entity.native_value is None
    assert entity.state_class == SensorStateClass.MEASUREMENT


@pytest.mark.parametrize(
    ("sensor", "device_class", "unit"),
    [
        (_sensor("OT1"), SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        (_sensor("OH2"), SensorDeviceClass.HUMIDITY, PERCENTAGE),
        (_sensor("OVAD3"), None, None),
        (replace(_sensor("OT1"), prefix="ot"), SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    ],
    ids=["temperature", "humidity", "unknown prefix", "lower-case prefix"],
)
def test_unit_follows_the_prefix(
    sensor: OneWireSensor, device_class: SensorDeviceClass | None, unit: str | None
) -> None:
    entity = ComexioOneWireSensor(_entity_coordinator(None, None), sensor)

    assert entity.device_class == device_class
    assert entity.native_unit_of_measurement == unit


@pytest.mark.parametrize(
    ("statuses", "is_on"),
    [
        ({"OIB1": _status("OIB1", value=1.0)}, True),
        ({"OIB1": _status("OIB1", value=0.0)}, False),
        ({"OIB1": _status("OIB1", value=None)}, None),
        ({}, None),
        (None, None),
    ],
    ids=["on", "off", "no value", "missing", "unknown"],
)
def test_digital_sensor_is_on_while_its_reading_is_not_zero(
    statuses: dict[str, OneWireStatus] | None, is_on: bool | None
) -> None:
    sensor = _sensor("OIB1", kind=TYPE_DIGITAL)
    entity = ComexioOneWireBinarySensor(_entity_coordinator({"OIB1": sensor}, statuses), sensor)

    assert entity.is_on is is_on


def test_every_1wire_entity_sits_on_the_one_1wire_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different identifiers would split the sensors over two devices."""
    monkeypatch.setattr(comexio_init.entity, "build_device_info", lambda _coordinator, **kwargs: kwargs)
    coordinator = _entity_coordinator(None, None)
    entities = [
        ComexioOneWireSensor(coordinator, _sensor()),
        ComexioOneWireBinarySensor(coordinator, _sensor("OIB1", kind=TYPE_DIGITAL)),
        ComexioOneWireProblemSensor(coordinator, "srv"),
    ]

    infos = [entity.device_info for entity in entities]

    assert all(info == infos[0] for info in infos)
    assert infos[0]["identifiers"] == {(DOMAIN, "srv_onewire")}
    assert infos[0]["name"] == "srv 1-Wire"


def test_problem_sensor_follows_the_states() -> None:
    sensors = {"OT1": _sensor(), "OT2": _sensor("OT2")}
    coordinator = _entity_coordinator(sensors, None)
    entity = ComexioOneWireProblemSensor(coordinator, "srv")

    assert entity.is_on is None
    assert entity.extra_state_attributes == {}

    coordinator.onewire.apply_status({"OT1": _status(), "OT2": _status("OT2", online=False)})
    assert entity.is_on is True
    assert entity.extra_state_attributes == {PROBLEM_READ_ERROR: ["OT2"], PROBLEM_NOT_FOUND: [], PROBLEM_MISSING: []}

    coordinator.onewire.apply_status({"OT1": _status(), "OT2": _status("OT2")})
    assert entity.is_on is False
    assert entity.unique_id == onewire_problem_unique_id("srv")


def test_problem_sensor_names_a_sensor_the_monitor_does_not_list() -> None:
    entity = ComexioOneWireProblemSensor(_entity_coordinator({"OT1": _sensor()}, {}), "srv")

    assert entity.is_on is True
    assert entity.extra_state_attributes[PROBLEM_MISSING] == ["OT1"]


def test_problem_sensor_is_unknown_while_the_sensor_list_is() -> None:
    """Known states but no sensor list: "no problem" would claim sensors that nothing checked."""
    entity = ComexioOneWireProblemSensor(_entity_coordinator(None, {"OT1": _status(online=False)}), "srv")

    assert entity.is_on is None
    assert entity.extra_state_attributes == {}


@pytest.mark.parametrize(
    ("entity_cls", "args"),
    [
        (ComexioOneWireSensor, (_sensor(),)),
        (ComexioOneWireBinarySensor, (_sensor("OIB1", kind=TYPE_DIGITAL),)),
        (ComexioOneWireProblemSensor, ("srv",)),
    ],
)
def test_entities_listen_to_the_onewire_signal(monkeypatch: pytest.MonkeyPatch, entity_cls: type, args: tuple) -> None:
    connect = MagicMock(return_value="disconnect")
    module = binary_sensor if entity_cls is ComexioOneWireProblemSensor else comexio_init.entity
    monkeypatch.setattr(module, "async_dispatcher_connect", connect)
    entity = entity_cls(_entity_coordinator(None, None), *args)
    entity.hass = MagicMock()
    entity.async_on_remove = MagicMock()

    asyncio.run(entity.async_added_to_hass())

    connect.assert_called_once_with(entity.hass, onewire_signal("srv"), entity.async_write_ha_state)
    entity.async_on_remove.assert_called_once_with("disconnect")


def test_setup_splits_the_sensors_by_kind_and_adds_the_problem_sensor() -> None:
    sensors = {"OT1": _sensor(), "OIB2": _sensor("OIB2", kind=TYPE_DIGITAL), "OX3": _sensor("OX3", kind=None)}
    coordinator = _entity_coordinator(sensors, None)

    assert [e.unique_id for e in _onewire_sensors(coordinator)] == [
        "comexio_srv_onewire_ot1",
        "comexio_srv_onewire_ox3",
    ]
    assert [e.unique_id for e in _onewire_binary_sensors(coordinator)] == [
        "comexio_srv_onewire_oib2",
        onewire_problem_unique_id("srv"),
    ]


@pytest.mark.parametrize("sensors", [None, {}], ids=["unknown", "none configured"])
def test_setup_adds_no_1wire_entity_without_sensors(sensors: dict[str, OneWireSensor] | None) -> None:
    coordinator = _entity_coordinator(sensors, None)

    assert _onewire_sensors(coordinator) == []
    assert _onewire_binary_sensors(coordinator) == []
    assert onewire_sensors_for_platform(SimpleNamespace(data=None), "sensor") == []


# --- orphan cleanup ---------------------------------------------------------------------------------------------


def test_cleanup_keeps_the_configured_sensors_on_their_platform() -> None:
    data = {ONEWIRE_DATA_KEY: {"OT1": _sensor(), "OIB2": _sensor("OIB2", kind=TYPE_DIGITAL)}}

    uids, platforms = comexio_init._onewire_entity_uids("srv", data, MagicMock(), "entry")

    assert platforms == {
        "comexio_srv_onewire_ot1": "sensor",
        "comexio_srv_onewire_oib2": "binary_sensor",
        onewire_problem_unique_id("srv"): "binary_sensor",
    }
    assert uids == set(platforms)


def test_cleanup_drops_every_1wire_entity_once_none_is_configured() -> None:
    assert comexio_init._onewire_entity_uids("srv", {ONEWIRE_DATA_KEY: {}}, MagicMock(), "entry") == (set(), {})


def test_cleanup_keeps_the_existing_1wire_entities_while_the_list_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bad config page must not remove the entities, nor their history."""
    entries = [
        SimpleNamespace(unique_id="comexio_srv_onewire_ot1"),
        SimpleNamespace(unique_id=onewire_problem_unique_id("srv")),
        SimpleNamespace(unique_id="comexio_srv_m12"),
        SimpleNamespace(unique_id="comexio_other_onewire_ot1"),
    ]
    monkeypatch.setattr(comexio_init.er, "async_entries_for_config_entry", lambda _reg, _entry_id: entries)

    uids, platforms = comexio_init._onewire_entity_uids("srv", {}, MagicMock(), "entry")

    assert uids == {"comexio_srv_onewire_ot1", onewire_problem_unique_id("srv")}
    assert platforms == {}
