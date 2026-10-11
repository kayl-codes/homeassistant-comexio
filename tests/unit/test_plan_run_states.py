"""Function plan run states: cache update, HA's own start/stop, the poll and its guards."""

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioAuthenticationError, ComexioConnectionError, ComexioResponseError
import pytest

from custom_components.comexio import coordinator as coordinator_module, plan_watchdog, sensor as sensor_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS,
    FUNCTION_PLAN_LIST_UNREAD_THRESHOLD,
    FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_DEBUG_INTERVAL_SEC,
    FUNCTION_PLAN_RUN_STATE_PREVIEW_INTERVAL_SEC,
    FUNCTION_PLAN_WATCHDOG_BOOT_GRACE_SEC,
    PLAN_TRANSITION_STARTING,
    PLAN_TRANSITION_STOPPING,
    function_plan_ids,
    function_plan_run_state_unique_id,
    plan_watch_signal,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.plan_watchdog import ManagedPlanWatchdog, start_action_id

FUBS = {"19": {"Id": 19, "Name": "Test1", "Active": 0}, "43": {"Id": 43, "Name": "Licht", "Active": 1}}


@pytest.fixture(autouse=True)
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Dispatcher signals the coordinator sends (no hass in these tests)."""
    signals: list[str] = []
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", lambda _hass, signal: signals.append(signal))
    return signals


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
    assert function_plan_ids({"19": {}, "43": {}, "x": {}, "²": {}}) == {19, 43}
    assert function_plan_run_state_unique_id("IOSRV1", 19) == "comexio_iosrv1_fub19"


class _FakeCoordinator(SimpleNamespace):
    """Just the attributes the run-state poll reads, with the real coordinator methods."""

    plan_run_states_available = ComexioCoordinator.plan_run_states_available
    plan_run_state_available = ComexioCoordinator.plan_run_state_available
    _count_missed_plan_run_states = ComexioCoordinator._count_missed_plan_run_states
    _track_plan_list_read = ComexioCoordinator._track_plan_list_read
    _plan_run_state_poll_blocked = ComexioCoordinator._plan_run_state_poll_blocked
    _async_refresh_plan_run_states = ComexioCoordinator._async_refresh_plan_run_states
    _apply_fetched_run_states = ComexioCoordinator._apply_fetched_run_states
    _async_refresh_run_states_in_preview = ComexioCoordinator._async_refresh_run_states_in_preview
    _async_plan_run_state_tick = ComexioCoordinator._async_plan_run_state_tick
    _async_plan_run_state_fetch_failed = ComexioCoordinator._async_plan_run_state_fetch_failed
    _existing_plan_names = ComexioCoordinator._existing_plan_names
    _managed_plan_names = ComexioCoordinator._managed_plan_names
    _watched_user_plan_names = ComexioCoordinator._watched_user_plan_names
    watchdog_user_plan_candidates = ComexioCoordinator.watchdog_user_plan_candidates
    _watchdog_run_state = ComexioCoordinator._watchdog_run_state
    _unresolved_plan_identities = ComexioCoordinator._unresolved_plan_identities
    live_plan_list = ComexioCoordinator.live_plan_list
    _warn_unresolved_watch_picks = ComexioCoordinator._warn_unresolved_watch_picks
    managed_plan_start_blocked = ComexioCoordinator.managed_plan_start_blocked
    async_watch_managed_plans = ComexioCoordinator.async_watch_managed_plans
    async_initial_plan_watch = ComexioCoordinator.async_initial_plan_watch
    plan_transition = ComexioCoordinator.plan_transition
    async_plan_transition = ComexioCoordinator.async_plan_transition
    _async_watchdog_start_plan = ComexioCoordinator._async_watchdog_start_plan
    _async_watchdog_auto_start_plan = ComexioCoordinator._async_watchdog_auto_start_plan
    async_start_managed_plan = ComexioCoordinator.async_start_managed_plan
    is_watched_plan = ComexioCoordinator.is_watched_plan
    async_handle_notification_action = ComexioCoordinator.async_handle_notification_action
    note_comexio_unreachable = ComexioCoordinator.note_comexio_unreachable
    _note_comexio_reachable = ComexioCoordinator._note_comexio_reachable
    _in_plan_watch_boot_grace = ComexioCoordinator._in_plan_watch_boot_grace
    _plan_watch_held = ComexioCoordinator._plan_watch_held
    _watchdog_grace_run_state = ComexioCoordinator._watchdog_grace_run_state
    comexio_unreachable = ComexioCoordinator.comexio_unreachable
    _async_bus_load_emergency_reboot = ComexioCoordinator._async_bus_load_emergency_reboot


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
        # A poll has read the plan list: before that the watchdog judges nothing (#155).
        scraped_plan_ids=function_plan_ids(api.fub_data),
        plan_scrape_generation=0,
        _plan_watch_deferred=False,
        hass=None,
        async_update_listeners=MagicMock(),
        _watchdog_lock=asyncio.Lock(),
        _plan_transitions={},
        _unresolved_watch_picks=frozenset(),
        _comexio_unreachable=False,
        _plan_watch_grace_until=None,
        config_entry=SimpleNamespace(options={}),
        plan_watchdog=SimpleNamespace(async_check=AsyncMock(return_value=False)),
    )
    fake.__dict__.update(overrides)
    return fake


def test_every_watchdog_check_signals_the_problem_sensor(api: ComexioAPI, dispatched: list[str]) -> None:
    """last_check moves with every check, also when nothing changed and no listener update follows."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator.async_update_listeners.assert_not_called()
    assert dispatched == [plan_watch_signal("iosrv1")]


def test_a_blocked_watchdog_check_does_not_signal(api: ComexioAPI, dispatched: list[str]) -> None:
    coordinator = _coordinator(api, in_sync=True)
    assert asyncio.run(coordinator.async_watch_managed_plans()) is False
    assert dispatched == []


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


@pytest.mark.parametrize(
    "write_cache",
    [
        lambda api, fubs: api.parse_config({"Fubs": fubs}),
        lambda api, fubs: api.update_fub_cache_entry(19, fubs["19"]),
        lambda api, fubs: api._load_all_plans_verified(fubs),
    ],
    ids=["full poll", "single plan re-read", "sync plan reload"],
)
def test_older_answer_does_not_overwrite_a_plan_list_fetched_during_it(api: ComexioAPI, write_cache) -> None:
    # Regression (Sourcery, PR #126): the poll guard only checks before the fetch; a plan list a
    # full poll, sync or restore wrote while the answer was on its way was overwritten by it.
    api.function_plan_load_all_plans = AsyncMock(return_value={19: {}, 43: {}})

    async def fetch(*_args, **_kwargs) -> dict[int, bool]:
        fresh = {key: dict(fub) for key, fub in FUBS.items()}
        fresh["19"]["Active"] = 1  # Comexio started plan 19 after the run-state answer was read
        if asyncio.iscoroutine(written := write_cache(api, fresh)):
            await written
        return {19: False, 43: False}

    api.client.get_function_plan_run_states = AsyncMock(side_effect=fetch)
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert api.get_fub_active(19) is True
    assert api.get_fub_active(43) is True
    assert _available(coordinator) is True  # the fetch worked, only its answer was dropped

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: False})
    asyncio.run(coordinator._async_refresh_plan_run_states())  # nothing written meanwhile: applied
    assert api.get_fub_active(19) is False


def test_dropped_answer_counts_no_misses(api: ComexioAPI) -> None:
    # Regression (Sourcery, PR #126): an answer dropped for a plan list written during the fetch
    # still counted its omissions, so an obsolete answer could turn a plan's sensor unavailable.
    async def fetch(*_args, **_kwargs) -> dict[int, bool]:
        api.parse_config({"Fubs": {key: dict(fub) for key, fub in FUBS.items()}})
        return {19: False}  # leaves plan 43 out

    api.client.get_function_plan_run_states = AsyncMock(side_effect=fetch)
    coordinator = _coordinator(api)
    for _ in range(FUNCTION_PLAN_RUN_STATE_FAIL_STREAK_THRESHOLD):
        asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.plan_run_state_available(43) is True
    coordinator.async_update_listeners.assert_not_called()


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
    coordinator = _coordinator(
        api, scraped_plan_ids=None, _plan_run_state_stale_count=5, _plan_run_state_relogin_refused=True
    )

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
    plan_map = {"Test1": 19, "Licht": "43", "HA - Gone": 99, "broken": "x"}
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
        CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19},
        CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["50:Rollo Logik", "19:Test1", "99:Gone"],
    }
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    managed, _, user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert managed == {19: "Test1", 50: "Rollo Logik"}
    assert set(user_plans) == {50}


def test_a_mapped_id_comexio_gave_another_plan_is_not_watched(api: ComexioAPI) -> None:
    """Review (Copilot, #132): Comexio reuses a deleted plan's ID — the watchdog must not start the newcomer."""
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Marker 1": 19, "Licht": 43}}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    assert coordinator._managed_plan_names() == {43: "Licht"}
    assert coordinator.is_watched_plan(19) is False
    # The newcomer is a user plan like any other.
    assert coordinator.watchdog_user_plan_candidates() == {19: "Test1"}


def test_a_user_pick_holds_only_while_its_id_carries_its_name(api: ComexioAPI) -> None:
    """Review (Copilot, #132): a pick whose ID Comexio gave another plan does not pass to that plan.

    A bare ID saved before picks carried the name is trusted; an unreadable pick is skipped.
    """
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["43:Alt", "19", "x:y"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    assert coordinator._watched_user_plan_names() == {19: "Test1"}
    assert coordinator.is_watched_plan(43) is False


def test_a_reused_id_picked_as_user_plan_is_watched_as_one(api: ComexioAPI) -> None:
    """The newcomer at a stale plan_map ID, once picked, follows the user-plan switch, not the HA-plan one."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True})
    options = {
        CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Marker 1": 19},
        CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19:Test1"],
    }
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    managed, _, user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert managed == {19: "Test1"}
    assert set(user_plans) == {19}


def test_a_plan_dropping_out_of_the_watchdog_is_warned_once(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """Review (#132): a renamed, deleted or reused pick leaves the watchdog — said once, not with every poll."""
    options = {
        CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19, "HA - Gone": 99},
        CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["43:Alt", "43", "x"],
    }
    # A poll has read the full plan list — only then does a missing pick count as gone.
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options), scraped_plan_ids={19, 43})
    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator.async_watch_managed_plans())
        asyncio.run(coordinator.async_watch_managed_plans())
    warnings = [r.getMessage() for r in caplog.records if "no longer watches" in r.getMessage()]
    assert len(warnings) == 2
    assert "'Alt' (ID 43)" in warnings[0]
    assert "'HA - Gone' (ID 99)" in warnings[1]
    # Resolved again (picked anew), then lost again: warned anew.
    options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] = ["43:Licht"]
    asyncio.run(coordinator.async_watch_managed_plans())
    options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] = ["43:Alt"]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator.async_watch_managed_plans())
    assert "'Alt' (ID 43)" in caplog.text


def test_the_watchdog_waits_for_the_first_full_plan_list(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture, dispatched: list[str]
) -> None:
    """#155: before a poll read the plan list, the cache holds only plans HA created or looked up.

    A failed plan fetch must not read as every watched plan gone either (no "no longer watches").

    Judging by it would drop the picks missing from it without a word and clear their repairs:
    the watchdog judges nothing until the list is read, and says so once.
    """
    api._fub_data.pop("43")  # the picked user plan is missing from the partial cache
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}, CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["43:Licht"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options), scraped_plan_ids=None)

    with caplog.at_level(logging.INFO):
        assert asyncio.run(coordinator.async_watch_managed_plans()) is False
        assert asyncio.run(coordinator.async_watch_managed_plans()) is False

    coordinator.plan_watchdog.async_check.assert_not_called()
    assert dispatched == []
    assert caplog.text.count("waits for the first poll that reads the plan list") == 1
    assert "no longer watches" not in caplog.text

    # The first poll that reads the plan list ends the wait: the pick is judged with the managed plan.
    api._fub_data["43"] = dict(FUBS["43"])
    coordinator.scraped_plan_ids = {19, 43}
    asyncio.run(coordinator.async_watch_managed_plans())
    watched, _, user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert set(watched) == {19, 43}
    assert set(user_plans) == {43}


def test_a_push_for_a_reused_id_starts_nothing(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """Review (Copilot, #132): a push from the deleted plan must not start the plan that got its ID."""
    hass = MagicMock()
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Marker 1": 19}}
    coordinator = _coordinator(
        api,
        hass=hass,
        config_entry=SimpleNamespace(options=options),
        plan_watchdog=SimpleNamespace(async_dismiss_push=MagicMock(return_value="dismiss")),
        _async_start_plan_from_push=MagicMock(return_value="start"),
    )
    with caplog.at_level(logging.WARNING):
        _push_action(coordinator, 19)
    assert "no longer watched" in caplog.text
    coordinator._async_start_plan_from_push.assert_not_called()
    coordinator.plan_watchdog.async_dismiss_push.assert_called_once_with(19)


def test_user_plan_candidates_leave_out_the_ha_plans(api: ComexioAPI) -> None:
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}
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


def test_setup_judges_the_watched_plans_without_waiting_for_a_poll(api: ComexioAPI) -> None:
    """The problem sensor gets its first verdict from the run states the first refresh read."""
    coordinator = _coordinator(
        api,
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}),
        plan_watchdog=SimpleNamespace(async_check=AsyncMock(return_value=True)),
    )
    asyncio.run(coordinator.async_initial_plan_watch())
    managed, _, _ = coordinator.plan_watchdog.async_check.call_args.args
    assert managed == {19: "Test1"}
    api.client.get_function_plan_run_states.assert_not_called()
    coordinator.async_update_listeners.assert_called_once()


def test_a_started_plan_is_cleared_before_the_verification_fetch(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review (Copilot, #132): a plan stopped again at once must keep the verdict that fetch gives it."""
    order: list[str] = []
    monkeypatch.setattr(api, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "function_plan_run_fup_outcome", AsyncMock(return_value=True))
    coordinator = _coordinator(
        api,
        plan_watchdog=SimpleNamespace(
            async_plan_started=AsyncMock(side_effect=lambda *_a, **_k: order.append("clear"))
        ),
        _async_refresh_plan_run_states=AsyncMock(side_effect=lambda: order.append("verify")),
    )
    assert asyncio.run(coordinator.async_start_managed_plan(19)) is True
    assert order == ["clear", "verify"]


def _push_action(coordinator: _FakeCoordinator, fub_id: int) -> None:
    coordinator.async_handle_notification_action(SimpleNamespace(data={"action": start_action_id("iosrv1", fub_id)}))


def test_push_action_starts_only_a_watched_plan(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """Review (Copilot, #132): a push from before a plan was unselected or deleted must not start it."""
    hass = MagicMock()
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}
    coordinator = _coordinator(
        api,
        hass=hass,
        config_entry=SimpleNamespace(options=options),
        plan_watchdog=SimpleNamespace(async_dismiss_push=MagicMock(return_value="dismiss")),
        _async_start_plan_from_push=MagicMock(return_value="start"),
    )
    with caplog.at_level(logging.WARNING):
        _push_action(coordinator, 43)
    assert "no longer watched" in caplog.text
    # The stale push is taken off the phone instead of answering every tap with silence.
    coordinator.plan_watchdog.async_dismiss_push.assert_called_once_with(43)
    coordinator._async_start_plan_from_push.assert_not_called()
    _push_action(coordinator, 19)
    coordinator._async_start_plan_from_push.assert_called_once_with(19)
    assert [c.args[0] for c in hass.async_create_background_task.call_args_list] == ["dismiss", "start"]


def test_setup_judgement_waits_while_a_sync_runs(api: ComexioAPI) -> None:
    coordinator = _coordinator(api)

    async def setup_during_sync() -> None:
        async with coordinator._watchdog_lock:
            await coordinator.async_initial_plan_watch()

    asyncio.run(setup_during_sync())
    coordinator.plan_watchdog.async_check.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()


def _watched_coordinator(api: ComexioAPI, **overrides) -> _FakeCoordinator:
    return _coordinator(
        api, config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}), **overrides
    )


def _check_args(coordinator: _FakeCoordinator) -> tuple[Any, bool]:
    """run_state and auto_start of the last watchdog check."""
    call = coordinator.plan_watchdog.async_check.call_args
    return call.args[1], call.kwargs["auto_start"]


def test_after_an_outage_the_watchdog_only_clears_plans_for_a_while(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """A rebooting Comexio starts its plans itself: in the boot grace none is judged stopped or started."""
    caplog.set_level(logging.INFO)
    coordinator = _watched_coordinator(api)
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioConnectionError("down"))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator._in_plan_watch_boot_grace() is False  # no grace while still unreachable

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    asyncio.run(coordinator._async_refresh_plan_run_states())
    run_state, auto_start = _check_args(coordinator)
    assert auto_start is False
    assert (run_state(19), run_state(43)) == (None, True)  # stopped keeps the last verdict, running clears it
    assert coordinator._in_plan_watch_boot_grace() is True
    assert caplog.text.count("answers again after an outage") == 1

    coordinator._plan_watch_grace_until = time.monotonic() - 1  # grace over
    asyncio.run(coordinator._async_refresh_plan_run_states())
    run_state, auto_start = _check_args(coordinator)
    assert auto_start is True
    assert run_state(19) is False
    assert coordinator._plan_watch_grace_until is None
    assert "reports and starts stopped plans again" in caplog.text


def test_an_outage_during_the_grace_starts_it_again(api: ComexioAPI) -> None:
    coordinator = _watched_coordinator(api, _plan_watch_grace_until=time.monotonic() + 5)
    api.client.get_function_plan_run_states = AsyncMock(side_effect=TimeoutError())
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.comexio_unreachable is True

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    read_started = time.monotonic()
    asyncio.run(coordinator._async_refresh_plan_run_states())
    read_done = time.monotonic()
    # The full grace again, counted from the read that answered.
    grace_until = coordinator._plan_watch_grace_until
    assert read_started + FUNCTION_PLAN_WATCHDOG_BOOT_GRACE_SEC <= grace_until
    assert grace_until <= read_done + FUNCTION_PLAN_WATCHDOG_BOOT_GRACE_SEC


@pytest.mark.parametrize(("login_error", "outage"), [("connection", True), ("rejected", False)])
def test_a_run_state_relogin_without_reply_counts_as_an_outage(api: ComexioAPI, login_error: str, outage: bool) -> None:
    """The fetch's lapsed session is no outage, but a re-login that gets no reply is."""
    api.client.get_function_plan_run_states = AsyncMock(side_effect=ComexioAuthenticationError("login form"))

    async def fail() -> bool:
        api.last_login_error = login_error
        return False

    api.login = AsyncMock(side_effect=fail)
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    api.login.assert_awaited_once()
    assert coordinator.comexio_unreachable is outage
    assert coordinator._plan_run_state_relogin_refused is not outage


def test_while_comexio_is_unreachable_the_watchdog_only_clears_plans(api: ComexioAPI) -> None:
    """No grace deadline runs yet, but a check from setup or the switch must not judge stale states."""
    coordinator = _watched_coordinator(api, _comexio_unreachable=True)
    asyncio.run(coordinator.async_initial_plan_watch())
    run_state, auto_start = _check_args(coordinator)
    assert auto_start is False
    assert run_state == coordinator._watchdog_grace_run_state  # bound methods: equal, never identical
    assert coordinator._plan_watch_grace_until is None  # the grace starts with the next read, not here


@pytest.mark.parametrize(("unreachable", "grace_left"), [(True, None), (False, 5.0)], ids=["unreachable", "boot grace"])
def test_a_queued_auto_start_is_not_sent_in_an_outage(
    api: ComexioAPI, unreachable: bool, grace_left: float | None
) -> None:
    """An outage noted while an earlier start awaited Comexio stops the later starts of the same check."""
    coordinator = _coordinator(
        api,
        _comexio_unreachable=unreachable,
        _plan_watch_grace_until=None if grace_left is None else time.monotonic() + grace_left,
    )
    api.function_plan_run_fup_outcome = AsyncMock(return_value=True)
    assert asyncio.run(coordinator._async_watchdog_auto_start_plan(19)) is None
    api.function_plan_run_fup_outcome.assert_not_awaited()

    # A start from the repair or push is the user's own call and still goes out.
    api.login = AsyncMock(return_value=True)
    coordinator.plan_watchdog = SimpleNamespace(async_plan_started=AsyncMock())
    coordinator._async_refresh_plan_run_states = AsyncMock()
    assert asyncio.run(coordinator.async_start_managed_plan(19)) is True
    api.function_plan_run_fup_outcome.assert_awaited_once_with(19)


def test_an_outage_during_an_auto_start_defers_the_queued_ones(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real watchdog check: once the first start notes an outage, the later plans are not started."""
    monkeypatch.setattr(plan_watchdog, "ir", MagicMock())
    monkeypatch.setattr(plan_watchdog, "persistent_notification", MagicMock())
    coordinator = _coordinator(api)
    sent: list[int] = []

    async def no_reply(fub_id: int) -> None:
        sent.append(fub_id)
        coordinator.note_comexio_unreachable()

    api.function_plan_run_fup_outcome = no_reply
    hass = MagicMock()
    hass.config.language = "en"
    watchdog = ManagedPlanWatchdog(
        hass,
        entry_id="e1",
        server_id="iosrv1",
        start_plan=coordinator._async_watchdog_auto_start_plan,
        notify_targets=list,
    )
    asyncio.run(watchdog.async_check({19: "Test1", 43: "Test2"}, lambda _fub_id: False))
    assert sent == [19]


@pytest.mark.parametrize("accepted", [True, False])
def test_an_accepted_emergency_reboot_counts_as_an_outage(api: ComexioAPI, accepted: bool) -> None:
    coordinator = _coordinator(
        api,
        _notify_watchdog=MagicMock(),
        _async_persist_watchdog_event=AsyncMock(),
        _watchdog_cooldown_until=None,
    )
    api.system_emergency_reboot = AsyncMock(return_value=accepted)
    asyncio.run(coordinator._async_bus_load_emergency_reboot())
    assert coordinator.comexio_unreachable is accepted


@pytest.mark.parametrize(
    "error",
    [ComexioResponseError("HTTP 500", status=500), ComexioAuthenticationError("lapsed"), None],
    ids=["http error", "lapsed session", "answer without states"],
)
def test_an_answering_comexio_starts_no_boot_grace(api: ComexioAPI, error: Exception | None) -> None:
    """Only an unreachable Comexio may have rebooted; an answer with an error or odd data says nothing."""
    coordinator = _watched_coordinator(api)
    api.client.get_function_plan_run_states = AsyncMock(side_effect=error, return_value={})
    api.login = AsyncMock(return_value=True)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator.comexio_unreachable is False
    assert coordinator._plan_run_state_fail_streak == 1  # the fetch did fail

    api.client.get_function_plan_run_states = AsyncMock(return_value={19: False, 43: True})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator._in_plan_watch_boot_grace() is False
    assert _check_args(coordinator)[1] is True


@pytest.mark.parametrize(
    ("error", "next_scraped", "grace"),
    [
        (TimeoutError(), True, True),
        (ComexioConnectionError("down"), True, True),
        (TimeoutError(), False, False),
        (ValueError("bug"), True, False),
    ],
    ids=["timeout, then scraped", "connection error, then scraped", "timeout, then unscraped", "other error"],
)
def test_full_poll_outage_starts_the_boot_grace_with_the_next_scraped_poll(
    api: ComexioAPI, error: Exception, next_scraped: bool, grace: bool
) -> None:
    coordinator = _coordinator(api)

    async def failing() -> dict:
        coordinator._last_poll_scraped = False
        coordinator._polled_plan_ids = None
        raise error

    async def answering() -> dict:
        coordinator._last_poll_scraped = next_scraped
        coordinator._polled_plan_ids = None
        return {}

    coordinator._async_fetch_and_audit = failing
    poll = ComexioCoordinator._async_update_data(coordinator)
    with pytest.raises(type(error)):
        asyncio.run(poll)
    assert coordinator._plan_watch_grace_until is None

    coordinator._async_fetch_and_audit = answering
    asyncio.run(ComexioCoordinator._async_update_data(coordinator))
    assert (coordinator._plan_watch_grace_until is not None) is grace
    assert coordinator._in_plan_watch_boot_grace() is grace


class _StopAfterFetch(Exception):
    """Ends _async_fetch_parsed_config right after the config fetch."""


@pytest.mark.parametrize("relogin_unreachable", [True, False])
def test_a_poll_whose_relogin_got_no_reply_counts_as_an_outage(api: ComexioAPI, relogin_unreachable: bool) -> None:
    """get_raw_config answers {} when the re-login of a lapsed session (e.g. after a reboot) gets no reply.

    A last_login_error left over from an earlier login says nothing about this poll.
    """
    coordinator = _coordinator(api, marker_states={}, knx_states={})

    async def fetch() -> dict:
        api.config_relogin_unreachable = relogin_unreachable
        return {}

    api.get_raw_config = fetch
    api.last_login_error = "connection"
    api.get_live_states = AsyncMock(side_effect=_StopAfterFetch)
    fetch_parsed = ComexioCoordinator._async_fetch_parsed_config(coordinator, {})
    with pytest.raises(_StopAfterFetch):
        asyncio.run(fetch_parsed)
    assert coordinator.comexio_unreachable is relogin_unreachable
