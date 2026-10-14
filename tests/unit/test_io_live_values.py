"""Physical IO values on a poll come from their own dashboard refresh, not the config page (#175)."""

import asyncio
import copy
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioConnectionError
import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.coordinator import ComexioCoordinator, _io_live_requests
from tests.common import load_json_fixture

# config_basic: BASE (extension 1, online) carries I1=11, Q1=12, AI1=13 and the inactive Q2=14;
# UD 1 (extension 2) is offline and carries QI1=21. Config page values: I1 "1", Q1 0, AI1 "21,5", QI1 "55".
ONLINE_REQUESTS = [(1, "11"), (1, "12"), (1, "13")]
NO_VALUE_YET = {"11": None, "12": None, "13": None, "21": None}
NO_LIVE_VALUE_LOG = "Comexio sent no live value for IOs"
FETCH_FAILED_LOG = "IO live states fetch failed"

IoLive = dict[str, Any] | None | BaseException | AsyncMock


def _coordinator(
    api: ComexioAPI, *, io_states: dict[str, Any] | None = None, real_ids: set[str] | None = None
) -> ComexioCoordinator:
    """A coordinator with just what _async_fetch_parsed_config and the IO live path read, no HA instance.

    real_ids defaults to every id in io_states: a value a test seeds counts as a real reading.
    """
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    io_states = dict(io_states or {})
    attrs: dict[str, Any] = {
        "api": api,
        "server_id": "iosrv1",
        "marker_states": {},
        "knx_states": {},
        "io_states": io_states,
        "_io_real_value_ids": set(io_states) if real_ids is None else set(real_ids),
        "_polled_io_real_ids": set(),
        "_unanswered_io_ids": set(),
        "_ios_without_ext_id": set(),
        "_conflicting_io_ids": set(),
        "_io_live_fetch_failing": False,
        "_io_index": {},
        "_fire_plan_event": MagicMock(),
        "async_update_listeners": MagicMock(),
        "schedule_plan_preview_refresh": MagicMock(),
        "_webhook_updated_markers": set(),
        "_webhook_updated_io_ids": set(),
        "_last_poll_scraped": False,
        "_polled_plan_ids": None,
        "note_comexio_unreachable": MagicMock(),
        "_referenced_marker_ids": lambda: set(),
        "_last_referenced_markers_from_snapshot": False,
        "_last_referenced_marker_ids": set(),
    }
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


def _poll(
    coordinator: ComexioCoordinator,
    conf: dict[str, Any],
    io_live: IoLive,
    *,
    marker_live: dict[str, Any] | None = None,
    entry_conf: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], AsyncMock]:
    """One _async_fetch_parsed_config run plus the R1 merge: ({IO id: entry}, {marker id: entry}, fetch mock).

    An exception goes through the real ComexioAPI.get_io_live_states (it must turn into None there).
    """
    api = coordinator.api
    api.get_raw_config = AsyncMock(return_value=conf)
    api.get_live_states = AsyncMock(return_value=(marker_live or {}, {}))
    if isinstance(io_live, BaseException):
        vars(api).pop("get_io_live_states", None)
        api.client.get_io_live_states = fetch = AsyncMock(side_effect=io_live)
    else:
        fetch = io_live if isinstance(io_live, AsyncMock) else AsyncMock(return_value=io_live)
        api.get_io_live_states = fetch
    _raw, parsed, _knx = asyncio.run(coordinator._async_fetch_parsed_config(entry_conf or {}))
    coordinator._merge_polled_states({"markers": parsed["markers"], "io": parsed["io"]})
    return {io["id"]: io for io in parsed["io"]}, {m["id"]: m for m in parsed["markers"]}, fetch


def _values(ios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {io_id: io["value"] for io_id, io in ios.items()}


def _config(**io_values: Any) -> dict[str, Any]:
    """config_basic with the config page values of BASE IOs replaced ({IO id: value})."""
    conf = copy.deepcopy(load_json_fixture("config_basic.json"))
    for io_id, value in io_values.items():
        conf["FubModules"]["1"]["1"]["inoutput"][io_id.removeprefix("io")]["Value"] = value
    return conf


@pytest.fixture
def api(parsing_api: ComexioAPI) -> ComexioAPI:
    parsing_api._client = MagicMock()
    parsing_api._client_credentials = parsing_api._credentials()
    return parsing_api


def test_live_values_replace_the_config_page_default(api: ComexioAPI) -> None:
    """The config page's 0 for Q1 is no reading: the live "1" counts; "74" on an analog IO is 74.0."""
    coordinator = _coordinator(api)
    ios, _markers, fetch = _poll(coordinator, _config(), {"11": "0", "12": "1", "13": "74"})

    assert (ios["11"]["value"], ios["12"]["value"], ios["13"]["value"]) == (0.0, 1.0, 74.0)
    # Active IOs of online extensions only: not the inactive Q2, not the offline extension's QI1.
    fetch.assert_awaited_once_with(ONLINE_REQUESTS)
    assert coordinator.io_states == {"11": 0.0, "12": 1.0, "13": 74.0, "21": None}
    assert coordinator._io_real_value_ids == {"11", "12", "13"}


def test_ios_without_a_live_answer_keep_their_last_known_value(api: ComexioAPI) -> None:
    """Not answered (e.g. "0:not_found", dropped by aiocomexio) or offline: last known, not the config's 0."""
    coordinator = _coordinator(api, io_states={"11": 1.0, "12": 1.0, "21": 40.0})
    ios, _markers, _fetch = _poll(coordinator, _config(), {"13": "9,5"})

    assert _values(ios) == {"11": 1.0, "12": 1.0, "13": 9.5, "21": 40.0}


def test_an_io_never_known_is_unknown_and_an_offline_one_is_not_requested(api: ComexioAPI) -> None:
    """No live answer and no push yet: None ("unknown"), never the config page's 21.5 / 55."""
    coordinator = _coordinator(api)
    ios, _markers, fetch = _poll(coordinator, _config(), {})

    assert (ios["13"]["value"], ios["21"]["value"]) == (None, None)
    fetch.assert_awaited_once_with(ONLINE_REQUESTS)
    assert coordinator._io_real_value_ids == set()


def test_a_failed_fetch_keeps_every_last_known_value(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """A failed IO fetch reaches the coordinator as None and must not reset the IOs to the config's 0."""
    known = {"11": 0.0, "12": 1.0, "13": 18.25, "21": 40.0}
    coordinator = _coordinator(api, io_states=known)

    with caplog.at_level(logging.WARNING):
        ios, _markers, fetch = _poll(coordinator, _config(), ComexioConnectionError("gone"))

    fetch.assert_awaited_once()
    assert _values(ios) == known
    assert f"{FETCH_FAILED_LOG}; 4 IOs keep their last known value, 0 without one yet" in caplog.text


def test_a_refused_request_is_a_failed_fetch_not_a_failed_poll(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """aiocomexio refuses a contradictory request with ValueError; the poll goes on with the last known values.

    The one warning names the cause, so a bug behind it is visible without DEBUG logging.
    """
    coordinator = _coordinator(api, io_states={"13": 18.25})
    with caplog.at_level(logging.WARNING):
        ios, _markers, _fetch = _poll(coordinator, _config(), ValueError("IO 13 under two extensions"))

    assert ios["13"]["value"] == 18.25
    assert "(cause: ValueError: IO 13 under two extensions)" in caplog.text


def test_cold_start_without_a_real_value_is_unknown_never_the_placeholder(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """No IO value known yet and the fetch fails: every IO is None ("unknown"), not the config page value.

    A false 0 must never reach the recorder. A later poll that leaves an IO unanswered keeps it
    None instead of taking the config page value — that is a placeholder, not a reading.
    """
    coordinator = _coordinator(api)

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            ios, _markers, _fetch = _poll(coordinator, _config(), ComexioConnectionError("gone"))
            assert _values(ios) == NO_VALUE_YET
            assert coordinator.io_states == NO_VALUE_YET
            assert coordinator._io_real_value_ids == set()
    assert f"{FETCH_FAILED_LOG}; 0 IOs keep their last known value, 4 without one yet show unknown" in caplog.text

    ios, _markers, _fetch = _poll(coordinator, _config(), {"11": "0"})
    assert _values(ios) == {**NO_VALUE_YET, "11": 0.0}
    assert coordinator._io_real_value_ids == {"11"}

    # AI1 still unanswered while its config page value moved on: still unknown, not 30.
    ios, _markers, _fetch = _poll(coordinator, _config(io13="30"), {"11": "1"})
    assert (ios["11"]["value"], ios["13"]["value"]) == (1.0, None)
    assert coordinator._io_real_value_ids == {"11"}


def test_a_failed_fetch_after_a_real_value_keeps_that_value(api: ComexioAPI) -> None:
    coordinator = _coordinator(api)
    _poll(coordinator, _config(), {"13": "74"})

    ios, _markers, _fetch = _poll(coordinator, _config(io13="30"), ComexioConnectionError("gone"))

    assert ios["13"]["value"] == 74.0
    assert coordinator._io_real_value_ids == {"13"}


@pytest.mark.parametrize("raw", ["", "  ", "0:not_found", "abc", "nan", "inf", "1.234,5", True, None, [1]])
def test_a_non_numeric_live_value_is_no_answer_and_never_zero(api: ComexioAPI, raw: Any) -> None:
    """Whatever aiocomexio passes through: a value that is no finite number never turns into 0."""
    coordinator = _coordinator(api, io_states={"12": 1.0})
    ios, _markers, _fetch = _poll(coordinator, _config(), {"12": raw, "13": raw})

    assert (ios["12"]["value"], ios["13"]["value"]) == (1.0, None)
    assert coordinator._io_real_value_ids == {"12"}


def test_a_decimal_comma_with_blanks_is_a_number(api: ComexioAPI) -> None:
    ios, _markers, _fetch = _poll(_coordinator(api), _config(), {"13": " 7,5 "})

    assert ios["13"]["value"] == 7.5


def test_unanswered_ios_warn_on_change_only(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(api)

    def poll(io_live: dict[str, str]) -> list[logging.LogRecord]:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _poll(coordinator, _config(), io_live)
        return [r for r in caplog.records if r.name == "custom_components.comexio.coordinator"]

    [first] = poll({"11": "1"})
    assert (first.levelno, first.getMessage()) == (
        logging.WARNING,
        f"[iosrv1] {NO_LIVE_VALUE_LOG} BASE AI1, BASE Q1; they keep their last known value "
        "(unknown if none is known yet)",
    )
    assert poll({"11": "1"}) == []
    [recovered] = poll({"11": "1", "12": "0", "13": "20"})
    assert (recovered.levelno, recovered.getMessage()) == (
        logging.INFO,
        "[iosrv1] Live values back for IOs BASE AI1, BASE Q1",
    )
    [again] = poll({"11": "1", "12": "0"})
    assert again.levelno == logging.WARNING
    assert f"{NO_LIVE_VALUE_LOG} BASE AI1;" in again.getMessage()


def test_a_failed_fetch_leaves_the_unanswered_set_alone(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """A failed fetch says nothing about single IOs: no recovery line, no repeated warning afterwards."""
    coordinator = _coordinator(api)
    _poll(coordinator, _config(), {"11": "1", "12": "0"})

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _poll(coordinator, _config(), ComexioConnectionError("gone"))
        _poll(coordinator, _config(), {"11": "1", "12": "0"})

    assert NO_LIVE_VALUE_LOG not in caplog.text
    assert "Live values back" not in caplog.text
    assert coordinator._unanswered_io_ids == {"13"}


def test_an_io_with_a_non_integer_extension_id_is_skipped_and_logged_once(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    conf = _config()
    # "²" passes str.isdigit but not int(): isdecimal must reject it before the int() call.
    conf["FubModules"]["1"]["²"] = conf["FubModules"]["1"].pop("1")
    coordinator = _coordinator(api)

    with caplog.at_level(logging.WARNING):
        ios, _markers, fetch = _poll(coordinator, conf, {})
    fetch.assert_awaited_once_with([])
    assert ios["13"]["value"] is None
    assert "No live value requested for IOs BASE AI1, BASE I1, BASE Q1: their extension id is no integer (²)" in (
        caplog.text
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _poll(coordinator, conf, {})
    assert "No live value requested" not in caplog.text


def test_webhook_value_during_the_io_fetch_wins(api: ComexioAPI) -> None:
    """R1: a push that lands while get_io_live_states is awaited is newer than the poll's answer."""
    coordinator = _coordinator(api)

    coordinator._io_index = {("base", "ai1"): {"id": "13", "ext_name": "BASE", "identifier": "AI1"}}

    async def push_during_fetch(_requests: list[tuple[int, str]]) -> dict[str, str]:
        coordinator.update_io_by_name("BASE", "AI1", 99.0)
        return {"12": "1", "13": "74"}

    ios, _markers, _fetch = _poll(coordinator, _config(), AsyncMock(side_effect=push_during_fetch))

    assert (ios["12"]["value"], ios["13"]["value"]) == (1.0, 99.0)
    assert (coordinator.io_states["12"], coordinator.io_states["13"]) == (1.0, 99.0)
    # The real push path marks the value as real, and the R1 merge keeps that mark.
    assert coordinator._io_real_value_ids == {"12", "13"}


def test_a_pushed_value_survives_a_later_poll_without_a_live_answer(api: ComexioAPI) -> None:
    """A webhook value counts as known: the next poll that leaves the IO unanswered keeps it, not None."""
    coordinator = _coordinator(api)
    _poll(coordinator, _config(), {})
    coordinator._io_index = {("base", "ai1"): {"id": "13", "ext_name": "BASE", "identifier": "AI1"}}
    coordinator.update_io_by_name("BASE", "AI1", 42.0)
    coordinator._webhook_updated_io_ids.clear()  # what _async_update_data does before the next fetch

    ios, _markers, _fetch = _poll(coordinator, _config(), {"11": "1"})

    assert ios["13"]["value"] == 42.0
    assert coordinator.io_states["13"] == 42.0
    assert "13" in coordinator._io_real_value_ids


def test_without_a_webhook_the_live_value_wins_and_is_stored(api: ComexioAPI) -> None:
    coordinator = _coordinator(api, io_states={"13": 99.0})

    ios, _markers, _fetch = _poll(coordinator, _config(), {"13": "74"})

    assert ios["13"]["value"] == 74.0
    assert coordinator.io_states["13"] == 74.0


def test_marker_and_io_with_the_same_number_are_not_mixed(api: ComexioAPI) -> None:
    """Marker 15 and IO 15 share the number but come from separate requests and stay apart."""
    conf = _config()
    conf["FubModules"]["2"]["15"] = {"Id": 15, "Name": "Licht Keller", "Type": 1}
    conf["FubModules"]["1"]["1"]["inoutput"]["15"] = {
        "Id": 15,
        "Identifier": "AI2",
        "Description": "Temperatur",
        "InOutputTypeId": 5,
        "Active": True,
    }

    ios, markers, fetch = _poll(_coordinator(api), conf, {"15": "74"}, marker_live={"15": "1"})

    assert (markers["15"]["value"], ios["15"]["value"]) == (1.0, 74.0)
    fetch.assert_awaited_once_with([*ONLINE_REQUESTS, (1, "15")])


def test_io_states_are_pruned_to_the_scraped_ios(api: ComexioAPI) -> None:
    """A deleted IO's value and real-value mark go, so an IO that later reuses its id starts clean."""
    coordinator = _coordinator(api, io_states={"11": 1.0, "99": 5.0}, real_ids={"11", "99"})

    coordinator._prune_vanished_states({"FubModules": {"1": {}}}, {"io": [{"id": "11"}], "knx": []})

    assert coordinator.io_states == {"11": 1.0}
    assert coordinator._io_real_value_ids == {"11"}


def test_an_empty_scrape_prunes_nothing(api: ComexioAPI) -> None:
    """get_raw_config returns {} on a transient failure: that must not wipe the cached values."""
    coordinator = _coordinator(api, io_states={"11": 1.0, "99": 5.0})

    coordinator._prune_vanished_states({}, {"io": [], "knx": []})

    assert coordinator.io_states == {"11": 1.0, "99": 5.0}
    assert coordinator._io_real_value_ids == {"11", "99"}


def _seed_trackers(coordinator: ComexioCoordinator) -> None:
    coordinator._polled_io_real_ids = {"13"}
    coordinator._unanswered_io_ids = {"13"}
    coordinator._ios_without_ext_id = {"12"}
    coordinator._conflicting_io_ids = {"11"}
    coordinator._io_live_fetch_failing = True


def test_io_import_off_skips_the_fetch_and_resets_the_trackers(api: ComexioAPI) -> None:
    """Nobody shows the IOs: no Comexio round-trip, and a re-enabled import reports afresh."""
    coordinator = _coordinator(api)
    _seed_trackers(coordinator)

    _ios, _markers, fetch = _poll(coordinator, _config(), {"13": "74"}, entry_conf={"import_ios": False})

    fetch.assert_not_awaited()
    assert coordinator._polled_io_real_ids == set()
    assert coordinator._unanswered_io_ids == set()
    assert coordinator._ios_without_ext_id == set()
    assert coordinator._conflicting_io_ids == set()
    assert coordinator._io_live_fetch_failing is False


def test_an_empty_scrape_leaves_the_trackers_alone(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """get_raw_config returns {} on a blip: no fetch, and no repeated warning for the still-unanswered IOs."""
    coordinator = _coordinator(api)
    _poll(coordinator, _config(), {"11": "1", "12": "0"})
    assert coordinator._unanswered_io_ids == {"13"}

    _ios, _markers, fetch = _poll(coordinator, {}, {"11": "1", "12": "0"})
    fetch.assert_not_awaited()
    assert coordinator._unanswered_io_ids == {"13"}

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _poll(coordinator, _config(), {"11": "1", "12": "0"})
    assert NO_LIVE_VALUE_LOG not in caplog.text
    assert "Live values back" not in caplog.text


def test_a_persistent_fetch_failure_warns_once_and_reports_its_end(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """WARNING on the first failed poll, DEBUG on repeats, INFO once it works again; api.py stays at DEBUG."""
    coordinator = _coordinator(api, io_states={"13": 18.25})

    def poll(io_live: IoLive) -> list[tuple[str, int, str]]:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            _poll(coordinator, _config(), io_live)
        return [
            (r.name.rsplit(".", 1)[-1], r.levelno, r.getMessage())
            for r in caplog.records
            if r.name.startswith("custom_components.comexio") and "IO live states" in r.getMessage()
        ]

    first = poll(ComexioConnectionError("gone"))
    assert [(name, level) for name, level, _msg in first] == [("api", logging.DEBUG), ("coordinator", logging.WARNING)]
    assert "gone" in first[0][2]
    assert (
        f"[iosrv1] {FETCH_FAILED_LOG}; 1 IOs keep their last known value, 3 without one yet show unknown"
        " (cause: ComexioConnectionError: gone)" == (first[1][2])
    )

    second = poll(ComexioConnectionError("gone"))
    assert [(name, level) for name, level, _msg in second] == [("api", logging.DEBUG), ("coordinator", logging.DEBUG)]

    assert poll({"11": "1", "12": "0", "13": "20"}) == [
        ("coordinator", logging.INFO, "[iosrv1] IO live states fetch works again")
    ]
    assert poll({"11": "1", "12": "0", "13": "20"}) == []


def test_conflicting_io_ids_are_left_out_and_logged_on_change(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """One IO id under two online extensions: only that id is left out, the other IOs keep their live values."""
    conf = _config()
    ud1 = conf["FubModules"]["1"]["2"]
    ud1["extension"]["Identifier"] = "1000-2000-3001"  # online
    ud1["inoutput"]["21"]["Id"] = 13  # the same IO id as BASE AI1
    coordinator = _coordinator(api, io_states={"13": 18.25})

    with caplog.at_level(logging.WARNING):
        ios, _markers, fetch = _poll(coordinator, conf, {"11": "1", "12": "0", "13": "74"})

    fetch.assert_awaited_once_with([(1, "11"), (1, "12")])
    assert (ios["11"]["value"], ios["12"]["value"], ios["13"]["value"]) == (1.0, 0.0, 18.25)
    assert (
        "[iosrv1] No live value requested for IOs BASE AI1 (id 13), UD 1 QI1 (id 13): "
        "each IO id appears under several extensions"
    ) in caplog.text
    assert NO_LIVE_VALUE_LOG not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _poll(coordinator, conf, {"11": "1", "12": "0"})
    assert "No live value requested" not in caplog.text


def test_io_live_requests_drop_only_conflicting_ids() -> None:
    """Identical duplicates stay (aiocomexio collapses them); an id under two extension ids goes entirely."""
    ios = [
        {"id": "13", "ext_id": "1"},
        {"id": "13", "ext_id": "2"},
        {"id": "11", "ext_id": "1"},
        {"id": "11", "ext_id": 1},
        {"id": "12", "ext_id": "3", "offline": True},
        {"id": "16", "ext_id": "٣"},  # Arabic-Indic 3: isdecimal, but aiocomexio refuses non-ASCII
    ]

    assert _io_live_requests(ios) == ([(1, "11"), (1, "11")], {"13"})


def test_an_io_that_left_the_request_is_not_reported_as_recovered(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """BASE goes offline: its unanswered IOs leave the request and get no "back" line."""
    coordinator = _coordinator(api)
    _poll(coordinator, _config(), {"11": "1"})
    assert coordinator._unanswered_io_ids == {"12", "13"}
    offline = _config()
    offline["FubModules"]["1"]["1"]["extension"]["Identifier"] = "5011"

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _ios, _markers, fetch = _poll(coordinator, offline, {})

    fetch.assert_awaited_once_with([])
    assert "Live values back" not in caplog.text
    assert coordinator._unanswered_io_ids == set()
