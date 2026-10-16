"""Function plan run states: cache update, HA's own start/stop, the poll and its guards."""

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    FUNCTION_PLAN_WATCHDOG_EXISTENCE_CHECK_MAX_SEC,
    PLAN_TRANSITION_STARTING,
    PLAN_TRANSITION_STOPPING,
    function_plan_ids,
    function_plan_run_state_unique_id,
    plan_watch_signal,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.plan_watchdog import (
    ACTION_RESTORE,
    ACTION_UNWATCH,
    ManagedPlanWatchdog,
    plan_action_id,
    start_action_id,
    vanished_plan_token,
)
from custom_components.comexio.services import _context

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
    _plan_watch_blocked = ComexioCoordinator._plan_watch_blocked
    _async_start_plan_from_push = ComexioCoordinator._async_start_plan_from_push
    plan_edit = ComexioCoordinator.plan_edit
    plan_edits_running = ComexioCoordinator.plan_edits_running
    async_watch_managed_plans = ComexioCoordinator.async_watch_managed_plans
    _async_watch_plans_and_refresh = ComexioCoordinator._async_watch_plans_and_refresh
    _watchdog_run_state_for = ComexioCoordinator._watchdog_run_state_for
    _async_watch_managed_plans = ComexioCoordinator._async_watch_managed_plans
    _async_vanished_user_plans_named = ComexioCoordinator._async_vanished_user_plans_named
    _notify_bare_pick_kept = ComexioCoordinator._notify_bare_pick_kept
    async_initial_plan_watch = ComexioCoordinator.async_initial_plan_watch
    plan_transition = ComexioCoordinator.plan_transition
    async_plan_transition = ComexioCoordinator.async_plan_transition
    _async_watchdog_start_plan = ComexioCoordinator._async_watchdog_start_plan
    _async_watchdog_auto_start_plan = ComexioCoordinator._async_watchdog_auto_start_plan
    async_start_managed_plan = ComexioCoordinator.async_start_managed_plan
    watched_plan_name = ComexioCoordinator.watched_plan_name
    is_watched_plan = ComexioCoordinator.is_watched_plan
    watches_plan_raised_for = ComexioCoordinator.watches_plan_raised_for
    _has_bare_pick = ComexioCoordinator._has_bare_pick
    async_handle_notification_action = ComexioCoordinator.async_handle_notification_action
    _dismiss_stale_push = ComexioCoordinator._dismiss_stale_push
    note_comexio_unreachable = ComexioCoordinator.note_comexio_unreachable
    _note_comexio_reachable = ComexioCoordinator._note_comexio_reachable
    _in_plan_watch_boot_grace = ComexioCoordinator._in_plan_watch_boot_grace
    _plan_watch_held = ComexioCoordinator._plan_watch_held
    _watchdog_grace_run_state = ComexioCoordinator._watchdog_grace_run_state
    _watchdog_verified_run_state = ComexioCoordinator._watchdog_verified_run_state
    _vanished_user_plans = ComexioCoordinator._vanished_user_plans
    _handle_vanished_plan_action = ComexioCoordinator._handle_vanished_plan_action
    async_restore_vanished_plan = ComexioCoordinator.async_restore_vanished_plan
    async_newest_backup_field = ComexioCoordinator.async_newest_backup_field
    async_unwatch_user_plan = ComexioCoordinator.async_unwatch_user_plan
    _repoint_vanished_pick = ComexioCoordinator._repoint_vanished_pick
    _async_restore_vanished_from_backup = ComexioCoordinator._async_restore_vanished_from_backup
    _async_restore_vanished_when_free = ComexioCoordinator._async_restore_vanished_when_free
    _async_wait_for_restore_slot = ComexioCoordinator._async_wait_for_restore_slot
    _vanished_pick_present = ComexioCoordinator._vanished_pick_present
    _notify_vanished_restore_not_run = ComexioCoordinator._notify_vanished_restore_not_run
    note_plan_restored_as_new = ComexioCoordinator.note_plan_restored_as_new
    note_vanished_plan_back = ComexioCoordinator.note_vanished_plan_back
    _backed_up_plan_name = ComexioCoordinator._backed_up_plan_name
    _async_report_vanished_restored = ComexioCoordinator._async_report_vanished_restored
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
        _plan_stop_unverified={},
        unloading=False,
        _vanished_restores=set(),
        _vanished_restore_lock=asyncio.Lock(),
        _plan_watch_check_lock=asyncio.Lock(),
        _plan_watch_backups_unloadable=False,
        _restored_as_new={},
        _vanished_plans_back=set(),
        function_plan_backup=SimpleNamespace(
            async_load=AsyncMock(), plan_names_for_fub_id_sync=MagicMock(return_value=set())
        ),
        request_options_update_without_reload=MagicMock(),
        # Closes the scheduled coroutine (the existence check's full poll) instead of running it.
        hass=SimpleNamespace(async_create_background_task=MagicMock(side_effect=lambda coro, **_kw: coro.close())),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        _watchdog_lock=asyncio.Lock(),
        _plan_edits_running=0,
        _plan_edits_done=asyncio.Event(),
        _plan_transitions={},
        _unresolved_watch_picks=frozenset(),
        _comexio_unreachable=False,
        _plan_watch_grace_until=None,
        config_entry=SimpleNamespace(options={}),
        plan_watchdog=SimpleNamespace(
            async_check=AsyncMock(return_value=False),
            async_sync_vanished=AsyncMock(),
            stopped=None,
            vanished=None,
            async_dismiss_push=AsyncMock(),
            async_push_no_backup=AsyncMock(),
            async_push_restored=AsyncMock(),
            async_push_restore_failed=AsyncMock(),
            async_push_check_pending=AsyncMock(),
            async_dismiss_vanished_push=AsyncMock(),
        ),
    )
    fake.__dict__.update(overrides)
    watchdog = fake.plan_watchdog
    if not hasattr(watchdog, "vanished_name"):
        watchdog.vanished_name = lambda token: ManagedPlanWatchdog.vanished_name(watchdog, token)
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


def test_a_failed_fetch_still_syncs_the_vanished_plans(api: ComexioAPI) -> None:
    """(Review, #181) a failing run-state endpoint left a deleted watched plan without its repair and push."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={})  # format changed: no plan classifiable
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator.plan_watchdog.async_sync_vanished.assert_awaited_once()
    assert coordinator._plan_run_state_fail_streak == 1
    assert coordinator._plan_run_state_fetching is False
    # (Review) No verdict and no start on the last known states: the clock-based steps would take them as new.
    _managed, run_state, _user_plans = coordinator.plan_watchdog.async_check.call_args.args
    assert api.get_fub_active(43) is True  # the cache still holds a state …
    assert run_state(43) is None  # … the watchdog does not judge it
    assert coordinator.plan_watchdog.async_check.call_args.kwargs["auto_start"] is False


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
    # (cl) A plan newly read as stopped waits for a plan list read after it: deleted plans read alike.
    assert (run_state(19), run_state(43)) == (None, True)
    coordinator.async_request_refresh.assert_called_once()
    coordinator.plan_scrape_generation += 1
    assert run_state(19) is False
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
        plan_watchdog=SimpleNamespace(async_dismiss_push=MagicMock(return_value="dismiss"), stopped={}),
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


def test_no_plan_is_started_while_a_plan_edit_runs(api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """(Review) an auto-start, repair or push start could run a plan a sort, connect or stop is changing."""
    monkeypatch.setattr(api, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "function_plan_run_fup_outcome", AsyncMock(return_value=True))
    coordinator = _coordinator(api)

    async def run() -> tuple[bool | None, bool | None]:
        with coordinator.plan_edit():
            return await coordinator._async_watchdog_start_plan(19), await coordinator.async_start_managed_plan(19)

    assert asyncio.run(run()) == (None, None)
    api.function_plan_run_fup_outcome.assert_not_awaited()
    api.login.assert_not_awaited()


def test_the_watchdog_still_judges_while_a_plan_edit_runs(api: ComexioAPI) -> None:
    """Only the start waits for the edit: the verdict after HA's own start or stop is not delayed."""
    coordinator = _coordinator(api)

    async def run() -> None:
        with coordinator.plan_edit():
            await coordinator.async_watch_managed_plans()

    asyncio.run(run())
    coordinator.plan_watchdog.async_check.assert_awaited_once()
    # The auto-start waits for the edit's end instead of logging "deferred" on every tick.
    assert coordinator.plan_watchdog.async_check.await_args.kwargs["auto_start"] is False
    asyncio.run(coordinator.async_watch_managed_plans())
    assert coordinator.plan_watchdog.async_check.await_args.kwargs["auto_start"] is True


def test_a_plan_edit_starting_during_the_login_stops_a_repair_or_push_start(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(Review) the login awaits Comexio: an edit starting then means "not sent", not "start failed"."""
    coordinator = _coordinator(api)

    async def login() -> bool:
        coordinator._plan_edits_running = 1
        return True

    monkeypatch.setattr(api, "login", login)
    monkeypatch.setattr(api, "function_plan_run_fup_outcome", AsyncMock(return_value=True))
    assert asyncio.run(coordinator.async_start_managed_plan(19)) is None
    api.function_plan_run_fup_outcome.assert_not_awaited()


@pytest.mark.parametrize("busy", [True, False], ids=["plan-edit-runs", "free"])
def test_a_push_start_during_a_plan_edit_is_reported_as_not_sent(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    monkeypatch.setattr(api, "login", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "function_plan_run_fup_outcome", AsyncMock(return_value=False))
    monkeypatch.setattr(api, "get_fub_active", MagicMock(return_value=False))
    coordinator = _coordinator(
        api,
        plan_watchdog=SimpleNamespace(async_push_start_busy=AsyncMock(), async_push_start_failed=AsyncMock()),
    )

    async def run() -> None:
        with coordinator.plan_edit() if busy else contextlib.nullcontext():
            await coordinator._async_start_plan_from_push(19)

    asyncio.run(run())
    if busy:
        api.function_plan_run_fup_outcome.assert_not_awaited()
        coordinator.plan_watchdog.async_push_start_busy.assert_awaited_once()
        coordinator.plan_watchdog.async_push_start_failed.assert_not_awaited()
    else:  # sent and refused by Comexio: the failure push
        api.function_plan_run_fup_outcome.assert_awaited_once_with(19)
        coordinator.plan_watchdog.async_push_start_failed.assert_awaited_once()
        coordinator.plan_watchdog.async_push_start_busy.assert_not_awaited()


def test_a_plan_start_keeps_plan_edits_out_until_comexio_answers(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(Review) the start guard holds through the start request, not only at its check."""
    coordinator = _coordinator(api)
    seen: list[bool] = []

    async def start(_fub_id: int) -> bool:
        with patch.object(_context.persistent_notification, "async_create") as create:
            seen.append(_context.refuse_while_plans_busy(coordinator.hass, coordinator, "T"))
        assert create.call_args.args[1] == _context.PLAN_EDIT_RUNNING_MSG
        return True

    monkeypatch.setattr(api, "function_plan_run_fup_outcome", start)
    assert asyncio.run(coordinator._async_watchdog_start_plan(19)) is True
    assert seen == [True]
    assert not coordinator.plan_edits_running
    assert coordinator._plan_edits_done.is_set()


@pytest.mark.parametrize(
    "error", [ComexioConnectionError("down"), asyncio.CancelledError()], ids=["error", "cancelled"]
)
def test_a_failing_plan_start_releases_the_plan_edit_mark(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Otherwise every restore, sort and sync would refuse until HA restarts."""
    monkeypatch.setattr(api, "function_plan_run_fup_outcome", AsyncMock(side_effect=error))
    coordinator = _coordinator(api)
    start = coordinator._async_watchdog_start_plan(19)
    with pytest.raises(type(error)):
        asyncio.run(start)
    assert not coordinator.plan_edits_running
    assert coordinator._plan_edits_done.is_set()
    assert not coordinator._watchdog_lock.locked()


def test_setup_judges_the_watched_plans_without_waiting_for_a_poll(api: ComexioAPI) -> None:
    """The problem sensor gets its first verdict from the run states the first refresh read."""
    coordinator = _coordinator(
        api,
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}),
        plan_watchdog=SimpleNamespace(
            async_check=AsyncMock(return_value=True), async_sync_vanished=AsyncMock(), stopped=None, vanished=None
        ),
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


def _push_action(coordinator: _FakeCoordinator, fub_id: int, name: str = "Test1") -> None:
    coordinator.async_handle_notification_action(
        SimpleNamespace(data={"action": start_action_id("iosrv1", fub_id, name)})
    )


def test_push_action_starts_only_a_watched_plan(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """Review (Copilot, #132): a push from before a plan was unselected or deleted must not start it."""
    hass = MagicMock()
    options = {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}
    coordinator = _coordinator(
        api,
        hass=hass,
        config_entry=SimpleNamespace(options=options),
        plan_watchdog=SimpleNamespace(async_dismiss_push=MagicMock(return_value="dismiss"), stopped={}),
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


@pytest.mark.parametrize(
    ("options", "action", "outcome"),
    [
        ({CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}, start_action_id("iosrv1", 19, "Test1"), "start"),
        # the push was sent for plan "Old" under ID 19; Comexio gave the ID to the watched plan "Test1" since
        ({CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}, start_action_id("iosrv1", 19, "Old"), "ignore"),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19:Test1"]}, start_action_id("iosrv1", 19, "Old"), "ignore"),
        # a bare-ID pick watches whichever plan holds the ID: one renamed since the push still starts
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]}, start_action_id("iosrv1", 19, "Old"), "start"),
        # sent before the action carried the name digest: it names no plan, only a bare-ID pick starts from it
        ({CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}, "COMEXIO_START_PLAN_iosrv1_19", "legacy"),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19:Test1"]}, "COMEXIO_START_PLAN_iosrv1_19", "legacy"),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]}, "COMEXIO_START_PLAN_iosrv1_19", "start"),
        ({}, "COMEXIO_START_PLAN_iosrv1_19", "dismiss"),
        # an HA-managed plan holds its name even next to a bare pick of its ID
        (
            {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}, CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]},
            "COMEXIO_START_PLAN_iosrv1_19",
            "legacy",
        ),
        (
            {CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}, CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]},
            start_action_id("iosrv1", 19, "Old"),
            "ignore",
        ),
        ({}, start_action_id("iosrv1", 19, "Test1"), "dismiss"),
    ],
    ids=[
        "same-plan",
        "id-reused-managed",
        "id-reused-named-pick",
        "bare-pick-renamed",
        "legacy-action-managed",
        "legacy-action-named-pick",
        "legacy-action-bare-pick",
        "legacy-action-unwatched",
        "legacy-action-managed-and-bare-pick",
        "id-reused-managed-and-bare-pick",
        "unwatched",
    ],
)
def test_push_action_starts_only_the_plan_it_was_sent_for(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture, options: dict, action: str, outcome: str
) -> None:
    """(Sourcery, #184/#185) a watched ID is not enough: the push names the plan by ID and name digest."""
    hass = MagicMock()
    coordinator = _coordinator(
        api,
        hass=hass,
        config_entry=SimpleNamespace(options=options),
        plan_watchdog=SimpleNamespace(
            async_dismiss_push=MagicMock(return_value="dismiss"),
            async_push_start_outdated=MagicMock(return_value="outdated"),
            stopped={19: "Test1"},  # the ID's plan is stopped: its own alarm holds the tag
        ),
        _async_start_plan_from_push=MagicMock(return_value="start"),
    )
    with caplog.at_level(logging.WARNING):
        coordinator.async_handle_notification_action(SimpleNamespace(data={"action": action}))
    scheduled = {"start": ["start"], "dismiss": ["dismiss"], "legacy": ["outdated"], "ignore": []}[outcome]
    assert [c.args[0] for c in hass.async_create_background_task.call_args_list] == scheduled
    if outcome == "start":
        coordinator._async_start_plan_from_push.assert_called_once_with(19)
        assert "ignored" not in caplog.text
    else:
        coordinator._async_start_plan_from_push.assert_not_called()
    # The ID's current plan has its own push under the same tag: a stale one of another plan is not cleared.
    if outcome == "dismiss":
        coordinator.plan_watchdog.async_dismiss_push.assert_called_once_with(19)
    else:
        coordinator.plan_watchdog.async_dismiss_push.assert_not_called()
    if outcome == "ignore":
        assert "belongs to another watched plan now (Test1)" in caplog.text
        assert "sent before the update" not in caplog.text
    if outcome == "dismiss":
        assert "no longer watched" in caplog.text
        assert "sent before the update" not in caplog.text
    # The alarm is pushed on the stop only: the old push is replaced by one naming the ID's plan now.
    if outcome == "legacy":
        coordinator.plan_watchdog.async_push_start_outdated.assert_called_once_with(19, "Test1")
        assert "sent before the update" in caplog.text
        assert "belongs to another watched plan" not in caplog.text
    else:
        coordinator.plan_watchdog.async_push_start_outdated.assert_not_called()


@pytest.mark.parametrize("stopped", [{}, None], ids=["running", "before-the-first-check"])
@pytest.mark.parametrize(
    ("action", "logged"),
    [
        (start_action_id("iosrv1", 19, "Old"), "belongs to another watched plan now (Test1)"),
        # sent before the action carried the name digest: no "start" prompt for a plan that runs
        ("COMEXIO_START_PLAN_iosrv1_19", "Test1 runs, so it is taken off the phone"),
    ],
    ids=["id-reused", "legacy-action"],
)
def test_a_stale_push_of_an_id_whose_plan_runs_now_is_taken_off_the_phone(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture, stopped: dict | None, action: str, logged: str
) -> None:
    """(Sourcery, #185) No alarm of the ID's plan replaced it, and its clear may have failed: dismissed."""
    hass = MagicMock()
    coordinator = _coordinator(
        api,
        hass=hass,
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}),
        plan_watchdog=SimpleNamespace(
            async_dismiss_push=MagicMock(return_value="dismiss"),
            async_push_start_outdated=MagicMock(return_value="outdated"),
            stopped=stopped,
        ),
        _async_start_plan_from_push=MagicMock(return_value="start"),
    )
    with caplog.at_level(logging.WARNING):
        coordinator.async_handle_notification_action(SimpleNamespace(data={"action": action}))
    assert [c.args[0] for c in hass.async_create_background_task.call_args_list] == ["dismiss"]
    coordinator.plan_watchdog.async_dismiss_push.assert_called_once_with(19)
    coordinator.plan_watchdog.async_push_start_outdated.assert_not_called()
    coordinator._async_start_plan_from_push.assert_not_called()
    assert logged in caplog.text


@pytest.mark.parametrize(
    ("options", "name", "raised_for_old"),
    [
        ({CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}}, "Test1", False),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19:Test1"]}, "Test1", False),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]}, "Test1", True),  # bare pick: the plan holding the ID
        # an HA-managed plan holds its name even next to a bare pick of its ID
        ({CONF_FUNCTION_PLAN_PLAN_MAP: {"Test1": 19}, CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19"]}, "Test1", False),
        ({CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["19:Old"]}, None, False),  # ID now named otherwise
        ({}, None, False),
    ],
)
def test_the_watched_plan_name_and_the_plan_a_push_was_raised_for(
    api: ComexioAPI, options: dict, name: str | None, raised_for_old: bool
) -> None:
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    assert coordinator.watched_plan_name(19) == name
    assert coordinator.watches_plan_raised_for(19, lambda current: current == "Test1") is (name is not None)
    assert coordinator.watches_plan_raised_for(19, lambda current: current == "Old") is raised_for_old


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
    assert run_state(19) is None  # (cl) re-reads the plan list first
    coordinator.plan_scrape_generation += 1
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


# (cl) A watched plan deleted in Comexio reads as stopped: verified against the plan list first.


def test_a_newly_stopped_plan_is_judged_after_the_check_timeout_without_a_plan_list(api: ComexioAPI) -> None:
    """A poll that brings no plan list must not silence the watchdog."""
    api._fub_data["19"]["Active"] = 0  # read from Comexio, not stopped by HA
    coordinator = _coordinator(api)
    assert coordinator._watchdog_verified_run_state(19) is None
    assert coordinator._watchdog_verified_run_state(19) is None  # asks for one poll only
    coordinator.async_request_refresh.assert_called_once()
    generation, since = coordinator._plan_stop_unverified[19]
    coordinator._plan_stop_unverified[19] = (generation, since - FUNCTION_PLAN_WATCHDOG_EXISTENCE_CHECK_MAX_SEC + 5)
    assert coordinator._watchdog_verified_run_state(19) is None  # not yet
    coordinator._plan_stop_unverified[19] = (generation, since - FUNCTION_PLAN_WATCHDOG_EXISTENCE_CHECK_MAX_SEC)
    assert coordinator._watchdog_verified_run_state(19) is False


def test_a_plan_already_judged_stopped_or_running_needs_no_existence_check(api: ComexioAPI) -> None:
    api._fub_data["19"]["Active"] = 0  # read from Comexio, not stopped by HA
    coordinator = _coordinator(api, _plan_stop_unverified={43: (0, 0.0)})
    coordinator.plan_watchdog.stopped = {19: "Test1"}
    assert coordinator._watchdog_verified_run_state(19) is False
    assert coordinator._watchdog_verified_run_state(43) is True
    assert coordinator._plan_stop_unverified == {}  # running again: a later stop is verified anew
    coordinator.async_request_refresh.assert_not_called()


def test_user_picks_gone_from_the_plan_list_are_vanished(api: ComexioAPI) -> None:
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["99:Gone", "19:Test1", "98", "97", "96:Named", "96", "95", "x"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    names = {97: {"Old"}, 96: {"Old"}, 95: {"A", "B"}}  # (Sourcery) a bare-ID pick takes its backups' one name
    coordinator.function_plan_backup.plan_names_for_fub_id_sync.side_effect = lambda fub_id: names.get(fub_id, set())
    # 95: the ID held several plans — no guess at which one to restore
    assert coordinator._vanished_user_plans() == {
        (99, "Gone"),
        (98, "ID 98"),
        (97, "Old"),
        (96, "Named"),  # the bare "96" is the named pick's plan
        (95, "ID 95"),
    }
    coordinator.scraped_plan_ids = None  # no plan list read yet: nothing is judged deleted
    assert coordinator._vanished_user_plans() == set()


def test_two_deleted_picks_on_one_id_are_two_vanished_plans(api: ComexioAPI) -> None:
    """(Sourcery) An ID can be reused: "99:Gone" and "99:Other" were two plans, both deleted."""
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["99:Gone", "99:Other", "99"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    assert coordinator._vanished_user_plans() == {(99, "Gone"), (99, "Other")}


def test_a_user_pick_on_an_ha_managed_id_is_not_vanished(api: ComexioAPI) -> None:
    """(Sourcery) A pick overlapping a plan_map ID raises no deleted-user-plan alert for HA's own plan."""
    options = {
        CONF_FUNCTION_PLAN_PLAN_MAP: {"HA - Gone": 99, "HA - Old": 98},
        CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["99:HA - Gone", "98:Gone"],
    }
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    # 98: a stale plan_map entry of another plan on the reused ID does not hide the deleted user plan
    assert coordinator._vanished_user_plans() == {(98, "Gone")}


def test_the_poll_hands_the_vanished_user_plans_to_the_watchdog(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True})
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    coordinator.plan_watchdog.async_sync_vanished.assert_awaited_once_with({(47, "Test4")})


TOKEN = vanished_plan_token(47, "Test4")


def _task_names(coordinator: _FakeCoordinator) -> list[str]:
    return [call.kwargs["name"] for call in coordinator.hass.async_create_background_task.call_args_list]


def _task_coroutines(coordinator: _FakeCoordinator) -> list[str]:
    """Names of the coroutines handed to background tasks (the fixture closes them unrun)."""
    return [call.args[0].cr_code.co_name for call in coordinator.hass.async_create_background_task.call_args_list]


@pytest.mark.parametrize(("kind", "task"), [(ACTION_RESTORE, "restore_plan_47"), (ACTION_UNWATCH, "unwatch_plan_47")])
def test_push_actions_of_a_vanished_plan(api: ComexioAPI, kind: str, task: str) -> None:
    coordinator = _coordinator(api)
    coordinator.plan_watchdog.vanished = {(47, "Test4"), (47, "Other")}
    scheduled_with: list[dict[str, Any]] = []

    def record(coro: Any, **_kwargs: Any) -> None:
        scheduled_with.append(dict(coro.cr_frame.f_locals))  # the call's arguments; gone once closed
        coro.close()

    coordinator.hass.async_create_background_task.side_effect = record
    action = plan_action_id(kind, "iosrv1", TOKEN)
    coordinator.async_handle_notification_action(SimpleNamespace(data={"action": action}))
    assert _task_names(coordinator) == [f"comexio_iosrv1_{task}"]
    expected = "async_restore_vanished_plan" if kind == ACTION_RESTORE else "async_unwatch_user_plan"
    assert _task_coroutines(coordinator) == [expected]
    # (Sourcery) The restore reports its outcome to the phone: it must be told it came from the push.
    # (Sourcery) The action names its plan by the token: not the other deleted plan on ID 47.
    (arguments,) = scheduled_with
    assert (arguments["fub_id"], arguments["name"]) == (47, "Test4")
    if kind == ACTION_RESTORE:
        assert arguments["from_push"] is True


def test_a_vanished_plan_push_before_the_first_check_asks_to_try_again(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """(Review) Before the first check after setup nothing is known: the action is not dropped as stale."""
    coordinator = _coordinator(api)
    action = plan_action_id(ACTION_RESTORE, "iosrv1", TOKEN)
    coordinator.async_handle_notification_action(SimpleNamespace(data={"action": action}))
    assert _task_names(coordinator) == ["comexio_iosrv1_check_pending_push_47"]
    coordinator.plan_watchdog.async_push_check_pending.assert_called_once_with(47, TOKEN)  # (Sourcery) its push
    assert "the plan list was not read yet" in caplog.text


def test_a_stale_vanished_plan_push_is_dismissed(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(api)
    coordinator.plan_watchdog.vanished = {(47, "Other")}  # another deleted plan on ID 47 is not this one
    action = plan_action_id(ACTION_RESTORE, "iosrv1", TOKEN)
    coordinator.async_handle_notification_action(SimpleNamespace(data={"action": action}))
    assert _task_names(coordinator) == [f"comexio_iosrv1_dismiss_push_{TOKEN}"]
    coordinator.plan_watchdog.async_dismiss_vanished_push.assert_called_once_with(47, TOKEN)
    assert "the plan is not missing" in caplog.text


def test_a_refused_auto_start_re_reads_the_plan_list(api: ComexioAPI) -> None:
    """(Sourcery) A plan judged stopped earlier and deleted since: its next start is refused."""
    coordinator = _coordinator(api)
    coordinator._async_watchdog_start_plan = AsyncMock(return_value=False)  # type: ignore[method-assign]
    assert asyncio.run(coordinator._async_watchdog_auto_start_plan(47)) is False
    assert _task_names(coordinator) == ["comexio_iosrv1_plan_existence_check"]
    coordinator.hass.async_create_background_task.reset_mock()
    coordinator._async_watchdog_start_plan = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert asyncio.run(coordinator._async_watchdog_auto_start_plan(47)) is True
    assert _task_names(coordinator) == []


def test_the_last_plan_deleted_still_reaches_the_watchdog(api: ComexioAPI) -> None:
    """(Sourcery) No plan left means no run state to fetch, but the deleted watched one is reported."""
    api.client.get_function_plan_run_states = AsyncMock()
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    api._fub_data.clear()
    coordinator.scraped_plan_ids = set()
    asyncio.run(coordinator._async_refresh_plan_run_states())
    api.client.get_function_plan_run_states.assert_not_awaited()
    coordinator.plan_watchdog.async_sync_vanished.assert_awaited_once_with({(47, "Test4")})
    coordinator.function_plan_backup.async_load.assert_awaited()  # (Sourcery) before naming bare-ID picks


def _backups(*entries: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(async_load=AsyncMock(), plan_backups_for_identity_sync=MagicMock(return_value=list(entries)))


def test_the_restore_takes_the_newest_backup_of_any_kind(api: ComexioAPI) -> None:
    backups = _backups(
        {"kind": "change", "slot": 3, "captured_at": "2026-10-02T08:00:00+00:00"},
        {"kind": "auto", "slot": 1, "captured_at": "2026-10-02T09:00:00+00:00"},
        {"kind": "auto", "slot": 2, "captured_at": None},
    )
    coordinator = _coordinator(api, function_plan_backup=backups)
    assert asyncio.run(coordinator.async_newest_backup_field(47, "Test4")) == "47:auto:1:Test4"
    backups.plan_backups_for_identity_sync.assert_called_once_with(47, "Test4")


def _restore_coordinator(api: ComexioAPI, outcome: Any) -> _FakeCoordinator:
    """outcome: True moves the pick to the new plan like a restore-as-new, False leaves it, else raised.

    "back": the restore action refuses it, the plan is back under its ID and name.
    """
    coordinator = _coordinator(
        api,
        function_plan_backup=_backups({"kind": "change", "slot": 0, "captured_at": "2026-10-02T08:00:00+00:00"}),
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}, entry_id="entry1"),
    )

    async def restore(*_args: Any, **_kwargs: Any) -> None:
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "back":  # the restore action found the plan back under its ID and name
            coordinator.note_vanished_plan_back(47, "Test4")
        elif outcome:
            api._fub_data["52"] = {"Id": 52, "Name": "Test4", "Active": 1}
            coordinator.note_plan_restored_as_new(47, "Test4", 52, complete=True)
            coordinator.config_entry.options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["52:Test4"]}

    coordinator.hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=restore))
    return coordinator


def test_a_restored_plan_whose_confirmation_push_fails_is_not_reported_as_not_restored(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(local review) The plan was back, yet the user heard "not restored: it failed" and got Restore again."""
    notifications = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.persistent_notification.async_create", notifications)
    coordinator = _restore_coordinator(api, True)
    coordinator.plan_watchdog.async_push_restored.side_effect = RuntimeError("notify down")
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is True
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()
    notifications.assert_not_called()
    assert "was restored as ID 52, but updating its watchdog pick or push failed" in caplog.text


def test_a_restore_that_finds_the_plan_back_is_not_reported_as_failed(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(Sourcery) The restore action refused it (the plan is back), yet the phone heard "restore failed"."""
    notifications = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.persistent_notification.async_create", notifications)
    coordinator = _restore_coordinator(api, "back")
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is False
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()
    coordinator.plan_watchdog.async_dismiss_push.assert_awaited_once_with(47, "Test4")
    notifications.assert_not_called()
    assert coordinator._vanished_plans_back == set()


def test_restoring_a_vanished_plan_runs_the_restore_action_as_a_new_started_plan(api: ComexioAPI) -> None:
    coordinator = _restore_coordinator(api, True)
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4")) is True
    coordinator.plan_watchdog.async_dismiss_push.assert_awaited_once_with(47, "Test4")  # the old alarm goes
    coordinator.hass.services.async_call.assert_awaited_once_with(
        "comexio",
        "function_plan_restore",
        {
            "config_entry": "entry1",
            "snapshot": "47:change:0:Test4",
            "confirm": True,
            "on_conflict": "new_id",
            "auto_start": True,
        },
        blocking=True,
    )


@pytest.mark.parametrize("from_push", [False, True])
def test_a_vanished_plan_without_a_backup_is_not_restored(api: ComexioAPI, from_push: bool) -> None:
    coordinator = _coordinator(
        api,
        function_plan_backup=_backups(),
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}),
    )
    coordinator.plan_watchdog.async_push_no_backup = AsyncMock()
    coordinator.hass.services = SimpleNamespace(async_call=AsyncMock())
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=from_push)) is False
    coordinator.hass.services.async_call.assert_not_called()
    assert coordinator.plan_watchdog.async_push_no_backup.await_args_list == ([call(47, "Test4")] if from_push else [])


def test_a_restore_from_the_push_confirms_on_the_phone(api: ComexioAPI) -> None:
    coordinator = _restore_coordinator(api, True)
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is True
    coordinator.plan_watchdog.async_push_restored.assert_awaited_once_with(47, "Test4", complete=True)
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()


@pytest.mark.parametrize("outcome", [False, RuntimeError("Session is closed")], ids=["refused", "raised"])
@pytest.mark.parametrize("from_push", [False, True])
def test_a_failed_or_refused_restore_is_reported(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture, outcome: Any, from_push: bool
) -> None:
    """(Review) The action reports a refusal only as a notification: the phone must hear it too."""
    coordinator = _restore_coordinator(api, outcome)
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=from_push)) is False
    assert "Function plan 'Test4' (ID 47) was not restored" in caplog.text
    assert coordinator.plan_watchdog.async_push_restore_failed.await_count == int(from_push)
    coordinator.plan_watchdog.async_push_restored.assert_not_awaited()
    coordinator.plan_watchdog.async_dismiss_push.assert_not_awaited()


def test_stop_watching_drops_the_named_and_the_bare_pick(api: ComexioAPI) -> None:
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4", "47", "19:Test1", "47:Other"]}
    coordinator = _coordinator(
        api, config_entry=SimpleNamespace(options=options), request_options_update_without_reload=MagicMock()
    )
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is True
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["19:Test1", "47:Other"]
    coordinator.config_entry.options = new_options
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is False


def test_stop_watching_drops_the_bare_pick_of_a_plan_still_holding_its_id(api: ComexioAPI) -> None:
    """(Sourcery) The plan itself at its ID is no reuse: "stop watching" must not keep its bare pick."""
    api._fub_data["47"] = {"Id": 47, "Name": "Test4", "Active": 0}
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47", "19:Test1"]}
    coordinator = _coordinator(
        api, config_entry=SimpleNamespace(options=options), request_options_update_without_reload=MagicMock()
    )
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is True
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["19:Test1"]


def test_stop_watching_keeps_a_bare_pick_whose_id_another_plan_holds_again(api: ComexioAPI) -> None:
    """(Sourcery) Comexio reused the ID since the last check: the bare pick watches that plan by now."""
    api._fub_data["47"] = {"Id": 47, "Name": "Other", "Active": 1}
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4", "47", "19:Test1"]}
    coordinator = _coordinator(
        api, config_entry=SimpleNamespace(options=options), request_options_update_without_reload=MagicMock()
    )
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is True
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["47", "19:Test1"]


def test_stop_watching_a_bare_pick_whose_id_another_plan_holds_says_why_it_stays(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(local review) The tap removed nothing and said nothing: the watchdog watched the other plan on."""
    notifications = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.persistent_notification.async_create", notifications)
    api._fub_data["47"] = {"Id": 47, "Name": "Other", "Active": 1}
    coordinator = _coordinator(
        api,
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47", "19:Test1"]}),
        request_options_update_without_reload=MagicMock(),
    )
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is False
    coordinator.request_options_update_without_reload.assert_not_called()
    assert "plan 'Other' holds that ID now" in notifications.call_args.args[1]


def test_stop_watching_refreshes_the_watch_right_away(api: ComexioAPI) -> None:
    coordinator = _coordinator(
        api,
        config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}),
        request_options_update_without_reload=MagicMock(),
        async_watch_managed_plans=AsyncMock(return_value=True),
    )
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is True
    coordinator.async_watch_managed_plans.assert_awaited_once()
    coordinator.async_update_listeners.assert_called_once()
    coordinator.config_entry.options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: "47:Test4"}  # malformed
    assert asyncio.run(coordinator.async_unwatch_user_plan(47, "Test4")) is False


def test_a_vanished_plan_leaves_the_stopped_check(api: ComexioAPI) -> None:
    """Its "stopped" push is cleared by the check; the alarm has its own tag, so the order does not matter."""
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True})
    options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4"]}
    coordinator = _coordinator(api, config_entry=SimpleNamespace(options=options))
    asyncio.run(coordinator._async_refresh_plan_run_states())
    check = coordinator.plan_watchdog.async_check.call_args
    assert 47 not in check.args[0]
    assert "keep_push" not in check.kwargs


def test_an_unverified_stop_of_a_plan_no_longer_watched_is_forgotten(api: ComexioAPI) -> None:
    api.client.get_function_plan_run_states = AsyncMock(return_value={19: True, 43: True})
    coordinator = _coordinator(api, _plan_stop_unverified={99: (0, 0.0)})
    asyncio.run(coordinator._async_refresh_plan_run_states())
    assert coordinator._plan_stop_unverified == {}


def test_a_restore_that_built_the_plan_but_kept_the_pick_counts_as_restored(api: ComexioAPI) -> None:
    """(Review) Moving the pick can fail after the plan was built: the pick is moved here instead."""
    coordinator = _restore_coordinator(api, True)

    def built_without_moving_the_pick(*_args: Any, **_kwargs: Any) -> None:
        api._fub_data["52"] = {"Id": 52, "Name": "Test4", "Active": 1}
        coordinator.note_plan_restored_as_new(47, "Test4", 52, complete=True)

    coordinator.hass.services.async_call.side_effect = built_without_moving_the_pick
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is True
    coordinator.plan_watchdog.async_push_restored.assert_awaited_once_with(47, "Test4", complete=True)
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["52:Test4"]


def test_a_restore_that_left_a_half_built_plan_is_reported_as_failed(api: ComexioAPI) -> None:
    """(Sourcery) A build failing after create_fup leaves a plan of that name: not a restore."""
    coordinator = _restore_coordinator(api, False)
    coordinator.hass.services.async_call.side_effect = lambda *_a, **_k: api._fub_data.update(
        {"52": {"Id": 52, "Name": "Test4", "Active": 0}}
    )
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is False
    coordinator.plan_watchdog.async_push_restore_failed.assert_awaited_once_with(47, "Test4")
    coordinator.plan_watchdog.async_push_restored.assert_not_awaited()


def test_a_live_plan_of_the_same_name_does_not_stop_the_restore(api: ComexioAPI) -> None:
    """(Sourcery) Plan names are not unique, and a failed restore leaves a half-built plan of that name."""
    coordinator = _restore_coordinator(api, True)
    api._fub_data["50"] = {"Id": 50, "Name": "Test4", "Active": 0}  # an earlier, half-built restore
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is True
    coordinator.hass.services.async_call.assert_awaited_once()
    coordinator.plan_watchdog.async_push_restored.assert_awaited_once_with(47, "Test4", complete=True)


def test_a_partial_restore_is_reported_as_partial(api: ComexioAPI, caplog: pytest.LogCaptureFixture) -> None:
    """(Sourcery) The plan is back, so its pick moves, but the phone hears it is incomplete."""
    coordinator = _restore_coordinator(api, True)

    def built_partially(*_args: Any, **_kwargs: Any) -> None:
        coordinator.note_plan_restored_as_new(47, "Test4", 52, complete=False)

    coordinator.hass.services.async_call.side_effect = built_partially
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is True
    coordinator.plan_watchdog.async_push_restored.assert_awaited_once_with(47, "Test4", complete=False)
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()
    assert "was restored as ID 52, but not completely" in caplog.text
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["52:Test4"]


def test_a_bare_pick_stays_with_the_plan_now_holding_the_old_id(api: ComexioAPI) -> None:
    """(Sourcery) Comexio reused ID 47 for another plan meanwhile: the bare pick watches that one."""
    coordinator = _restore_coordinator(api, True)
    coordinator.config_entry.options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47", "47:Test4"]}

    def built_without_moving_the_pick(*_args: Any, **_kwargs: Any) -> None:
        api._fub_data["47"] = {"Id": 47, "Name": "Other", "Active": 1}
        coordinator.note_plan_restored_as_new(47, "Test4", 52, complete=True)

    coordinator.hass.services.async_call.side_effect = built_without_moving_the_pick
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4")) is True
    (new_options,) = coordinator.request_options_update_without_reload.call_args.args
    assert new_options[CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS] == ["47", "52:Test4"]


def test_a_second_tap_during_the_backup_lookup_restores_nothing(api: ComexioAPI) -> None:
    """(Sourcery) Both taps pass the lock check before the first restore takes the lock."""
    coordinator = _restore_coordinator(api, True)

    async def load_yielding() -> None:
        await asyncio.sleep(0)  # the backup store's load: the second tap runs meanwhile

    coordinator.function_plan_backup.async_load.side_effect = load_yielding

    async def run() -> list[bool]:
        return list(
            await asyncio.gather(
                coordinator.async_restore_vanished_plan(47, "Test4", from_push=True),
                coordinator.async_restore_vanished_plan(47, "Test4", from_push=True),
            )
        )

    assert asyncio.run(run()) == [True, False]
    coordinator.hass.services.async_call.assert_awaited_once()
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()
    assert coordinator._vanished_restores == set()


@pytest.fixture
def fast_restore_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.comexio.coordinator.FUNCTION_PLAN_UNLOAD_POLL_SEC", 0.001)
    monkeypatch.setattr("custom_components.comexio.coordinator.FUNCTION_PLAN_VANISHED_RESTORE_WAIT_SEC", 2)


@pytest.mark.usefixtures("fast_restore_wait")
def test_a_restore_tap_while_another_restore_runs_waits_for_it(api: ComexioAPI) -> None:
    """(Sourcery) The running restore may be another plan's: this tap is not dropped, it follows."""
    coordinator = _restore_coordinator(api, True)

    async def run() -> bool:
        await coordinator._restore_lock.acquire()
        tap = asyncio.create_task(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True))
        for _ in range(5):
            await asyncio.sleep(0.001)
        coordinator.hass.services.async_call.assert_not_called()  # still waiting
        coordinator._restore_lock.release()
        return await tap

    assert asyncio.run(run()) is True
    coordinator.hass.services.async_call.assert_awaited_once()
    coordinator.plan_watchdog.async_push_restored.assert_awaited_once_with(47, "Test4", complete=True)


def test_a_tap_whose_wait_runs_out_is_refused_by_the_restore_and_reported(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other restore outlasted the wait: the action refuses it (no second restore), the phone hears so."""
    monkeypatch.setattr("custom_components.comexio.coordinator.FUNCTION_PLAN_UNLOAD_POLL_SEC", 0.001)
    monkeypatch.setattr("custom_components.comexio.coordinator.FUNCTION_PLAN_VANISHED_RESTORE_WAIT_SEC", 0.02)
    coordinator = _restore_coordinator(api, False)  # the action, refused by the running restore, builds nothing

    async def run() -> bool:
        async with coordinator._restore_lock:
            return await coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)

    assert asyncio.run(run()) is False
    coordinator.hass.services.async_call.assert_awaited_once()
    coordinator.plan_watchdog.async_push_restore_failed.assert_awaited_once_with(47, "Test4")
    assert coordinator._vanished_restores == set()


@pytest.mark.usefixtures("fast_restore_wait")
def test_a_plan_restored_while_its_tap_waited_is_not_restored_again(api: ComexioAPI) -> None:
    coordinator = _restore_coordinator(api, True)

    async def run() -> bool:
        async with coordinator._restore_lock:  # the plan card restores it meanwhile: its pick moves
            tap = asyncio.create_task(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True))
            await asyncio.sleep(0.001)
            coordinator.config_entry.options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["52:Test4"]}
        return await tap

    assert asyncio.run(run()) is False
    coordinator.hass.services.async_call.assert_not_called()
    coordinator.plan_watchdog.async_push_restore_failed.assert_not_awaited()


@pytest.mark.usefixtures("fast_restore_wait")
def test_a_tap_whose_entry_unloaded_while_it_waited_is_reported(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.persistent_notification.async_create", notifications)
    coordinator = _restore_coordinator(api, True)

    async def run() -> bool:
        async with coordinator._restore_lock:
            tap = asyncio.create_task(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True))
            await asyncio.sleep(0.001)
            coordinator.unloading = True
            return await tap

    assert asyncio.run(run()) is False
    coordinator.hass.services.async_call.assert_not_called()
    coordinator.plan_watchdog.async_push_restore_failed.assert_awaited_once_with(47, "Test4")
    assert "the integration reloaded while it waited" in notifications.call_args.args[1]


def test_a_failing_backup_lookup_is_reported_like_a_failed_restore(
    api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery) The lookup ran outside the restore's error handling: the phone heard nothing."""
    notifications = MagicMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.persistent_notification.async_create", notifications)
    coordinator = _restore_coordinator(api, True)
    coordinator.function_plan_backup.async_load.side_effect = OSError("disk")
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4", from_push=True)) is False
    coordinator.hass.services.async_call.assert_not_called()
    coordinator.plan_watchdog.async_push_restore_failed.assert_awaited_once_with(47, "Test4")
    assert "it failed (OSError)" in notifications.call_args.args[1]
    assert "Restore of function plan 'Test4' (ID 47) failed" in caplog.text


def test_same_named_vanished_plans_do_not_block_each_others_restore(api: ComexioAPI) -> None:
    """(Sourcery) The in-flight guard was keyed by name: the second plan's tap restored nothing."""
    coordinator = _restore_coordinator(api, True)
    coordinator.config_entry.options = {CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47:Test4", "48:Test4"]}

    new_ids = {47: 52, 48: 53}

    async def restore(_domain: str, _service: str, data: dict[str, Any], **_kwargs: Any) -> None:
        await asyncio.sleep(0)
        old_fub_id = int(data["snapshot"].split(":")[0])
        coordinator.note_plan_restored_as_new(old_fub_id, "Test4", new_ids[old_fub_id], complete=True)

    coordinator.hass.services.async_call.side_effect = restore

    async def run() -> list[bool]:
        return list(
            await asyncio.gather(
                coordinator.async_restore_vanished_plan(47, "Test4"),
                coordinator.async_restore_vanished_plan(48, "Test4"),
            )
        )

    assert asyncio.run(run()) == [True, True]
    assert coordinator.hass.services.async_call.await_count == 2


def test_a_same_named_restore_outcome_is_not_taken_by_another_deleted_plan(api: ComexioAPI) -> None:
    """(Sourcery) Keyed by name alone, plan 47's Restore took the new ID of plan 48's restore."""
    coordinator = _restore_coordinator(api, True)

    async def restore(*_args: Any, **_kwargs: Any) -> None:
        coordinator.note_plan_restored_as_new(48, "Test4", 53, complete=True)  # another plan's restore finished

    coordinator.hass.services.async_call.side_effect = restore
    assert asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4")) is False
    assert coordinator._restored_as_new == {(48, "Test4"): (53, True)}
    assert coordinator._vanished_restores == set()


def test_a_running_vanished_restore_is_known_by_id_and_name(api: ComexioAPI) -> None:
    coordinator = _restore_coordinator(api, True)
    seen: list[tuple[bool, bool]] = []

    async def restore(*_args: Any, **_kwargs: Any) -> None:
        seen.append(
            (
                ComexioCoordinator.vanished_restore_running(coordinator, 47, "Test4"),
                ComexioCoordinator.vanished_restore_running(coordinator, 47, "Other"),
            )
        )

    coordinator.hass.services.async_call.side_effect = restore
    asyncio.run(coordinator.async_restore_vanished_plan(47, "Test4"))
    assert seen == [(True, False)]
    assert ComexioCoordinator.vanished_restore_running(coordinator, 47, "Test4") is False


def test_watchdog_checks_run_one_at_a_time(api: ComexioAPI) -> None:
    """(Sourcery) An older check resuming after a newer one synced its stale vanished plans again."""
    coordinator = _coordinator(api)
    gate = asyncio.Event()
    running: list[int] = []
    overlap: list[bool] = []

    async def check(*_args: Any, **_kwargs: Any) -> bool:
        overlap.append(bool(running))
        running.append(1)
        await gate.wait()
        running.pop()
        return False

    coordinator.plan_watchdog.async_check = AsyncMock(side_effect=check)

    async def run() -> None:
        first = asyncio.create_task(coordinator.async_watch_managed_plans())
        second = asyncio.create_task(coordinator.async_watch_managed_plans())
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert overlap == [False, False]
    assert coordinator.plan_watchdog.async_sync_vanished.await_count == 2


def test_a_backup_store_that_fails_to_load_does_not_stop_the_watchdog_check(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery) The load error aborted the whole check: no plan was judged, no repair synced."""
    coordinator = _coordinator(api)
    coordinator.function_plan_backup.async_load.side_effect = OSError("disk")
    asyncio.run(coordinator.async_watch_managed_plans())
    coordinator.plan_watchdog.async_check.assert_awaited_once()
    coordinator.plan_watchdog.async_sync_vanished.assert_awaited_once()
    assert "could not load the backups" in caplog.text


def test_a_backup_store_that_stays_unloadable_is_logged_once_and_keeps_the_plan_names(
    api: ComexioAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """(local review) Every check (5-60 s) logged it, and a bare pick fell back to "ID n": no backup found."""
    coordinator = _coordinator(
        api, config_entry=SimpleNamespace(options={CONF_FUNCTION_PLAN_WATCHDOG_USER_PLANS: ["47"]})
    )
    caplog.set_level(logging.INFO)
    coordinator.plan_watchdog.vanished = {(47, "Test4")}  # named from its backups by the last check
    coordinator.function_plan_backup.async_load.side_effect = OSError("disk")
    asyncio.run(coordinator.async_watch_managed_plans())
    asyncio.run(coordinator.async_watch_managed_plans())
    assert caplog.text.count("could not load the backups") == 1
    for synced in coordinator.plan_watchdog.async_sync_vanished.await_args_list:
        assert synced.args[0] == {(47, "Test4")}
    coordinator.function_plan_backup.async_load.side_effect = None
    asyncio.run(coordinator.async_watch_managed_plans())
    assert "loads the backups again" in caplog.text
    assert coordinator.plan_watchdog.async_sync_vanished.await_args.args[0] == {(47, "ID 47")}  # no backup names it


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        ({(47, "Test4"), (12, "Else")}, "Test4"),
        ({(47, "A"), (47, "B")}, "ID 47"),  # two deleted plans on the ID: no guess at which one
        ({(12, "Else")}, "ID 47"),
        (set(), "ID 47"),
    ],
)
def test_a_bare_pick_keeps_the_last_name_only_if_it_was_one(previous: set[tuple[int, str]], expected: str) -> None:
    """With the backups unloadable, a bare pick keeps the name the last check gave it, never an arbitrary one."""
    assert coordinator_module._previous_vanished_name(previous, 47) == expected


def test_no_plan_starts_while_the_entry_unloads(api: ComexioAPI) -> None:
    """(Sourcery) The API session closes under a start begun after the unload's wait."""
    coordinator = _coordinator(api, unloading=True)
    assert coordinator.managed_plan_start_blocked is True
