"""Where the migration to per-command webhook ids ends (ej): nothing to migrate, commands it cannot rewrite, a restart.

Each way the old id could stay on for good either switches it off (no Web-IO device exists, nothing can push to it)
or tells the user with a repair what to do about it.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components import comexio as comexio_init
from custom_components.comexio import coordinator as coordinator_module, issue_owner
from custom_components.comexio.api import SystemInfoReading
from custom_components.comexio.const import (
    CONF_SERVER_ID,
    ISSUE_WEBHOOK_COMMANDS_STUCK,
    ISSUE_WEBHOOK_RESTART,
    WEBHOOK_RESTORE_ATTEMPTS,
    WEBHOOK_STUCK_LISTED,
)
from custom_components.comexio.coordinator import count_own_web_devices
from custom_components.comexio.system_info import SystemInfoState
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
        pytest.param({"web_devices": 1}, id="a-web-device-of-ours-exists"),
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


@pytest.mark.parametrize(
    ("devices", "expected", "switched_off"),
    [
        pytest.param(("Garage lights", "Heating"), "done", True, id="only-devices-of-the-user"),
        pytest.param(("Garage lights", "HomeAssistant [IO]"), _PENDING, False, id="a-device-of-ours-beside-them"),
    ],
)
def test_the_nightly_run_ignores_the_web_devices_of_the_user(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, devices: tuple[str, ...], expected: str, switched_off: bool
) -> None:
    """(ek) The count comes from the devices as Comexio lists them, not from a number set by hand."""
    raw_config = {"WebDevices": {str(i): {"Name": name} for i, name in enumerate(devices, 1)}}
    coordinator = _without_web_devices(monkeypatch, web_devices=count_own_web_devices(raw_config, "HomeAssistant"))
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_finish_webhook_migration()) == expected

    assert security.legacy_enabled is not switched_off


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
    coordinator._webhook_notices_restored = True  # a reading during the save had counted the restore as decided

    migration = coordinator._async_finish_webhook_migration()

    with pytest.raises(asyncio.CancelledError):
        _run(migration)

    assert security.legacy_enabled  # the store may still hold "on": memory must not run ahead of it
    assert coordinator._webhook_notices_restored is False  # and the restore is open again with it


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


def _after_a_start(monkeypatch: pytest.MonkeyPatch, *, boot: Any, written: Any = None) -> Any:
    """A coordinator of a freshly started Home Assistant: the repairs of the last night are gone, data is there."""
    coordinator = _coordinator(monkeypatch, boot=boot, written=written)
    coordinator._webhook_notices_restored = False
    coordinator._webhook_restore_failures = 0
    coordinator._last_poll_scraped = True  # the first poll of the start read the config ...
    coordinator._own_web_device_count = 0  # ... including `$WebDevices`
    coordinator.last_update_success = True  # ... and committed it
    coordinator._full_poll_running = False
    coordinator._polls_open = 0
    coordinator._poll_overlap = False
    return coordinator


def _stuck_command(coordinator: Any, *, cmd_id: str) -> None:
    coordinator.data = {
        "webio_commands": {"HA m": {"cmdId": cmd_id, "webioClass": "marker"}},
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    coordinator._webio_range_targets = lambda: {"HA m": _TARGET}


def test_a_start_raises_the_stuck_repair_again_without_waiting_for_the_nightly_run(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """(SH-226) The repair is not persistent: after a restart of Home Assistant it was gone until 01:30."""
    coordinator = _after_a_start(monkeypatch, boot=NOW)
    _stuck_command(coordinator, cmd_id="")

    coordinator._restore_webhook_migration_notices()

    assert _raised(issues)[_STUCK_ID]["translation_placeholders"]["commands"] == "HA m"
    assert _RESTART_ID not in _raised(issues)  # nothing was written, so there is no restart to ask for
    notifications.async_create.assert_not_called()
    assert _STUCK_ID not in _deleted(issues)
    assert coordinator._webhook_notices_restored is True
    coordinator.api.webhook_security.async_save.assert_not_awaited()


def test_a_start_with_a_stuck_command_and_a_restart_due_raises_the_stuck_repair_only(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """Not all commands are rewritten, so "restart Comexio" is not true yet, however the boot time compares."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    _stuck_command(coordinator, cmd_id="")

    coordinator._restore_webhook_migration_notices()

    assert _STUCK_ID in _raised(issues)
    assert _RESTART_ID not in _raised(issues)
    notifications.async_create.assert_not_called()
    assert _RESTART_ID in _deleted(issues)


def test_a_start_raises_the_restart_repair_and_notification_again_while_comexio_has_not_restarted(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)

    coordinator._restore_webhook_migration_notices()

    assert _raised(issues)[_RESTART_ID]["translation_placeholders"] == {"server_id": SERVER}
    assert notifications.async_create.call_args.kwargs["notification_id"] == f"comexio_{SERVER}_webhook_restart"
    assert _STUCK_ID in _deleted(issues)  # nothing stuck: an older stuck repair goes, like at the nightly run
    assert coordinator.api.webhook_security.legacy_enabled  # reporting only: nothing is switched off here
    coordinator.api.webhook_security.async_save.assert_not_awaited()


def test_a_start_asks_for_no_restart_once_comexio_restarted_after_the_last_write(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW + timedelta(hours=1), written=NOW)

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    # a reload of the entry comes through here, too: the request an earlier run raised is out of date
    assert _RESTART_ID in _deleted(issues)
    assert notifications.async_dismiss.call_args.args[1] == f"comexio_{SERVER}_webhook_restart"


def test_a_start_asks_for_no_restart_while_a_command_still_has_to_be_rewritten(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """ "All commands are rewritten, restart Comexio" is not true then: the nightly run does the rewriting first."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    _stuck_command(coordinator, cmd_id="9")

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()  # neither the restart repair nor the stuck one: it can be rewritten
    notifications.async_create.assert_not_called()
    assert _RESTART_ID in _deleted(issues)  # one raised before a sync put a command back is out of date


def test_a_start_raises_nothing_once_the_old_id_is_off(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    _stuck_command(coordinator, cmd_id="")
    coordinator.api.webhook_security.legacy_enabled = False

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    assert coordinator._webhook_notices_restored is True  # decided, nothing to come back for


def test_a_start_with_the_old_id_off_takes_back_what_a_run_cut_short_left_standing(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """(Sourcery, #313) A run that persisted the switch-off and was cancelled before it cleared its notices: a reload
    of the entry (no restart of Home Assistant) keeps the repair and the notification, so the reload takes them back."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    coordinator.api.webhook_security.legacy_enabled = False

    coordinator._restore_webhook_migration_notices()

    assert {_STUCK_ID, _RESTART_ID} <= _deleted(issues)
    assert notifications.async_dismiss.call_args.args[1] == f"comexio_{SERVER}_webhook_restart"
    issues.async_create_issue.assert_not_called()
    assert coordinator._webhook_notices_restored is True


def test_a_save_that_fails_while_a_reading_saw_the_old_id_off_opens_the_restore_again(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """(Sourcery, #313) The old id is off in memory while the switch-off is saved. A reading in that window counts the
    restore as decided; the failed save puts the old id back on, so the restore has to run again."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    security = coordinator.api.webhook_security
    observed: list[bool] = []

    async def save_met_by_a_reading(*_: Any) -> None:
        coordinator._restore_webhook_migration_notices()  # the uptime tick, while the store is written
        observed.append(coordinator._webhook_notices_restored)
        raise OSError("disk full")

    security.async_save.side_effect = save_met_by_a_reading

    assert _run(coordinator._async_switch_legacy_webhook_id_off(security)) is False

    assert observed == [True]  # the reading did latch, or the check below proves nothing
    assert security.legacy_enabled
    assert coordinator._webhook_notices_restored is False

    coordinator._restore_webhook_migration_notices()  # the next reading, with the old id on again

    assert _RESTART_ID in _raised(issues)
    assert coordinator._webhook_notices_restored is True


def test_a_save_that_goes_through_leaves_the_restore_decided(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    security = coordinator.api.webhook_security

    coordinator._webhook_notices_restored = True  # a restore that was decided before the night's switch-off

    assert _run(coordinator._async_switch_legacy_webhook_id_off(security)) is True

    assert security.legacy_enabled is False
    assert coordinator._webhook_notices_restored is True  # only a save that failed opens it again
    issues.async_create_issue.assert_not_called()


_POLL_RESULT = {"sentinel": 1}


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(None, id="returns"),
        pytest.param(OSError("save"), id="raises"),
        pytest.param(asyncio.CancelledError(), id="cancelled"),
    ],
)
def test_a_poll_counts_as_open_up_to_the_moment_it_returns_its_data(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock, failure: BaseException | None
) -> None:
    """(Sourcery, #313) After the fetch the poll still awaits (settling the old id, saving the config) while its scrape
    flags already describe data that `self.data` does not hold yet: the stocktake may not run in that stretch."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    seen: list[tuple[int, bool]] = []

    async def fetch_then_settle() -> dict[str, Any]:
        coordinator._full_poll_running = False  # the fetch is over, the awaits after it follow
        security = coordinator.api.webhook_security
        seen.append((coordinator._polls_open, coordinator._webhook_stocktake_readable(security)))
        if failure is not None:
            raise failure
        return _POLL_RESULT

    coordinator._async_run_update = fetch_then_settle

    poll = coordinator._async_update_data()
    if failure is None:
        assert _run(poll) is _POLL_RESULT  # what Home Assistant stores is what the body returned
    else:
        with pytest.raises(type(failure)):
            _run(poll)

    assert seen == [(1, False)]  # open, and not readable although every other guard is open
    assert coordinator._polls_open == 0  # closed again on every way out, a cancel included
    assert coordinator._webhook_stocktake_readable(coordinator.api.webhook_security) is True


def test_a_poll_that_ends_while_another_is_still_under_way_does_not_open_the_stocktake(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """(silent-failure-hunter, #313) A direct refresh can overlap the scheduled poll: the one that ends first may not
    count the other one out."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    security = coordinator.api.webhook_security
    readable_when_first_ended: list[bool] = []

    async def overlapping_polls() -> None:
        release = asyncio.Event()

        async def quick() -> dict[str, Any]:
            return {}

        async def slow() -> dict[str, Any]:
            await release.wait()
            return {}

        coordinator._async_run_update = slow
        slow_poll = asyncio.ensure_future(coordinator._async_update_data())
        await asyncio.sleep(0)  # the slow poll is under way
        coordinator._async_run_update = quick
        await coordinator._async_update_data()  # the second poll ends first
        readable_when_first_ended.append(coordinator._webhook_stocktake_readable(security))
        release.set()
        await slow_poll

    _run(overlapping_polls())

    assert readable_when_first_ended == [False]
    assert coordinator._polls_open == 0
    # Both ended, but the scrape flags may belong to the other poll than `self.data`: shut until one ran alone.
    assert coordinator._webhook_stocktake_readable(security) is False


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(None, id="returns"),
        pytest.param(OSError("poll"), id="raises"),
        pytest.param(asyncio.CancelledError(), id="cancelled"),
    ],
)
def test_the_restore_waits_after_overlapping_polls_until_a_poll_runs_alone(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock, failure: BaseException | None
) -> None:
    """(Sourcery, #313) After overlapping polls the flags and `self.data` need not describe the same poll, however the
    second one ended: the restore decides nothing and latches nothing until a poll that started alone."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    _healthy_after_a_rewrite(coordinator)
    security = coordinator.api.webhook_security

    async def overlapping_polls() -> None:
        release = asyncio.Event()

        async def slow() -> dict[str, Any]:
            await release.wait()
            return {}

        async def quick() -> dict[str, Any]:
            if failure is not None:
                raise failure
            return {}

        coordinator._async_run_update = slow
        slow_poll = asyncio.ensure_future(coordinator._async_update_data())
        await asyncio.sleep(0)  # the slow poll started alone and is under way
        coordinator._async_run_update = quick
        await asyncio.gather(coordinator._async_update_data(), return_exceptions=True)  # overlaps it, ends first
        release.set()
        await slow_poll

    _run(overlapping_polls())

    assert coordinator._polls_open == 0
    assert coordinator._webhook_stocktake_readable(security) is False
    coordinator._restore_webhook_migration_notices()
    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    assert coordinator._webhook_notices_restored is False

    async def poll_alone() -> None:
        coordinator._async_run_update = _async_empty_poll
        await coordinator._async_update_data()

    _run(poll_alone())
    coordinator._restore_webhook_migration_notices()

    assert _RESTART_ID in _raised(issues)
    assert coordinator._webhook_notices_restored is True


async def _async_empty_poll() -> dict[str, Any]:
    return {}


def test_an_unknown_boot_time_waits_instead_of_asking_for_a_restart_that_may_not_be_due(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """`_restart_pending` takes an unknown boot time for a restart still to come: before the first uptime reading
    that would raise the repair on every start, so nothing is decided until Comexio's boot time is known."""
    coordinator = _after_a_start(monkeypatch, boot=None, written=NOW)

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    assert coordinator._webhook_notices_restored is False

    coordinator.system_info = SimpleNamespace(boot_time=NOW + timedelta(hours=1))  # the first reading came

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()  # restarted after the write: nothing to ask for
    assert coordinator._webhook_notices_restored is True


@pytest.mark.parametrize(
    ("data", "unloading"),
    [pytest.param({}, False, id="no-data-yet"), pytest.param({"webio_commands": {}}, True, id="entry-unloading")],
)
def test_a_start_without_data_or_during_an_unload_raises_nothing_and_stays_open(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock, data: dict[str, Any], unloading: bool
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    coordinator.data = data
    coordinator.unloading = unloading

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    assert coordinator._webhook_notices_restored is False

    coordinator.data = {"webio_commands": {}, "webio_devices": {}}  # the data came / the unload ended
    coordinator.unloading = False

    coordinator._restore_webhook_migration_notices()

    assert _RESTART_ID in _raised(issues)  # "stays open" means the next reading does it


def test_a_poll_that_did_not_scrape_the_config_decides_nothing_until_a_later_reading(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """Its `webio_commands` may be empty: the stuck repair would be taken away and a restart asked for wrongly."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    coordinator._last_poll_scraped = False
    coordinator.data = {"webio_commands": {}, "webio_devices": {}}

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    issues.async_delete_issue.assert_not_called()  # a stuck repair of the night before stays
    notifications.async_create.assert_not_called()
    notifications.async_dismiss.assert_not_called()
    assert coordinator._webhook_notices_restored is False

    coordinator._last_poll_scraped = True  # the next full poll scraped it

    coordinator._restore_webhook_migration_notices()

    assert _RESTART_ID in _raised(issues)


def _poll_in_flight(coordinator: Any) -> None:
    coordinator._polls_open = 1  # counted by _async_update_data for the whole poll ...
    coordinator._full_poll_running = True  # ... and, for its fetch, this one


def _poll_awaiting_its_commit(coordinator: Any) -> None:
    coordinator._polls_open = 1  # the fetch is over (`_full_poll_running` cleared), settling and saving still await


def _poll_failed(coordinator: Any) -> None:
    coordinator.last_update_success = False


def _web_devices_unread(coordinator: Any) -> None:
    coordinator._own_web_device_count = None  # reset at the start of a poll, set once `$WebDevices` was read


def _commands_not_decoded(coordinator: Any) -> None:
    coordinator.api.webhook_security.migrated = {"HA m"}  # the ledger knows a command ...
    coordinator.data = {"webio_commands": {}, "webio_devices": {}}  # ... the data has none


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param(_poll_in_flight, id="poll-under-way"),
        pytest.param(_poll_awaiting_its_commit, id="poll-awaiting-its-commit"),
        pytest.param(_poll_failed, id="poll-failed"),
        pytest.param(_web_devices_unread, id="web-devices-not-read"),
        pytest.param(_commands_not_decoded, id="ledger-without-stored-commands"),
    ],
)
def test_a_stocktake_that_cannot_be_relied_on_decides_nothing_and_stays_open(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock, unreadable: Any
) -> None:
    """(Sourcery, #313) A poll sets its scrape flags before it commits; incomplete commands would clear a stuck
    repair and dismiss a restart request that is due: nothing is touched, and the next reading decides."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    unreadable(coordinator)

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    issues.async_delete_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    notifications.async_dismiss.assert_not_called()
    assert coordinator._webhook_notices_restored is False

    _healthy_after_a_rewrite(coordinator)  # the poll committed / the next one read everything

    coordinator._restore_webhook_migration_notices()

    assert _RESTART_ID in _raised(issues)  # nothing latched: the next reading decides, with a ledger and commands
    notifications.async_create.assert_called_once()
    assert coordinator._webhook_notices_restored is True


def _healthy_after_a_rewrite(coordinator: Any) -> None:
    """Every guard open, the ledger names a command and the data holds it: the state after the first nightly rewrite."""
    coordinator._full_poll_running = False
    coordinator._polls_open = 0
    coordinator.last_update_success = True
    coordinator._own_web_device_count = 0
    coordinator.api.webhook_security.migrated = {"HA m"}
    _stuck_command(coordinator, cmd_id="9")


def test_a_ledger_with_a_command_that_the_data_holds_does_not_block_the_restore(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """The readiness check may not shut every start after the first rewrite out (a ledger is not an empty store)."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    _healthy_after_a_rewrite(coordinator)

    coordinator._restore_webhook_migration_notices()

    assert _RESTART_ID in _raised(issues)
    notifications.async_create.assert_called_once()
    assert coordinator._webhook_notices_restored is True


def test_a_start_without_a_webhook_store_raises_nothing_and_stays_open(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    coordinator.api.webhook_security = None

    coordinator._restore_webhook_migration_notices()

    issues.async_create_issue.assert_not_called()
    notifications.async_create.assert_not_called()
    assert coordinator._webhook_notices_restored is False


def test_the_repairs_are_raised_again_once_per_start_not_at_every_uptime_reading(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """A user who dismissed the notification is not given it again every five minutes."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)

    coordinator._restore_webhook_migration_notices()
    assert _RESTART_ID in _raised(issues)  # the first reading did raise it, or the second one proves nothing
    notifications.async_create.assert_called_once()
    notifications.async_create.reset_mock()
    issues.async_create_issue.reset_mock()
    coordinator._restore_webhook_migration_notices()

    notifications.async_create.assert_not_called()
    issues.async_create_issue.assert_not_called()


def test_the_first_uptime_reading_of_a_start_raises_the_repairs_again(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """The real tick gives the boot time and calls the restore, with the boot time it just stored."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: NOW)
    coordinator = _after_a_start(monkeypatch, boot=None, written=NOW)
    coordinator.system_info = SystemInfoState()
    coordinator.api.get_system_info = AsyncMock(
        return_value=SystemInfoReading(None, timedelta(hours=1), "unused", None)
    )

    _run(coordinator._async_system_info_tick())

    assert coordinator.system_info.boot_time == NOW - timedelta(hours=1)  # before the last write: restart is due
    assert _RESTART_ID in _raised(issues)
    assert notifications.async_create.call_args.kwargs["notification_id"] == f"comexio_{SERVER}_webhook_restart"


def test_a_failing_restore_is_logged_does_not_end_the_uptime_reading_and_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """(Sourcery, #313) A transient failure must not leave the repairs missing until the nightly run."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: NOW)
    coordinator = _after_a_start(monkeypatch, boot=None, written=NOW)
    coordinator.system_info = SystemInfoState()
    coordinator.api.get_system_info = AsyncMock(
        return_value=SystemInfoReading(None, timedelta(hours=1), "unused", None)
    )
    coordinator._webio_range_targets = MagicMock(side_effect=KeyError("Name"))  # what the stocktake trips over

    _run(coordinator._async_system_info_tick())

    assert coordinator.system_info.boot_time == NOW - timedelta(hours=1)  # the uptime was applied before
    assert caplog.text.count("could not be raised again") == 1
    assert coordinator._webhook_notices_restored is False
    issues.async_create_issue.assert_not_called()

    coordinator._webio_range_targets = dict  # the failure was transient
    _run(coordinator._async_system_info_tick())

    assert _RESTART_ID in _raised(issues)  # the next reading did what the first one could not
    notifications.async_create.assert_called_once()
    assert coordinator._webhook_notices_restored is True


def test_a_restore_that_fails_every_time_gives_up_after_its_attempts_instead_of_logging_for_ever(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """(silent-failure-hunter, #313) A deterministic failure would log a traceback every five minutes."""
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", MagicMock())
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: NOW)
    coordinator = _after_a_start(monkeypatch, boot=None, written=NOW)
    coordinator.system_info = SystemInfoState()
    coordinator.api.get_system_info = AsyncMock(
        return_value=SystemInfoReading(None, timedelta(hours=1), "unused", None)
    )
    coordinator._webio_range_targets = MagicMock(side_effect=KeyError("Name"))

    for _ in range(WEBHOOK_RESTORE_ATTEMPTS):
        assert coordinator._webhook_notices_restored is False
        _run(coordinator._async_system_info_tick())

    assert coordinator._webhook_notices_restored is True  # given up: the nightly run raises what is due
    assert caplog.text.count("could not be raised again") == WEBHOOK_RESTORE_ATTEMPTS
    failures = [r.getMessage() for r in caplog.records if "could not be raised again" in r.getMessage()]
    assert "giving up" in failures[-1]
    assert not any("giving up" in message for message in failures[:-1])
    calls = coordinator._webio_range_targets.call_count

    _run(coordinator._async_system_info_tick())

    assert coordinator._webio_range_targets.call_count == calls  # no further attempt
    assert caplog.text.count("could not be raised again") == WEBHOOK_RESTORE_ATTEMPTS
    issues.async_create_issue.assert_not_called()


def test_a_restore_that_failed_after_its_first_repair_is_tried_again_without_doubling_it(
    monkeypatch: pytest.MonkeyPatch, issues: MagicMock, notifications: MagicMock
) -> None:
    """The repairs are created by id, so the second run replaces what the first one raised before it failed."""
    coordinator = _after_a_start(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)
    real_notify = coordinator._notify_webhook_restart
    failures = [OSError("notification")]

    def failing_once() -> None:
        if failures:
            raise failures.pop()
        real_notify()

    coordinator._notify_webhook_restart = failing_once

    with pytest.raises(OSError, match="notification"):
        coordinator._restore_webhook_migration_notices()
    assert coordinator._webhook_notices_restored is False

    coordinator._restore_webhook_migration_notices()

    assert coordinator._webhook_notices_restored is True
    assert [call.args[2] for call in issues.async_create_issue.call_args_list] == [_RESTART_ID]
    notifications.async_create.assert_called_once()


async def _none_coroutine(*_: Any) -> None:
    return None
