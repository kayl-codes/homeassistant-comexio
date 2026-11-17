"""Doorbell events: which IOs are bell buttons, when an event rings, and what the entity sits on."""

import asyncio
from typing import Any
from unittest.mock import MagicMock, call, patch

from aiocomexio.doorstation import Doorstation
import pytest

from custom_components.comexio import DOORSTATION_ENTITY_DOMAINS, PLATFORMS
from custom_components.comexio.const import (
    DOMAIN,
    DOORBELL_EVENT_RING,
    doorstation_doorbell_unique_id,
    io_push_signal,
    io_unique_id,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.doorstation import DoorstationTracker, doorbell_ios
from custom_components.comexio.event import ComexioDoorbellEvent, _DoorbellSync, async_setup_entry

SERVER = "iosrv1"


def _io(identifier: str = "DI1", ext_id: str = "14", **overrides: Any) -> dict[str, Any]:
    io = {
        "id": f"io-{ext_id}-{identifier}",
        "ext_id": ext_id,
        "ext_name": "DOOR",
        "identifier": identifier,
        "is_binary": True,
        "is_input": True,
    }
    return io | overrides


def _tracker(*ext_ids: int) -> DoorstationTracker:
    tracker = DoorstationTracker()
    tracker.update({ext_id: Doorstation(ext_id=ext_id, camera_id=1, name="DOOR") for ext_id in ext_ids})
    return tracker


def _event(io: dict[str, Any] | None = None, tracker: DoorstationTracker | None = None) -> ComexioDoorbellEvent:
    coordinator = MagicMock(spec=ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.offline_extensions = set()
    coordinator.last_update_success = True
    coordinator.doorstations = tracker or _tracker(14)
    return ComexioDoorbellEvent(coordinator, io or _io())


# --- which IOs are bell buttons -----------------------------------------------------------------------------------


def test_the_bell_buttons_are_the_digital_inputs_di1_to_di3_of_a_doorstation() -> None:
    ios = [_io("DI1"), _io("DI2"), _io("DI3"), _io("PIR1"), _io("DI4"), _io("NameLED1", is_input=False)]

    assert [io["identifier"] for io in doorbell_ios(ios, [14])] == ["DI1", "DI2", "DI3"]


def test_a_di_of_an_extension_that_is_no_doorstation_is_no_bell() -> None:
    assert doorbell_ios([_io("DI1", ext_id="3")], [14]) == []


@pytest.mark.parametrize("doorstation_key", [14, "14"])
def test_the_extension_ids_are_compared_as_text(doorstation_key: int | str) -> None:
    assert doorbell_ios([_io("DI1", ext_id="14")], [doorstation_key]) == [_io("DI1", ext_id="14")]


def test_the_identifier_is_compared_in_upper_case() -> None:
    assert len(doorbell_ios([_io("di1")], [14])) == 1


def test_an_analog_di_or_an_output_is_no_bell() -> None:
    ios = [_io("DI1", is_binary=False), _io("DI2", is_input=False)]

    assert doorbell_ios(ios, [14]) == []


def test_an_io_without_a_flag_that_says_input_counts_as_an_input() -> None:
    io = _io("DI1")
    del io["is_input"]

    assert doorbell_ios([io], [14]) == [io]


def test_no_doorstation_known_means_no_bell() -> None:
    assert doorbell_ios([_io("DI1")], []) == []


# --- identity -----------------------------------------------------------------------------------------------------


def test_the_unique_id_names_the_extension_id_and_the_button_never_the_extension_name() -> None:
    assert doorstation_doorbell_unique_id(SERVER, 14, "DI1") == "comexio_iosrv1_doorstation14_di1_doorbell"
    assert _event().unique_id == "comexio_iosrv1_doorstation14_di1_doorbell"


def test_the_event_shares_no_unique_id_with_the_binary_sensor_of_the_same_button() -> None:
    """The orphan cleanup matches a unique_id of an IO with its platform: a shared id would delete one of them."""
    io = _io("DI1")

    assert _event(io).unique_id != io_unique_id(SERVER, io)


def test_the_name_carries_the_number_of_the_button() -> None:
    assert _event(_io("DI3")).translation_placeholders == {"button": "3"}


def test_the_event_is_a_doorbell_with_the_one_event_type_ring() -> None:
    event = _event()

    assert event.device_class == "doorbell"
    assert event.event_types == [DOORBELL_EVENT_RING] == ["ring"]


def test_the_event_joins_the_device_of_the_extension_like_its_io() -> None:
    with patch("custom_components.comexio.event.build_device_info", side_effect=lambda *_a, **kw: kw):
        info = _event().device_info

    assert info["identifiers"] == {("comexio", "iosrv1_door")}
    assert info["name"] == "iosrv1 EXT - DOOR"


# --- ringing ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1.0, 1, 2.5])
def test_a_push_that_reports_the_button_down_rings(value: float) -> None:
    event = _event()

    with patch.object(ComexioDoorbellEvent, "async_write_ha_state") as write:
        event._handle_push(value)

    assert event.state_attributes["event_type"] == "ring"
    write.assert_called_once_with()


@pytest.mark.parametrize("value", [0.0, 0, -1.0, None])
def test_a_push_that_reports_the_button_up_or_unknown_does_not_ring(value: float | None) -> None:
    event = _event()

    with patch.object(ComexioDoorbellEvent, "async_write_ha_state") as write:
        event._handle_push(value)

    assert event.state_attributes["event_type"] is None
    write.assert_not_called()


def test_every_push_down_rings_again() -> None:
    event = _event()

    with patch.object(ComexioDoorbellEvent, "async_write_ha_state") as write:
        event._handle_push(1.0)
        event._handle_push(0.0)
        event._handle_push(1.0)

    assert write.call_count == 2


def test_a_push_down_rings_whatever_the_value_before_it_was() -> None:
    """A poll that read the button down just before the push must not swallow the ring."""
    event = _event()

    with patch.object(ComexioDoorbellEvent, "async_write_ha_state") as write:
        event._handle_push(1.0)
        event._handle_push(1.0)

    assert write.call_count == 2


def test_the_event_listens_to_the_push_signal_of_its_io_and_stops_with_its_removal() -> None:
    event = _event()
    event.hass = MagicMock()
    event.async_on_remove = MagicMock()  # type: ignore[method-assign]

    with patch("custom_components.comexio.event.async_dispatcher_connect") as connect:
        asyncio.run(event.async_added_to_hass())

    connect.assert_called_once_with(event.hass, io_push_signal(SERVER, 14, "DI1"), event._handle_push)
    assert call(connect.return_value) in event.async_on_remove.call_args_list


def test_the_signal_names_the_io_by_its_extension_id_not_by_its_name_or_comexio_id() -> None:
    """A rename of the extension in Comexio must not move the signal away from the entity that listens to it."""
    assert io_push_signal(SERVER, 14, "DI1") != io_push_signal(SERVER, 14, "DI2")
    assert io_push_signal(SERVER, 14, "DI1") != io_push_signal(SERVER, 15, "DI1")
    assert io_push_signal(SERVER, 14, "DI1") != io_push_signal("other", 14, "DI1")
    assert io_push_signal(SERVER, 14, "DI1") == io_push_signal(SERVER, "14", "di1")  # group key as text, any case


def test_a_ring_while_the_entity_is_unavailable_is_dropped() -> None:
    """Written as it is, its timestamp would show up as a late ring once the entity is available again."""
    event = _event()
    event.coordinator.offline_extensions = {"DOOR"}

    with patch.object(ComexioDoorbellEvent, "async_write_ha_state") as write:
        event._handle_push(1.0)

    assert event.state_attributes["event_type"] is None
    write.assert_not_called()


# --- availability -------------------------------------------------------------------------------------------------


def test_the_event_is_available_while_its_doorstation_is_known_and_its_extension_online() -> None:
    event = _event()

    with patch("custom_components.comexio.event.CoordinatorEntity.available", True):
        assert event.available is True


def test_the_event_is_unavailable_while_the_last_poll_failed() -> None:
    event = _event()
    event.coordinator.last_update_success = False

    assert event.available is False


def test_the_event_is_unavailable_while_its_extension_is_offline() -> None:
    event = _event()
    event.coordinator.offline_extensions = {"DOOR"}

    with patch("custom_components.comexio.event.CoordinatorEntity.available", True):
        assert event.available is False


def test_the_event_is_unavailable_once_its_doorstation_is_no_longer_reported() -> None:
    tracker = _tracker(14)
    event = _event(tracker=tracker)

    with patch("custom_components.comexio.event.CoordinatorEntity.available", True):
        tracker.update({15: Doorstation(ext_id=15, camera_id=2, name="OTHER")})
        assert event.available is False


# --- the sync -----------------------------------------------------------------------------------------------------


def _sync(
    ios: list[dict[str, Any]], tracker: DoorstationTracker, include_offline: bool = False
) -> tuple[_DoorbellSync, MagicMock, MagicMock]:
    coordinator = MagicMock(spec=ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.data = {"io": ios}
    coordinator.doorstations = tracker
    add = MagicMock()
    return _DoorbellSync(coordinator, add, include_offline), add, coordinator


def _added(add: MagicMock) -> list[str]:
    return [entity.unique_id for call in add.call_args_list for entity in call.args[0]]


def test_the_sync_adds_an_event_for_each_bell_button_once() -> None:
    sync, add, _ = _sync([_io("DI1"), _io("DI2"), _io("PIR1")], _tracker(14))

    sync()
    sync()

    assert _added(add) == [
        "comexio_iosrv1_doorstation14_di1_doorbell",
        "comexio_iosrv1_doorstation14_di2_doorbell",
    ]


def test_the_sync_waits_for_the_doorstation_and_adds_the_events_with_the_poll_that_reports_it() -> None:
    sync, add, coordinator = _sync([_io("DI1")], DoorstationTracker())

    sync()
    assert _added(add) == []

    coordinator.doorstations.update({14: Doorstation(ext_id=14, camera_id=1, name="DOOR")})
    sync()

    assert _added(add) == ["comexio_iosrv1_doorstation14_di1_doorbell"]


def test_the_sync_adds_no_second_event_when_comexio_hands_the_button_another_id() -> None:
    sync, add, coordinator = _sync([_io("DI1", id="old")], _tracker(14))
    sync()

    coordinator.data = {"io": [_io("DI1", id="new")]}
    sync()

    assert _added(add) == ["comexio_iosrv1_doorstation14_di1_doorbell"]


def test_the_sync_adds_one_event_for_two_ios_of_one_button_in_the_same_poll() -> None:
    sync, add, _ = _sync([_io("DI1", id="a"), _io("DI1", id="b")], _tracker(14))

    sync()

    assert _added(add) == ["comexio_iosrv1_doorstation14_di1_doorbell"]


def test_the_sync_adds_an_event_for_a_button_that_shows_up_later() -> None:
    sync, add, coordinator = _sync([_io("DI1")], _tracker(14))
    sync()

    coordinator.data = {"io": [_io("DI1"), _io("DI2")]}
    sync()

    assert _added(add) == [
        "comexio_iosrv1_doorstation14_di1_doorbell",
        "comexio_iosrv1_doorstation14_di2_doorbell",
    ]


def test_the_sync_skips_a_button_of_an_offline_extension_unless_those_are_included() -> None:
    offline = _io("DI1", offline=True)

    skipped, add_skipped, _ = _sync([offline], _tracker(14), include_offline=False)
    included, add_included, _ = _sync([offline], _tracker(14), include_offline=True)
    skipped()
    included()

    assert _added(add_skipped) == []
    assert _added(add_included) == ["comexio_iosrv1_doorstation14_di1_doorbell"]


def _setup(options: dict[str, Any]) -> tuple[MagicMock, MagicMock, MagicMock]:
    coordinator = MagicMock(spec=ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.data = {"io": [_io("DI1")]}
    coordinator.doorstations = _tracker(14)
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    entry = MagicMock(entry_id="entry-1", data={}, options=options)
    add = MagicMock()
    asyncio.run(async_setup_entry(hass, entry, add))
    return coordinator, entry, add


def test_the_setup_adds_the_known_bells_and_keeps_listening_for_later_ones() -> None:
    coordinator, entry, add = _setup({})

    assert _added(add) == ["comexio_iosrv1_doorstation14_di1_doorbell"]
    (listener,), _ = coordinator.async_add_listener.call_args
    assert isinstance(listener, _DoorbellSync)
    entry.async_on_unload.assert_called_once_with(coordinator.async_add_listener.return_value)


def test_the_setup_without_imported_ios_adds_nothing_and_listens_to_nothing() -> None:
    coordinator, entry, add = _setup({"import_ios": False})

    add.assert_not_called()
    coordinator.async_add_listener.assert_not_called()
    entry.async_on_unload.assert_not_called()


def test_the_platform_is_set_up_and_its_registry_entries_survive_the_orphan_cleanup() -> None:
    assert "event" in PLATFORMS
    assert {"camera", "event"} == DOORSTATION_ENTITY_DOMAINS


def test_the_sync_copes_with_a_coordinator_that_has_no_data_yet() -> None:
    sync, add, coordinator = _sync([], _tracker(14))
    coordinator.data = None

    sync()

    add.assert_not_called()
