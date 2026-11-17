"""Where the migration to per-command webhook ids ends (ej): nothing to migrate, commands it cannot rewrite, a restart.

Each way the old id could stay on for good either switches it off (no Web-IO device exists, nothing can push to it)
or tells the user with a repair what to do about it.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components import comexio as comexio_init
from custom_components.comexio import coordinator as coordinator_module, issue_owner
from custom_components.comexio.const import (
    CONF_SERVER_ID,
    ISSUE_WEBHOOK_COMMANDS_STUCK,
    ISSUE_WEBHOOK_RESTART,
    WEBHOOK_STUCK_LISTED,
)
from tests.unit.test_webhook_security import NOW, SERVER, _coordinator, _run
from tests.unit.test_webhook_security_flow import (
    _TARGET,
    _form,
    _fresh_coordinator,
    _tick_coordinator,
    _verify_coordinator,
)

_ENTRY_ID = "entry-1"
_STUCK_ID = f"{ISSUE_WEBHOOK_COMMANDS_STUCK}_{SERVER}"
_RESTART_ID = f"{ISSUE_WEBHOOK_RESTART}_{SERVER}"
_PENDING = "pending"


@pytest.fixture
def issues(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    registry = MagicMock()
    registry.IssueSeverity = ir.IssueSeverity
    monkeypatch.setattr(coordinator_module, "ir", registry)
    monkeypatch.setattr(issue_owner, "ir", registry)  # the create goes through async_create_entry_issue
    monkeypatch.setattr(issue_owner, "OWNER_KEYWORD_SUPPORTED", True)  # so the entry link shows in the call
    return registry


@pytest.fixture
def notifications(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    module = MagicMock()
    monkeypatch.setattr(coordinator_module, "persistent_notification", module)
    return module


def _deleted(registry: MagicMock) -> set[str]:
    return {call.args[2] for call in registry.async_delete_issue.call_args_list}


def _raised(registry: MagicMock) -> dict[str, dict[str, Any]]:
    return {call.args[2]: call.kwargs for call in registry.async_create_issue.call_args_list}


def _without_web_devices(monkeypatch: pytest.MonkeyPatch, *, web_devices: int | None = 0) -> Any:
    """The night after a poll that read `$WebDevices`: targets exist, no command of ours is stored."""
    coordinator = _fresh_coordinator(monkeypatch, never_polled=False, web_devices=web_devices)
    coordinator.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)
    coordinator.data = {"webio_commands": {}, "webio_devices": {}}
    coordinator._webio_range_targets = lambda: {"HA m": _TARGET}
    return coordinator


def test_an_entry_that_never_synced_switches_the_old_id_off_at_the_nightly_run(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _without_web_devices(monkeypatch)
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    assert security.legacy_enabled is False
    security.async_save.assert_awaited_once()
    coordinator.sync_webhooks.assert_called_once_with()  # the registered old id goes with it
    assert security.accepts(f"comexio_{SERVER}", {"type": "marker", "id": "5", "value": 1}) is False
    assert _RESTART_ID in _deleted(issues)  # the stuck repair went in the same run, when it found none left


@pytest.mark.parametrize(
    "keep_on",
    [
        pytest.param({"web_devices": None}, id="web-devices-not-read-by-the-last-poll"),
        pytest.param({"scraped": False}, id="the-last-poll-did-not-scrape-the-config"),
        pytest.param({"web_devices": 2}, id="web-devices-exist-that-are-not-recognised-as-ours"),
        pytest.param({"migrated": {"HA m"}}, id="a-command-was-rewritten"),
        pytest.param({"host_changed": True}, id="ledger-recorded-for-another-host"),
        pytest.param({"written": NOW}, id="a-command-was-written"),
    ],
)
def test_the_nightly_run_keeps_the_old_id_on_unless_nothing_can_push_to_it(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, caplog: pytest.LogCaptureFixture, keep_on: dict[str, Any]
) -> None:
    coordinator = _without_web_devices(monkeypatch, web_devices=keep_on.get("web_devices", 0))
    security = coordinator.api.webhook_security
    security.migrated = keep_on.get("migrated", set())
    security.host_changed = keep_on.get("host_changed", False)
    security.last_write = keep_on.get("written")
    coordinator._last_poll_scraped = keep_on.get("scraped", True)
    coordinator.system_info = SimpleNamespace(boot_time=NOW + timedelta(hours=1))  # restarted after the write

    assert _run(coordinator._async_finish_webhook_migration()) == _PENDING

    assert security.legacy_enabled
    security.async_save.assert_not_awaited()
    assert "no Web-IO command is known to verify" in caplog.text
    assert _deleted(issues) == {_RESTART_ID}  # Comexio restarted, so only the request to restart is out of date


def test_a_switch_that_cannot_be_saved_keeps_the_old_id_on_at_the_nightly_run(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _without_web_devices(monkeypatch)
    security = coordinator.api.webhook_security
    security.async_save.side_effect = OSError("disk full")

    assert _run(coordinator._async_finish_webhook_migration()) == _PENDING

    assert security.legacy_enabled  # after a restart it would be on again, so do not run on a state that is lost
    assert "could not be saved" in caplog.text
    coordinator.sync_webhooks.assert_not_called()
    assert _STUCK_ID not in _deleted(issues)  # nothing was switched off, so nothing is taken back


def test_the_commands_the_run_cannot_rewrite_raise_a_repair_that_names_them(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _without_web_devices(monkeypatch)
    coordinator.data = {
        "webio_commands": {"HA no id": {"cmdId": "", "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }

    coordinator._report_unrewritable_webhooks(["HA no id"])

    raised = _raised(issues)[_STUCK_ID]
    assert raised["config_entry_id"] == _ENTRY_ID
    assert raised["is_fixable"] is False
    assert raised["severity"] is ir.IssueSeverity.WARNING
    assert raised["translation_key"] == ISSUE_WEBHOOK_COMMANDS_STUCK
    assert raised["translation_placeholders"] == {"server_id": SERVER, "count": "1", "commands": "HA no id"}
    issues.async_delete_issue.assert_not_called()


def test_a_command_the_run_can_rewrite_is_no_reason_for_the_repair(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _without_web_devices(monkeypatch)
    coordinator.data = {
        "webio_commands": {"HA m": {"cmdId": "9", "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }

    coordinator._report_unrewritable_webhooks(["HA m"])  # not migrated yet, but the run rewrites it

    issues.async_create_issue.assert_not_called()
    assert _deleted(issues) == {_STUCK_ID}


def test_the_repair_names_only_the_first_commands_but_counts_them_all(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _without_web_devices(monkeypatch)
    names = [f"HA m{i}\nx" for i in range(WEBHOOK_STUCK_LISTED + 5)]
    coordinator.data = {
        "webio_commands": {name: {"cmdId": "", "webioClass": "marker"} for name in names},
        "webio_devices": {},
    }

    coordinator._report_unrewritable_webhooks(names)

    placeholders = _raised(issues)[_STUCK_ID]["translation_placeholders"]
    assert placeholders["count"] == str(len(names))
    assert placeholders["commands"] == ", ".join(f"HA m{i} x" for i in range(WEBHOOK_STUCK_LISTED))


def test_a_night_without_unrewritable_commands_takes_the_repair_away(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _without_web_devices(monkeypatch)

    coordinator._report_unrewritable_webhooks([])

    issues.async_create_issue.assert_not_called()
    assert _deleted(issues) == {_STUCK_ID}


def test_a_comexio_that_has_to_restart_gets_a_repair_beside_the_notification(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _coordinator(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    coordinator.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    raised = _raised(issues)[_RESTART_ID]
    assert raised["config_entry_id"] == _ENTRY_ID
    assert raised["is_fixable"] is False
    assert raised["translation_key"] == ISSUE_WEBHOOK_RESTART
    assert raised["translation_placeholders"] == {"server_id": SERVER}


def test_the_restart_repair_goes_when_comexio_restarted_and_the_old_id_is_off(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    coordinator.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    assert coordinator.api.webhook_security.legacy_enabled is False
    assert {_RESTART_ID, _STUCK_ID} <= _deleted(issues)
    issues.async_create_issue.assert_not_called()


def _tick(coordinator: Any) -> str:
    return _run(coordinator._async_webhook_migration_tick())


def test_the_nightly_run_raises_the_stuck_repair_and_takes_it_back_once_the_command_can_be_rewritten(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _tick_coordinator(monkeypatch, cmd_id="", boot=NOW + timedelta(hours=1))

    assert _tick(coordinator) == _PENDING

    assert _raised(issues)[_STUCK_ID]["translation_placeholders"]["commands"] == "HA m"
    assert _STUCK_ID not in _deleted(issues)

    coordinator.data["webio_commands"]["HA m"]["cmdId"] = "9"  # a sync gave it its ids

    assert _tick(coordinator) == "done"  # Comexio booted after the write: nothing left to restart
    assert _STUCK_ID in _deleted(issues)
    assert _RESTART_ID not in _raised(issues)


def test_only_the_commands_the_run_cannot_rewrite_are_named(monkeypatch: pytest.MonkeyPatch, issues: MagicMock) -> None:
    coordinator = _without_web_devices(monkeypatch)
    coordinator.data = {
        "webio_commands": {
            "HA m": {"cmdId": "9", "webioClass": "marker"},
            "HA no id": {"cmdId": "", "webioClass": "marker"},
        },
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    coordinator._webio_range_targets = lambda: {"HA m": _TARGET, "HA no id": _TARGET}

    coordinator._report_unrewritable_webhooks(["HA m", "HA no id"])

    placeholders = _raised(issues)[_STUCK_ID]["translation_placeholders"]
    assert placeholders["count"] == "1"
    assert placeholders["commands"] == "HA no id"


def test_a_write_during_the_check_raises_the_restart_repair_again(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security

    async def _read_while_a_sync_writes(*_args: Any) -> Any:
        security.last_write = NOW + timedelta(hours=2)
        return _form()

    coordinator.api.get_webio_command_form = MagicMock(side_effect=_read_while_a_sync_writes)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert _RESTART_ID in _raised(issues)
    assert _STUCK_ID not in _deleted(issues)


def test_a_write_while_the_switch_is_saved_raises_the_restart_repair_and_keeps_both(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form())
    security = coordinator.api.webhook_security

    async def _save_while_a_sync_writes() -> None:
        security.last_write = NOW + timedelta(hours=2)

    security.async_save = MagicMock(side_effect=_save_while_a_sync_writes)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert _RESTART_ID in _raised(issues)
    assert _STUCK_ID not in _deleted(issues)  # the old id is on again: what held it still does


def test_a_comexio_that_restarted_but_whose_commands_differ_only_takes_the_restart_repair_back(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock
) -> None:
    coordinator = _verify_coordinator(monkeypatch, _form(parameter="/api/webhook/other"))

    assert _run(coordinator._async_finish_webhook_migration()) == _PENDING

    assert coordinator.api.webhook_security.legacy_enabled
    assert _deleted(issues) == {_RESTART_ID}


def test_the_restart_notification_and_its_repair_come_and_go_together(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    notification_id = f"comexio_{SERVER}_webhook_restart"
    waiting = _coordinator(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    waiting.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)

    assert _run(waiting._async_finish_webhook_migration()) == "needs_restart"

    assert notifications.async_create.call_args.kwargs["notification_id"] == notification_id
    assert _RESTART_ID in _raised(issues)

    restarted = _verify_coordinator(monkeypatch, _form())
    restarted.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)

    assert _run(restarted._async_finish_webhook_migration()) == "done"

    assert notifications.async_dismiss.call_args.args[1] == notification_id
    assert _RESTART_ID in _deleted(issues)


def test_a_save_cut_short_by_an_unload_puts_the_old_id_back(monkeypatch: pytest.MonkeyPatch, issues: MagicMock) -> None:
    coordinator = _without_web_devices(monkeypatch)
    security = coordinator.api.webhook_security
    security.async_save.side_effect = asyncio.CancelledError

    migration = coordinator._async_finish_webhook_migration()

    with pytest.raises(asyncio.CancelledError):
        _run(migration)

    assert security.legacy_enabled  # the store may still hold "on": memory must not run ahead of it


def test_a_command_put_back_takes_the_restart_request_back_at_the_nightly_run(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _tick_coordinator(monkeypatch, cmd_id="", boot=NOW + timedelta(hours=1))
    coordinator.config_entry = SimpleNamespace(entry_id=_ENTRY_ID)

    assert _tick(coordinator) == _PENDING  # a command without ids is left: "all rewritten" is not true

    assert _RESTART_ID in _deleted(issues)
    assert notifications.async_dismiss.call_args.args[1] == f"comexio_{SERVER}_webhook_restart"


def test_removing_the_entry_takes_both_repairs_and_the_restart_notification_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MagicMock()
    shown = MagicMock()
    monkeypatch.setattr(comexio_init, "ir", registry)
    monkeypatch.setattr(comexio_init, "persistent_notification", shown)
    monkeypatch.setattr(comexio_init, "async_remove_certificate_pushes", lambda *_: None)
    monkeypatch.setattr(comexio_init, "async_remove_legacy_plan_preview", MagicMock(side_effect=_none_coroutine))
    for store in ("saved_config_store", "webhook_security_store", "plan_preview_store"):
        monkeypatch.setattr(comexio_init, store, lambda *_: MagicMock(async_remove=_none_coroutine))
    entry = MagicMock(entry_id=_ENTRY_ID, data={CONF_SERVER_ID: SERVER})

    _run(comexio_init.async_remove_entry(MagicMock(), entry))

    assert {_STUCK_ID, _RESTART_ID} <= _deleted(registry)
    assert shown.async_dismiss.call_args.args[1] == f"comexio_{SERVER}_webhook_restart"


def test_a_failing_cleanup_does_not_keep_the_stores_of_the_removed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The webhook secret and the ledger must not stay on disk because a repair or the preview could not be dropped."""
    removed: list[str] = []

    def store(name: str) -> MagicMock:
        async def remove() -> None:
            removed.append(name)

        return MagicMock(async_remove=remove)

    async def failing_preview_cleanup(*_: Any) -> None:
        raise OSError("preview")

    monkeypatch.setattr(comexio_init, "ir", MagicMock())
    monkeypatch.setattr(comexio_init, "persistent_notification", MagicMock())
    monkeypatch.setattr(comexio_init, "async_remove_certificate_pushes", lambda *_: None)
    monkeypatch.setattr(comexio_init, "async_remove_legacy_plan_preview", failing_preview_cleanup)
    for name in ("saved_config_store", "webhook_security_store", "plan_preview_store"):
        monkeypatch.setattr(comexio_init, name, lambda *_, name=name: store(name))
    removal = comexio_init.async_remove_entry(MagicMock(), MagicMock(entry_id=_ENTRY_ID, data={CONF_SERVER_ID: SERVER}))

    with pytest.raises(OSError, match="preview"):
        _run(removal)

    assert removed == ["saved_config_store", "webhook_security_store", "plan_preview_store"]


def test_a_failing_certificate_push_cleanup_still_drops_the_repairs_and_the_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(Sourcery, #310) The repairs and the notification go before the cleanups that can fail."""
    registry = MagicMock()
    shown = MagicMock()

    def failing_push_cleanup(*_: Any) -> None:
        raise OSError("push")

    monkeypatch.setattr(comexio_init, "ir", registry)
    monkeypatch.setattr(comexio_init, "persistent_notification", shown)
    monkeypatch.setattr(comexio_init, "async_remove_certificate_pushes", failing_push_cleanup)
    for name in ("saved_config_store", "webhook_security_store", "plan_preview_store"):
        monkeypatch.setattr(comexio_init, name, lambda *_: MagicMock(async_remove=_none_coroutine))
    removal = comexio_init.async_remove_entry(MagicMock(), MagicMock(entry_id=_ENTRY_ID, data={CONF_SERVER_ID: SERVER}))

    with pytest.raises(OSError, match="push"):
        _run(removal)

    assert {_STUCK_ID, _RESTART_ID} <= _deleted(registry)
    assert shown.async_dismiss.call_args.args[1] == f"comexio_{SERVER}_webhook_restart"


async def _none_coroutine(*_: Any) -> None:
    return None
