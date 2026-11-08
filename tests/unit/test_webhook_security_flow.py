"""The ledger, the migration run and the handler of the per-command webhook ids (dg), end to end."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
        server_id=SERVER, webhook_command_ids=lambda: set(wanted), async_add_listener=MagicMock(), sync_webhooks=None
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


def test_the_old_id_goes_off_only_when_every_command_reads_back_as_its_target(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    coordinator.api.get_webio_command_form.assert_awaited_once_with("9", "7")
    assert not coordinator.api.webhook_security.legacy_enabled


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
