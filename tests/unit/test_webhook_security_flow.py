"""The ledger, the migration run and the handler of the per-command webhook ids (dg), end to end."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components import comexio as comexio_init
from custom_components.comexio import _setup_webhooks, webhook as ha_webhook, webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.coordinator import ComexioCoordinator
from tests.unit.test_api_helpers import PARSED
from tests.unit.test_webhook_security import (
    NOW,
    SERVER,
    _coordinator,
    _NoLock,
    _run,
    _security,
    _use_client,
)

_DOMAIN = "comexio"


def test_a_command_saved_with_its_own_path_is_scheduled_for_saving_the_old_path_is_not(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    _use_client(comexio_api, SimpleNamespace(save_webio_command=AsyncMock()))
    own = comexio_api.build_webio_commands(SERVER, PARSED)[0]

    assert _run(comexio_api.save_single_command(1, 2, {"Name": "HA old", "Parameter": "/api/webhook/comexio_srv"}))
    security._store.async_delay_save.assert_not_called()
    assert security.last_write is None
    assert _run(comexio_api.save_single_command(1, 2, own))

    security._store.async_delay_save.assert_called_once()
    saved = security._store.async_delay_save.call_args.args[0]()
    assert saved["migrated"] == [own["Name"]]


def test_the_ledger_survives_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    security.legacy_enabled = False
    security.mark_written("HA M5", NOW)
    _run(security.async_save())
    again = _security(monkeypatch, security._store.stored)

    _run(again.async_load())

    assert (again.secret, again.legacy_enabled, again.migrated, again.last_write) == ("s3cret", False, {"HA M5"}, NOW)


def test_a_naive_stored_stamp_becomes_aware_so_the_boot_time_comparison_cannot_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _security(monkeypatch, {"secret": "abc", "last_write": "2026-11-04T12:00:00"})

    _run(security.async_load())

    assert security.last_write == NOW  # the naive 12:00 read as UTC, comparable with an aware boot time
    assert security.last_write <= NOW + timedelta(hours=1)


def test_an_unreadable_store_is_reported_but_a_first_start_is_not(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _run(_security(monkeypatch, None).async_load())
    assert "unreadable" not in caplog.text

    _run(_security(monkeypatch, {"secret": 5}).async_load())

    assert "unreadable" in caplog.text


def test_a_command_the_run_cannot_rewrite_keeps_the_old_id_on(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=NOW + timedelta(hours=1), written=NOW)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    coordinator.data = {
        "webio_commands": {"HA no id": {"cmdId": "", "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    coordinator._webio_range_targets = lambda: {"HA no id": {"Name": "HA no id"}}

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert coordinator.api.webhook_security.legacy_enabled


def _tick_coordinator(monkeypatch: pytest.MonkeyPatch, *, cmd_id: str, boot: Any) -> Any:
    coordinator = _coordinator(monkeypatch, boot=boot, written=NOW)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    coordinator.data = {
        "webio_commands": {"HA m": {"cmdId": cmd_id, "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    coordinator._webio_range_targets = lambda: {"HA m": {"Name": "HA m", "Parameter": "/api/webhook/x"}}
    coordinator.api.get_webio_command_form = AsyncMock(
        return_value=SimpleNamespace(parameter="/api/webhook/x", data="")
    )
    coordinator.api.save_single_command = AsyncMock(
        side_effect=lambda *_a, **_k: coordinator.api.webhook_security.migrated.add("HA m") or True
    )
    return coordinator


def test_the_tick_waits_for_the_command_it_could_not_write_then_finishes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _tick_coordinator(monkeypatch, cmd_id="", boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"
    assert "cannot be moved to their own webhook id" in caplog.text
    assert "HA m" not in security.migrated
    assert security.legacy_enabled

    coordinator.data["webio_commands"]["HA m"]["cmdId"] = "9"

    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert "HA m" in security.migrated
    assert not security.legacy_enabled
    coordinator.sync_webhooks.assert_called_once_with()


def test_a_naive_boot_time_is_compared_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=(NOW + timedelta(hours=1)).replace(tzinfo=None), written=NOW)

    assert _run(coordinator._async_finish_webhook_migration()) == "done"


def test_the_tick_saves_the_ledger_even_when_the_run_fails_unexpectedly(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.unloading = False
    coordinator.data = {"webio_commands": {}}
    coordinator._async_rewrite_pending_webhooks = AsyncMock(side_effect=RuntimeError("boom"))
    tick = coordinator._async_webhook_migration_tick()

    with pytest.raises(RuntimeError):
        _run(tick)

    coordinator.api.webhook_security.async_save.assert_awaited()


@pytest.mark.parametrize("state", ["no_security", "no_data", "unloading", "legacy_off"])
def test_the_tick_does_nothing_when_it_cannot_or_need_not_run(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.unloading = state == "unloading"
    coordinator.data = {} if state == "no_data" else {"webio_commands": {}}
    coordinator.api.webhook_security.legacy_enabled = state != "legacy_off"
    if state == "no_security":
        coordinator.api.webhook_security = None
    coordinator._async_rewrite_pending_webhooks = AsyncMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator._async_rewrite_pending_webhooks.assert_not_awaited()


def test_a_run_past_its_budget_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    coordinator._pending_webhook_migrations = lambda: [("HA 1", {"cmdId": "1"}, {}, {})]
    coordinator.api.save_single_command = AsyncMock()
    monkeypatch.setattr("custom_components.comexio.coordinator.WEBHOOK_MIGRATION_BUDGET_SEC", -1)

    assert _run(coordinator._async_rewrite_pending_webhooks(coordinator.api.webhook_security)) == (0, 0)

    coordinator.api.save_single_command.assert_not_awaited()


def test_the_migration_targets_carry_their_own_path(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    comexio_api.webhook_security = coordinator.api.webhook_security
    coordinator.api = comexio_api
    del coordinator._webio_range_targets  # the helper's stand-in; this test wants the real method
    monkeypatch.setattr(ComexioCoordinator, "ignored_marker_ids", set())
    monkeypatch.setattr(ComexioCoordinator, "ignored_knx_ids", set())
    coordinator.webio_source_data = lambda include_held=False: PARSED

    targets = coordinator._webio_range_targets()

    assert targets
    assert not any(t["Parameter"].endswith(ws.legacy_webhook_id(SERVER)) for t in targets.values())


def _setup(security: ws.WebhookSecurity, wanted: set[str]) -> tuple[Any, Any, Any]:
    coordinator = SimpleNamespace(
        server_id=SERVER,
        data={"io": []},
        webhook_command_ids=lambda: set(wanted),
        async_add_listener=MagicMock(),
        sync_webhooks=None,
    )
    entry = MagicMock(entry_id="e1")
    hass = MagicMock(data={_DOMAIN: {}})
    _setup_webhooks(hass, entry, coordinator, security)  # type: ignore[arg-type]
    return hass, entry, coordinator


def test_the_registered_ids_follow_the_items_and_the_legacy_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[str] = []
    unregistered: list[str] = []
    monkeypatch.setattr(ha_webhook, "async_register", lambda _h, _d, _n, wid, _handler: registered.append(wid))
    monkeypatch.setattr(ha_webhook, "async_unregister", lambda _h, wid: unregistered.append(wid))
    security = _security(monkeypatch)
    wanted = {"a" * 32, "b" * 32}
    hass, entry, coordinator = _setup(security, wanted)

    assert set(registered) == {*wanted, ws.legacy_webhook_id(SERVER)}

    security.legacy_enabled = False
    wanted.discard("b" * 32)
    coordinator.webhook_command_ids = lambda: set(wanted)
    coordinator.sync_webhooks()
    assert set(unregistered) == {ws.legacy_webhook_id(SERVER), "b" * 32}

    unload_callbacks = [call.args[0] for call in entry.async_on_unload.call_args_list]
    unregister_callback = unload_callbacks[1]
    unregister_callback()
    unregister_callback()  # a second run (unload, then the callback) unregisters nothing twice
    assert unregistered.count("a" * 32) == 1
    assert hass.data[_DOMAIN]["e1_webhooks"] == set()

    _run(unload_callbacks[2]())  # the flush of the ledger
    security._store.async_save.assert_awaited()


@pytest.mark.parametrize("legacy_enabled", [True, False])
def test_the_setup_without_data_registers_only_the_old_id_and_unregisters_nothing(
    monkeypatch: pytest.MonkeyPatch, legacy_enabled: bool
) -> None:
    registered: list[str] = []
    unregister = MagicMock()
    monkeypatch.setattr(ha_webhook, "async_register", lambda _h, _d, _n, wid, _handler: registered.append(wid))
    monkeypatch.setattr(ha_webhook, "async_unregister", unregister)
    security = _security(monkeypatch)
    security.legacy_enabled = legacy_enabled
    coordinator = SimpleNamespace(
        server_id=SERVER,
        data={},
        webhook_command_ids=lambda: {"a" * 32},
        async_add_listener=MagicMock(),
        sync_webhooks=None,
    )
    _setup_webhooks(MagicMock(data={_DOMAIN: {}}), MagicMock(entry_id="e1"), coordinator, security)  # type: ignore[arg-type]
    coordinator.sync_webhooks()  # idempotent: the old id is not registered twice

    assert registered == ([ws.legacy_webhook_id(SERVER)] if legacy_enabled else [])
    unregister.assert_not_called()


@pytest.mark.parametrize(
    ("boot", "written", "pending"),
    [
        (None, None, False),  # nothing written: nothing to wait for
        (None, NOW, True),  # a write, but no boot time known
        (NOW - timedelta(hours=1), NOW, True),
        (NOW + timedelta(hours=1), NOW, False),
        (NOW.replace(tzinfo=None) + timedelta(hours=1), NOW, False),  # a naive boot time is read as UTC
    ],
)
def test_a_restart_is_pending_until_comexio_booted_after_the_last_write(
    monkeypatch: pytest.MonkeyPatch, boot: Any, written: Any, pending: bool
) -> None:
    coordinator = _coordinator(monkeypatch, boot=boot, written=written)

    assert coordinator._restart_pending(coordinator.api.webhook_security) is pending


def test_an_id_another_registration_holds_is_skipped_warned_once_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    taken = {"c" * 32}
    registered: list[str] = []

    def register(_h: Any, _d: Any, _n: Any, webhook_id: str, _handler: Any) -> None:
        if webhook_id in taken:
            raise ValueError("taken")
        registered.append(webhook_id)

    monkeypatch.setattr(ha_webhook, "async_register", register)
    security = _security(monkeypatch)
    security.legacy_enabled = False

    hass, _entry, coordinator = _setup(security, {"c" * 32, "d" * 32})
    coordinator.sync_webhooks()
    coordinator.sync_webhooks()

    assert hass.data[_DOMAIN]["e1_webhooks"] == {"d" * 32}
    assert caplog.text.count("already registered") == 1

    taken.clear()
    coordinator.sync_webhooks()

    assert hass.data[_DOMAIN]["e1_webhooks"] == {"c" * 32, "d" * 32}


class _Request:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _handler(monkeypatch: pytest.MonkeyPatch, security: ws.WebhookSecurity, coordinator: Any) -> Any:
    handlers: list[Any] = []
    monkeypatch.setattr(ha_webhook, "async_register", lambda _h, _d, _n, _wid, handler: handlers.append(handler))
    coordinator.server_id = SERVER
    coordinator.webhook_command_ids = lambda: {"d" * 32}
    hass = MagicMock(data={_DOMAIN: {}})
    _setup_webhooks(hass, MagicMock(entry_id="e1"), coordinator, security)
    return handlers[0]


def test_the_handler_forwards_a_push_only_on_the_id_of_its_own_command(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    security = _security(monkeypatch)
    coordinator = MagicMock()
    handler = _handler(monkeypatch, security, coordinator)
    own = ws.derive_webhook_id(security.secret, "marker:5")
    other = ws.derive_webhook_id(security.secret, "marker:6")

    _run(handler(None, other, _Request({"type": "marker", "id": "5", "value": 1, "server": SERVER})))
    assert "does not belong to its payload" in caplog.text
    _run(handler(None, own, _Request([1])))
    assert "not a JSON object" in caplog.text
    _run(handler(None, own, _Request(ValueError("not json"))))
    assert "non-JSON payload" in caplog.text
    coordinator.update_marker.assert_not_called()
    _run(handler(None, own, _Request({"type": "marker", "id": "5", "value": 1, "server": SERVER})))

    coordinator.update_marker.assert_called_once_with("5", 1)


@pytest.mark.parametrize(
    ("payload", "method", "args"),
    [
        ({"type": "io", "ext": "BASE", "io": "Q1", "value": 1}, "update_io_by_name", ("BASE", "Q1", 1)),
        ({"type": "quickbus", "id": "77", "value": 1}, "update_quickbus_io", ("77", 1)),
        ({"type": "knx", "id": "5", "value": 1}, "update_knx", ("5", 1)),
        ({"id": "5", "value": 1}, "update_marker", ("5", 1)),
    ],
)
def test_the_handler_routes_every_kind_of_push_to_its_coordinator_method(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], method: str, args: tuple[Any, ...]
) -> None:
    security = _security(monkeypatch)
    coordinator = MagicMock()
    handler = _handler(monkeypatch, security, coordinator)

    _run(handler(None, ws.legacy_webhook_id(SERVER), _Request(payload)))

    getattr(coordinator, method).assert_called_once_with(*args)


_TARGET = {
    "Name": "HA m",
    "Parameter": "/api/webhook/" + "a" * 32,
    "Data": f'return {{id="5", value=a, type="marker", server="{SERVER}"}}',
}


def _verify_coordinator(monkeypatch: pytest.MonkeyPatch, form: Any) -> Any:
    """A coordinator past the restart check whose one migrated command reads back as `form`."""
    coordinator = _coordinator(monkeypatch, boot=NOW + timedelta(hours=1), written=NOW)
    coordinator.api.webhook_security.migrated = {"HA m"}
    coordinator.data = {
        "webio_commands": {"HA m": {"cmdId": "9", "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    coordinator._webio_range_targets = lambda: {"HA m": _TARGET}
    coordinator.api.get_webio_command_form = AsyncMock(return_value=form)
    return coordinator


def _form(parameter: str | None = _TARGET["Parameter"], data: str | None = _TARGET["Data"]) -> Any:
    return SimpleNamespace(parameter=parameter, data=data)


@pytest.mark.parametrize(
    ("parameter", "data", "why"),
    [
        (_TARGET["Parameter"], _TARGET["Data"], None),
        (f" {_TARGET['Parameter']}\n", _TARGET["Data"], None),  # blanks the form leaves around the path
        ("/api/webhook/comexio_srv", _TARGET["Data"], "path"),
        (_TARGET["Parameter"], 'return {id="5", value=a, type="marker"}', "body without"),
        (_TARGET["Parameter"], _TARGET["Data"].replace(SERVER, "other"), "body without"),
    ],
)
def test_a_command_matches_its_target_by_path_and_server_field(parameter: str, data: str, why: str | None) -> None:
    deviation = ws.command_deviation(_TARGET, parameter, data)

    assert (deviation is None) if why is None else (deviation is not None and why in deviation)


def test_a_path_deviation_never_names_a_webhook_id() -> None:
    """The reason is logged, and the id in a path is the secret of its command."""
    wrong = "/api/webhook/comexio_srv"

    deviation = ws.command_deviation(_TARGET, wrong, _TARGET["Data"])

    assert deviation is not None
    assert wrong.rsplit("/", 1)[-1] not in deviation
    assert _TARGET["Parameter"].rsplit("/", 1)[-1] not in deviation


def test_the_old_id_goes_off_only_when_every_command_reads_back_as_its_target(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    coordinator.api.get_webio_command_form.assert_awaited_once_with("9", "7")
    assert not coordinator.api.webhook_security.legacy_enabled


def test_a_write_during_the_check_keeps_the_old_id_on_until_comexio_restarted_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security

    async def _read_while_a_sync_writes(*_args: Any) -> Any:
        security.last_write = NOW + timedelta(hours=2)  # after the Comexio boot of the check's start
        return _form()

    coordinator.api.get_webio_command_form = AsyncMock(side_effect=_read_while_a_sync_writes)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()


def test_a_command_that_differs_in_comexio_keeps_the_old_id_on_and_leaves_the_ledger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form(parameter="/api/webhook/comexio_srv"))
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert security.legacy_enabled
    assert "HA m" not in security.migrated  # the next run rewrites it
    assert "HA m: path" in caplog.text
    coordinator.sync_webhooks.assert_not_called()


@pytest.mark.parametrize("form", [None, _form(parameter=None), _form(data=None)], ids=["no form", "no path", "no body"])
def test_a_command_that_cannot_be_read_keeps_the_old_id_on_and_stays_in_the_ledger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, form: Any
) -> None:
    coordinator = _verify_coordinator(monkeypatch, form)
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert security.legacy_enabled
    assert "HA m" in security.migrated
    assert "1 could not be read (HA m)" in caplog.text


def test_a_command_without_command_id_is_unreadable_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.data["webio_commands"]["HA m"]["cmdId"] = ""

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    coordinator.api.get_webio_command_form.assert_not_awaited()


@pytest.mark.parametrize("stop", ["budget", "unloading", "restore"])
def test_an_unfinished_check_switches_nothing_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stop: str
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.unloading = stop == "unloading"
    coordinator._restore_lock = SimpleNamespace(locked=lambda: stop == "restore")
    if stop == "budget":
        monkeypatch.setattr("custom_components.comexio.coordinator.WEBHOOK_VERIFY_BUDGET_SEC", -1)

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert coordinator.api.webhook_security.legacy_enabled
    assert "ran out of time" in caplog.text
    coordinator.api.get_webio_command_form.assert_not_awaited()


def test_commands_outside_the_ledger_or_the_targets_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.api.webhook_security.migrated = set()

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    coordinator.api.get_webio_command_form.assert_not_awaited()


def test_a_command_built_by_the_lib_passes_its_own_check(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real builder's path and server field, not a hand-written target: the check must accept what it creates."""
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    comexio_api.webhook_security = coordinator.api.webhook_security
    target = comexio_api.build_webio_commands(SERVER, PARSED)[0]

    assert 'server="' in target["Data"]
    assert ws.command_deviation(target, target["Parameter"], target["Data"].lstrip("\n")) is None


def test_a_failing_read_makes_the_command_unreadable_and_the_check_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.api.get_webio_command_form = AsyncMock(side_effect=TimeoutError("slow"))

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert coordinator.api.webhook_security.legacy_enabled
    assert "1 could not be read (HA m)" in caplog.text


def test_a_deviating_command_leaves_the_ledger_on_disk_too(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form(parameter="/api/webhook/comexio_srv"))

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    coordinator.api.webhook_security.async_save.assert_awaited()


def test_a_failing_save_does_not_switch_the_old_id_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security
    security.async_save = AsyncMock(side_effect=OSError("disk full"))

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert security.legacy_enabled  # a state that is not persisted is not run on
    coordinator.sync_webhooks.assert_not_called()
    assert "could not be saved" in caplog.text


def test_a_write_while_the_switch_is_saved_puts_the_old_id_back(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security

    saved: list[bool] = []

    async def _save_while_a_sync_writes() -> None:
        saved.append(security.legacy_enabled)
        security.last_write = NOW + timedelta(hours=2)  # after the Comexio boot

    security.async_save = AsyncMock(side_effect=_save_while_a_sync_writes)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()
    assert saved == [False, True]  # the switch, then the switch back


def test_a_failing_switch_back_is_retried_by_a_delayed_save(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security
    calls: list[int] = []

    async def _second_save_fails() -> None:
        calls.append(1)
        if len(calls) == 1:
            security.last_write = NOW + timedelta(hours=2)
            return
        raise OSError("disk full")

    security.async_save = AsyncMock(side_effect=_second_save_fails)
    security.schedule_save = MagicMock()  # type: ignore[method-assign]

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    security.schedule_save.assert_called_once_with()
    assert "delayed save retries" in caplog.text


def test_targets_without_any_stored_command_verify_nothing_and_keep_the_old_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.data["webio_commands"] = {}

    assert _run(coordinator._async_finish_webhook_migration()) == "pending"

    assert coordinator.api.webhook_security.legacy_enabled
    assert "no Web-IO command is known" in caplog.text


def test_an_unload_during_the_rewrite_stops_it_before_the_next_command(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.api.webhook_security.migrated = set()

    async def save(*_args: Any, **_kwargs: Any) -> bool:
        coordinator.unloading = True
        return True

    coordinator.api.save_single_command = AsyncMock(side_effect=save)
    coordinator.data["webio_commands"]["HA n"] = {"cmdId": "10", "webioClass": "marker"}
    coordinator._webio_range_targets = lambda: {"HA m": _TARGET, "HA n": {**_TARGET, "Name": "HA n"}}

    assert _run(coordinator._async_rewrite_pending_webhooks(coordinator.api.webhook_security)) == (1, 0)

    assert coordinator.api.save_single_command.await_count == 1


def test_a_failing_rewrite_counts_as_failed_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.api.webhook_security.migrated = set()
    coordinator.api.save_single_command = AsyncMock(side_effect=TimeoutError("slow"))

    assert _run(coordinator._async_rewrite_pending_webhooks(coordinator.api.webhook_security)) == (0, 1)

    assert "Moving Web-IO command 'HA m' failed" in caplog.text


def _hosted(monkeypatch: pytest.MonkeyPatch, stored: Any, host: str) -> ws.WebhookSecurity:
    security = _security(monkeypatch, stored)
    security.host = host
    return security


def _switched_off_ledger(host: str | None) -> dict[str, Any]:
    ledger: dict[str, Any] = {
        "secret": "abc",
        "legacy": False,
        "migrated": ["HA M5"],
        "last_write": NOW.isoformat(),
    }
    if host is not None:
        ledger["host"] = host
    return ledger


def test_a_changed_host_switches_the_old_id_on_again_and_keeps_the_ledger_for_the_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery, dg #279) The commands of another server may still POST to the old id."""
    security = _hosted(monkeypatch, _switched_off_ledger("old.local"), "new.local")
    later = NOW + timedelta(days=1)
    monkeypatch.setattr(ws.dt_util, "utcnow", lambda: later)

    _run(security.async_load())

    assert security.legacy_enabled is True
    assert security.migrated == {"HA M5"}  # read back from the new host by the check, not rewritten blindly
    assert security.last_write == later
    stored = security._store.stored  # persisted too: a restart must not forget the switch
    assert (stored["host"], stored["legacy"]) == ("new.local", True)
    assert stored["last_write"] != NOW.isoformat()
    assert "host changed" in caplog.text


def test_a_changed_host_without_migrated_commands_still_switches_the_old_id_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery, dg #279) The new host may hold commands on the old id although nothing was migrated here."""
    security = _hosted(monkeypatch, {**_switched_off_ledger("old.local"), "migrated": []}, "new.local")

    _run(security.async_load())

    assert security.legacy_enabled is True
    assert security.last_write == NOW  # nothing was written, so no restart is waited for
    assert security._store.stored["host"] == "new.local"
    assert "host changed" in caplog.text


def test_an_entry_without_a_host_keeps_the_stored_host_and_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _hosted(monkeypatch, _switched_off_ledger("old.local"), "")

    _run(security.async_load())

    assert security.legacy_enabled is False
    assert security.host == "old.local"  # the next save writes it back instead of dropping it
    security._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("stored_host", [None, "same.local"])
def test_an_unchanged_or_unrecorded_host_leaves_the_switch_alone_and_keeps_the_host(
    monkeypatch: pytest.MonkeyPatch, stored_host: str | None
) -> None:
    security = _hosted(monkeypatch, _switched_off_ledger(stored_host), "same.local")

    _run(security.async_load())

    assert security.legacy_enabled is False
    assert security.last_write == NOW
    # only a ledger that never recorded its host is written (once); an unchanged one is left alone
    assert security._store.async_save.await_count == (1 if stored_host is None else 0)
    assert security._store.stored.get("host", "same.local") == "same.local"


def test_a_missing_quickbus_or_knx_id_is_logged_under_the_name_of_its_kind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _handler(monkeypatch, _security(monkeypatch), MagicMock())
    legacy = ws.legacy_webhook_id(SERVER)

    for kind in ("quickbus", "knx", ["odd"], None):
        _run(handler(None, legacy, _Request({"type": kind, "value": 1})))

    assert "Webhook Quickbus event missing id" in caplog.text
    assert "Webhook KNX event missing id" in caplog.text
    assert caplog.text.count("Webhook marker event missing id") == 2


def test_removing_the_entry_drops_the_webhook_store_even_when_the_saved_config_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(Sourcery, dg #279) The secret and the ledger must not stay on disk behind a failed delete."""
    removed: list[str] = []

    def store(name: str, fails: bool = False) -> MagicMock:
        async def remove() -> None:
            removed.append(name)
            if fails:
                raise OSError(name)

        return MagicMock(async_remove=remove)

    monkeypatch.setattr(comexio_init, "saved_config_store", lambda *_: store("saved", fails=True))
    monkeypatch.setattr(comexio_init, "webhook_security_store", lambda *_: store("security"))
    monkeypatch.setattr(comexio_init, "plan_preview_store", lambda *_: store("preview"))
    monkeypatch.setattr(comexio_init, "async_remove_certificate_pushes", lambda *_: None)
    removal = comexio_init.async_remove_entry(MagicMock(), MagicMock(entry_id="e1", data={}))

    with pytest.raises(OSError, match="saved"):
        _run(removal)

    assert removed == ["saved", "security", "preview"]
