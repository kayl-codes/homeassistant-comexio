"""Function plan run states: cache update, HA's own start/stop, the poll and its guards."""

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioAuthenticationError, ComexioConnectionError
import pytest

from custom_components.comexio import coordinator as coordinator_module, sensor as sensor_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    FUNCTION_PLAN_LIST_UNREAD_THRESHOLD,
    FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_DEBUG_INTERVAL_SEC,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_INTERVAL_SEC,
    PLAN_TRANSITION_STARTING,
    PLAN_TRANSITION_STOPPING,
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


def test_config_fetched_before_ha_started_a_plan_keeps_that_state(api: ComexioAPI) -> None:
    api.set_fub_active(43, False)  # before the fetch: the config is newer and wins
    mark = api.run_state_mark()
    stale = {key: dict(fub) for key, fub in FUBS.items()}  # fetched before the start below
    api.set_fub_active(19, True)

    api.parse_config({"Fubs": stale}, run_state_mark=mark)

    assert api.get_fub_active(19) is True
    assert api.get_fub_active(43) is True

    api.parse_config({"Fubs": {key: dict(fub) for key, fub in FUBS.items()}})

    assert api.get_fub_active(19) is False  # without a mark the config is taken as it is


def _fetch_while_ha_starts(api: ComexioAPI, start_during_fetch: bool) -> dict:
    """get_raw_config() answering a config from before HA started plan 19 (mid-fetch if asked)."""

    async def fetch() -> SimpleNamespace:
        if start_during_fetch:
            api.set_fub_active(19, True)
        stale = {key: dict(fub) for key, fub in FUBS.items()}  # a new $Fubs per fetch, like the real one
        return SimpleNamespace(variables={"Fubs": stale}, io_types={}, io_input_types={}, comexio_version=None)

    api.client.get_raw_config = fetch
    if not start_during_fetch:
        api.set_fub_active(19, True)  # before the fetch: the config is newer and wins
    return asyncio.run(api.get_raw_config())


def _load_all_plans(api: ComexioAPI, conf: dict) -> None:
    api.function_plan_load_all_plans = AsyncMock(return_value={19: {}, 43: {}})
    asyncio.run(api._load_all_plans_verified(conf["Fubs"]))


@pytest.mark.parametrize(
    "write_cache",
    [
        lambda api, conf: api.parse_config(conf),
        lambda api, conf: api.update_fub_cache_entry(19, conf["Fubs"]["19"]),
        _load_all_plans,
    ],
    ids=["parse_config", "update_fub_cache_entry", "load_all_plans_verified"],
)
@pytest.mark.parametrize(("start_during_fetch", "running"), [(True, True), (False, False)])
def test_every_cache_write_keeps_a_start_made_during_its_fetch(
    api: ComexioAPI, write_cache, start_during_fetch: bool, running: bool
) -> None:
    # Regression: only the coordinator poll passed a run_state_mark; the other reloads wrote the
    # older Active flag back over a plan HA had started while they were fetching.
    conf = _fetch_while_ha_starts(api, start_during_fetch)

    write_cache(api, conf)

    assert api.get_fub_active(19) is running


def test_out_of_band_lookup_after_a_poll_takes_the_fresh_state(api: ComexioAPI) -> None:
    # Regression: with the last poll's $Fubs as the cache, update_fub_cache_entry matched that
    # older fetch's mark and put back a start HA made before the fresh fetch, which says stopped.
    api.parse_config(_fetch_while_ha_starts(api, start_during_fetch=False))  # the poll: cache = its $Fubs
    api.set_fub_active(19, True)  # HA starts the plan, Comexio does not run it
    fresh = asyncio.run(api.get_raw_config())  # the button's re-read: Active=0

    api.update_fub_cache_entry(19, fresh["Fubs"]["19"])

    assert api.get_fub_active(19) is False


def test_plan_ids_and_unique_id() -> None:
    assert function_plan_ids({"19": {}, "43": {}, "x": {}}) == {19, 43}
    assert function_plan_run_state_unique_id("IOSRV1", 19) == "comexio_iosrv1_fub19"


class _FakeCoordinator(SimpleNamespace):
    """Just the attributes the run-state poll reads, with the real coordinator methods."""

    plan_run_states_available = ComexioCoordinator.plan_run_states_available
    plan_run_state_available = ComexioCoordinator.plan_run_state_available
    _count_missed_plan_run_states = ComexioCoordinator._count_missed_plan_run_states
    _track_plan_list_read = ComexioCoordinator._track_plan_list_read
    _plan_run_state_poll_blocked = ComexioCoordinator._plan_run_state_poll_blocked
    _async_refresh_plan_run_states = ComexioCoordinator._async_refresh_plan_run_states
    _async_refresh_run_states_in_preview = ComexioCoordinator._async_refresh_run_states_in_preview
    _async_plan_run_state_tick = ComexioCoordinator._async_plan_run_state_tick
    _async_plan_run_state_fetch_failed = ComexioCoordinator._async_plan_run_state_fetch_failed
    _existing_plan_names = ComexioCoordinator._existing_plan_names
    _managed_plan_names = ComexioCoordinator._managed_plan_names
    _watched_user_plan_names = ComexioCoordinator._watched_user_plan_names
    watchdog_user_plan_candidates = ComexioCoordinator.watchdog_user_plan_candidates
    _watchdog_run_state = ComexioCoordinator._watchdog_run_state
    managed_plan_start_blocked = ComexioCoordinator.managed_plan_start_blocked
    async_watch_managed_plans = ComexioCoordinator.async_watch_managed_plans


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
        _plan_run_state_missed={},
        _plan_list_unread_polls=0,
        _plan_run_state_last_fetch=0.0,
        _plan_run_state_fetching=False,
        _plan_run_state_relogin_refused=False,
        scraped_plan_ids=None,
        plan_scrape_generation=0,
        async_update_listeners=MagicMock(),
        _watchdog_lock=asyncio.Lock(),
        config_entry=SimpleNamespace(options={}),
        plan_watchdog=SimpleNamespace(async_check=AsyncMock(return_value=False)),
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


def test_poll_does_not_undo_a_start_made_during_the_fetch(api: ComexioAPI) -> None:
    async def fetch(*_args, **_kwargs) -> dict[int, bool]:
        api.set_fub_active(19, True)  # HA started the plan while the answer was on its way
        return {19: False, 43: False}

    api.client.get_function_plan_run_states = AsyncMock(side_effect=fetch)
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert api.get_fub_active(19) is True
    assert api.get_fub_active(43) is False
    coordinator.async_update_listeners.assert_called_once_with()


def test_slow_fetch_is_not_overlapped_by_the_next_tick(api: ComexioAPI) -> None:
    """A preview tick while the previous answer is still on its way must not start a second fetch."""
    release = asyncio.Event()

    async def slow_fetch(*_args, **_kwargs) -> dict[int, bool]:
        await release.wait()
        return {19: True, 43: True}

    api.client.get_function_plan_run_states = AsyncMock(side_effect=slow_fetch)
    coordinator = _coordinator(api)

    async def run() -> None:
        first = asyncio.create_task(coordinator._async_refresh_plan_run_states())
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator._async_refresh_plan_run_states())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        await coordinator._async_refresh_plan_run_states()

    asyncio.run(run())
    assert api.client.get_function_plan_run_states.await_count == 2
    assert api.get_fub_active(19) is True


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


def test_polls_without_a_plan_list_turn_the_sensors_unavailable(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    coordinator = _coordinator(api)

    def poll(scraped: bool, polled: set[int] | None) -> None:
        async def fetch() -> dict:
            coordinator._last_poll_scraped = scraped
            coordinator._polled_plan_ids = polled
            return {}

        coordinator._async_fetch_and_audit = fetch
        asyncio.run(ComexioCoordinator._async_update_data(coordinator))

    poll(scraped=False, polled=None)  # skipped poll or failed page fetch: nothing read, nothing missed
    assert coordinator._plan_list_unread_polls == 0
    for count in range(1, FUNCTION_PLAN_LIST_UNREAD_THRESHOLD + 1):
        assert _available(coordinator)
        poll(scraped=True, polled=None)
        assert coordinator._plan_list_unread_polls == count
    assert not _available(coordinator)
    assert caplog.text.count("no readable plan list") == 1
    # A working run-state fetch meanwhile changes nothing, so it refreshes no entity either.
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator.async_update_listeners.assert_not_called()

    poll(scraped=True, polled={19, 43})

    assert _available(coordinator)
    assert "Plan list ($Fubs) readable again" in caplog.text


class _FakeRegistry:
    def __init__(self, entity_ids: dict[str, str]) -> None:
        self.entity_ids = entity_ids
        self.removed: list[str] = []

    def async_get_entity_id(self, domain: str, _platform: str, unique_id: str) -> str | None:
        assert domain == "sensor"
        return self.entity_ids.get(unique_id)

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


def _plan_sensor_sync(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, **coordinator_overrides):
    registry = _FakeRegistry(
        {function_plan_run_state_unique_id("iosrv1", fid): f"sensor.iosrv1_fub{fid}" for fid in (19, 43)}
    )
    monkeypatch.setattr(sensor_module.er, "async_get", lambda _hass: registry)
    coordinator = _coordinator(api, **coordinator_overrides)
    added: list = []
    sync = sensor_module._PlanRunStateSensorSync(None, coordinator, lambda ents: added.extend(ents))
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
    assert registry.removed == ["sensor.iosrv1_fub19"]


def test_plan_created_by_ha_after_the_scrape_keeps_its_sensor(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """create_fup adds the plan to fub_data between scrapes; the old plan list must not prune it."""
    sync, coordinator, registry, added = _plan_sensor_sync(
        api, monkeypatch, scraped_plan_ids={19, 43}, plan_scrape_generation=1
    )
    registry.entity_ids[function_plan_run_state_unique_id("iosrv1", 77)] = "sensor.iosrv1_fub77"
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


@pytest.mark.parametrize(
    ("fub_id", "transition", "expected"),
    [
        (43, None, "running"),
        (19, None, "stopped"),
        (19, PLAN_TRANSITION_STARTING, "starting"),
        (43, PLAN_TRANSITION_STOPPING, "stopping"),
        (99, None, None),
    ],
    ids=["running", "stopped", "starting", "stopping", "unknown-plan"],
)
def test_run_state_sensor_value(api: ComexioAPI, fub_id: int, transition: str | None, expected: str | None) -> None:
    coordinator = _coordinator(api)
    coordinator.plan_transition = {fub_id: transition}.get
    sensor = sensor_module.ComexioFunctionPlanRunStateSensor(coordinator, "iosrv1", fub_id)
    assert sensor.native_value == expected
    assert expected is None or expected in sensor.options


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


def test_failed_connection_value_poll_keeps_the_run_states_coming(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The armed preview pauses the run-state timer, so its poll must fetch the states even when
    # its own request fails — on the admin session, the preview session may be what failed.
    monkeypatch.setattr(api, "ensure_preview_session", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        api, "get_function_plan_connection_values", AsyncMock(side_effect=ComexioConnectionError("down"))
    )
    coordinator = _coordinator(api, _preview_plan_cache={"fub_id": 19}, _connection_poll_fail_count=0)
    coordinator._async_refresh_run_states_in_preview = AsyncMock()
    asyncio.run(ComexioCoordinator._async_poll_connection_values(coordinator, None))
    assert coordinator._connection_poll_fail_count == 1
    coordinator._async_refresh_run_states_in_preview.assert_awaited_once_with(None)


def test_a_plan_left_out_of_every_answer_turns_unavailable_alone(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A working fetch for the other plans must not keep a plan's unreadable state alive."""
    caplog.set_level(logging.INFO)
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False})
    coordinator = _coordinator(api)
    for _ in range(FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD - 1):
        asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.plan_run_state_available(43) is True
    coordinator.async_update_listeners.assert_not_called()

    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.plan_run_state_available(43) is False
    assert coordinator.plan_run_state_available(19) is True
    coordinator.async_update_listeners.assert_called_once_with()
    assert "function plan 43 unreadable" in caplog.text

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.plan_run_state_available(43) is True
    assert coordinator.async_update_listeners.call_count == 2  # back available, without a state change
    assert "function plan 43 readable again" in caplog.text


def test_poll_hands_the_managed_plans_to_the_watchdog(api: ComexioAPI) -> None:
    """Only plan_map plans Comexio still has are judged; HA's own recent stop reads as unknown."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    plan_map = {"HA - TRIGGER": 19, "HA - Marker 1": "43", "HA - Gone": 99, "broken": "x"}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: plan_map}))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    managed, run_state, user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert managed == {19: "Test1", 43: "Licht"}
    assert set(user_plans) == set()
    assert (run_state(19), run_state(43)) == (False, True)
    api.set_fub_active(43, False)  # HA itself stopped it, e.g. for a sort
    assert run_state(43) is None


def test_poll_adds_the_watched_user_plans(api: ComexioAPI) -> None:
    """Picked user plans are judged too; a pick that is HA-managed or gone from Comexio is left out."""
    api._fub_data["50"] = {"Id": 50, "Name": "Rollo Logik", "Active": 0}
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True, 50: False})
    options = {
        CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - TRIGGER": 19},
        CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["50", "19", "99"],
    }
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    managed, _, user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert managed == {19: "Test1", 50: "Rollo Logik"}
    assert set(user_plans) == {50}


def test_user_plan_candidates_leave_out_the_ha_plans(api: ComexioAPI) -> None:
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - TRIGGER": 19}}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    assert coordinator.watchdog_user_plan_candidates() == {43: "Licht"}


def test_watchdog_waits_while_a_cascade_or_sync_runs(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    coordinator = _coordinator(api)

    async def poll_during_cascade() -> None:
        async with coordinator._watchdog_lock:
            await coordinator._async_refresh_plan_run_states()

    asyncio.run(poll_during_cascade())
    coordinator.plan_watchdog.async_check.assert_not_called()
