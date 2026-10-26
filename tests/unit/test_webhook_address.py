"""Webhook address modes of the Web-IO devices (#165), and the rule that an address change writes only the devices.

The resolver returns the external / custom URL's address directly and falls back to the automatic
address when that URL is unusable. Only host and port reach the Web-IO devices: the commands keep HTTP
whatever the address or scheme (Comexio picks HTTP or HTTPS itself), so an address change is one device
write per class and never rewrites a command.
"""

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio import WebioCommandForm
from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
import pytest

from custom_components.comexio import coordinator as coordinator_module, repairs as repairs_module
from custom_components.comexio.button import ComexioSyncButton, _class_action_eta, _SyncContext
from custom_components.comexio.const import (
    CONF_WEBHOOK_ADDRESS_MODE,
    CONF_WEBHOOK_URL,
    DOMAIN,
    ICON_NETWORK,
    RANGE_CHECK_CHECKED,
    RANGE_CHECK_CORRECTION_FAILED,
    RANGE_CHECK_FAILED,
    RANGE_CHECK_FIXED,
    SYNC_DURATION_DELETE,
    SYNC_DURATION_RECREATE,
    SYNC_DURATION_WRITE,
    WEBHOOK_ADDRESS_AUTO,
    WEBHOOK_ADDRESS_CUSTOM,
    WEBHOOK_ADDRESS_EXTERNAL,
    WEBIO_CLASSES,
)
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.ha_address import (
    HaAddressResolver,
    WebhookTarget,
    webhook_url_error,
    webhook_url_target,
)
from custom_components.comexio.options_flow import WEBHOOK_URL_EXAMPLE, ComexioOptionsFlow
from custom_components.comexio.repairs import _ADDRESS_FALLBACK_REASONS, ComexioRepairFlow

MARKER = WEBIO_CLASSES[0]


def _hass(internal_url: str | None = None, external_url: str | None = None, use_ssl: bool | None = None) -> Any:
    api = None if use_ssl is None else SimpleNamespace(use_ssl=use_ssl, port=8123)
    return SimpleNamespace(config=SimpleNamespace(internal_url=internal_url, external_url=external_url, api=api))


# --- URL parsing ---


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://ha.example.com", WebhookTarget("ha.example.com:443", True)),
        ("https://ha.example.com/", WebhookTarget("ha.example.com:443", True)),
        ("http://ha.fritz.box", WebhookTarget("ha.fritz.box:80", False)),
        ("https://ha.example.com:8443", WebhookTarget("ha.example.com:8443", True)),
        ("  http://192.0.2.10:8123  ", WebhookTarget("192.0.2.10:8123", False)),
        ("https://[fd00::10]:8123", WebhookTarget("[fd00::10]:8123", True)),
        ("HTTPS://HA.Example.com", WebhookTarget("ha.example.com:443", True)),
        # LAN names may carry "_" although DNS host names do not.
        ("http://my_ha.fritz.box:8123", WebhookTarget("my_ha.fritz.box:8123", False)),
        # A fully qualified name ends in one dot; an internationalized name is checked in its IDNA form.
        ("https://ha.example.com.", WebhookTarget("ha.example.com.:443", True)),
        ("https://bücher.example", WebhookTarget("bücher.example:443", True)),
        # The longest label (63) and the longest name (253 characters) DNS allows.
        (f"https://{'a' * 63}.example.com", WebhookTarget(f"{'a' * 63}.example.com:443", True)),
        (
            "https://" + ".".join(["a" * 63] * 3 + ["a" * 61]),
            WebhookTarget(".".join(["a" * 63] * 3 + ["a" * 61]) + ":443", True),
        ),
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
        # A host that is no host name: it would be saved as an address Comexio cannot reach.
        "https://ha example.com",
        "https://ha..example.com",
        "https://-ha.example.com",
        "https://ha-.example.com",
        f"https://{'a' * 64}.example.com",
        "https://ha.example.com..",
        "https://" + ".".join(["a" * 63] * 4),  # 255 characters, beyond DNS's 253
        f"https://{'ü' * 64}.example",  # a label beyond 63 characters fails the IDNA encoding
    ],
)
def test_an_unusable_webhook_url_is_invalid(url: str | None) -> None:
    assert webhook_url_target(url) is None
    assert webhook_url_error(url) == "invalid_url"


@pytest.mark.parametrize("url", ["https://localhost", "http://127.0.0.1:8123", "http://0.0.0.0:8123", "http://[::1]"])
def test_a_loopback_webhook_url_is_refused(url: str) -> None:
    assert webhook_url_error(url) == "loopback_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://ha.example.com",
        "http://ha.example.com:8123",
        "http://8.8.8.8:8123",
        "http://[::ffff:8.8.8.8]:8123",
        "http://[2606:4700::1]:8123",
        "http://100.63.255.255",
        "http://100.128.0.1",
        "http://evilfritz.box",
        "http://ha.local.example.com",
        "http://HA.EXAMPLE.COM.",
        "http://8.8.8.8.:8123",
    ],
)
def test_plain_http_outside_the_local_network_is_refused(url: str) -> None:
    """The values would cross the internet in cleartext, to a webhook whose id is predictable."""
    assert webhook_url_target(url) is not None
    assert webhook_url_error(url) == "insecure_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.178.20:8123",
        "http://10.0.0.5",
        "http://[fd00::10]:8123",
        "http://[fe80::1]:8123",
        "http://100.101.102.103:8123",  # shared address space: carrier-grade NAT, Tailscale
        "http://homeassistant:8123",  # single-label name
        "http://homeassistant.fritz.box:8123",
        "http://ha.local",
        "http://ha.home.arpa.",
        "http://fritz.box",
        "https://ha.example.com:8123",
        "http://[::ffff:192.168.1.5]:8123",
        "http://192.168.1.5.:8123",
        "http://100.127.255.255",
        "http://HA.FRITZ.BOX",
    ],
)
def test_plain_http_inside_the_local_network_or_https_is_allowed(url: str) -> None:
    assert webhook_url_error(url) is None


# --- resolver ---


def test_the_external_mode_returns_the_external_url_without_searching() -> None:
    resolver = HaAddressResolver(_hass(external_url="https://ha.example.com"), WEBHOOK_ADDRESS_EXTERNAL)
    resolver._async_get = AsyncMock()  # type: ignore[method-assign]

    assert asyncio.run(resolver.async_target("192.0.2.10:8123")) == WebhookTarget("ha.example.com:443", True)
    assert asyncio.run(resolver.async_get()) == "ha.example.com:443"
    resolver._async_get.assert_not_awaited()


def test_the_custom_mode_returns_its_own_url() -> None:
    resolver = HaAddressResolver(_hass(), WEBHOOK_ADDRESS_CUSTOM, "http://ha.fritz.box:8123")
    assert asyncio.run(resolver.async_target()) == WebhookTarget("ha.fritz.box:8123", False)
    assert resolver.fallback_reason is None


@pytest.mark.parametrize(
    ("mode", "external_url", "url", "reason"),
    [
        (WEBHOOK_ADDRESS_EXTERNAL, None, None, "invalid_url"),
        (WEBHOOK_ADDRESS_EXTERNAL, "https://localhost", None, "loopback_url"),
        (WEBHOOK_ADDRESS_EXTERNAL, "http://ha.example.com", None, "insecure_url"),
        (WEBHOOK_ADDRESS_CUSTOM, "https://ha.example.com", None, "invalid_url"),
        (WEBHOOK_ADDRESS_CUSTOM, None, "https://ha.example.com/path", "invalid_url"),
        (WEBHOOK_ADDRESS_CUSTOM, None, "http://ha.example.com:8123", "insecure_url"),
    ],
)
def test_an_unusable_fixed_url_falls_back_to_the_automatic_address_and_warns_once(
    caplog: pytest.LogCaptureFixture, mode: str, external_url: str | None, url: str | None, reason: str
) -> None:
    resolver = HaAddressResolver(_hass(internal_url="http://192.0.2.10:8123", external_url=external_url), mode, url)
    resolver._async_get = AsyncMock(return_value="192.0.2.10:8123")  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.ha_address"):
        assert asyncio.run(resolver.async_target()) == WebhookTarget("192.0.2.10:8123", False)
        assert asyncio.run(resolver.async_target()) == WebhookTarget("192.0.2.10:8123", False)

    notices = [r for r in caplog.records if "has no usable URL" in r.getMessage()]
    assert [r.levelname for r in notices] == ["WARNING", "DEBUG"]
    assert repr(mode) in notices[0].getMessage()
    assert reason in notices[0].getMessage()
    assert resolver.fallback_reason == reason


@pytest.mark.parametrize(
    ("url", "logged"),
    [
        ("https://user:s3cret@ha.example.com", "'https://***@ha.example.com'"),
        # A password with a character that ends the URL's host part early, and a URL without a scheme.
        ("https://user:pa/s3cret@ha.example.com", "'https://***@ha.example.com'"),
        ("https://user:pa?s3cret@ha.example.com", "'https://***@ha.example.com'"),
        ("https://user:pa#s3cret@ha.example.com", "'https://***@ha.example.com'"),
        ("user:s3cret@192.0.2.10:8123", "'***@192.0.2.10:8123'"),
        ("user:pa//s3cret@192.0.2.10:8123", "'***@192.0.2.10:8123'"),
        # An "@" in the password: only the last one ends the user info.
        ("https://user:pa@s3cret@ha.example.com", "'https://***@ha.example.com'"),
        ("HTTPS://user:s3cret@ha.example.com", "'HTTPS://***@ha.example.com'"),
    ],
)
def test_a_refused_url_is_logged_without_its_password(caplog: pytest.LogCaptureFixture, url: str, logged: str) -> None:
    resolver = HaAddressResolver(_hass(internal_url="http://192.0.2.10:8123"), WEBHOOK_ADDRESS_CUSTOM, url)
    resolver._async_get = AsyncMock(return_value="192.0.2.10:8123")  # type: ignore[method-assign]

    asyncio.run(resolver.async_target())

    assert resolver.fallback_reason == "invalid_url"
    assert logged in caplog.text
    assert "s3cret" not in caplog.text


def test_a_fixed_url_that_works_again_warns_again_when_it_breaks(caplog: pytest.LogCaptureFixture) -> None:
    hass = _hass(external_url=None)
    resolver = HaAddressResolver(hass, WEBHOOK_ADDRESS_EXTERNAL)
    resolver._async_get = AsyncMock(return_value="192.0.2.10:8123")  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="custom_components.comexio.ha_address"):
        asyncio.run(resolver.async_target())
        assert resolver.fallback_reason == "invalid_url"
        hass.config.external_url = "https://ha.example.com"
        asyncio.run(resolver.async_target())
        assert resolver.fallback_reason is None
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


def test_the_device_audit_checks_only_the_address() -> None:
    """No protocol record, no protocol mismatch: the commands are not audited for the address at all."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.ha_address = SimpleNamespace(async_is_own_address=AsyncMock(return_value=True))
    devices = {MARKER: {"device_id": "7", "device_ip": "192.0.2.10:443", "base_id": "3"}}

    audit = asyncio.run(coordinator._async_audit_webio_devices(devices, "192.0.2.10:8443"))

    assert audit[MARKER]["ip_mismatch"] is True
    assert all("protocol" not in key for dev in audit.values() for key in dev)


def test_the_audit_takes_the_fallback_reason_with_the_address_it_explains() -> None:
    """Read before the next await: a later resolution (button, sync) rewrites the resolver's reason."""
    source = inspect.getsource(ComexioCoordinator._async_audit_webio)
    resolved = source.index("async_webhook_target(")
    taken = source.index("address_fallback = self.ha_address.fallback_reason")
    assert resolved < taken < source.index("await ", resolved + 1)
    assert '"address_fallback": address_fallback,' in source


_PARSED = {
    "markers": [{"id": "5", "name": "M5 Licht", "type": "digital"}, {"id": "6", "name": "M6 Soll", "type": "analog"}],
    "io": [{"ext_name": "BASE", "identifier": "Q1", "is_binary": True, "min": 0, "max": 1}],
    "knx": [],
}


def _protocols(value: Any) -> list[Any]:
    """Every "Protocol" field anywhere in a parsed Web-IO JSON upload."""
    if isinstance(value, dict):
        return [v for k, v in value.items() if k == "Protocol"] + [p for v in value.values() for p in _protocols(v)]
    if isinstance(value, list):
        return [p for v in value for p in _protocols(v)]
    return []


@pytest.mark.parametrize(
    "target", [WebhookTarget("x:8123", False), WebhookTarget("x:443", True), WebhookTarget("x:8443", True)]
)
def test_every_payload_the_api_builds_keeps_http_whatever_the_webhook_target(
    comexio_api: Any, target: WebhookTarget
) -> None:
    """Delta sync and audit (build_webio_commands) and the recreate upload (generate_webio_json) alike."""
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.data = None
    coordinator.api = comexio_api
    coordinator.ha_address = SimpleNamespace(async_target=AsyncMock(return_value=target))

    assert asyncio.run(coordinator.async_webhook_target()) == target

    commands = comexio_api.build_webio_commands("srv", _PARSED)
    assert len(commands) == 3
    assert [command["Protocol"] for command in commands] == [WEBIO_PROTOCOL_HTTP] * 3
    uploaded = _protocols(json.loads(comexio_api.generate_webio_json("srv", "HomeAssistant", _PARSED)))
    assert len(uploaded) == 3
    assert set(uploaded) == {WEBIO_PROTOCOL_HTTP}


# --- nightly range check ---

_TARGET = {"Name": "M1 a", "Min": -32768, "Max": 32767, "Protocol": WEBIO_PROTOCOL_HTTP}
_DEVICE = {"device_id": "7", "base_id": "3"}


def _range_coordinator(form: WebioCommandForm | None, saved: bool = True) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = SimpleNamespace(
        get_webio_command_form=AsyncMock(return_value=form),
        save_single_command=AsyncMock(return_value=saved),
    )
    return coordinator


def _check(coordinator: ComexioCoordinator) -> dict[str, int]:
    result = dict.fromkeys(
        (RANGE_CHECK_CHECKED, RANGE_CHECK_FIXED, RANGE_CHECK_FAILED, RANGE_CHECK_CORRECTION_FAILED), 0
    )
    asyncio.run(coordinator._async_check_one_webio_range("M1 a", "11", _DEVICE, _TARGET, result))
    return result


@pytest.mark.parametrize("protocol", [WEBIO_PROTOCOL_HTTPS, None])
def test_the_range_check_leaves_the_protocol_of_a_command_alone(protocol: int | None) -> None:
    """Matching Min/Max: no write, whatever protocol the command carries."""
    coordinator = _range_coordinator(WebioCommandForm(-32768, 32767, protocol))

    assert _check(coordinator)[RANGE_CHECK_CHECKED] == 1
    coordinator.api.save_single_command.assert_not_awaited()


def test_a_min_max_drift_is_corrected(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _range_coordinator(WebioCommandForm(0, 100, WEBIO_PROTOCOL_HTTP))

    assert _check(coordinator)[RANGE_CHECK_FIXED] == 1
    coordinator.api.save_single_command.assert_awaited_once_with("3", "7", _TARGET, existing_cmd_id="11")
    assert "Min/Max drifted (0/100 -> -32768/32767)" in caplog.text


def test_a_failed_min_max_correction_is_counted() -> None:
    coordinator = _range_coordinator(WebioCommandForm(0, 100, WEBIO_PROTOCOL_HTTP), saved=False)
    assert _check(coordinator)[RANGE_CHECK_CORRECTION_FAILED] == 1


@pytest.mark.parametrize("form", [None, WebioCommandForm(None, 32767, WEBIO_PROTOCOL_HTTP)])
def test_an_unreadable_form_is_a_failed_read(form: WebioCommandForm | None) -> None:
    coordinator = _range_coordinator(form)
    assert _check(coordinator)[RANGE_CHECK_FAILED] == 1
    coordinator.api.save_single_command.assert_not_awaited()


# --- sync ---


def _item(cmd_id: str) -> dict[str, Any]:
    return {"id": cmd_id, "name": f"M{cmd_id}", "payload": {"Name": f"M{cmd_id}"}, "webio_class": MARKER}


_W, _D = SYNC_DURATION_WRITE, SYNC_DURATION_DELETE


@pytest.mark.parametrize(
    ("action", "ip_mismatch", "expected"),
    [
        ("update_ip", True, _W),  # the device write alone, whatever else the audit found
        ("update_ip", False, 0),
        # one each of rename, orphan, missing, type fix, plus the device address
        ("full_sync", True, 4 * _W + _D + 4),
        ("delete_orphans", True, 2 * _D + 1),  # the address does not count here
        ("update_types", True, _W),
        ("ignore", True, 0),
    ],
)
def test_the_class_eta_counts_only_the_tasks_of_its_action(action: str, ip_mismatch: bool, expected: int) -> None:
    orphans = [_item("o1"), _item("o2")] if action == "delete_orphans" else [_item("o1")]
    assert _class_action_eta(action, [_item("m")], [_item("r")], [_item("2")], orphans, ip_mismatch) == expected


def test_an_address_update_builds_no_command_task() -> None:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    assert button._build_delta_tasks("update_ip", [_item("1")], [_item("2")], [_item("3")], [_item("4")]) == []


def _ctx(action: str) -> _SyncContext:
    return _SyncContext(
        api=SimpleNamespace(
            save_single_command=AsyncMock(return_value=True),
            delete_single_command=AsyncMock(return_value=True),
            update_webio_device_ip=AsyncMock(return_value=True),
        ),
        action=action,
        ha_address="192.0.2.10:8443",
        webio_devices_audit={MARKER: {"base_id": "3"}},
        start_time=MagicMock(),
        class_names={cls: f"HomeAssistant {cls}" for cls in WEBIO_CLASSES},
        update_status=MagicMock(),
    )


def test_an_address_change_writes_only_the_device() -> None:
    """A new address is one device write per class: the commands stay as they are, the device is never deleted."""
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.server_id = "cx1"
    button.coordinator = SimpleNamespace(cancel_sync=False)  # type: ignore[assignment]
    button._async_fast_track = AsyncMock(return_value=True)  # type: ignore[method-assign]
    button._cleanup_delta_debris = AsyncMock(return_value=({}, 0))  # type: ignore[method-assign]
    ctx = _ctx("update_ip")
    many = [_item(str(i)) for i in range(SYNC_DURATION_RECREATE // SYNC_DURATION_WRITE + 1)]

    result = asyncio.run(button._sync_class(ctx, MARKER, "7", many, many, many, many, [], True, 10, 50))

    ctx.api.update_webio_device_ip.assert_awaited_once_with("7", "192.0.2.10:8443", f"HomeAssistant {MARKER}")
    ctx.api.save_single_command.assert_not_awaited()
    ctx.api.delete_single_command.assert_not_awaited()
    button._async_fast_track.assert_not_awaited()
    assert result["updated_ip"] is True
    assert (result["added"], result["updated"], result["renamed"], result["removed"]) == (0, 0, 0, 0)
    assert not ctx.failed_writes


def test_the_sync_records_no_protocol() -> None:
    """No store, no record: nothing left that could mark a class unknown and rewrite all its commands."""
    assert not hasattr(ComexioCoordinator, "async_set_webio_synced_protocol")
    assert not hasattr(ComexioCoordinator, "async_load_webio_protocols")
    assert "protocol" not in inspect.getsource(ComexioSyncButton._sync_class)


# --- repair issue ---


def _issue_coordinator(ip_mismatch: bool) -> ComexioCoordinator:
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
        "ha_address": "192.0.2.10:8123",
        "address_fallback": None,
    }
    return coordinator


@pytest.mark.parametrize("ip_mismatch", [True, False])
def test_the_issue_counts_the_device_address_only(monkeypatch: pytest.MonkeyPatch, ip_mismatch: bool) -> None:
    created = MagicMock()
    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", created)
    coordinator = _issue_coordinator(ip_mismatch)

    coordinator._update_sync_mismatch_issue({"x"}, {}, {}, {})

    counts = created.call_args.kwargs["data"]["counts"]
    assert counts["ip_mismatch"] == (1 if ip_mismatch else 0)
    assert all("protocol" not in key for key in counts)


@pytest.mark.parametrize("reason", [None, "insecure_url"])
def test_the_issue_carries_the_address_to_write_and_why_it_is_the_automatic_one(
    monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    created = MagicMock()
    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", created)
    coordinator = _issue_coordinator(True)
    coordinator.last_audit_results["address_fallback"] = reason

    coordinator._update_sync_mismatch_issue({"x"}, {}, {}, {})

    data = created.call_args.kwargs["data"]
    assert (data["ha_address"], data["address_fallback"]) == ("192.0.2.10:8123", reason)


def _repair_form(counts: dict[str, int], language: str = "en", **issue_data: Any) -> dict[str, Any]:
    flow = ComexioRepairFlow("sync_mismatch_cx1", {"entry_id": "e1", "counts": counts, **issue_data})
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config=SimpleNamespace(language=language),
        data={DOMAIN: {"e1": SimpleNamespace(in_sync=False, api=SimpleNamespace(fub_data={}))}},
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


@pytest.mark.parametrize(
    ("language", "label", "summary"),
    [
        ("en", "Update HA Server Address (IP:Port)", "the server address (IP:Port) in the Comexio"),
        ("de", "HA Server-Adresse (IP:Port) aktualisieren", "dass die Server-Adresse (IP:Port) im"),
    ],
)
def test_the_address_fix_is_one_device_write(language: str, label: str, summary: str) -> None:
    form = _repair_form({"ip_mismatch": 1}, language)

    assert _options(form) == {"update_ip": f"{ICON_NETWORK} {label} (~{SYNC_DURATION_WRITE}s)"}
    text = form["description_placeholders"]["summary_text"]
    assert summary in text
    assert "HTTP" not in text


def test_the_full_sync_eta_counts_the_address_as_one_write() -> None:
    form = _repair_form({"type": 1, "ip_mismatch": 1})
    assert _eta(2 * SYNC_DURATION_WRITE) in _options(form)["full_sync"]
    assert "**Server Address:** Incorrect" in form["description_placeholders"]["summary_text"]


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
        (WEBHOOK_ADDRESS_EXTERNAL, "http://ha.example.com", {CONF_WEBHOOK_ADDRESS_MODE: "insecure_external_url"}),
        (WEBHOOK_ADDRESS_EXTERNAL, "http://ha.fritz.box:8123", {}),
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


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("ha.example.com", "invalid_url"),
        ("http://127.0.0.1", "loopback_url"),
        ("http://ha.example.com", "insecure_url"),
    ],
)
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


# --- options flow routing ---


def _init(flow: ComexioOptionsFlow, mode: str, **extra: Any) -> dict[str, Any]:
    flow.hass.data = {}
    flow.hass.services = SimpleNamespace(async_services_for_domain=lambda _domain: {})
    return asyncio.run(flow.async_step_init({CONF_WEBHOOK_ADDRESS_MODE: mode, **extra}))


def test_the_custom_mode_asks_for_the_url_in_a_second_step() -> None:
    flow = _flow()
    result = _init(flow, WEBHOOK_ADDRESS_CUSTOM)

    assert result["step_id"] == "webhook_url"
    assert result["description_placeholders"] == {"example_url": WEBHOOK_URL_EXAMPLE}
    assert flow._pending_input[CONF_WEBHOOK_ADDRESS_MODE] == WEBHOOK_ADDRESS_CUSTOM
    flow.async_create_entry.assert_not_called()


def test_the_custom_url_step_keeps_the_settings_of_the_first_step() -> None:
    flow = _flow(options={"audit_ignored": False})
    _init(flow, WEBHOOK_ADDRESS_CUSTOM, audit_ignored=True)

    result = asyncio.run(flow.async_step_webhook_url({CONF_WEBHOOK_URL: "https://ha.example.com:8443"}))

    assert result["type"] == "create_entry"
    assert result["data"]["audit_ignored"] is True
    assert result["data"][CONF_WEBHOOK_ADDRESS_MODE] == WEBHOOK_ADDRESS_CUSTOM
    assert result["data"][CONF_WEBHOOK_URL] == "https://ha.example.com:8443"


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


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            "unusable (plain HTTP outside the local network). Fixing the server address sets the automatic address "
            "**192.0.2.10:8123**",
        ),
        (
            "de",
            "nicht nutzbar (unverschlüsseltes HTTP außerhalb des lokalen Netzes). Die Korrektur der Server-Adresse "
            "setzt stattdessen die automatische "
            "Adresse **192.0.2.10:8123**",
        ),
    ],
)
def test_the_repair_says_when_it_sets_the_automatic_address_instead_of_the_options_url(
    language: str, expected: str
) -> None:
    form = _repair_form({"ip_mismatch": 1}, language, ha_address="192.0.2.10:8123", address_fallback="insecure_url")
    assert expected in form["description_placeholders"]["summary_text"]


@pytest.mark.parametrize("issue_data", [{}, {"ha_address": "192.0.2.10:8123", "address_fallback": None}])
def test_the_repair_adds_no_fallback_note_while_the_options_url_is_used(issue_data: dict[str, Any]) -> None:
    form = _repair_form({"ip_mismatch": 1}, **issue_data)
    assert "webhook URL from the options" not in form["description_placeholders"]["summary_text"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "* 🌐 **Server Address:** Incorrect\n  * ⚠️ The webhook URL from the options"),
        ("de", "**Server-Adresse:** Falsch\n  * ⚠️ Die Webhook-URL aus den Optionen"),
    ],
)
def test_with_configuration_issues_the_note_follows_the_address_line(language: str, expected: str) -> None:
    form = _repair_form(
        {"ip_mismatch": 1, "missing": 2}, language, ha_address="x:8123", address_fallback="insecure_url"
    )
    assert expected in form["description_placeholders"]["summary_text"]


@pytest.mark.parametrize(
    ("counts", "issue_data"),
    [
        ({"ip_mismatch": 1, "missing": 2}, {"address_fallback": None}),
        ({"ip_mismatch": 0, "missing": 2}, {"address_fallback": "insecure_url"}),
    ],
)
def test_with_configuration_issues_and_no_fallback_or_no_address_fix_there_is_no_note(
    counts: dict[str, int], issue_data: dict[str, Any]
) -> None:
    form = _repair_form(counts, ha_address="x:8123", **issue_data)
    assert "webhook URL from the options" not in form["description_placeholders"]["summary_text"]


def test_every_url_error_has_a_fallback_reason_in_both_languages() -> None:
    """A new webhook_url_error key would otherwise reach the repair as a raw key."""
    errors = {webhook_url_error(url) for url in ("ha.example.com", "http://127.0.0.1", "http://ha.example.com")}
    assert errors == set(_ADDRESS_FALLBACK_REASONS)
    assert all(de and en and de != en for de, en in _ADDRESS_FALLBACK_REASONS.values())
