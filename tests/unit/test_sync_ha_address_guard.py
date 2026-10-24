"""A sync never writes a loopback HA address into the Web-IO devices (#177).

The resolver falls back to the loopback local IP only when nothing else is usable. A class
without a device would be created with it, so the sync stops before its first write; existing
devices keep their stored address: no Fast-Track (it deletes and recreates the device) and no
IP mismatch in the audit, so no repair offers an address update that cannot work.
"""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio import button as button_module
from custom_components.comexio.button import ComexioSyncButton, _SyncContext
from custom_components.comexio.const import (
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    WEBIO_CLASSES,
    WebioClass,
)
from custom_components.comexio.coordinator import _device_ip_mismatch
from custom_components.comexio.ha_address import HaAddressResolver, is_loopback_address

LOOPBACK = "127.0.0.1:8123"
REACHABLE = "192.168.0.110:8123"
_CLASS_RESULT = {
    "added": 0,
    "removed": 0,
    "updated": 0,
    "renamed": 0,
    "updated_ip": False,
    "skipped_creates": 0,
    "debris_removed": 0,
    "recreated": False,
}


@pytest.mark.parametrize(
    ("address", "loopback"),
    [
        (LOOPBACK, True),
        ("127.0.1.1:8123", True),
        ("localhost:8123", True),
        ("[::1]:8123", True),
        ("[::ffff:127.0.0.1]:8123", True),
        ("0.0.0.0:8123", True),
        ("[::]:8123", True),
        (REACHABLE, False),
        ("ha.example.lan:8123", False),
        ("[fd00::10]:8123", False),
        ("", False),
    ],
)
def test_is_loopback_address(address: str, loopback: bool) -> None:
    assert is_loopback_address(address) is loopback


def _button(ha_address: str, dev_id: str | None = "7") -> ComexioSyncButton:
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="e1", data={}, options={}),
        api=SimpleNamespace(
            login=AsyncMock(return_value=True),
            get_webio_device_info=AsyncMock(return_value=dev_id),
            webio_device_delete_error=AsyncMock(return_value=None),
            generate_webio_json=MagicMock(return_value='{"commands": [{"name": "HA M1"}]}'),
        ),
        webio_source_data=MagicMock(return_value={}),
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
        async_ha_address=AsyncMock(return_value=ha_address),
        ha_address=HaAddressResolver(MagicMock()),
        required_webio_classes=list(WEBIO_CLASSES),
        last_audit_results={},
        cancel_sync=False,
        data={},
        async_set_updated_data=MagicMock(),
    )
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = coordinator  # type: ignore[assignment]
    button.hass = MagicMock()
    button.server_id = "cx1"
    button._async_acquire_sync_lock = AsyncMock(return_value=True)  # type: ignore[method-assign]
    button._finalize_sync = AsyncMock()  # type: ignore[method-assign]
    button._sync_class = AsyncMock(return_value=_CLASS_RESULT)  # type: ignore[method-assign]
    button._handle_cleanup_entities = AsyncMock()  # type: ignore[method-assign]
    button._wire_created_pairs = AsyncMock(return_value=[])  # type: ignore[method-assign]
    button._wire_trigger_pairs = AsyncMock(return_value=[])  # type: ignore[method-assign]
    button._wire_knx_full = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return button


def _press(button: ComexioSyncButton, action: str = "full_sync") -> Any:
    with patch.object(button_module, "_LOGGER") as logger:
        asyncio.run(button.async_handle_press(action))
    return logger


def _ctx(button: ComexioSyncButton, ha_address: str, action: str = "full_sync") -> _SyncContext:
    return _SyncContext(
        api=button.coordinator.api,
        action=action,
        ha_address=ha_address,
        webio_devices_audit={},
        start_time=MagicMock(),
        class_names={cls: f"HomeAssistant {cls}" for cls in WEBIO_CLASSES},
        update_status=MagicMock(),
    )


@pytest.mark.parametrize("action", ["full_sync", "create_missing", "update_renames"])
def test_a_sync_refuses_to_create_a_device_with_a_loopback_address(action: str) -> None:
    button = _button(LOOPBACK, dev_id=None)  # no device: every action would force a recreate
    logger = _press(button, action)

    button._sync_class.assert_not_awaited()  # nothing written: no device, no class, no command
    assert button.coordinator.sync_error is True
    assert "Comexio cannot reach" in button.coordinator.sync_progress_text
    assert "Settings → System → Network" in button.coordinator.sync_progress_text
    # Reported like a failed login: one error line, no traceback.
    logger.exception.assert_not_called()
    assert logger.error.call_args.args[0] == "[%s] Sync not started: %s"


@pytest.mark.parametrize(("ha_address", "dev_id"), [(LOOPBACK, "7"), (REACHABLE, None), (REACHABLE, "7")])
def test_a_sync_runs_when_no_device_gets_a_loopback_address(ha_address: str, dev_id: str | None) -> None:
    """Existing devices keep their stored address; a reachable address may create new ones."""
    button = _button(ha_address, dev_id)
    ctx = _ctx(button, ha_address)
    dev_ids = dict.fromkeys(WEBIO_CLASSES, dev_id)

    asyncio.run(button._sync_all_classes(ctx, {}, dev_ids))

    assert button._sync_class.await_count == len(WEBIO_CLASSES)


@pytest.mark.parametrize(("quickbus_dev_id", "synced"), [(None, False), ("9", True)])
def test_the_quickbus_class_is_synced_only_while_required_or_present(quickbus_dev_id: str | None, synced: bool) -> None:
    """No Quickbus IO: no empty [QB] class is created, but a leftover [QB] device is still synced."""
    button = _button(REACHABLE)
    button.coordinator.required_webio_classes = (WebioClass.MARKER, WebioClass.IO)
    ctx = _ctx(button, REACHABLE)
    dev_ids = {**dict.fromkeys(WEBIO_CLASSES, "7"), WebioClass.QUICKBUS: quickbus_dev_id}

    asyncio.run(button._sync_all_classes(ctx, {}, dev_ids))

    synced_classes = {call.args[1] for call in button._sync_class.await_args_list}
    assert (WebioClass.QUICKBUS in synced_classes) is synced
    assert {WebioClass.MARKER, WebioClass.IO} <= synced_classes


@pytest.mark.parametrize(
    ("action", "handler"),
    [
        ("cleanup_entities", "_handle_cleanup_entities"),
        ("function_plan_add_missing", "_wire_created_pairs"),
        ("knx_bridge_add_missing", "_wire_knx_full"),
    ],
)
def test_actions_without_device_writes_still_run_with_a_loopback_address(action: str, handler: str) -> None:
    button = _button(LOOPBACK, dev_id=None)
    _press(button, action)

    assert button.coordinator.sync_error is False
    button._sync_class.assert_not_awaited()
    for name in ("_handle_cleanup_entities", "_wire_created_pairs", "_wire_knx_full"):
        assert getattr(button, name).await_count == (name == handler), name


def test_a_failed_login_is_still_reported_without_a_traceback() -> None:
    button = _button(REACHABLE)
    button.coordinator.api.login = AsyncMock(return_value=False)
    button.coordinator.api.last_login_error = "rejected"
    logger = _press(button)

    logger.exception.assert_not_called()
    assert logger.error.call_args.args[0] == "[%s] Sync not started: %s"
    assert button.coordinator.sync_error is True


def test_an_unexpected_error_still_logs_its_traceback() -> None:
    button = _button(REACHABLE)
    button._sync_class = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    logger = _press(button)

    logger.exception.assert_called_once_with("[%s] Sync failed", "cx1")
    assert button.coordinator.sync_error is True


_FAST_TRACK_MISSING = [{}] * (SYNC_DURATION_RECREATE // SYNC_DURATION_WRITE + 1)  # ETA above the threshold
_FAST_TRACK_ORPHANS = [{}] * (SYNC_DURATION_RECREATE // SYNC_DURATION_DELETE + 1)


def test_no_fast_track_with_a_loopback_address() -> None:
    """Fast-Track deletes the device and recreates it — with the address Comexio cannot reach."""
    button = _button(LOOPBACK)
    ctx = _ctx(button, LOOPBACK)
    with patch.object(button_module, "_LOGGER") as logger:
        action = asyncio.run(
            button._decide_effective_action(ctx, WEBIO_CLASSES[0], "7", _FAST_TRACK_MISSING, [], [], [], False)
        )

    assert action == "full_sync"
    button.coordinator.api.webio_device_delete_error.assert_not_awaited()  # the device was not deleted
    assert LOOPBACK in logger.warning.call_args.args


def test_fast_track_with_a_reachable_address() -> None:
    button = _button(REACHABLE)
    ctx = _ctx(button, REACHABLE)
    with patch.object(button_module, "webio_still_present", AsyncMock(return_value=None)):
        action = asyncio.run(
            button._decide_effective_action(ctx, WEBIO_CLASSES[0], "7", _FAST_TRACK_MISSING, [], [], [], False)
        )

    assert action == "recreate"
    button.coordinator.api.webio_device_delete_error.assert_awaited_once_with("7")


@pytest.mark.parametrize("cls", [WebioClass.QUICKBUS, WebioClass.KNX])
def test_no_fast_track_for_a_class_with_no_wanted_command(cls: WebioClass) -> None:
    """Quickbus with no device selected, every KNX object ignored: the device is synced (orphans
    removed), never rebuilt from an empty template."""
    button = _button(REACHABLE)
    button.coordinator.api.generate_webio_json.return_value = '{"commands": []}'
    ctx = _ctx(button, REACHABLE)
    with patch.object(button_module, "webio_still_present", AsyncMock(return_value=None)):
        action = asyncio.run(button._decide_effective_action(ctx, cls, "7", [], [], [], _FAST_TRACK_ORPHANS, False))

    assert action == "full_sync"
    button.coordinator.api.webio_device_delete_error.assert_not_awaited()


@pytest.mark.parametrize(
    ("ha_address", "stored", "updated"),
    [
        (REACHABLE, "192.168.0.111:8123", True),  # internal_url changed after the last audit
        (LOOPBACK, "192.168.0.111:8123", False),  # the stored address is kept, never replaced by loopback
        (REACHABLE, REACHABLE, False),
    ],
)
def test_a_sync_judges_the_ip_against_its_own_address(ha_address: str, stored: str, updated: bool) -> None:
    """(Review) the last audit's ip_mismatch was used: a changed internal_url, or one that fell back
    to loopback since that audit, was handled as if nothing had changed."""
    button = _button(ha_address)
    ctx = _ctx(button, ha_address)
    # The last audit judged against another address: it says the opposite of what applies now.
    ctx.webio_devices_audit = {cls: {"device_ip": stored, "ip_mismatch": not updated} for cls in WEBIO_CLASSES}

    asyncio.run(button._sync_all_classes(ctx, {}, dict.fromkeys(WEBIO_CLASSES, "7")))

    assert _ip_mismatch_per_class(button) == dict.fromkeys(WEBIO_CLASSES, updated)


def test_a_class_without_a_device_gets_no_ip_update() -> None:
    button = _button(REACHABLE)
    ctx = _ctx(button, REACHABLE)
    ctx.webio_devices_audit = {cls: {"device_ip": "192.168.0.111:8123"} for cls in WEBIO_CLASSES}
    dev_ids: dict[str, str | None] = {**dict.fromkeys(WEBIO_CLASSES, "7"), WEBIO_CLASSES[0]: None}

    asyncio.run(button._sync_all_classes(ctx, {}, dev_ids))

    assert _ip_mismatch_per_class(button) == {cls: dev_ids[cls] is not None for cls in WEBIO_CLASSES}


def _ip_mismatch_per_class(button: ComexioSyncButton) -> dict[str, bool]:
    """dev_ip_mismatch of every _sync_class call, by class (bound by name, not by position)."""
    signature = inspect.signature(ComexioSyncButton._sync_class)
    bound = (signature.bind(button, *call.args, **call.kwargs) for call in button._sync_class.await_args_list)
    return {args.arguments["cls"]: args.arguments["dev_ip_mismatch"] for args in bound}


def test_the_audit_reports_no_ip_mismatch_for_a_loopback_address() -> None:
    """Otherwise the repair would keep offering an address update the sync refuses."""
    resolver = HaAddressResolver(MagicMock())
    assert asyncio.run(_device_ip_mismatch(resolver, LOOPBACK, "192.168.0.110:8123", "7")) is False
    assert asyncio.run(_device_ip_mismatch(resolver, REACHABLE, "192.168.0.111:8124", "7")) is True
