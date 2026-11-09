"""1-Wire sensors by Web-IO push: config parsing, pushed readings, status with the config poll, entities, cleanup."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import ComexioCertificateMismatchError, ComexioDataError
from aiocomexio.onewire import OneWireStatus
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import pytest

from custom_components.comexio import (
    _dispatch_push,
    _onewire_entity_uids,
    api as api_module,
    coordinator as coordinator_module,
)
from custom_components.comexio.api import SYSTEM_INFO_CERTIFICATE_CHANGED, ComexioAPI
from custom_components.comexio.binary_sensor import ComexioOneWireBinarySensor, ComexioOneWireProblemSensor
from custom_components.comexio.button import ComexioSyncButton, _known_source_names
from custom_components.comexio.const import (
    ONEWIRE_DATA_KEY,
    ONEWIRE_PENDING_VALUES_MAX,
    ONEWIRE_PROBLEM_MISSING,
    ONEWIRE_PROBLEM_NOT_FOUND,
    ONEWIRE_PROBLEM_READ_ERROR,
    SOURCE_CATEGORIES,
    WebioClass,
    onewire_entity_name,
    onewire_platform,
    onewire_problem_unique_id,
    onewire_problems,
    onewire_reading_ok,
    onewire_unique_id,
    required_webio_classes,
    stable_object_id,
)
from custom_components.comexio.coordinator import ComexioCoordinator, _imported_data
from custom_components.comexio.diagnostics import DIAGNOSTICS_OPTION_KEYS
from custom_components.comexio.entity import onewire_sensors_for_platform
from custom_components.comexio.sensor import ComexioOneWireSensor
from custom_components.comexio.services import flow_diagram
from custom_components.comexio.services.backup import _attach_backup_diffs, _label_backup_identity, _LabelMaps
from custom_components.comexio.services.misc import _build_element_matcher, _search_plan_labels
from custom_components.comexio.services.plan_actions import _build_visualize_lines
from custom_components.comexio.webhook_security import derive_webhook_id, payload_key

SERVER = "srv"


def _entry(config_id: int, prefix: str = "OT", *, binary: int = 2, active: int = 1) -> dict[str, Any]:
    """One $FubModules["12"] entry as Comexio stores it."""
    return {
        "Id": config_id,
        "Prefix": prefix,
        "Name": f"Sensor {config_id}",
        "HwId": f"28-{config_id}",
        "Binary": binary,
        "Active": active,
    }


def _fub_modules(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"2": {}, "12": {str(entry["Id"]): entry for entry in entries}}


def _item(address: str = "OT1", *, config_id: int = 1, kind: str = "analog", title: str = "Kitchen") -> dict[str, Any]:
    """A 1-Wire data item as api.onewire_item shapes it."""
    prefix = address.rstrip("0123456789")
    return {
        "id": config_id,
        "address": address,
        "name": f"1Wire {address}",
        "title": title,
        "type": kind,
        "prefix": prefix,
        "hw_id": "28-1",
        "value": None,
    }


def _status(address: str = "OT1", *, online: bool = True, visible: bool = True) -> OneWireStatus:
    return OneWireStatus(address=address, device_id="28-1", name="", online=online, visible=visible, value=1.0)


def _coordinator(*items: dict[str, Any]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.data = {ONEWIRE_DATA_KEY: list(items)}
    coordinator._onewire_values = {}
    coordinator.onewire_status = None
    coordinator._onewire_status_warned = False
    coordinator._warned_unknown_webhook_ios = set()
    coordinator._warned_unknown_webhook_cap = False
    coordinator._async_publish_pushed_value = MagicMock()
    coordinator.schedule_plan_preview_refresh = MagicMock()
    coordinator._preview_plan_cache = None
    coordinator.last_update_success = True
    return coordinator


# --- api: the sensor list from the config ------------------------------------------------------------------------


def test_the_config_gives_the_sensors_of_known_kind_sorted_by_config_id(comexio_api: ComexioAPI) -> None:
    items = comexio_api._parse_onewire(
        _fub_modules(_entry(3, "OIB", binary=1), _entry(1), _entry(2, "OH", binary=7), _entry(4, active=0))
    )

    assert [(i["address"], i["type"]) for i in items] == [("OT1", "analog"), ("OIB3", "digital")]
    assert items[0] == {
        "id": 1,
        "address": "OT1",
        "name": "1Wire OT1",
        "title": "Sensor 1",
        "type": "analog",
        "prefix": "OT",
        "hw_id": "28-1",
        "value": None,
    }
    assert comexio_api.onewire_known is True


@pytest.mark.parametrize("fub_modules", [None, {}, "text"], ids=["missing", "empty", "not a mapping"])
def test_a_failed_config_fetch_keeps_the_last_sensors(comexio_api: ComexioAPI, fub_modules: object) -> None:
    """A failed fetch is no installation without 1-Wire sensors: their commands must not become orphans."""
    known = comexio_api._parse_onewire(_fub_modules(_entry(1)))

    assert comexio_api._parse_onewire(fub_modules) == known


def test_the_sensors_are_unknown_until_a_group_parsed(comexio_api: ComexioAPI) -> None:
    assert comexio_api._parse_onewire(None) == []
    assert comexio_api.onewire_known is False


def test_the_parsed_items_are_copies(comexio_api: ComexioAPI) -> None:
    # The coordinator writes pushed values into the items; the kept list must not pick them up.
    comexio_api._parse_onewire(_fub_modules(_entry(1)))[0]["value"] = 21.5

    assert comexio_api._parse_onewire(None)[0]["value"] is None


def test_a_changed_config_group_keeps_the_last_sensors_and_warns_once_per_cause(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    known = comexio_api._parse_onewire(_fub_modules(_entry(1)))
    parse = MagicMock(side_effect=ComexioDataError("changed group"))
    monkeypatch.setattr(api_module, "parse_onewire_config", parse)

    with caplog.at_level(logging.DEBUG):
        first = comexio_api._parse_onewire(_fub_modules(_entry(2)))
        second = comexio_api._parse_onewire(_fub_modules(_entry(2)))
        parse.side_effect = ComexioDataError("another change")
        comexio_api._parse_onewire(_fub_modules(_entry(2)))

    assert first == known
    assert second == known
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "changed group" in warnings[0]
    assert "another change" in warnings[1]


def test_a_config_without_fub_modules_warns_once_until_one_parsed(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        comexio_api._parse_onewire(None)
        comexio_api._parse_onewire({})
        comexio_api._parse_onewire(_fub_modules(_entry(1)))
        comexio_api._parse_onewire(None)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all(api_module.ONEWIRE_NO_FUB_MODULES_CAUSE in w for w in warnings)


def test_a_sensor_of_unknown_kind_warns_once_per_changed_set(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The sensor is in the config but HA never shows it: that must not show in a debug log only."""
    unknown_oh2 = _fub_modules(_entry(1), _entry(2, "OH", binary=7))
    with caplog.at_level(logging.WARNING):
        comexio_api._parse_onewire(unknown_oh2)
        comexio_api._parse_onewire(unknown_oh2)
        comexio_api._parse_onewire(_fub_modules(_entry(1), _entry(2, "OH", binary=7), _entry(3, "OX", binary=7)))
        comexio_api._parse_onewire(_fub_modules(_entry(1)))

    warnings = [r.getMessage() for r in caplog.records if "unknown kind" in r.getMessage()]
    assert len(warnings) == 2
    assert warnings[0].endswith("OH2")
    assert warnings[1].endswith("OH2, OX3")


def test_of_sensors_sharing_a_config_id_only_the_first_by_address_gets_an_entity(
    comexio_api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """The audit, the sync and the plan wiring key a sensor by its config id: all of them must keep the same one.

    A sensor of unknown kind gets no entity anyway, so it takes no config id from one of known kind.
    """
    shared = {
        "2": {},
        "12": {
            "1": _entry(4, "OT"),
            "2": _entry(4, "OH"),
            "3": _entry(5),
            "4": _entry(6, "OA", binary=7),
            "5": _entry(6),
        },
    }
    with caplog.at_level(logging.WARNING):
        items = comexio_api._parse_onewire(shared)
        comexio_api._parse_onewire(shared)
        comexio_api._parse_onewire(_fub_modules(_entry(5)))
        comexio_api._parse_onewire(shared)
        comexio_api._parse_onewire({"2": {}, "12": {**shared["12"], "6": _entry(5, "OX")}})

    assert [(i["id"], i["address"]) for i in items] == [(4, "OH4"), (5, "OT5"), (6, "OT6")]
    warnings = [r.getMessage() for r in caplog.records if r.msg == api_module.ONEWIRE_SHARED_ID_LOG_MSG]
    assert warnings == [api_module.ONEWIRE_SHARED_ID_LOG_MSG % s for s in ("OT4", "OT4", "OT4, OX5")]


def test_the_status_fetch_reports_a_changed_certificate_without_raising(comexio_api: ComexioAPI) -> None:
    err = ComexioCertificateMismatchError("changed", expected="a" * 64, actual="b" * 64)
    comexio_api._client = MagicMock(get_onewire_status=AsyncMock(side_effect=err))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_onewire_status()) == (None, SYSTEM_INFO_CERTIFICATE_CHANGED)
    assert comexio_api.certificate_mismatch is err


def test_the_status_fetch_returns_the_statuses(comexio_api: ComexioAPI) -> None:
    statuses = {"OT1": _status()}
    comexio_api._client = MagicMock(get_onewire_status=AsyncMock(return_value=statuses))
    comexio_api._client_credentials = comexio_api._credentials()

    assert asyncio.run(comexio_api.get_onewire_status()) == (statuses, None)


# --- coordinator: pushed readings ---------------------------------------------------------------------------------


def test_a_push_stores_the_reading_of_its_sensor_by_address() -> None:
    ot1, oh1 = _item("OT1"), _item("OH1", config_id=1)  # a shared config id must not mix them up
    coordinator = _coordinator(ot1, oh1)

    coordinator.update_onewire("OH1", "55.5")

    assert oh1["value"] == pytest.approx(55.5)
    assert ot1["value"] is None
    assert coordinator._onewire_values == {"OH1": pytest.approx(55.5)}
    coordinator._async_publish_pushed_value.assert_called_once()


@pytest.mark.parametrize(("plan_ids", "logged"), [({"4"}, True), ({"5"}, False)], ids=["in plan", "other plan"])
def test_a_push_of_a_sensor_in_the_shown_plan_reaches_the_plan_card_log(plan_ids: set[str], logged: bool) -> None:
    # The plan element refers to the config id (4), the push names the address (OT1).
    coordinator = _coordinator(_item("OT1", config_id=4))
    coordinator.hass = MagicMock()
    coordinator._preview_plan_cache = {"fub_id": 7, "plan_name": "Plan", "onewire_ids": plan_ids}

    coordinator.update_onewire("OT1", 21.5)

    fired = [c.args[1] for c in coordinator.hass.bus.async_fire.call_args_list]
    expected = {"type": "onewire", "id": "4", "label": "OT1 Kitchen", "value": 21.5}
    assert [{k: event[k] for k in expected} for event in fired] == ([expected] if logged else [])


def test_the_shown_plan_tracks_its_source_elements_for_the_plan_card_log() -> None:
    # reference.type comes as int or string depending on the response shape. The keys are "<kind>_ids" of the
    # kinds _fire_plan_event is called with (marker, io, knx, onewire): a renamed key would silence that kind.
    refs = [(2, 5), ("2", "6"), (1, 11), ("1", 12), (11, 54), ("11", 55), (12, 4), ("12", "6"), (99, 1), ("4", 2)]
    elements = {str(n): {"reference": {"type": t, "ref_id": r}} for n, (t, r) in enumerate(refs)}
    elements["no ref"] = {"type": "comment"}

    assert coordinator_module._plan_ref_ids(elements) == {
        "marker_ids": {"5", "6"},
        "io_ids": {"11", "12"},
        "knx_ids": {"54", "55"},
        "onewire_ids": {"4", "6"},
    }


@pytest.mark.parametrize("pushed", ["nan", "inf", "abc", None])
def test_a_push_without_a_finite_number_makes_the_reading_unknown(pushed: Any) -> None:
    item = _item()
    coordinator = _coordinator(item)
    coordinator.update_onewire("OT1", 20)

    coordinator.update_onewire("OT1", pushed)

    assert item["value"] is None
    assert coordinator._onewire_values == {}


def test_a_push_for_an_unknown_sensor_warns_once_and_again_after_it_was_known(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _item()
    coordinator = _coordinator()

    with caplog.at_level(logging.DEBUG):
        coordinator.update_onewire("OT1", 1)
        coordinator.update_onewire("OT1", 2)
        coordinator.data[ONEWIRE_DATA_KEY] = [item]
        coordinator.update_onewire("OT1", 3)
        coordinator.data[ONEWIRE_DATA_KEY] = []
        coordinator.update_onewire("OT1", 4)

    levels = [r.levelno for r in caplog.records if "unknown 1-Wire sensor" in r.msg]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]


def test_the_next_config_keeps_the_pushed_readings_of_the_sensors_still_there() -> None:
    coordinator = _coordinator(_item("OT1"), _item("OT2", config_id=2))
    coordinator.update_onewire("OT1", 21)
    coordinator.update_onewire("OT2", 22)
    fresh = [_item("OT1"), _item("OT3", config_id=3)]

    coordinator._merge_onewire_values(fresh)

    assert [i["value"] for i in fresh] == [21, None]
    assert coordinator._onewire_values == {"OT1": 21}


def test_a_push_for_a_sensor_the_fetch_in_flight_brings_survives_its_merge() -> None:
    # The push lands while get_raw_config is still running: the data has no such sensor yet.
    coordinator = _coordinator()
    coordinator.update_onewire("OT1", "21,5")
    coordinator.update_onewire("OT9", 3)
    coordinator.update_onewire("OT9", "n/a")  # an unreadable push forgets the reading, like for a known sensor
    assert coordinator._onewire_values == {"OT1": pytest.approx(21.5)}
    fresh = [_item("OT1"), _item("OT9", config_id=9)]

    coordinator._merge_onewire_values(fresh)

    assert [i["value"] for i in fresh] == [pytest.approx(21.5), None]
    assert coordinator._onewire_values == {"OT1": pytest.approx(21.5)}


def test_the_readings_kept_for_sensors_not_in_the_data_are_bounded() -> None:
    # The pushed address comes from the request: a flood of new ones must not grow the dict without end.
    coordinator = _coordinator()
    for number in range(ONEWIRE_PENDING_VALUES_MAX + 10):
        coordinator.update_onewire(f"OT{number}", number)
    coordinator.update_onewire("OT0", 99)  # one already kept is still updated

    assert len(coordinator._onewire_values) == ONEWIRE_PENDING_VALUES_MAX
    assert coordinator._onewire_values["OT0"] == 99


def test_a_restart_seeds_the_last_state_unless_a_push_came_first() -> None:
    ot1, ot2 = _item("OT1"), _item("OT2", config_id=2)
    coordinator = _coordinator(ot1, ot2)
    coordinator.update_onewire("OT2", 30)

    coordinator.restore_onewire_value("OT1", 19.0)
    coordinator.restore_onewire_value("OT2", 18.0)
    coordinator.restore_onewire_value("OT9", 17.0)  # no longer configured

    assert (ot1["value"], ot2["value"]) == (19.0, 30)
    assert coordinator._onewire_values == {"OT1": 19.0, "OT2": 30}


# --- coordinator: the status with the config poll -----------------------------------------------------------------


def _status_coordinator(fetch: AsyncMock) -> ComexioCoordinator:
    coordinator = _coordinator(_item())
    coordinator.api = MagicMock(get_onewire_status=fetch)
    return coordinator


def test_without_sensors_no_status_is_fetched() -> None:
    fetch = AsyncMock()
    coordinator = _status_coordinator(fetch)
    coordinator.onewire_status = {"OT1": _status()}

    asyncio.run(coordinator._async_refresh_onewire_status([]))

    fetch.assert_not_awaited()
    assert coordinator.onewire_status is None


def test_a_failing_status_warns_once_until_it_is_fetched_again(caplog: pytest.LogCaptureFixture) -> None:
    statuses = {"OT1": _status()}
    fetch = AsyncMock(side_effect=[(None, "timeout"), (None, "timeout"), (statuses, None), (None, "timeout")])
    coordinator = _status_coordinator(fetch)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            asyncio.run(coordinator._async_refresh_onewire_status([_item()]))
        assert coordinator.onewire_status is statuses
        asyncio.run(coordinator._async_refresh_onewire_status([_item()]))

    assert coordinator.onewire_status is None
    messages = [(r.levelno, r.getMessage()) for r in caplog.records if "1-Wire status" in r.getMessage()]
    assert [level for level, _ in messages] == [logging.WARNING, logging.INFO, logging.WARNING]
    assert "timeout" in messages[0][1]


def test_a_changed_certificate_makes_the_status_unknown_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _status_coordinator(AsyncMock(return_value=(None, SYSTEM_INFO_CERTIFICATE_CHANGED)))
    coordinator.onewire_status = {"OT1": _status()}

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_refresh_onewire_status([_item()]))

    assert coordinator.onewire_status is None
    assert not caplog.records


def test_an_unexpected_status_error_does_not_break_the_poll(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _status_coordinator(AsyncMock(side_effect=RuntimeError("boom")))

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_refresh_onewire_status([_item()]))

    assert coordinator.onewire_status is None
    assert "unexpected error (RuntimeError)" in caplog.text
    # A bug in the fetch: its traceback must reach the log at the default level, not at debug only.
    traceback = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(traceback) == 1
    assert traceback[0].exc_info is not None


def test_a_rejected_login_during_the_status_fetch_reaches_the_poll() -> None:
    coordinator = _status_coordinator(AsyncMock(side_effect=ConfigEntryAuthFailed("rejected")))

    refresh = coordinator._async_refresh_onewire_status([_item()])
    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(refresh)


# --- const: naming, ids, problems ----------------------------------------------------------------------------------


def test_the_ids_carry_the_address_only() -> None:
    assert onewire_unique_id("Srv", "OT1") == "comexio_srv_onewire_ot1"
    assert stable_object_id(onewire_unique_id("srv", "OT1")) == "srv_onewire_ot1"
    assert onewire_problem_unique_id("srv") == "comexio_srv_onewire_problem"


@pytest.mark.parametrize(
    ("title", "expected"),
    [("Kitchen", "OT1 Kitchen"), ("", "OT1"), ("a\nb", "OT1 a b")],
    ids=["named", "unnamed", "multi-line"],
)
def test_the_entity_name_is_the_address_and_the_title(title: str, expected: str) -> None:
    assert onewire_entity_name(_item(title=title)) == expected


def test_a_digital_sensor_is_a_binary_sensor() -> None:
    assert onewire_platform(_item(kind="digital")) == "binary_sensor"
    assert onewire_platform(_item(kind="analog")) == "sensor"


def test_the_problems_name_every_affected_sensor_by_kind() -> None:
    statuses = {
        "OT1": _status("OT1"),
        "OT2": _status("OT2", online=False),
        "OT3": _status("OT3", visible=False, online=False),
    }

    assert onewire_problems(statuses, ["OT4", "OT3", "OT2", "OT1", "OT2"]) == {
        ONEWIRE_PROBLEM_READ_ERROR: ["OT2"],
        ONEWIRE_PROBLEM_NOT_FOUND: ["OT3"],
        ONEWIRE_PROBLEM_MISSING: ["OT4"],
    }


def test_a_reading_shows_while_the_status_is_unknown_or_ok_only() -> None:
    statuses = {"OT1": _status("OT1"), "OT2": _status("OT2", online=False)}

    assert onewire_reading_ok(None, "OT2") is True
    assert onewire_reading_ok(statuses, "OT1") is True
    assert onewire_reading_ok(statuses, "OT2") is False
    assert onewire_reading_ok(statuses, "OT9") is False


# --- data, Web-IO classes, audit and sync ---------------------------------------------------------------------------


def test_the_import_switch_takes_the_sensors_out_of_the_data() -> None:
    parsed = {"markers": [], "io": [], ONEWIRE_DATA_KEY: [_item()]}

    assert _imported_data(parsed, {})[ONEWIRE_DATA_KEY] == [_item()]  # on by default
    assert _imported_data(parsed, {"import_onewire": False})[ONEWIRE_DATA_KEY] == []


def test_every_import_switch_shows_in_the_diagnostics() -> None:
    # The switches decide which entities exist: a diagnostics dump without one cannot tell "off" from "no sensors".
    assert {cat.import_conf_key for cat in SOURCE_CATEGORIES.values()} <= set(DIAGNOSTICS_OPTION_KEYS)


def test_the_onewire_class_is_needed_only_with_a_sensor() -> None:
    assert WebioClass.ONEWIRE not in required_webio_classes({}, {ONEWIRE_DATA_KEY: []})
    assert WebioClass.ONEWIRE in required_webio_classes({}, {ONEWIRE_DATA_KEY: [_item()]})
    assert WebioClass.ONEWIRE not in required_webio_classes({"import_onewire": False}, {ONEWIRE_DATA_KEY: [_item()]})


def test_the_audit_expects_a_command_per_sensor_by_config_id() -> None:
    coordinator = _coordinator()
    coordinator.ignored_ids_for = lambda _cls: set()
    coordinator.config_entry = SimpleNamespace(data={}, options={})
    data = {"markers": [], "knx": [], "io": [], ONEWIRE_DATA_KEY: [_item("OT1", config_id=4)]}

    ha_map, _io_meta = coordinator._build_ha_audit_map(data)

    assert ha_map == {"1W4": {"name": "HA 1Wire OT1", "type": "analog"}}


@pytest.mark.parametrize(
    ("wired", "expected"), [(set(), {"name": "HA 1Wire OT1", "onewire_id": 4}), ({("4", "9")}, None)]
)
def test_an_unwired_onewire_command_is_a_plan_gap(wired: set[tuple[str, str]], expected: dict | None) -> None:
    pairs = {ref_type: (set(), set()) for ref_type in ("2", "11", "1")} | {"12": (wired, set())}

    item = ComexioCoordinator._function_plan_gap_item("1W4", "HA 1Wire OT1", {"webIoId": 9}, pairs, None, set())

    assert item == expected


def test_a_sensor_element_left_without_its_command_is_plan_debris() -> None:
    # Config id 4 is a configured sensor (named like its command), 9 is gone from the config (bare key).
    coordinator = _coordinator()
    coordinator._dangling_source_ids = lambda source_type, *_args: {"4": [70], "9": [70]} if source_type == "12" else {}
    no_wiring = {ref_type: (set(), set()) for ref_type in ("2", "11", "12", "1")}
    mismatches: set[str] = set()

    items = coordinator._function_plan_dangling_items(
        {"markers": [], "knx": [], ONEWIRE_DATA_KEY: [_item("OT1", config_id=4)]}, no_wiring, True, set(), mismatches
    )

    assert items == [
        {"name": "1Wire OT1", "ref_id": "4", "webio_class": WebioClass.ONEWIRE},
        {"name": "1W9", "ref_id": "9", "webio_class": WebioClass.ONEWIRE},
    ]
    assert mismatches == {"function_plan_dangling_1W4", "function_plan_dangling_1W9"}


def test_a_sensor_push_reaches_a_registered_webhook_id() -> None:
    # The id the push resolves to (payload_key) must be one the setup registers, or Comexio's POST gets a 404.
    item = _item("OT1", config_id=4)
    coordinator = _coordinator(item)
    coordinator.api = SimpleNamespace(webhook_security=SimpleNamespace(secret="s" * 64))
    coordinator.webio_source_data = lambda include_held=False: {ONEWIRE_DATA_KEY: [item]}

    ids = coordinator.webhook_command_ids()

    assert derive_webhook_id("s" * 64, payload_key({"type": "onewire", "id": "OT1", "value": 1})) in ids


def test_the_sync_finds_a_created_command_by_its_exact_name() -> None:
    # A 1-Wire command name ends in the address (OT1), not in the config id the plan element needs (4).
    data = {"markers": [{"id": 5, "name": "M5 Licht"}], ONEWIRE_DATA_KEY: [_item("OT1", config_id=4)]}
    cats = [SOURCE_CATEGORIES[WebioClass.MARKER], SOURCE_CATEGORIES[WebioClass.ONEWIRE]]

    known = _known_source_names(data, cats)

    assert known == {"HA M5 Licht": (WebioClass.MARKER, 5), "HA 1Wire OT1": (WebioClass.ONEWIRE, 4)}
    # The created names go to the wiring by the id their plan element carries.
    assert ComexioSyncButton._classify_created_names(["HA 1Wire OT1", "HA M5 Licht"], cats, {}, known) == (
        {WebioClass.ONEWIRE: [4], WebioClass.MARKER: [5]},
        [],
    )


def test_a_command_name_two_items_share_is_left_to_the_gap_closing() -> None:
    # Two markers with the same name: the command could be either one's, so it is attributed to neither.
    data = {
        "markers": [{"id": 5, "name": "M5 Licht"}, {"id": 6, "name": "M5 Licht"}, {"id": 7, "name": "M7 Tür"}],
        ONEWIRE_DATA_KEY: [_item("OT1", config_id=4)],
    }
    cats = [SOURCE_CATEGORIES[WebioClass.MARKER], SOURCE_CATEGORIES[WebioClass.ONEWIRE]]

    known = _known_source_names(data, cats)

    assert known == {"HA M7 Tür": (WebioClass.MARKER, 7), "HA 1Wire OT1": (WebioClass.ONEWIRE, 4)}
    # Not attributed to either item by name; a marker's name still carries its id, which the name parse reads.
    assert ComexioSyncButton._classify_created_names(["HA M5 Licht", "HA M7 Tür"], cats, {}, known) == (
        {WebioClass.MARKER: [5, 7]},
        [],
    )


def test_a_dropped_ambiguous_command_name_is_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    data = {"markers": [{"id": 5, "name": "M5 Licht"}, {"id": 6, "name": "M5 Licht"}]}

    _known_source_names(data, [SOURCE_CATEGORIES[WebioClass.MARKER]])

    assert "command 'HA M5 Licht' is named by more than one Comexio item" in caplog.text


def test_the_plan_preview_labels_a_sensor_element_like_its_entity() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4), _item("OH4", config_id=4, title="Bath"))

    labels = coordinator.function_plan_onewire_label_map()

    assert list(labels) == ["4"]
    assert labels["4"]["name"] == "OT1 Kitchen"
    assert labels["4"]["address"] == "OT1"


def test_the_source_label_maps_carry_the_knx_and_1wire_maps_of_the_plan_services() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))

    maps = coordinator.function_plan_source_label_maps()

    assert set(maps) == {"knx_by_id", "onewire_by_id"}
    assert maps["onewire_by_id"] == coordinator.function_plan_onewire_label_map()


def test_the_visualize_text_names_a_sensor_element_like_its_pill() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))
    elements = {"1": {"reference": {"type": 12, "ref_id": 4}, "position_x": 0, "position_y": 0}}

    _conn_lines, orphan_lines = _build_visualize_lines(
        elements, {}, {}, ({}, {}, {}), coordinator.function_plan_source_label_maps()
    )

    assert "OT1 Kitchen" in orphan_lines[0]


def test_the_visualize_text_names_a_sensor_on_either_end_of_a_connection() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))
    elements = {
        "1": {"reference": {"type": 12, "ref_id": 4}, "position_x": 0, "position_y": 0},
        "2": {"reference": {"type": 12, "ref_id": 4}, "position_x": 100, "position_y": 0},
    }
    connections = {"1": {"input": {"FubElementId": 1}, "output": [{"FubElementId": 2}], "type": 0}}

    conn_lines, _orphan_lines = _build_visualize_lines(
        elements, connections, {}, ({}, {}, {}), coordinator.function_plan_source_label_maps()
    )

    assert conn_lines[0].count("OT1 Kitchen") == 2


def test_a_backup_diff_names_a_sensor_connection_like_its_pill() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))
    coordinator.function_plan_label_maps = lambda: ({}, {}, {})
    coordinator.function_plan_catalog = SimpleNamespace(async_get_catalog=AsyncMock(return_value={}))
    elements = {
        "1": {"reference": {"type": 12, "ref_id": 4}, "position_x": 0, "position_y": 0},
        "2": {"reference": {"type": 2, "ref_id": 5}, "position_x": 100, "position_y": 0},
    }
    wire = {"1": {"input": {"FubElementId": 1}, "output": [{"FubElementId": 2}], "type": 0}}
    snapshots = {0: {"elements": elements, "connections": wire}, 1: {"elements": elements, "connections": {}}}

    async def get_snapshot(_kind: str, _fub_id: int, _name: str, slot: int) -> dict[str, Any] | None:
        return snapshots.get(slot)

    coordinator.function_plan_backup = SimpleNamespace(async_get_snapshot=get_snapshot)
    entries = [{"fub_id": 7, "plan_name": "Plan", "slot": 0}]

    asyncio.run(_attach_backup_diffs(coordinator, entries, "auto"))

    assert "OT1 Kitchen" in str(entries[0]["diff"]["connections"])


def test_a_backup_diff_names_a_sensor_endpoint_like_its_pill() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))
    maps = _LabelMaps({}, {}, {}, coordinator.function_plan_source_label_maps())

    # diff_snapshots() identity of a type without a stable cross-snapshot reference: (type, ref_id, x, y, name).
    assert "OT1 Kitchen" in _label_backup_identity((12, 4, 10.0, 20.0, ""), {}, maps)


def test_the_flow_diagram_names_a_sensor_element_like_its_pill() -> None:
    coordinator = _coordinator(_item("OT1", config_id=4))
    coordinator.function_plan_catalog = SimpleNamespace(async_get_catalog=AsyncMock(return_value={}))
    elements = {
        "1": {"reference": {"type": 12, "ref_id": 4}, "position_x": 0, "position_y": 0},
        "2": {"reference": {"type": 2, "ref_id": 5}, "position_x": 100, "position_y": 0},
    }
    connections = {"1": {"input": {"FubElementId": 1}, "output": [{"FubElementId": 2}], "type": 0}}
    source = (coordinator, MagicMock(), 7, "Plan", elements, connections, "live", None)

    with patch.object(flow_diagram, "_resolve_visualize_live_source", AsyncMock(return_value=source)):
        result = asyncio.run(flow_diagram._handle_function_plan_flow_diagram(MagicMock(), SimpleNamespace(data={})))

    assert result["error"] is None
    assert "OT1" in result["svg"]


@pytest.mark.parametrize(("query", "found"), [("OT1", True), ("ot*", True), ("OT2", False), ('"Kitchen"', True)])
def test_the_plan_search_finds_a_sensor_element_by_its_address(query: str, found: bool) -> None:
    # The element carries the config id (4); a user searches the address the entity shows (OT1).
    coordinator = _coordinator(_item("OT1", config_id=4))
    coordinator.function_plan_plans = {7: {"elements": {"1": {"reference": {"type": 12, "ref_id": 4}}}}}
    source_maps = {"knx_by_id": {}, "onewire_by_id": coordinator.function_plan_onewire_label_map()}

    hits, failure = asyncio.run(
        _search_plan_labels(
            coordinator, None, 7, {"Name": "Plan"}, {}, {}, {}, {}, source_maps, _build_element_matcher(query), {}
        )
    )

    assert failure is None
    assert bool(hits) is found
    if found:
        assert "OT1" in hits[0]


# --- webhook dispatch ------------------------------------------------------------------------------------------------


def test_a_onewire_push_goes_to_its_sensor_by_address() -> None:
    coordinator = MagicMock(server_id=SERVER)

    _dispatch_push(coordinator, MagicMock(), {"type": "onewire", "id": "OT1", "value": 21.5})

    coordinator.update_onewire.assert_called_once_with("OT1", 21.5)
    coordinator.update_marker.assert_not_called()


def test_a_onewire_push_without_an_id_is_refused() -> None:
    coordinator, security = MagicMock(server_id=SERVER), MagicMock()

    _dispatch_push(coordinator, security, {"type": "onewire", "value": 1})

    coordinator.update_onewire.assert_not_called()
    assert security.warn_refused.call_args.args[0] == "missing-id-1-Wire"


# --- entities and the setup cleanup ----------------------------------------------------------------------------------


def test_the_platforms_split_the_sensors_by_kind() -> None:
    coordinator = _coordinator(_item("OT1"), _item("OIB2", config_id=2, kind="digital"))

    assert [s["address"] for s in onewire_sensors_for_platform(coordinator, "sensor")] == ["OT1"]
    assert [s["address"] for s in onewire_sensors_for_platform(coordinator, "binary_sensor")] == ["OIB2"]


@pytest.mark.parametrize(
    ("address", "device_class", "unit"),
    [
        ("OT1", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("OH1", SensorDeviceClass.HUMIDITY, PERCENTAGE),
        ("OP1", None, None),
    ],
)
def test_an_analog_sensor_takes_its_class_from_the_prefix(
    address: str, device_class: SensorDeviceClass | None, unit: str | None
) -> None:
    item = _item(address)
    entity = ComexioOneWireSensor(_coordinator(item), item)

    assert entity.device_class == device_class
    assert entity.native_unit_of_measurement == unit
    assert entity.unique_id == onewire_unique_id(SERVER, address)
    assert entity.name == f"{address} Kitchen"


def test_an_analog_sensor_shows_the_pushed_reading_while_its_status_is_ok() -> None:
    item = _item()
    coordinator = _coordinator(item)
    entity = ComexioOneWireSensor(coordinator, item)

    coordinator.update_onewire("OT1", 21.5)
    assert (entity.native_value, entity.available) == (21.5, True)  # status unknown: the reading stays

    coordinator.onewire_status = {"OT1": _status(online=False)}
    assert entity.available is False  # a read error can still carry a value
    coordinator.onewire_status = {}
    assert entity.available is False  # missing from the 1-Wire monitor


@pytest.mark.parametrize(("value", "expected"), [(None, None), (0, False), (1, True), (2.0, True)])
def test_a_digital_sensor_is_on_while_its_reading_is_not_zero(value: float | None, expected: bool | None) -> None:
    item = {**_item("OIB1", kind="digital"), "value": value}

    assert ComexioOneWireBinarySensor(_coordinator(item), item).is_on is expected


def test_the_problem_sensor_lists_the_affected_sensors_and_is_unknown_without_a_status() -> None:
    coordinator = _coordinator(_item("OT1"), _item("OT2", config_id=2))
    entity = ComexioOneWireProblemSensor(coordinator)

    assert (entity.is_on, entity.extra_state_attributes) == (None, {})

    coordinator.onewire_status = {"OT1": _status("OT1"), "OT2": _status("OT2")}
    assert entity.is_on is False

    coordinator.onewire_status = {"OT1": _status("OT1", visible=False)}
    assert entity.is_on is True
    assert entity.extra_state_attributes == {
        ONEWIRE_PROBLEM_READ_ERROR: [],
        ONEWIRE_PROBLEM_NOT_FOUND: ["OT1"],
        ONEWIRE_PROBLEM_MISSING: ["OT2"],
    }
    assert entity.unique_id == onewire_problem_unique_id(SERVER)


def _last_sensor_data(value: Any) -> SimpleNamespace:
    return SimpleNamespace(native_value=value)


@pytest.mark.parametrize(("last", "restored"), [("19.5", 19.5), ("unknown", None), (None, None)])
def test_an_analog_sensor_restores_a_numeric_last_state(
    monkeypatch: pytest.MonkeyPatch, last: Any, restored: float | None
) -> None:
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())
    item = _item()
    coordinator = _coordinator(item)
    entity = ComexioOneWireSensor(coordinator, item)
    entity.entity_id = "sensor.srv_onewire_ot1"
    entity.async_get_last_sensor_data = AsyncMock(return_value=_last_sensor_data(last))  # type: ignore[method-assign]

    asyncio.run(entity.async_added_to_hass())

    assert item["value"] == restored


@pytest.mark.parametrize(("state", "restored"), [("on", 1.0), ("off", 0.0), ("unavailable", None)])
def test_a_digital_sensor_restores_its_last_state(
    monkeypatch: pytest.MonkeyPatch, state: str, restored: float | None
) -> None:
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())
    item = _item("OIB1", kind="digital")
    entity = ComexioOneWireBinarySensor(_coordinator(item), item)
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=state))  # type: ignore[method-assign]

    asyncio.run(entity.async_added_to_hass())

    assert item["value"] == restored


def _registry_entry(unique_id: str, domain: str) -> SimpleNamespace:
    return SimpleNamespace(unique_id=unique_id, domain=domain)


def test_the_cleanup_keeps_the_entities_of_the_configured_sensors_and_the_problem_sensor() -> None:
    data = {ONEWIRE_DATA_KEY: [_item("OT1"), _item("OIB2", config_id=2, kind="digital")]}

    assert _onewire_entity_uids(SERVER, data, True, []) == {
        onewire_unique_id(SERVER, "OT1"): "sensor",
        onewire_unique_id(SERVER, "OIB2"): "binary_sensor",
        onewire_problem_unique_id(SERVER): "binary_sensor",
    }


def test_without_sensors_the_cleanup_removes_the_problem_sensor_too() -> None:
    stale = [_registry_entry(onewire_problem_unique_id(SERVER), "binary_sensor")]

    assert _onewire_entity_uids(SERVER, {ONEWIRE_DATA_KEY: []}, True, stale) == {}


def test_while_the_sensors_are_unknown_the_cleanup_keeps_the_registered_ones() -> None:
    """No config group parsed yet (e.g. a failed first fetch): the entities must survive the setup."""
    registered = [
        _registry_entry(onewire_unique_id(SERVER, "OT1"), "sensor"),
        _registry_entry(onewire_problem_unique_id(SERVER), "binary_sensor"),
        _registry_entry(f"comexio_{SERVER}_m5", "switch"),
    ]

    assert _onewire_entity_uids(SERVER, {}, False, registered) == {
        onewire_unique_id(SERVER, "OT1"): "sensor",
        onewire_problem_unique_id(SERVER): "binary_sensor",
    }
