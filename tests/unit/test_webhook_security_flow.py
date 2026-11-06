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

    assert security.last_write is not None
    assert security.last_write.tzinfo is not None


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

    entry.async_on_unload.call_args_list[-1].args[0]()  # the unload callback
    assert "a" * 32 in unregistered
    assert hass.data[_DOMAIN]["e1_webhooks"] == set()


def test_an_id_another_registration_holds_is_not_taken_over(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any) -> None:
        raise ValueError("taken")

    monkeypatch.setattr(ha_webhook, "async_register", refuse)
    security = _security(monkeypatch)
    security.legacy_enabled = False

    hass, _entry, _coordinator_stub = _setup(security, {"c" * 32})

    assert hass.data[_DOMAIN]["e1_webhooks"] == set()


class _Request:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self) -> Any:
        return self._body


def _handler(monkeypatch: pytest.MonkeyPatch, security: ws.WebhookSecurity, coordinator: Any) -> Any:
    handlers: list[Any] = []
    monkeypatch.setattr(ha_webhook, "async_register", lambda _h, _d, _n, _wid, handler: handlers.append(handler))
    coordinator.server_id = SERVER
    coordinator.webhook_command_ids = lambda: {"d" * 32}
    hass = MagicMock(data={_DOMAIN: {}})
    _setup_webhooks(hass, MagicMock(entry_id="e1"), coordinator, security)
    return handlers[0]


def test_the_handler_forwards_a_push_only_on_the_id_of_its_own_command(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    coordinator = MagicMock()
    handler = _handler(monkeypatch, security, coordinator)
    own = ws.derive_webhook_id(security.secret, "marker:5")
    other = ws.derive_webhook_id(security.secret, "marker:6")

    _run(handler(None, other, _Request({"type": "marker", "id": "5", "value": 1, "server": SERVER})))
    coordinator.update_marker.assert_not_called()
    _run(handler(None, own, _Request([1])))
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
