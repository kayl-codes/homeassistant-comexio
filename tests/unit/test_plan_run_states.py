"""Function plan run states: cache update, HA's own start/stop, the poll and its guards."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioAuthenticationError, ComexioConnectionError
import pytest

from custom_components.comexio import binary_sensor as binary_sensor_module, coordinator as coordinator_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_DEBUG_INTERVAL_SEC,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_INTERVAL_SEC,
    function_plan_ids,
    function_plan_run_state_unique_id,
)
from custom_components.comexio.coordinator import ComexioCoordinator

FUBS = {"19": {"Id": 19, "Name": "Test1", "Active": 0}, "43": {"Id": 43, "Name": "Licht", "Active": 1}}


@pytest.fixture
def api(comexio_api: ComexioAPI) -> ComexioAPI:
    comexio_api._fub_data = {key: dict(fub) for key, fub in FUBS.items()}
    comexio_api._client = MagicMock()
    comexio_api._client_credentials = comexio_api._credentials()
    return comexio_api


def test_run_states_update_the_active_flags(api: ComexioAPI) -> None:
    old = api.fub_data["19"]
    assert api.apply_fub_run_states({19: True, 43: True}) is True
    assert api.get_fub_active(19) is True
    assert api.get_fub_active(43) is True
    assert old["Active"] == 0  # replaced, not mutated: parse_config shares it with the raw config


def test_unchanged_or_unknown_plans_report_no_change(api: ComexioAPI) -> None:
    api._fub_data["7"] = "malformed"
    assert api.apply_fub_run_states({19: False, 43: True, 99: True, 7: True}) is False
    assert "99" not in api.fub_data


@pytest.mark.parametrize(
    ("method", "client_method", "running"),
    [("function_plan_run_fup", "run_function_plan", True), ("function_plan_stop_fup", "stop_function_plan", False)],
)
def test_ha_started_or_stopped_plan_updates_right_away(
    api: ComexioAPI, method: str, client_method: str, running: bool
) -> None:
    api._fub_data["19"]["Active"] = int(not running)
    setattr(api.client, client_method, AsyncMock())
    listener = MagicMock()
    api.run_state_listener = listener
    assert asyncio.run(getattr(api, method)(19)) is True
    assert api.get_fub_active(19) is running
    listener.assert_called_once_with()


def test_unconfirmed_start_leaves_the_flag(api: ComexioAPI) -> None:
    api.client.run_function_plan = AsyncMock(side_effect=ComexioConnectionError("down"))
    listener = MagicMock()
    api.run_state_listener = listener
    assert asyncio.run(api.function_plan_run_fup(19)) is False
    assert api.get_fub_active(19) is False
    listener.assert_not_called()


def test_plan_ids_and_unique_id() -> None:
    assert function_plan_ids({"19": {}, "43": {}, "x": {}}) == {19, 43}
    assert function_plan_run_state_unique_id("IOSRV1", 19) == "comexio_iosrv1_fub19"


class _FakeCoordinator(SimpleNamespace):
    """Just the attributes the run-state poll reads, with the real coordinator methods."""

    plan_run_states_available = ComexioCoordinator.plan_run_states_available
    _plan_run_state_poll_blocked = ComexioCoordinator._plan_run_state_poll_blocked
    _async_refresh_plan_run_states = ComexioCoordinator._async_refresh_plan_run_states
    _async_refresh_run_states_in_preview = ComexioCoordinator._async_refresh_run_states_in_preview
    _async_plan_run_state_tick = ComexioCoordinator._async_plan_run_state_tick
    _async_plan_run_state_fetch_failed = ComexioCoordinator._async_plan_run_state_fetch_failed


def _coordinator(api: ComexioAPI, **overrides) -> _FakeCoordinator:
    fake = _FakeCoordinator(
        api=api,
        server_id="iosrv1",
        in_sync=False,
        _full_poll_running=False,
        _sync_lock=asyncio.Lock(),
        _restore_lock=asyncio.Lock(),
        _preview_plan_cache=None,
        _connection_poll_fast_requested=False,
        _plan_run_state_fail_streak=0,
        _plan_run_state_stale_count=0,
        _plan_run_state_last_fetch=0.0,
        _plan_run_state_relogin_refused=False,
        scraped_plan_ids=None,
        plan_scrape_generation=0,
        async_update_listeners=MagicMock(),
    )
    fake.__dict__.update(overrides)
    return fake


def _available(coordinator: _FakeCoordinator) -> bool:
    return coordinator.plan_run_states_available


def test_poll_applies_a_changed_state_and_refreshes_the_entities(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True})
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert api.client.get_function_plan_run_states.await_args.args[0] == [19, 43]
    assert api.get_fub_active(19) is True
    coordinator.async_update_listeners.assert_called_once_with()


def test_poll_without_change_does_not_refresh_the_entities(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator.async_update_listeners.assert_not_called()


@pytest.mark.parametrize(
    "busy",
    [{"in_sync": True}, {"_full_poll_running": True}, "_sync_lock", "_restore_lock"],
    ids=["sync or repair", "full poll", "sync lock", "restore"],
)
def test_poll_waits_for_sync_repair_restore_and_full_poll(api: ComexioAPI, busy) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True})

    async def run() -> None:
        coordinator = _coordinator(api, **busy) if isinstance(busy, dict) else _coordinator(api)
        if isinstance(busy, str):
            await getattr(coordinator, busy).acquire()
        await coordinator._async_refresh_plan_run_states()

    asyncio.run(run())
    api.client.get_function_plan_run_states.assert_not_awaited()


def test_timer_tick_leaves_an_armed_preview_to_its_own_poll(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={})
    asyncio.run(_coordinator(api, _preview_plan_cache={"fub_id": 19})._async_plan_run_state_tick())
    api.client.get_function_plan_run_states.assert_not_awaited()
    asyncio.run(_coordinator(api)._async_plan_run_state_tick())
    api.client.get_function_plan_run_states.assert_awaited_once()


def test_failures_keep_the_state_then_turn_the_sensors_unavailable(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioConnectionError("down"))
    coordinator = _coordinator(api)
    for _ in range(FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD - 1):
        asyncio.run(coordinator._async_refresh_plan_run_states())
    assert _available(coordinator) is True
    assert api.get_fub_active(43) is True
    coordinator.async_update_listeners.assert_not_called()

    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert _available(coordinator) is False
    coordinator.async_update_listeners.assert_called_once_with()

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert _available(coordinator) is True
    assert coordinator.async_update_listeners.call_count == 2  # back available, without a state change


def test_full_poll_freshens_the_state_but_keeps_the_outage_count(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A full poll makes the sensors available again; the endpoint outage is still logged once."""
    caplog.set_level(logging.INFO)
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioConnectionError("down"))
    coordinator = _coordinator(api)
    for _ in range(FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD):
        asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator._plan_run_state_stale_count = 0  # what a scraped full poll does
    assert _available(coordinator) is True
    caplog.clear()
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert not [r for r in caplog.records if r.levelname == "WARNING"]

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert f"works again after {FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD + 1} failure(s)" in caplog.text


def test_an_answer_without_any_state_is_a_failure(api: ComexioAPI) -> None:
    """A changed answer format makes aiocomexio skip every plan — that is no fresh state."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={})
    coordinator = _coordinator(api)
    for _ in range(FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD):
        asyncio.run(coordinator._async_refresh_plan_run_states())
    assert _available(coordinator) is False


def test_lapsed_main_session_is_logged_in_again(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    api.login = AsyncMock(return_value=True)
    asyncio.run(_coordinator(api)._async_refresh_plan_run_states())
    api.login.assert_awaited_once()


def test_lapsed_preview_session_leaves_the_main_session_alone(api: ComexioAPI) -> None:
    api.get_function_plan_run_states = AsyncMock(side_effect=ComexioAuthenticationError("login form"))
    api.login = AsyncMock(return_value=True)
    asyncio.run(_coordinator(api)._async_refresh_plan_run_states(session=MagicMock()))
    api.login.assert_not_awaited()


def test_refused_relogin_is_not_retried_every_tick(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioAuthenticationError("login form"))

    async def refuse() -> bool:
        api.last_login_error = "rejected"
        return False

    api.login = AsyncMock(side_effect=refuse)
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    asyncio.run(coordinator._async_refresh_plan_run_states())
    api.login.assert_awaited_once()
    assert "refused the re-login" in caplog.text


@pytest.mark.parametrize(
    ("polled", "scraped", "fails"),
    [({43}, True, False), (None, True, False), ({43}, False, False), ({43}, True, True)],
    ids=["fubs read", "no fubs", "fubs read, empty fub modules", "poll failed after the scrape"],
)
def test_only_a_full_poll_that_read_fubs_publishes_the_plan_list(
    api: ComexioAPI, polled, scraped: bool, fails: bool
) -> None:
    coordinator = _coordinator(api, _plan_run_state_stale_count=5, _plan_run_state_relogin_refused=True)

    async def fetch() -> dict:
        coordinator._last_poll_scraped = scraped
        coordinator._polled_plan_ids = polled
        if fails:
            raise ComexioConnectionError("down")
        return {}

    coordinator._async_fetch_and_audit = fetch
    poll = ComexioCoordinator._async_update_data(coordinator)
    with pytest.raises(ComexioConnectionError) if fails else contextlib.nullcontext():
        asyncio.run(poll)
    published = polled is not None and not fails
    assert coordinator._full_poll_running is False
    assert coordinator.scraped_plan_ids == (polled if published else None)
    assert coordinator.plan_scrape_generation == int(published)
    assert (coordinator._plan_run_state_stale_count == 0) is published
    assert coordinator._plan_run_state_relogin_refused is not published


class _FakeRegistry:
    def __init__(self, entity_ids: dict[str, str]) -> None:
        self.entity_ids = entity_ids
        self.removed: list[str] = []

    def async_get_entity_id(self, _domain: str, _platform: str, unique_id: str) -> str | None:
        return self.entity_ids.get(unique_id)

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


def _plan_sensor_sync(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, **coordinator_overrides):
    registry = _FakeRegistry(
        {function_plan_run_state_unique_id("iosrv1", fid): f"binary_sensor.iosrv1_fub{fid}" for fid in (19, 43)}
    )
    monkeypatch.setattr(binary_sensor_module.er, "async_get", lambda _hass: registry)
    coordinator = _coordinator(api, **coordinator_overrides)
    added: list = []
    sync = binary_sensor_module._PlanRunStateSensorSync(None, coordinator, lambda ents: added.extend(ents))
    return sync, coordinator, registry, added


def test_plan_sensors_are_added_for_new_plans(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _coordinator_, _registry, added = _plan_sensor_sync(api, monkeypatch)
    sync()
    assert [sensor.unique_id for sensor in added] == ["comexio_iosrv1_fub19", "comexio_iosrv1_fub43"]
    sync()
    assert len(added) == 2


def test_empty_fub_data_without_a_scrape_removes_no_sensor(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed scrape outside the full poll (e.g. a config reload in a sync) empties fub_data."""
    sync, coordinator, registry, _added = _plan_sensor_sync(
        api, monkeypatch, scraped_plan_ids={19, 43}, plan_scrape_generation=1
    )
    sync()
    api._fub_data = {}
    sync()
    assert registry.removed == []


def test_sensor_of_a_deleted_plan_is_removed_after_a_scraped_poll(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync, coordinator, registry, _added = _plan_sensor_sync(
        api, monkeypatch, scraped_plan_ids={19, 43}, plan_scrape_generation=1
    )
    sync()
    del api._fub_data["19"]
    coordinator.scraped_plan_ids = {43}
    coordinator.plan_scrape_generation = 2
    sync()
    assert registry.removed == ["binary_sensor.iosrv1_fub19"]


def test_plan_created_by_ha_after_the_scrape_keeps_its_sensor(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """create_fup adds the plan to fub_data between scrapes; the old plan list must not prune it."""
    sync, coordinator, registry, added = _plan_sensor_sync(
        api, monkeypatch, scraped_plan_ids={19, 43}, plan_scrape_generation=1
    )
    registry.entity_ids[function_plan_run_state_unique_id("iosrv1", 77)] = "binary_sensor.iosrv1_fub77"
    sync()
    api._fub_data["77"] = {"Id": 77, "Name": "Neu", "Active": 1}
    sync()
    fubs, api._fub_data = api._fub_data, {}  # a failed reload during a sync empties fub_data
    sync()  # same generation: no pruning against the old plan list
    api._fub_data = fubs
    coordinator.plan_scrape_generation = 2  # a poll whose scrape predates the plan
    sync()
    assert added[-1].unique_id == "comexio_iosrv1_fub77"
    assert registry.removed == []


def test_no_scraped_plan_list_removes_no_sensor(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    sync, _coordinator_, registry, _added = _plan_sensor_sync(api, monkeypatch)
    sync()
    api._fub_data = {}
    sync()
    assert registry.removed == []


@pytest.mark.parametrize(
    ("debug", "interval"),
    [(False, FUNCTION_PLAN_RUN_STATE_PREVIEW_INTERVAL_SEC), (True, FUNCTION_PLAN_RUN_STATE_PREVIEW_DEBUG_INTERVAL_SEC)],
)
def test_preview_poll_fetches_the_run_states_rate_limited(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, debug: bool, interval: int
) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={})
    coordinator = _coordinator(api, _connection_poll_fast_requested=debug)
    now = 1000.0
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now)
    asyncio.run(coordinator._async_refresh_run_states_in_preview(None))
    now += interval - 0.5
    asyncio.run(coordinator._async_refresh_run_states_in_preview(None))
    assert api.client.get_function_plan_run_states.await_count == 1
    now += 0.5
    asyncio.run(coordinator._async_refresh_run_states_in_preview(None))
    assert api.client.get_function_plan_run_states.await_count == 2
