"""The ledger, the migration run and the handler of the per-command webhook ids (dg), end to end."""

import ast
from datetime import timedelta
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio.scrape import scrape_js_vars
import pytest

from custom_components import comexio as comexio_init
from custom_components.comexio import _setup_webhooks, webhook as ha_webhook, webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import WEBIO_DEVICE_NAME_KNX_LOOPBACK
from custom_components.comexio.coordinator import ComexioCoordinator, count_own_web_devices
from custom_components.comexio.services import misc as services_misc
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
_HOST_CHANGED = "host_changed"  # the ledger key of WebhookSecurity.host_changed


def test_a_command_saved_with_its_own_path_is_scheduled_for_saving_the_old_path_is_not(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.comexio.api.dt_util.utcnow", lambda: NOW)
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
    assert security.migrated == {own["Name"]}
    assert security.last_write == NOW  # the write is stamped: the boot-time comparison of the migration rests on it


def test_the_ledger_survives_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    security.legacy_enabled = False
    security.mark_written("HA M5", NOW)
    _run(security.async_save())
    again = _security(monkeypatch, security._store.stored)

    _run(again.async_load())

    assert again.secret == "s3cret"
    assert again.legacy_enabled is False
    assert again.migrated == {"HA M5"}
    assert again.last_write == NOW


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
    # the real bookkeeping stamps the write with the clock: pin it to NOW, the time the boot is measured from
    monkeypatch.setattr("custom_components.comexio.api.dt_util.utcnow", lambda: NOW)
    coordinator = _coordinator(monkeypatch, boot=boot, written=NOW)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    coordinator.data = {
        "webio_commands": {"HA m": {"cmdId": cmd_id, "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    target = {"Name": "HA m", "Parameter": "/api/webhook/x", "Data": 'local d = {server="srv", value=1}'}
    coordinator._webio_range_targets = lambda: {"HA m": target}
    held = {"Parameter": "/api/webhook/comexio_srv", "Data": "local d = {value=1}"}  # the command as Comexio holds it

    async def _send(_device: Any, payload: dict[str, Any], **_k: Any) -> None:
        held.update(Parameter=payload["Parameter"], Data=payload["Data"])  # what the tick really sent

    # the production save path runs on a fake client: its bookkeeping (ledger entry, last_write, scheduled save) is
    # what the tick tests below rely on, so they fail when it stops recording a write instead of passing on a ledger
    # that a stub filled in
    api = coordinator.api
    api.client = SimpleNamespace(save_webio_command=AsyncMock(side_effect=_send))
    # `api` is a namespace, not a ComexioAPI: the production method is bound to it for save_single_command to call
    api._note_command_written = lambda payload: ComexioAPI._note_command_written(api, payload)

    async def _save(base: Any, device: Any, payload: dict[str, Any], **kwargs: Any) -> bool:
        return await ComexioAPI.save_single_command(api, base, device, payload, **kwargs)

    coordinator.api.get_webio_command_form = AsyncMock(
        side_effect=lambda *_a, **_k: SimpleNamespace(parameter=held["Parameter"], data=held["Data"])
    )
    coordinator.api.save_single_command = AsyncMock(side_effect=_save)
    return coordinator


def test_the_tick_waits_for_the_command_it_could_not_write_then_finishes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _tick_coordinator(monkeypatch, cmd_id="", boot=NOW - timedelta(hours=1))  # booted before the write
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"
    assert "cannot be moved to their own webhook id" in caplog.text
    assert "HA m" not in security.migrated
    assert security.legacy_enabled

    coordinator.data["webio_commands"]["HA m"]["cmdId"] = "9"

    # the tick that writes the last command cannot finish: Comexio sends the new command only after a restart
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    assert "HA m" in security.migrated
    assert security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    assert security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()

    coordinator.system_info.boot_time = security.last_write + timedelta(minutes=5)

    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert not security.legacy_enabled
    coordinator.sync_webhooks.assert_called_once_with()


def test_the_tick_resolves_the_webhook_target_before_it_writes_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The protocol of a written command comes from HA's target (#165): a start from the saved config ran no audit."""
    coordinator = _tick_coordinator(monkeypatch, cmd_id="9", boot=NOW + timedelta(hours=1))
    order: list[str] = []
    coordinator.async_webhook_target = AsyncMock(side_effect=lambda *_a: order.append("target"))
    save = coordinator.api.save_single_command

    async def _recorded_save(*args: Any, **kwargs: Any) -> bool:
        order.append("save")
        return await save(*args, **kwargs)

    coordinator.api.save_single_command = _recorded_save

    _run(coordinator._async_webhook_migration_tick())

    assert order[:2] == ["target", "save"]


def test_the_tick_resolves_no_target_when_it_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.data = None

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator.async_webhook_target.assert_not_awaited()


def test_the_tick_keeps_the_old_id_on_when_comexio_holds_another_command_than_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _tick_coordinator(monkeypatch, cmd_id="9", boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security

    async def _save_without_effect(*_a: Any, **_k: Any) -> bool:
        security.migrated.add("HA m")  # reported as written, but Comexio still holds the old command
        return True

    coordinator.api.save_single_command = AsyncMock(side_effect=_save_without_effect)

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"
    assert security.legacy_enabled
    assert "differ from their target" in caplog.text


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
def test_the_setup_before_the_first_data_registers_only_the_saved_onewire_ids(
    monkeypatch: pytest.MonkeyPatch, legacy_enabled: bool
) -> None:
    # Before the login nothing is known; the saved 1-Wire sensors then bring the ids of their commands (the
    # coordinator's webhook_command_ids without data). Never the old id: nothing could take its pushes yet.
    registered: list[str] = []
    unregister = MagicMock()
    monkeypatch.setattr(ha_webhook, "async_register", lambda _h, _d, _n, wid, _handler: registered.append(wid))
    monkeypatch.setattr(ha_webhook, "async_unregister", unregister)
    security = _security(monkeypatch)
    security.legacy_enabled = legacy_enabled
    early: set[str] = set()
    coordinator = SimpleNamespace(
        server_id=SERVER,
        data=None,
        webhook_command_ids=lambda: set(early),
        async_add_listener=MagicMock(),
        sync_webhooks=None,
    )
    _setup_webhooks(MagicMock(data={_DOMAIN: {}}), MagicMock(entry_id="e1"), coordinator, security)  # type: ignore[arg-type]
    assert registered == []

    early.add("a" * 32)
    coordinator.sync_webhooks()
    coordinator.sync_webhooks()  # idempotent: the id is not registered twice
    assert registered == ["a" * 32]

    # A first refresh that fails tells the listeners without data: the early ids stay.
    early.clear()
    coordinator.sync_webhooks()
    unregister.assert_not_called()
    coordinator.data = {}  # emptied data is no data either: nothing is dropped, the old id stays unregistered
    coordinator.sync_webhooks()
    unregister.assert_not_called()
    assert ws.legacy_webhook_id(SERVER) not in registered

    # The first data: the ids of every command, the old id while it is on, and the early id only if still one.
    coordinator.data = {"io": []}
    coordinator.webhook_command_ids = lambda: {"b" * 32}
    coordinator.sync_webhooks()

    assert set(registered) == {"a" * 32, "b" * 32} | ({ws.legacy_webhook_id(SERVER)} if legacy_enabled else set())
    unregister.assert_called_once()
    assert unregister.call_args.args[1] == "a" * 32


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
        # Review (Sourcery, #279): the field once, as a field of its own - not twice, not the tail of another name.
        (_TARGET["Parameter"], _TARGET["Data"].replace("}", f', server="{SERVER}"}}'), "exactly one"),
        (_TARGET["Parameter"], _TARGET["Data"].replace(" server=", " xserver="), "exactly one"),
        (_TARGET["Parameter"], _TARGET["Data"].replace(" server=", ".server="), "exactly one"),
    ],
)
def test_a_command_matches_its_target_by_path_and_server_field(parameter: str, data: str, why: str | None) -> None:
    deviation = ws.command_deviation(_TARGET, parameter, data)

    if why is None:
        assert deviation is None
    else:
        assert deviation is not None
        assert why in deviation


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
    assert stored["host"] == "new.local"
    assert stored["legacy"] is True
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
    # the second marker line is throttled (same kind inside the interval), and no line carries the payload
    assert caplog.text.count("Webhook marker event missing id") == 1
    assert "odd" not in caplog.text


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


def test_a_refused_webhook_id_is_warned_once_and_never_shown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review (Sourcery, #279): the id is the secret of its command, so no part of it may reach the log."""
    taken = "ab12cd34" * 4
    monkeypatch.setattr(comexio_init.webhook, "async_register", MagicMock(side_effect=ValueError("taken")))
    registered: set[str] = set()
    refused: set[str] = set()

    with caplog.at_level("WARNING"):
        for _ in range(2):
            comexio_init._register_webhooks(MagicMock(), MagicMock(), SERVER, registered, refused, {taken})

    warnings = [record.getMessage() for record in caplog.records if "already registered" in record.getMessage()]
    assert len(warnings) == 1
    assert taken[:8] not in warnings[0]
    assert refused == {taken}
    assert not registered


def test_a_server_name_full_of_quotes_and_backslashes_still_passes_its_own_check(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lib escapes the name inside the field: a name that spells a field itself is not a second one."""
    comexio_api.webhook_security = _security(monkeypatch)
    weird = 'a"b server="x" \\ end'
    target = comexio_api.build_webio_commands(weird, PARSED)[0]

    assert ws.command_deviation(target, target["Parameter"], target["Data"].lstrip("\n")) is None


def test_refused_requests_are_logged_once_per_kind_and_interval_without_their_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review (Sourcery, #279): the endpoint is open to anybody, so a flood of refusals must not flood the log."""
    security = _security(monkeypatch)
    clock = [0.0]  # a freshly booted host: the first refusal must be logged although monotonic() is below the interval
    monkeypatch.setattr(ws, "monotonic", lambda: clock[0])
    server = "evil-" + "x" * 80
    flood = {"type": "marker", "id": "5", "value": 1, "server": server, "token": "s3cr3t"}

    with caplog.at_level("WARNING"):
        for _ in range(5):
            assert security.accepts("whatever", flood) is False
        security.warn_refused("other-kind", "[%s] another kind", SERVER)
        clock[0] += ws.WEBHOOK_REFUSAL_LOG_INTERVAL_SEC
        assert security.accepts("whatever", flood) is False
        for _ in range(2):
            clock[0] += 1
            assert security.accepts("whatever", flood) is False
        clock[0] += ws.WEBHOOK_REFUSAL_LOG_INTERVAL_SEC
        assert security.accepts("whatever", flood) is False

    lines = [record.getMessage() for record in caplog.records]
    foreign = [line for line in lines if "dropped" in line]
    assert len(foreign) == 3
    assert "more of this kind" not in foreign[0]
    assert "4 more of this kind" in foreign[1]
    assert "2 more of this kind" in foreign[2]  # the counter starts again after each line
    assert repr(server)[:40] in foreign[0]
    assert repr(server) not in caplog.text
    assert "another kind" in " ".join(lines)
    assert "s3cr3t" not in caplog.text


def test_the_handler_logs_each_kind_of_refusal_once_per_interval_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    security = _security(monkeypatch)
    monkeypatch.setattr(ws, "monotonic", lambda: 0.0)
    handler = _handler(monkeypatch, security, MagicMock())

    with caplog.at_level("WARNING"):
        _run(handler(None, "d" * 32, _Request(ValueError("not json"))))
        _run(handler(None, "d" * 32, _Request([1])))
        _run(handler(None, "d" * 32, _Request(ValueError("not json again"))))

    non_json = [record for record in caplog.records if "non-JSON payload" in record.getMessage()]
    assert [record.levelname for record in non_json] == ["WARNING"]
    assert "not a JSON object" in caplog.text


def _preview_message(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> str:
    """The text the generate_web_io preview puts into its persistent notification."""
    coordinator = SimpleNamespace(
        api=comexio_api,
        server_id=SERVER,
        config_entry=SimpleNamespace(data={}, options={}),
        required_webio_classes=("marker",),
        async_webhook_target=AsyncMock(),
        webio_source_data=lambda: PARSED,
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
    )
    create = MagicMock()
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", create)
    hass = SimpleNamespace(data={_DOMAIN: {"e1": coordinator}})

    _run(services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": False})))

    return create.call_args.args[1]


def test_the_preview_notification_shows_no_command_id(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #279): every HA user reads the notification, and a command's id is its secret."""
    comexio_api.webhook_security = _security(monkeypatch)
    ids = {
        command["Parameter"].rsplit("/", 1)[-1]
        for command in comexio_api.build_webio_commands(SERVER, PARSED, webio_class=ws.WebioClass.MARKER)
    }

    message = _preview_message(comexio_api, monkeypatch)

    assert len(ids) == 2
    assert not any(webhook_id in message for webhook_id in ids)
    assert ws.WEBHOOK_PATH_PREFIX + ws.WEBHOOK_ID_REDACTED in message
    assert "hidden" in message


def test_the_preview_of_a_server_without_own_ids_stays_as_generated(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _preview_message(comexio_api, monkeypatch)

    assert "/api/webhook/comexio_srv" in message
    assert ws.WEBHOOK_ID_REDACTED not in message
    assert "The webhook ids of the commands are hidden" not in message


def test_redact_webhook_ids_hides_every_id_and_leaves_the_rest() -> None:
    first, second = "a" * 32, "0123456789abcdef" * 2
    text = f'x "/api/webhook/{first}" y "/api/webhook/{second}" z "/api/webhook/comexio_srv" {"b" * 31}'

    shown = ws.redact_webhook_ids(text)

    assert shown.count(ws.WEBHOOK_ID_REDACTED) == 2
    assert first not in shown
    assert second not in shown
    assert "/api/webhook/comexio_srv" in shown
    assert "b" * 31 in shown


def _setup_call_lines(name: str) -> list[int]:
    """The lines of async_setup_entry that call a function or method of this name."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(comexio_init.async_setup_entry)))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name)
    ]


def test_the_webhooks_are_registered_before_the_platforms_set_up_their_entities() -> None:
    # A 1-Wire reading only comes by push: one sent while the platforms set up must find its webhook registered.
    webhooks = _setup_call_lines("_setup_webhooks")
    platforms = _setup_call_lines("async_forward_entry_setups")

    assert len(webhooks) == 1
    assert len(platforms) == 1
    assert webhooks[0] < platforms[0]


def test_the_webhooks_are_set_up_before_the_login_and_the_first_refresh() -> None:
    # A 1-Wire reading pushed during the login or the first refresh must find the webhook of its (saved) sensor.
    connect = _setup_call_lines("_async_connect")
    webhooks = _setup_call_lines("_setup_webhooks")

    assert len(connect) == 1
    assert len(webhooks) == 1
    assert webhooks[0] < connect[0]


def test_nothing_is_awaited_between_the_first_refresh_and_the_webhook_ids_of_every_command() -> None:
    # The first refresh tells the commands, and with them the webhook ids: a push during a later await of the setup
    # (stores, extension renames) must already find its webhook.
    tree = ast.parse(textwrap.dedent(inspect.getsource(comexio_init.async_setup_entry)))
    connect = _setup_call_lines("_async_connect")
    synced = _setup_call_lines("sync_webhooks")
    awaited = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Await | ast.AsyncWith | ast.AsyncFor)]

    # _async_connect runs the first refresh: the line the ids of every command must follow.
    assert "_async_first_refresh(" in inspect.getsource(comexio_init._async_connect)
    assert len(connect) == 1
    assert len(synced) == 1
    assert connect[0] < synced[0]
    assert [line for line in awaited if connect[0] < line < synced[0]] == []


def _fresh_coordinator(
    monkeypatch: pytest.MonkeyPatch, *, never_polled: bool = True, web_devices: int | None = 0, real_store: bool = False
) -> Any:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    if real_store:
        coordinator.api.webhook_security = _security(monkeypatch)
    coordinator._never_polled = never_polled
    coordinator._last_poll_scraped = True  # the poll that read `$WebDevices` scraped the config
    coordinator._own_web_device_count = web_devices
    return coordinator


def test_an_entry_without_a_saved_config_and_without_web_devices_switches_the_old_id_off_at_its_first_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _fresh_coordinator(monkeypatch)
    security = coordinator.api.webhook_security
    assert security.legacy_enabled  # a new store starts with it on

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled is False
    security.async_save.assert_awaited_once()
    assert security.accepts("comexio_srv", {"type": "marker", "id": "5", "value": 1}) is False
    assert coordinator._never_polled is False


@pytest.mark.parametrize(
    ("never_polled", "web_devices", "migrated", "written"),
    [
        pytest.param(False, 0, set(), None, id="entry-with-a-saved-config"),
        pytest.param(True, 3, set(), None, id="own-web-devices-in-comexio"),
        pytest.param(True, 0, {"HA M5"}, None, id="ledger-not-empty"),
        pytest.param(True, 0, set(), NOW, id="a-write-is-recorded"),
    ],
)
def test_every_other_entry_keeps_the_old_id_on_for_the_migration(
    monkeypatch: pytest.MonkeyPatch, never_polled: bool, web_devices: int, migrated: set[str], written: Any
) -> None:
    coordinator = _fresh_coordinator(monkeypatch, never_polled=never_polled, web_devices=web_devices)
    security = coordinator.api.webhook_security
    security.migrated = migrated
    security.last_write = written

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()
    assert coordinator._never_polled is False  # decided: the migration stays


def test_a_store_recorded_for_another_host_keeps_the_old_id_on_although_the_new_host_has_no_web_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(Sourcery, #307) The commands of the earlier host may still push to the old id."""
    coordinator = _fresh_coordinator(monkeypatch)
    security = coordinator.api.webhook_security
    security.host_changed = True

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()
    assert coordinator._never_polled is False  # decided: the migration decides the old id from here on


@pytest.mark.parametrize(
    ("stored_host", "expected"),
    [
        pytest.param("old.local", True, id="recorded-for-another-host"),
        pytest.param("new.local", False, id="same-host"),
        pytest.param(None, False, id="no-host-recorded-yet"),
    ],
)
def test_only_a_ledger_recorded_for_another_host_counts_as_a_host_change(
    monkeypatch: pytest.MonkeyPatch, stored_host: str | None, expected: bool
) -> None:
    security = _hosted(monkeypatch, _switched_off_ledger(stored_host), "new.local")

    _run(security.async_load())

    assert security.host_changed is expected


def test_a_recorded_host_change_stays_recorded_when_the_host_is_the_same_again(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _hosted(monkeypatch, {**_switched_off_ledger("new.local"), _HOST_CHANGED: True}, "new.local")

    _run(security.async_load())

    assert security.host_changed is True


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"secret": 5, _HOST_CHANGED: True}, id="secret-unreadable"),
        pytest.param({_HOST_CHANGED: True}, id="secret-missing"),
        pytest.param({"secret": 5, "host": "old.local"}, id="another-host-without-the-flag"),
        pytest.param({"secret": 5, "host": ""}, id="empty-host-recorded-counts-like-in-an-intact-ledger"),
    ],
)
def test_a_host_change_survives_a_lost_secret(monkeypatch: pytest.MonkeyPatch, stored: dict) -> None:
    """(silent-failure-hunter, Sourcery, #307) A new secret does not wipe the host change: the old id must stay on."""
    security = _hosted(monkeypatch, stored, "new.local")

    _run(security.async_load())

    assert security.host_changed is True
    assert security._store.stored[_HOST_CHANGED] is True
    # The reset ledger and the old id belong together: the new secret is in, the old id is on, nothing is migrated.
    assert security.secret
    assert security.legacy_enabled
    assert not security.migrated


def test_a_host_change_found_with_a_lost_secret_is_still_there_after_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _hosted(monkeypatch, {"secret": 5, "host": "old.local"}, "new.local")
    _run(security.async_load())

    # The stored host is the new one by now: only the persisted flag can tell the change on the next load.
    again = _hosted(monkeypatch, security._store.stored, "new.local")
    _run(again.async_load())

    assert again.host_changed is True
    assert again.legacy_enabled


@pytest.mark.parametrize(
    ("stored", "flag"),
    [
        pytest.param({"secret": 5, "host": "old.local"}, False, id="another-host-but-no-host-to-compare"),
        pytest.param(
            {"secret": 5, "host": "old.local", _HOST_CHANGED: True}, True, id="the-flag-survives-without-a-host"
        ),
    ],
)
def test_a_lost_secret_of_an_entry_without_a_host_keeps_only_the_recorded_flag(
    monkeypatch: pytest.MonkeyPatch, stored: dict, flag: bool
) -> None:
    security = _hosted(monkeypatch, stored, "")

    _run(security.async_load())

    assert security.host_changed is flag


@pytest.mark.parametrize(
    "stored", [pytest.param(["x"], id="list"), pytest.param("garbage", id="str"), pytest.param(5, id="int")]
)
def test_a_store_that_is_no_dict_is_reported_and_is_no_host_change(
    monkeypatch: pytest.MonkeyPatch, stored: object
) -> None:
    security = _hosted(monkeypatch, stored, "new.local")

    _run(security.async_load())

    assert security.host_changed is False
    assert security.secret


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"secret": 5, "host": "new.local"}, id="same-host"),
        pytest.param({"secret": 5}, id="no-host-recorded"),
        pytest.param({"secret": 5, "host": None}, id="host-recorded-as-none"),
    ],
)
def test_a_lost_secret_alone_is_no_host_change(monkeypatch: pytest.MonkeyPatch, stored: dict) -> None:
    security = _hosted(monkeypatch, stored, "new.local")

    _run(security.async_load())

    assert security.host_changed is False


def test_a_recorded_host_change_is_read_back_for_an_entry_without_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _hosted(monkeypatch, {**_switched_off_ledger("old.local"), _HOST_CHANGED: True}, "")

    _run(security.async_load())

    assert security.host_changed is True


def test_a_host_change_found_at_load_keeps_the_old_id_on_through_the_first_poll_and_a_restart_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(Sourcery, #307) The whole chain: the load finds another host, the settle of the poll leaves the old id on."""
    # No migrated commands and no write stamp: only the host change can keep the old id on here.
    ledger = {"secret": "abc", "legacy": False, "host": "old.local"}
    security = _hosted(monkeypatch, ledger, "new.local")
    _run(security.async_load())
    assert security._store.stored[_HOST_CHANGED] is True  # persisted with the switch-on

    # Home Assistant restarts before the first scraped poll: the next load sees the host recorded already.
    again = _hosted(monkeypatch, security._store.stored, "new.local")
    _run(again.async_load())
    assert again.host_changed is True
    assert again.legacy_enabled

    coordinator = _fresh_coordinator(monkeypatch)
    coordinator.api.webhook_security = again
    again.async_save = AsyncMock()

    _run(coordinator._async_settle_legacy_webhook_id())

    assert again.legacy_enabled
    again.async_save.assert_not_awaited()
    assert coordinator._never_polled is False


@pytest.mark.parametrize("security_present", [True, False])
def test_an_old_id_that_is_off_already_or_a_missing_ledger_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, security_present: bool
) -> None:
    coordinator = _fresh_coordinator(monkeypatch)
    security = coordinator.api.webhook_security
    security.legacy_enabled = False
    if not security_present:
        coordinator.api.webhook_security = None

    _run(coordinator._async_settle_legacy_webhook_id())

    security.async_save.assert_not_awaited()
    assert coordinator._never_polled is False


def test_a_poll_that_did_not_read_the_web_devices_decides_nothing_and_the_next_one_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An undecodable $WebDevices leaves webio_commands empty although commands of ours may exist.
    coordinator = _fresh_coordinator(monkeypatch, web_devices=None)
    security = coordinator.api.webhook_security

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()
    assert coordinator._never_polled is True

    coordinator._own_web_device_count = 0  # a later poll read it, and it is empty
    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled is False


def test_a_poll_without_a_scraped_config_decides_nothing_even_when_it_read_no_web_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `$WebDevices` read as empty from a poll that scraped nothing (no `$FubModules`) proves no absence of commands.
    coordinator = _fresh_coordinator(monkeypatch)
    coordinator._last_poll_scraped = False
    security = coordinator.api.webhook_security

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()
    assert coordinator._never_polled is True  # asked again at the next poll

    coordinator._last_poll_scraped = True
    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled is False


def test_only_the_poll_that_found_web_devices_decides_the_migration_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _fresh_coordinator(monkeypatch, web_devices=2)
    security = coordinator.api.webhook_security

    _run(coordinator._async_settle_legacy_webhook_id())
    coordinator._own_web_device_count = 0  # a later poll sees none: no decision any more
    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled


def test_a_switch_that_cannot_be_saved_is_not_kept_in_memory_and_tried_again_at_the_next_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a restart the stored switch would be on again: running on a state that is lost helps nobody.
    coordinator = _fresh_coordinator(monkeypatch)
    security = coordinator.api.webhook_security
    security.async_save.side_effect = OSError("disk full")

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled
    assert coordinator._never_polled is True

    security.async_save.side_effect = None
    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled is False
    assert coordinator._never_polled is False


_OWN_WEBIO_NAME = "HA"


def _web_devices(*names: Any) -> dict[str, Any]:
    return {"WebDevices": {str(i): {"Name": name} for i, name in enumerate(names, 1)}}


@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        pytest.param({}, None, id="missing"),
        pytest.param({"WebDevices": None}, None, id="none"),
        pytest.param({"WebDevices": "garbage"}, None, id="not-a-group"),
        pytest.param({"WebDevices": []}, None, id="raw-list-is-no-id-mapping"),
        pytest.param({"WebDevices": {}}, 0, id="read-and-empty"),
        pytest.param(_web_devices("HA [M]", "HA [IO]"), 2, id="two-devices-of-ours"),
        pytest.param(_web_devices("HA [M]", "Other"), 1, id="a-device-of-the-user-is-not-counted"),
        pytest.param(_web_devices("Other", "Garage lights"), 0, id="only-devices-of-the-user"),
        pytest.param(_web_devices("HA"), 1, id="the-pre-split-device-is-ours"),
        pytest.param(_web_devices("HA [1Wire]", "HA [QB]", "HA [KNX]"), 3, id="every-class-is-ours"),
        pytest.param(_web_devices(" ha [m] "), 1, id="blanks-and-case-are-ignored-in-doubt-it-is-ours"),
        pytest.param(_web_devices(" Ha "), 1, id="the-pre-split-name-too"),
        pytest.param(_web_devices("Earlier [IO]"), 1, id="a-class-suffix-under-another-base-name-is-ours"),
        pytest.param(_web_devices("Other [x]", "Lights [M] 2"), 0, id="a-suffix-only-counts-at-the-end"),
        pytest.param(_web_devices("Heizung[IO]", "[M]"), 0, id="a-suffix-needs-its-blank-and-a-base-name"),
        pytest.param(_web_devices("Sensors [IO]"), 1, id="a-user-device-named-like-a-class-is-ours-in-doubt"),
        pytest.param(_web_devices(WEBIO_DEVICE_NAME_KNX_LOOPBACK), 0, id="the-knx-loopback-pushes-to-comexio"),
        pytest.param(_web_devices("HA [M]", None), None, id="a-name-that-is-none-decides-nothing"),
        pytest.param(_web_devices("HA [M]", ["list"]), None, id="a-name-that-is-no-text-decides-nothing"),
        pytest.param({"WebDevices": {"1": {"Name": "Other"}, "2": "garbage"}}, None, id="an-entry-that-is-no-device"),
        pytest.param({"WebDevices": {"1": {"Ip": "10.0.0.1"}}}, None, id="an-entry-without-a-name"),
    ],
)
def test_only_our_own_web_devices_count_and_what_was_not_read_is_never_zero(
    raw_config: dict[str, Any], expected: int | None
) -> None:
    assert count_own_web_devices(raw_config, _OWN_WEBIO_NAME) == expected


def test_the_pre_split_device_follows_the_configured_class_name() -> None:
    raw_config = _web_devices("Other", "HomeAssistant")

    assert count_own_web_devices(raw_config, "Other") == 1


def test_a_device_with_the_configured_class_name_counts_even_when_it_is_the_loopback_name() -> None:
    """A device named like the configured class name is the pre-split device: in doubt it is ours (old id stays on).

    Only reachable when the user types the reserved loopback name as class name; the loopback device cannot be told
    apart from the pre-split device then, and counting it errs on the safe side.
    """
    raw_config = _web_devices(WEBIO_DEVICE_NAME_KNX_LOOPBACK)

    assert count_own_web_devices(raw_config, WEBIO_DEVICE_NAME_KNX_LOOPBACK) == 1


def test_a_class_name_with_blanks_and_brackets_is_compared_as_text() -> None:
    raw_config = _web_devices("My [Home]", "My [Home] [M]", "My")

    assert count_own_web_devices(raw_config, "My [Home]") == 2


@pytest.mark.parametrize(
    ("devices", "kept_on"),
    [
        pytest.param(("Garage lights", "Heating"), False, id="only-devices-of-the-user"),
        pytest.param(("Garage lights", "HA [IO]"), True, id="a-device-of-ours-beside-them"),
        pytest.param(("Garage lights", None), True, id="a-device-that-was-not-read"),
    ],
)
def test_the_decision_for_a_fresh_entry_follows_the_devices_read_from_comexio(
    monkeypatch: pytest.MonkeyPatch, devices: tuple[Any, ...], kept_on: bool
) -> None:
    """The count and the decision together: a device of the user's own alone does not keep the old id on (ek)."""
    count = count_own_web_devices(_web_devices(*devices), _OWN_WEBIO_NAME)
    coordinator = _fresh_coordinator(monkeypatch, web_devices=count)
    security = coordinator.api.webhook_security

    _run(coordinator._async_settle_legacy_webhook_id())

    assert security.legacy_enabled is kept_on


@pytest.mark.parametrize(
    ("literal", "expected"),
    [("[]", 0), ("{}", 0), ('[{"Name": "HA [M]"}]', 1), ('{"3": {"Name": "HA [M]"}}', 1), ('[{"Name": "Mine"}]', 0)],
    ids=["php-empty-array", "empty-object", "gap-free-array", "object", "a-device-of-the-user"],
)
def test_a_web_device_group_as_the_scrape_delivers_it_counts_whatever_shape_comexio_sent(
    literal: str, expected: int
) -> None:
    page = f"<script>var $WebDevices = {literal};</script>"

    assert count_own_web_devices(scrape_js_vars(page, page_label="function module"), _OWN_WEBIO_NAME) == expected


def test_the_switch_survives_a_reload_and_the_old_id_stays_refused_after_a_later_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _fresh_coordinator(monkeypatch, real_store=True)
    security = coordinator.api.webhook_security
    secret = security.secret

    _run(coordinator._async_settle_legacy_webhook_id())
    again = _security(monkeypatch, security._store.stored)
    _run(again.async_load())

    assert again.legacy_enabled is False
    assert again.secret == secret
    assert again.accepts("comexio_srv", {"type": "marker", "id": "5", "value": 1}) is False
    again.mark_written("HA M5", NOW)  # a sync writing a command later does not turn the old id on
    assert again.legacy_enabled is False


@pytest.mark.parametrize(
    ("stored", "load_error", "expected"),
    [
        pytest.param(None, None, True, id="no-saved-config"),
        pytest.param({"markers": []}, None, False, id="a-saved-config"),
        pytest.param({"unrelated": 1}, None, False, id="unreadable-layout-proves-nothing"),
        pytest.param(None, OSError("denied"), False, id="store-that-cannot-be-read-proves-nothing"),
    ],
)
def test_only_a_missing_saved_config_marks_the_entry_as_never_polled(
    stored: Any, load_error: Exception | None, expected: bool
) -> None:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator._never_polled = False
    coordinator.api = SimpleNamespace(seed_onewire_items=MagicMock())
    coordinator._saved_config_store = SimpleNamespace(async_load=AsyncMock(return_value=stored, side_effect=load_error))

    _run(coordinator.async_seed_saved_onewire_sensors())

    assert coordinator._never_polled is expected


def test_the_poll_decides_before_it_saves_the_config() -> None:
    # Saved first, a crash in between would make the entry look polled while the old id is still on.
    source = inspect.getsource(ComexioCoordinator._async_run_update)

    assert (
        0
        < source.index("_async_settle_legacy_webhook_id(")
        < source.index("_async_save_config_or_keep_onewire_status(")
    )
