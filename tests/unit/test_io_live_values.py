"""Physical IO values on a poll come from their own dashboard refresh, not the config page (#175)."""

import asyncio
import copy
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioConnectionError
import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.coordinator import ComexioCoordinator
from tests.common import load_json_fixture

# config_basic: BASE (extension 1, online) carries I1=11, Q1=12, AI1=13 and the inactive Q2=14;
# UD 1 (extension 2) is offline and carries QI1=21. Config page values: I1 "1", Q1 0, AI1 "21,5".
ONLINE_REQUESTS = [(1, "11"), (1, "12"), (1, "13")]


def _coordinator(api: ComexioAPI, *, io_states: dict[str, Any] | None = None) -> SimpleNamespace:
    """The attributes _async_fetch_parsed_config and the IO live path read, no HA instance."""
    fake = SimpleNamespace(
        api=api,
        server_id="iosrv1",
        marker_states={},
        knx_states={},
        io_states=io_states or {},
        _last_poll_scraped=False,
        _polled_plan_ids=None,
        note_comexio_unreachable=MagicMock(),
        _referenced_marker_ids=lambda: set(),
        _last_referenced_markers_from_snapshot=False,
        _last_referenced_marker_ids=set(),
    )
    fake._async_apply_io_live_states = lambda ios: ComexioCoordinator._async_apply_io_live_states(fake, ios)
    return fake


def _poll(
    api: ComexioAPI,
    conf: dict[str, Any],
    io_live: dict[str, str] | None | BaseException,
    *,
    io_states: dict[str, Any] | None = None,
    marker_live: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], AsyncMock]:
    """One _async_fetch_parsed_config run: ({IO id: entry}, {marker id: entry}, get_io_live_states mock)."""
    api.get_raw_config = AsyncMock(return_value=conf)
    api.get_live_states = AsyncMock(return_value=(marker_live or {}, {}))
    if isinstance(io_live, BaseException):
        api.client.get_io_live_states = AsyncMock(side_effect=io_live)
        fetch = api.client.get_io_live_states
    else:
        api.get_io_live_states = fetch = AsyncMock(return_value=io_live)
    coordinator = _coordinator(api, io_states=io_states)
    _raw, parsed, _knx = asyncio.run(ComexioCoordinator._async_fetch_parsed_config(coordinator, {}))
    return {io["id"]: io for io in parsed["io"]}, {m["id"]: m for m in parsed["markers"]}, fetch


@pytest.fixture
def api(parsing_api: ComexioAPI) -> ComexioAPI:
    parsing_api._client = MagicMock()
    parsing_api._client_credentials = parsing_api._credentials()
    return parsing_api


def test_live_values_replace_the_config_page_default(api: ComexioAPI) -> None:
    """The config page's 0 for Q1 is no reading: the live "1" counts; "74" on an analog IO is 74.0."""
    ios, _markers, fetch = _poll(api, load_json_fixture("config_basic.json"), {"11": "0", "12": "1", "13": "74"})

    assert (ios["11"]["value"], ios["12"]["value"], ios["13"]["value"]) == (0.0, 1.0, 74.0)
    # Active IOs of online extensions only: not the inactive Q2, not the offline extension's QI1.
    fetch.assert_awaited_once_with(ONLINE_REQUESTS)


def test_ios_without_a_live_answer_keep_their_last_known_value(api: ComexioAPI) -> None:
    """Not answered (e.g. "0:not_found", dropped by aiocomexio) or offline: last known, not the config's 0."""
    ios, _markers, _fetch = _poll(
        api,
        load_json_fixture("config_basic.json"),
        {"13": "9,5"},
        io_states={"11": 1.0, "12": 1.0, "21": 55.0},
    )

    assert {io_id: io["value"] for io_id, io in ios.items()} == {"11": 1.0, "12": 1.0, "13": 9.5, "21": 55.0}


def test_an_io_never_known_falls_back_to_the_parsed_value(api: ComexioAPI) -> None:
    ios, _markers, _fetch = _poll(api, load_json_fixture("config_basic.json"), {})

    assert ios["13"]["value"] == 21.5


def test_a_failed_fetch_keeps_every_last_known_value(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """A failed IO fetch reaches the coordinator as None and must not reset the IOs to the config's 0."""
    known = {"11": 0.0, "12": 1.0, "13": 18.25, "21": 40.0}

    with caplog.at_level(logging.WARNING):
        ios, _markers, fetch = _poll(
            api, load_json_fixture("config_basic.json"), ComexioConnectionError("gone"), io_states=known
        )

    fetch.assert_awaited_once()
    assert {io_id: io["value"] for io_id, io in ios.items()} == known
    assert "IO live states fetch failed; keeping last known values" in caplog.text


def test_marker_and_io_with_the_same_number_are_not_mixed(api: ComexioAPI) -> None:
    """Marker 15 and IO 15 share the number but come from separate requests and stay apart."""
    conf = copy.deepcopy(load_json_fixture("config_basic.json"))
    conf["FubModules"]["2"]["15"] = {"Id": 15, "Name": "Licht Keller", "Type": 1}
    conf["FubModules"]["1"]["1"]["inoutput"]["15"] = {
        "Id": 15,
        "Identifier": "AI2",
        "Description": "Temperatur",
        "InOutputTypeId": 5,
        "Active": True,
    }

    ios, markers, fetch = _poll(api, conf, {"15": "74"}, marker_live={"15": "1"})

    assert (markers["15"]["value"], ios["15"]["value"]) == (1.0, 74.0)
    fetch.assert_awaited_once_with([*ONLINE_REQUESTS, (1, "15")])
