"""Webhook address modes and the HTTP/HTTPS protocol of the Web-IO commands (#165).

The resolver returns the external / custom URL's address directly and falls back to the automatic
address when that URL is unusable; the protocol follows the URL scheme. The audit compares the
protocol the commands were last written with against the current one, the sync rewrites them and
records the protocol, and the nightly range check reads it back from each analog command's form.
"""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import WebioCommandForm
from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
import pytest

from custom_components.comexio import coordinator as coordinator_module, repairs as repairs_module
from custom_components.comexio.button import ComexioSyncButton, _protocol_only_items, _SyncContext
from custom_components.comexio.const import (
    CONF_WEBHOOK_ADDRESS_MODE,
    CONF_WEBHOOK_URL,
    DOMAIN,
    ICON_NETWORK,
    RANGE_CHECK_CHECKED,
    RANGE_CHECK_CORRECTION_FAILED,
    RANGE_CHECK_FAILED,
    RANGE_CHECK_FIXED,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    WEBHOOK_ADDRESS_AUTO,
    WEBHOOK_ADDRESS_CUSTOM,
    WEBHOOK_ADDRESS_EXTERNAL,
    WEBIO_CLASSES,
)
from custom_components.comexio.coordinator import ComexioCoordinator, _protocol_audit_items
from custom_components.comexio.ha_address import (
    HaAddressResolver,
    WebhookTarget,
    webhook_url_error,
    webhook_url_target,
)
from custom_components.comexio.options_flow import ComexioOptionsFlow
from custom_components.comexio.repairs import ComexioRepairFlow

MARKER, IO = WEBIO_CLASSES[0], WEBIO_CLASSES[1]


def _hass(internal_url: str | None = None, external_url: str | None = None, use_ssl: bool | None = None) -> Any:
    api = None if use_ssl is None else SimpleNamespace(use_ssl=use_ssl, port=8123)
    return SimpleNamespace(config=SimpleNamespace(internal_url=internal_url, external_url=external_url, api=api))


# --- URL parsing ---


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://ha.example.com", WebhookTarget("ha.example.com:443", True)),
        ("https://ha.example.com/", WebhookTarget("ha.example.com:443", True)),
        ("http://ha.example.com", WebhookTarget("ha.example.com:80", False)),
        ("https://ha.example.com:8443", WebhookTarget("ha.example.com:8443", True)),
        ("  http://192.0.2.10:8123  ", WebhookTarget("192.0.2.10:8123", False)),
        ("https://[fd00::10]:8123", WebhookTarget("[fd00::10]:8123", True)),
        ("HTTPS://HA.Example.com", WebhookTarget("ha.example.com:443", True)),
    ],
)
def test_a_webhook_url_gives_its_address_and_protocol(url: str, target: WebhookTarget) -> None:
    assert webhook_url_target(url) == target
    assert webhook_url_error(url) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "ha.example.com",  # no scheme
        "ftp://ha.example.com",
        "https://",
        "https://user:secret@ha.example.com",
        "https://user@ha.example.com",
        "https://ha.example.com/api",
        "https://ha.example.com?x=1",
        "https://ha.example.com#top",
        "https://ha.example.com:99999",
        "https://ha.example.com:port",
    ],
)
def test_an_unusable_webhook_url_is_invalid(url: str | None) -> None:
    assert webhook_url_target(url) is None
    assert webhook_url_error(url) == "invalid_url"


@pytest.mark.parametrize("url", ["https://localhost", "http://127.0.0.1:8123", "http://0.0.0.0:8123", "http://[::1]"])
def test_a_loopback_webhook_url_is_refused(url: str) -> None:
    assert webhook_url_error(url) == "loopback_url"


# --- resolver ---


def test_the_external_mode_returns_the_external_url_without_searching() -> None:
    resolver = HaAddressResolver(_hass(external_url="https://ha.example.com"), WEBHOOK_ADDRESS_EXTERNAL)
    resolver._async_get = AsyncMock()  # type: ignore[method-assign]

    assert asyncio.run(resolver.async_target("192.0.2.10:8123")) == WebhookTarget("ha.example.com:443", True)
    assert asyncio.run(resolver.async_get()) == "ha.example.com:443"
    resolver._async_get.assert_not_awaited()


def test_the_custom_mode_returns_its_own_url() -> None:
    resolver = HaAddressResolver(_hass(), WEBHOOK_ADDRESS_CUSTOM, "http://ha.example.com:8123")
    assert asyncio.run(resolver.async_target()) == WebhookTarget("ha.example.com:8123", False)


@pytest.mark.parametrize(
    ("mode", "external_url", "url"),
    [
        (WEBHOOK_ADDRESS_EXTERNAL, None, None),
        (WEBHOOK_ADDRESS_EXTERNAL, "https://localhost", None),
        (WEBHOOK_ADDRESS_CUSTOM, "https://ha.example.com", None),
        (WEBHOOK_ADDRESS_CUSTOM, None, "https://ha.example.com/path"),
    ],
)
def test_an_unusable_fixed_url_falls_back_to_the_automatic_address_and_warns_once(
    caplog: pytest.LogCaptureFixture, mode: str, external_url: str | None, url: str | None
) -> None:
    resolver = HaAddressResolver(_hass(internal_url="http://192.0.2.10:8123", external_url=external_url), mode, url)
    resolver._async_get = AsyncMock(return_value="192.0.2.10:8123")  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.ha_address"):
        assert asyncio.run(resolver.async_target()) == WebhookTarget("192.0.2.10:8123", False)
        assert asyncio.run(resolver.async_target()) == WebhookTarget("192.0.2.10:8123", False)

    notices = [r for r in caplog.records if "has no usable URL" in r.getMessage()]
    assert [r.levelname for r in notices] == ["WARNING", "DEBUG"]
    assert repr(mode) in notices[0].getMessage()


def test_a_fixed_url_that_works_again_warns_again_when_it_breaks(caplog: pytest.LogCaptureFixture) -> None:
    hass = _hass(external_url=None)
    resolver = HaAddressResolver(hass, WEBHOOK_ADDRESS_EXTERNAL)
    resolver._async_get = AsyncMock(return_value="192.0.2.10:8123")  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.ha_address"):
        asyncio.run(resolver.async_target())
        hass.config.external_url = "https://ha.example.com"
        asyncio.run(resolver.async_target())
        hass.config.external_url = None
        asyncio.run(resolver.async_target())

    notices = [r.levelname for r in caplog.records if "has no usable URL" in r.getMessage()]
    assert notices == ["WARNING", "WARNING"]


@pytest.mark.parametrize(
    ("hass", "https"),
    [
        (_hass(internal_url="https://ha.lan:8443"), True),  # a TLS proxy on its own port
        (_hass(internal_url="http://ha.lan:8123", use_ssl=True), False),  # internal_url's port: its scheme
        # No port: the address carries HA's own port, which a proxy on 443 does not serve (#165 review).
        (_hass(internal_url="https://ha.lan", use_ssl=False), False),
        (_hass(internal_url="https://ha.lan", use_ssl=True), True),
        (_hass(use_ssl=True), True),
        (_hass(use_ssl=False), False),
        (_hass(), False),  # http component not set up yet
    ],
)
def test_the_automatic_address_uses_https_only_where_its_port_speaks_tls(hass: Any, https: bool) -> None:
    resolver = HaAddressResolver(hass)
    resolver._async_get = AsyncMock(return_value="ha.lan:8123")  # type: ignore[method-assign]
    assert asyncio.run(resolver.async_target()) == WebhookTarget("ha.lan:8123", https)


# --- audit ---


def test_protocol_items_cover_every_built_command_of_a_stale_class() -> None:
    commands = {
        "M1 a": {"cmdId": "11", "webioClass": MARKER},
        "M2 b": {"cmdId": "12", "webioClass": MARKER},
        "M3 old name": {"cmdId": "13", "webioClass": MARKER},  # renamed/orphan: its own category
        "M4 no id": {"cmdId": None, "webioClass": MARKER},
        "IO 1": {"cmdId": "21", "webioClass": IO},
    }
    payloads = {"M1 a": {"Name": "M1 a"}, "M2 b": {"Name": "M2 b"}, "M4 no id": {}, "IO 1": {"Name": "IO 1"}}
    devices = {MARKER: {"protocol_mismatch": True}, IO: {"protocol_mismatch": False}}

    assert _protocol_audit_items(commands, payloads, devices) == [
        {"id": "11", "name": "M1 a", "payload": {"Name": "M1 a"}, "webio_class": MARKER},
        {"id": "12", "name": "M2 b", "payload": {"Name": "M2 b"}, "webio_class": MARKER},
    ]


def _coordinator(webhook_protocol: int = WEBIO_PROTOCOL_HTTP, stored: Any = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = SimpleNamespace(webhook_protocol=webhook_protocol)
    coordinator.webio_synced_protocols = {}
    coordinator._webio_protocol_store = SimpleNamespace(
        async_load=AsyncMock(return_value=stored), async_save=AsyncMock()
    )
    return coordinator


def test_a_class_without_a_record_counts_as_http() -> None:
    coordinator = _coordinator()
    assert coordinator.webio_synced_protocol(MARKER) == WEBIO_PROTOCOL_HTTP


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, {}),
        ({"classes": {MARKER: 1, IO: None}}, {MARKER: 1, IO: None}),
        # An invalid record is unknown (rewritten), not HTTP (which could hide an HTTPS class).
        ({"classes": {MARKER: "1", IO: True, "x": 0}}, {MARKER: None, IO: None, "x": 0}),
        ({"classes": [1]}, {}),
    ],
)
def test_the_protocol_store_is_restored_defensively(stored: Any, expected: dict) -> None:
    coordinator = _coordinator(stored=stored)
    asyncio.run(coordinator.async_load_webio_protocols())
    assert coordinator.webio_synced_protocols == expected


@pytest.mark.parametrize("stored", [{"classes": [1]}, {"classes": {MARKER: "1"}}])
def test_a_damaged_protocol_store_is_logged(stored: Any, caplog: pytest.LogCaptureFixture) -> None:
    asyncio.run(_coordinator(stored=stored).async_load_webio_protocols())
    assert "Web-IO protocol store" in caplog.text


def test_a_recorded_protocol_is_saved_only_when_it_changes() -> None:
    coordinator = _coordinator()
    save = coordinator._webio_protocol_store.async_save

    asyncio.run(coordinator.async_set_webio_synced_protocol(MARKER, WEBIO_PROTOCOL_HTTPS))
    asyncio.run(coordinator.async_set_webio_synced_protocol(MARKER, WEBIO_PROTOCOL_HTTPS))
    assert save.await_count == 1
    save.assert_awaited_with({"classes": {MARKER: WEBIO_PROTOCOL_HTTPS}})

    asyncio.run(coordinator.async_set_webio_synced_protocol(MARKER, None))
    assert coordinator.webio_synced_protocol(MARKER) is None
    assert save.await_count == 2


@pytest.mark.parametrize(
    ("synced", "device_id", "active", "mismatch"),
    [
        (WEBIO_PROTOCOL_HTTP, "7", True, True),
        (None, "7", True, True),  # unknown: always rewritten
        (WEBIO_PROTOCOL_HTTPS, "7", True, False),
        (WEBIO_PROTOCOL_HTTP, None, True, False),  # no device: a recreate writes the protocol
        (WEBIO_PROTOCOL_HTTP, "7", False, False),  # deselected class: left alone
    ],
)
def test_the_device_audit_flags_a_protocol_mismatch(
    synced: int | None, device_id: str | None, active: bool, mismatch: bool
) -> None:
    _assert_protocol_mismatch(WEBIO_PROTOCOL_HTTPS, synced, device_id, active, mismatch)


@pytest.mark.parametrize(
    ("synced", "mismatch"),
    [
        (WEBIO_PROTOCOL_HTTPS, True),  # back from HTTPS on another port to HTTP (or HTTPS on 443)
        (WEBIO_PROTOCOL_HTTP, False),
        (None, True),
    ],
)
def test_the_device_audit_flags_a_switch_back_to_http(synced: int | None, mismatch: bool) -> None:
    _assert_protocol_mismatch(WEBIO_PROTOCOL_HTTP, synced, "7", True, mismatch)


def test_an_install_without_records_needs_no_rewrite_for_http() -> None:
    coordinator = _coordinator(WEBIO_PROTOCOL_HTTP)
    coordinator.ha_address = HaAddressResolver(MagicMock())
    devices = {MARKER: {"device_id": "7", "device_ip": "192.0.2.10:8123"}}

    audit = asyncio.run(coordinator._async_audit_webio_devices(devices, "192.0.2.10:8123", [MARKER]))
    assert audit[MARKER]["protocol_mismatch"] is False


def _assert_protocol_mismatch(
    webhook_protocol: int, synced: int | None, device_id: str | None, active: bool, mismatch: bool
) -> None:
    coordinator = _coordinator(webhook_protocol)
    coordinator.ha_address = HaAddressResolver(MagicMock())
    coordinator.webio_synced_protocols = {MARKER: synced}
    devices = {MARKER: {"device_id": device_id, "device_ip": "192.0.2.10:8123"}}

    audit = asyncio.run(coordinator._async_audit_webio_devices(devices, "192.0.2.10:8123", [MARKER] if active else []))
    assert audit[MARKER]["protocol_mismatch"] is mismatch
    assert audit[IO]["protocol_mismatch"] is False


# --- nightly range check ---

_TARGET = {"Name": "M1 a", "Min": -32768, "Max": 32767, "Protocol": WEBIO_PROTOCOL_HTTPS}
_DEVICE = {"device_id": "7", "base_id": "3"}


def _range_coordinator(form: WebioCommandForm | None, saved: bool = True) -> ComexioCoordinator:
    coordinator = _coordinator(WEBIO_PROTOCOL_HTTPS)
    coordinator.webio_synced_protocols = {MARKER: WEBIO_PROTOCOL_HTTPS}
    coordinator.api = SimpleNamespace(
        webhook_protocol=WEBIO_PROTOCOL_HTTPS,
        get_webio_command_form=AsyncMock(return_value=form),
        save_single_command=AsyncMock(return_value=saved),
    )
    return coordinator


def _check(coordinator: ComexioCoordinator) -> dict[str, int]:
    result = dict.fromkeys(
        (RANGE_CHECK_CHECKED, RANGE_CHECK_FIXED, RANGE_CHECK_FAILED, RANGE_CHECK_CORRECTION_FAILED), 0
    )
    asyncio.run(coordinator._async_check_one_webio_range("M1 a", "11", MARKER, _DEVICE, _TARGET, result))
    return result


def test_a_drifted_protocol_is_corrected_and_marks_the_class_unknown(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, WEBIO_PROTOCOL_HTTP))

    assert _check(coordinator)[RANGE_CHECK_FIXED] == 1
    coordinator.api.save_single_command.assert_awaited_once_with("3", "7", _TARGET, existing_cmd_id="11")
    # Digital commands are never read here: the next audit rewrites the whole class.
    assert coordinator.webio_synced_protocol(MARKER) is None
    assert "protocol 0 -> 1" in caplog.text


@pytest.mark.parametrize("protocol", [WEBIO_PROTOCOL_HTTPS, None])  # None: unreadable, no evidence of drift
def test_a_matching_or_unreadable_protocol_is_no_drift(protocol: int | None) -> None:
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, protocol))

    assert _check(coordinator)[RANGE_CHECK_CHECKED] == 1
    coordinator.api.save_single_command.assert_not_awaited()
    assert coordinator.webio_synced_protocol(MARKER) == WEBIO_PROTOCOL_HTTPS


def test_a_failed_protocol_correction_is_counted_and_keeps_the_class_unknown() -> None:
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, WEBIO_PROTOCOL_HTTP), saved=False)

    assert _check(coordinator)[RANGE_CHECK_CORRECTION_FAILED] == 1
    assert coordinator.webio_synced_protocol(MARKER) is None


@pytest.mark.parametrize("form", [None, WebioCommandForm(None, 32767, WEBIO_PROTOCOL_HTTP)])
def test_an_unreadable_form_is_a_failed_read(form: WebioCommandForm | None) -> None:
    coordinator = _range_coordinator(form)
    assert _check(coordinator)[RANGE_CHECK_FAILED] == 1
    coordinator.api.save_single_command.assert_not_awaited()


# --- sync ---


def _item(cmd_id: str, cls: str = MARKER) -> dict[str, Any]:
    return {"id": cmd_id, "name": f"M{cmd_id}", "payload": {"Name": f"M{cmd_id}"}, "webio_class": cls}


def test_a_type_fix_already_covers_the_protocol_of_its_command() -> None:
    assert _protocol_only_items([_item("1"), _item("2")], [_item("2")]) == [_item("1")]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("full_sync", [("type", "2"), ("protocol", "1")]),
        ("update_ip", [("protocol", "1"), ("protocol", "2")]),
        ("update_types", [("type", "2")]),
        ("create_missing", []),
    ],
)
def test_protocol_tasks_run_with_the_server_address_actions(action: str, expected: list[tuple[str, str]]) -> None:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    tasks = button._build_delta_tasks(action, [], [], [], [_item("2")], [_item("1"), _item("2")])
    assert [(t["type"], t["item"]["id"]) for t in tasks] == expected


def _ctx(action: str = "update_ip", failed_writes: list[str] | None = None) -> _SyncContext:
    return _SyncContext(
        api=SimpleNamespace(webhook_protocol=WEBIO_PROTOCOL_HTTPS),
        action=action,
        ha_address="192.0.2.10:8123",
        webio_devices_audit={MARKER: {"base_id": "3"}},
        start_time=MagicMock(),
        class_names={cls: f"HomeAssistant {cls}" for cls in WEBIO_CLASSES},
        update_status=MagicMock(),
        failed_writes=failed_writes if failed_writes is not None else [],
        protocol_items=[_item("1"), _item("2", IO)],
        audit_protocol=WEBIO_PROTOCOL_HTTPS,
    )


def _sync_button(failed: list[str] | None = None, cancel: bool = False) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.server_id = "cx1"
    button.coordinator = SimpleNamespace(  # type: ignore[assignment]
        cancel_sync=cancel, async_set_webio_synced_protocol=AsyncMock(), active_webio_classes=list(WEBIO_CLASSES)
    )

    async def _execute(ctx: _SyncContext, *_args: Any) -> dict[str, Any]:
        ctx.failed_writes.extend(failed or [])
        return {"added": 0, "removed": 0, "updated": 2, "renamed": 0, "created_names": [], "debris_removed": 0}

    button._execute_delta_tasks = _execute  # type: ignore[method-assign]
    return button


def _sync_class(button: ComexioSyncButton, ctx: _SyncContext) -> dict[str, Any]:
    return asyncio.run(button._sync_class(ctx, MARKER, "7", [], [], [], [], [], False, 10, 50))


@pytest.mark.parametrize("action", ["full_sync", "update_ip"])
def test_a_delta_sync_records_the_protocol_its_payloads_carry(action: str) -> None:
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value=action)  # type: ignore[method-assign]

    result = _sync_class(button, _ctx(action))

    assert "protocol_written" not in result
    button.coordinator.async_set_webio_synced_protocol.assert_awaited_once_with(MARKER, WEBIO_PROTOCOL_HTTPS)
    # Only this class' protocol items reach the decision.
    assert button._decide_effective_action.await_args.args[7] == [_item("1")]


@pytest.mark.parametrize(
    ("action", "failed", "cancel"),
    [
        ("update_types", [], False),  # rewrites only some commands
        ("update_ip", ["protocol M1"], False),
        ("full_sync", ["type M2"], False),
        ("update_ip", [], True),
    ],
)
def test_a_delta_sync_keeps_the_old_record_when_not_every_command_was_rewritten(
    action: str, failed: list[str], cancel: bool
) -> None:
    button = _sync_button(failed, cancel)
    button._decide_effective_action = AsyncMock(return_value=action)  # type: ignore[method-assign]

    _sync_class(button, _ctx(action))

    button.coordinator.async_set_webio_synced_protocol.assert_not_awaited()


@pytest.mark.parametrize("address_written", [True, False])
def test_the_record_follows_the_commands_not_the_device_address(address_written: bool) -> None:
    """The record describes the commands: a failed address write is its own failure, shown again as an IP mismatch."""
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value="update_ip")  # type: ignore[method-assign]
    ctx = _ctx()
    ctx.api.update_webio_device_ip = AsyncMock(return_value=address_written)

    result = asyncio.run(button._sync_class(ctx, MARKER, "7", [], [], [], [], [], True, 10, 50))

    assert result["updated_ip"] is address_written
    assert ("Marker: server address update" in ctx.failed_writes) is not address_written
    button.coordinator.async_set_webio_synced_protocol.assert_awaited_once_with(MARKER, WEBIO_PROTOCOL_HTTPS)


def test_an_earlier_failure_of_another_class_does_not_block_the_record() -> None:
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value="update_ip")  # type: ignore[method-assign]

    _sync_class(button, _ctx(failed_writes=["protocol IO 1"]))

    button.coordinator.async_set_webio_synced_protocol.assert_awaited_once()


def test_a_recreate_records_the_protocol_of_this_sync() -> None:
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value="recreate")  # type: ignore[method-assign]
    button._recreate_class = AsyncMock()  # type: ignore[method-assign]
    ctx = _ctx()
    ctx.api.build_webio_commands = MagicMock(return_value=[])
    button.coordinator.data = {}
    button.coordinator.ignored_marker_ids = set()
    button.coordinator.ignored_knx_ids = set()

    _sync_class(button, ctx)

    button.coordinator.async_set_webio_synced_protocol.assert_awaited_once_with(MARKER, WEBIO_PROTOCOL_HTTPS)


# --- options flow ---


def _flow(external_url: str | None = None, options: dict | None = None) -> ComexioOptionsFlow:
    flow = ComexioOptionsFlow(SimpleNamespace(entry_id="e1", data={}, options=options or {}))
    flow.hass = _hass(external_url=external_url)  # type: ignore[assignment]
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(side_effect=lambda **kw: {"type": "create_entry", **kw})  # type: ignore[method-assign]
    return flow


@pytest.mark.parametrize(
    ("mode", "external_url", "errors"),
    [
        (WEBHOOK_ADDRESS_EXTERNAL, None, {CONF_WEBHOOK_ADDRESS_MODE: "no_external_url"}),
        (WEBHOOK_ADDRESS_EXTERNAL, "https://localhost", {CONF_WEBHOOK_ADDRESS_MODE: "no_external_url"}),
        (WEBHOOK_ADDRESS_EXTERNAL, "https://ha.example.com", {}),
        (WEBHOOK_ADDRESS_AUTO, None, {}),
        (WEBHOOK_ADDRESS_CUSTOM, None, {}),
    ],
)
def test_the_external_mode_needs_a_usable_external_url(mode: str, external_url: str | None, errors: dict) -> None:
    found: dict[str, str] = {}
    _flow(external_url)._validate_webhook_address_mode({CONF_WEBHOOK_ADDRESS_MODE: mode}, found)
    assert found == errors


def test_the_custom_url_step_shows_the_saved_url() -> None:
    flow = _flow(options={CONF_WEBHOOK_URL: "https://ha.example.com"})
    result = asyncio.run(flow.async_step_webhook_url())

    assert result["step_id"] == "webhook_url"
    assert result["errors"] == {}
    assert result["data_schema"]({}) == {CONF_WEBHOOK_URL: "https://ha.example.com"}


@pytest.mark.parametrize(("url", "error"), [("ha.example.com", "invalid_url"), ("http://127.0.0.1", "loopback_url")])
def test_the_custom_url_step_refuses_an_unusable_url(url: str, error: str) -> None:
    result = asyncio.run(_flow().async_step_webhook_url({CONF_WEBHOOK_URL: url}))
    assert result["type"] == "form"
    assert result["errors"] == {CONF_WEBHOOK_URL: error}


def test_the_custom_url_step_saves_the_init_input_with_the_url() -> None:
    flow = _flow(options={"scan_interval": 15})
    flow._pending_input = {CONF_WEBHOOK_ADDRESS_MODE: WEBHOOK_ADDRESS_CUSTOM, "scan_interval": 30}

    result = asyncio.run(flow.async_step_webhook_url({CONF_WEBHOOK_URL: " https://ha.example.com:8443 "}))

    assert result["type"] == "create_entry"
    assert result["data"] == {
        "scan_interval": 30,
        CONF_WEBHOOK_ADDRESS_MODE: WEBHOOK_ADDRESS_CUSTOM,
        CONF_WEBHOOK_URL: "https://ha.example.com:8443",
    }


def test_a_deselected_class_with_a_device_gets_no_record() -> None:
    """Its commands were not audited for the protocol, so a sync of its device IP rewrote none of them."""
    button = _sync_button()
    button.coordinator.active_webio_classes = [IO]
    button._decide_effective_action = AsyncMock(return_value="update_ip")  # type: ignore[method-assign]

    _sync_class(button, _ctx())

    button.coordinator.async_set_webio_synced_protocol.assert_not_awaited()


def test_a_failed_recreate_records_nothing() -> None:
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value="recreate")  # type: ignore[method-assign]
    button._recreate_class = AsyncMock(side_effect=RuntimeError("Upload failed"))  # type: ignore[method-assign]

    ctx = _ctx()

    with pytest.raises(RuntimeError):
        _sync_class(button, ctx)
    button.coordinator.async_set_webio_synced_protocol.assert_not_awaited()


def test_a_delta_sync_without_the_audit_protocol_records_nothing() -> None:
    button = _sync_button()
    button._decide_effective_action = AsyncMock(return_value="full_sync")  # type: ignore[method-assign]
    ctx = _ctx("full_sync")
    ctx.audit_protocol = None

    _sync_class(button, ctx)

    button.coordinator.async_set_webio_synced_protocol.assert_not_awaited()


def test_failed_creates_or_renames_do_not_block_the_record() -> None:
    """Only type-fixes and protocol rewrites carry the protocol of existing commands."""
    button = _sync_button(["create M9", "rename M8"])
    button._decide_effective_action = AsyncMock(return_value="full_sync")  # type: ignore[method-assign]

    _sync_class(button, _ctx("full_sync"))

    button.coordinator.async_set_webio_synced_protocol.assert_awaited_once()


def _decide(action: str, cls_types: list[dict], cls_protocol: list[dict]) -> tuple[str, Any]:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.server_id = "cx1"
    button._async_fast_track = AsyncMock(return_value=True)  # type: ignore[method-assign]
    result = asyncio.run(
        button._decide_effective_action(_ctx(action), MARKER, "7", [], [], cls_types, [], cls_protocol, False)
    )
    return result, button._async_fast_track


def _many(count: int) -> list[dict]:
    return [_item(str(i)) for i in range(count)]


_FAST_TRACK_COUNT = SYNC_DURATION_RECREATE // SYNC_DURATION_WRITE + 1


@pytest.mark.parametrize("action", ["full_sync", "update_ip"])
def test_many_protocol_rewrites_take_the_fast_track(action: str) -> None:
    result, fast_track = _decide(action, [], _many(_FAST_TRACK_COUNT))

    assert result == "recreate"
    eta = fast_track.await_args.args[3]
    assert eta == _FAST_TRACK_COUNT * SYNC_DURATION_WRITE + int(1.5 * (_FAST_TRACK_COUNT - 1))


def test_a_protocol_rewrite_a_type_fix_covers_counts_once_in_the_eta() -> None:
    items = _many(_FAST_TRACK_COUNT)
    _, fast_track = _decide("full_sync", items, items)
    assert fast_track.await_args.args[3] == _FAST_TRACK_COUNT * SYNC_DURATION_WRITE + int(1.5 * (_FAST_TRACK_COUNT - 1))


@pytest.mark.parametrize(
    ("action", "protocol", "expected"),
    [
        ("update_ip", [], "update_ip"),  # a bare address update never deletes the device
        ("update_ip", [_item("1")], "update_ip"),  # a short rewrite stays a delta sync
        ("update_types", _many(_FAST_TRACK_COUNT), "update_types"),  # protocol items do not count here
    ],
)
def test_short_or_unrelated_runs_skip_the_fast_track(action: str, protocol: list[dict], expected: str) -> None:
    result, fast_track = _decide(action, [], protocol)
    assert result == expected
    fast_track.assert_not_awaited()


def test_a_pending_protocol_change_is_not_written_ahead_by_the_range_check() -> None:
    """The class still records HTTP while the audit already builds HTTPS: no drift, no early rewrite."""
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, WEBIO_PROTOCOL_HTTP))
    coordinator.webio_synced_protocols = {MARKER: WEBIO_PROTOCOL_HTTP}

    assert _check(coordinator)[RANGE_CHECK_CHECKED] == 1
    coordinator.api.save_single_command.assert_not_awaited()


@pytest.mark.parametrize("live", [WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS])
def test_an_unknown_class_protocol_is_left_to_the_sync(live: int) -> None:
    """After a drift the class is unknown: the range check neither flags nor rewrites the protocol again."""
    coordinator = _range_coordinator(WebioCommandForm(0, 100, live))
    coordinator.webio_synced_protocols = {MARKER: None}

    assert _check(coordinator)[RANGE_CHECK_FIXED] == 1  # the Min/Max drift
    written = coordinator.api.save_single_command.await_args.args[2]
    assert written == {**_TARGET, "Protocol": live}
    assert coordinator.webio_synced_protocol(MARKER) is None


def test_an_unknown_class_with_matching_min_max_is_not_rewritten() -> None:
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, WEBIO_PROTOCOL_HTTP))
    coordinator.webio_synced_protocols = {MARKER: None}

    _check(coordinator)
    coordinator.api.save_single_command.assert_not_awaited()


def test_a_min_max_fix_during_a_pending_protocol_change_keeps_the_recorded_protocol() -> None:
    coordinator = _range_coordinator(WebioCommandForm(0, 100, WEBIO_PROTOCOL_HTTP))
    coordinator.webio_synced_protocols = {MARKER: WEBIO_PROTOCOL_HTTP}

    assert _check(coordinator)[RANGE_CHECK_FIXED] == 1
    written = coordinator.api.save_single_command.await_args.args[2]
    assert written == {**_TARGET, "Protocol": WEBIO_PROTOCOL_HTTP}
    assert coordinator.webio_synced_protocol(MARKER) == WEBIO_PROTOCOL_HTTP


def test_the_audit_resolves_the_protocol_before_it_builds_the_payloads() -> None:
    """The payloads must carry the current protocol, or a sync rewrites the class with the old one."""
    source = inspect.getsource(ComexioCoordinator._async_audit_webio)
    assert 0 <= source.index("async_webhook_target(") < source.index("build_webio_commands(")


# --- repair issue ---


def _issue_coordinator(ip_mismatch: bool, protocol: list[dict]) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.hass = MagicMock()
    coordinator.server_id = "cx1"
    coordinator.config_entry = SimpleNamespace(entry_id="e1")
    coordinator._cleanup_entity_ids = []
    coordinator._cleanup_function_plan_count = 0
    coordinator._function_plan_missing_eta_sec = MagicMock(return_value=0)  # type: ignore[method-assign]
    coordinator._function_plan_missing_detail = MagicMock(return_value={})  # type: ignore[method-assign]
    empty = ("type", "missing", "rename", "orphan", "function_plan_missing", "function_plan_dangling")
    coordinator.last_audit_results = {
        **dict.fromkeys(empty, []),
        "knx_bridge_missing": [],
        "knx_bridge_loopback_missing": [],
        "function_plan_trigger_missing": {},
        "function_plan_trigger_orphan": {},
        "ip_mismatch": ip_mismatch,
        "protocol": protocol,
    }
    return coordinator


@pytest.mark.parametrize(
    ("ip_mismatch", "protocol", "ip_count", "ip_address"),
    [(False, [_item("1"), _item("2")], 1, 0), (True, [], 1, 1), (True, [_item("1")], 1, 1)],
)
def test_a_protocol_to_rewrite_offers_the_server_address_fix(
    monkeypatch: pytest.MonkeyPatch, ip_mismatch: bool, protocol: list[dict], ip_count: int, ip_address: int
) -> None:
    created = MagicMock()
    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", created)
    coordinator = _issue_coordinator(ip_mismatch, protocol)

    coordinator._update_sync_mismatch_issue({"x"}, {}, {}, {})

    counts = created.call_args.kwargs["data"]["counts"]
    assert (counts["ip_mismatch"], counts["ip_address"], counts["protocol"]) == (ip_count, ip_address, len(protocol))
    assert created.call_args.kwargs["translation_placeholders"]["i_count"] == str(ip_count)


def _repair_form(counts: dict[str, int], language: str = "en") -> dict[str, Any]:
    flow = ComexioRepairFlow("sync_mismatch_cx1", {"entry_id": "e1", "counts": counts})
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config=SimpleNamespace(language=language), data={DOMAIN: {"e1": SimpleNamespace(in_sync=False)}}
    )
    flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)  # type: ignore[method-assign]
    issue = SimpleNamespace(
        translation_placeholders={"ha_count": "3", "com_count": "3"}, translation_key="sync_mismatch"
    )
    registry = SimpleNamespace(async_get_issue=MagicMock(return_value=issue))
    with patch.object(repairs_module.ir, "async_get", return_value=registry):
        return asyncio.run(flow.async_step_select_action())


def _eta(seconds: int) -> str:
    return f"~{seconds}s" if seconds < 60 else f"~{seconds // 60}:{seconds % 60:02d} min"


def _options(form: dict[str, Any]) -> dict[str, str]:
    return form["data_schema"].schema["action"].container


def test_the_repair_counts_the_protocol_rewrites_in_the_address_fix() -> None:
    form = _repair_form({"ip_mismatch": 1, "ip_address": 0, "protocol": 3})

    eta = _eta(4 * SYNC_DURATION_WRITE)
    assert _options(form) == {"update_ip": f"{ICON_NETWORK} Update HA Server Address (IP:Port, protocol) ({eta})"}
    assert "webhook protocol (HTTP/HTTPS)" in form["description_placeholders"]["summary_text"]


def test_the_repair_lists_a_protocol_next_to_other_issues_without_a_wrong_address_line() -> None:
    form = _repair_form({"type": 1, "ip_mismatch": 1, "ip_address": 0, "protocol": 3}, "de")

    summary = form["description_placeholders"]["summary_text"]
    assert "**Webhook-Protokoll:** 3" in summary
    assert "Server-Adresse" not in summary
    assert _eta(5 * SYNC_DURATION_WRITE) in _options(form)["full_sync"]  # type fix + 3 rewrites + address
    assert "(IP:Port, Protokoll)" in _options(form)["update_ip"]


def test_an_old_issue_without_the_address_count_still_names_the_address() -> None:
    form = _repair_form({"type": 1, "ip_mismatch": 1})
    assert "**Server Address:** Incorrect" in form["description_placeholders"]["summary_text"]


# --- options flow routing ---


def _init(flow: ComexioOptionsFlow, mode: str, **extra: Any) -> dict[str, Any]:
    flow.hass.data = {}
    flow.hass.services = SimpleNamespace(async_services_for_domain=lambda _domain: {})
    return asyncio.run(flow.async_step_init({CONF_WEBHOOK_ADDRESS_MODE: mode, **extra}))


def test_the_custom_mode_asks_for_the_url_in_a_second_step() -> None:
    flow = _flow()
    result = _init(flow, WEBHOOK_ADDRESS_CUSTOM)

    assert result["step_id"] == "webhook_url"
    assert flow._pending_input[CONF_WEBHOOK_ADDRESS_MODE] == WEBHOOK_ADDRESS_CUSTOM
    flow.async_create_entry.assert_not_called()


def test_the_external_mode_without_an_external_url_stays_on_the_first_step() -> None:
    result = _init(_flow(), WEBHOOK_ADDRESS_EXTERNAL)
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_WEBHOOK_ADDRESS_MODE: "no_external_url"}


@pytest.mark.parametrize(
    ("mode", "external_url"), [(WEBHOOK_ADDRESS_AUTO, None), (WEBHOOK_ADDRESS_EXTERNAL, "https://ha.example.com")]
)
def test_the_other_modes_save_right_away(mode: str, external_url: str | None) -> None:
    result = _init(_flow(external_url), mode)
    assert result["type"] == "create_entry"
    assert result["data"][CONF_WEBHOOK_ADDRESS_MODE] == mode


def test_an_error_on_the_first_step_keeps_the_custom_mode_from_moving_on() -> None:
    result = _init(_flow(), WEBHOOK_ADDRESS_CUSTOM, scan_interval="x")
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "invalid_number"}
