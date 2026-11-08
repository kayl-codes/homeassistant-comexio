"""Webhook ids of their own for every Web-IO command (dg)."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioConnectionError
import pytest

from custom_components.comexio import webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import WebioClass
from custom_components.comexio.coordinator import ComexioCoordinator
from tests.unit.test_api_helpers import PARSED

SERVER = "srv"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
QUICKBUS_IO = {
    "id": "77",
    "ext_name": "BASE",
    "identifier": "G1",
    "is_binary": True,
    "quickbus": {"device_id": 1, "name": "MT"},
}


_run = asyncio.run


class _Store:
    """The slice of HA's Store the security object uses."""

    def __init__(self, stored: Any = None) -> None:
        self.stored = stored
        self.async_load = AsyncMock(side_effect=lambda: self.stored)
        self.async_save = AsyncMock(side_effect=self._save)
        self.async_delay_save = MagicMock()

    async def _save(self, data: Any) -> None:
        self.stored = data


class _NoLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _security(monkeypatch: pytest.MonkeyPatch, stored: Any = None) -> ws.WebhookSecurity:
    store = _Store(stored)
    monkeypatch.setattr(ws, "webhook_security_store", lambda *_args: store)
    security = ws.WebhookSecurity(MagicMock(), "entry", SERVER)
    security.secret = "s3cret"
    return security


def _payload(lua: str) -> dict[str, str]:
    """The quoted fields of a command's Lua payload (`value=a` is not one)."""
    body = re.search(r"local d = \{ (.*) \}", lua)
    assert body
    return dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', body.group(1)))


def _use_client(api: ComexioAPI, fake: Any) -> None:
    """Make fake the main-session client (kept as long as the credentials stay)."""
    api._client = fake
    api._client_credentials = api._credentials()


def _own_id(command: dict[str, Any]) -> str:
    return str(command["Parameter"]).removeprefix(ws.WEBHOOK_PATH_PREFIX)


def test_derived_ids_differ_by_key_and_secret_and_are_128_bit_hex() -> None:
    first = ws.derive_webhook_id("a", "marker:5")

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first == ws.derive_webhook_id("a", "marker:5")
    assert first != ws.derive_webhook_id("a", "marker:6")
    assert first != ws.derive_webhook_id("b", "marker:5")


def test_every_built_command_gets_its_own_id_that_only_its_payload_fits(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    security = _security(monkeypatch)
    comexio_api.webhook_security = security
    source = {**PARSED, "io": [*PARSED["io"], QUICKBUS_IO]}

    commands = comexio_api.build_webio_commands(SERVER, source)

    assert len(commands) == 5
    ids = [_own_id(c) for c in commands]
    assert len(set(ids)) == len(ids)
    for command in commands:
        payload = _payload(command["Data"])
        assert payload["server"] == SERVER
        assert security.accepts(_own_id(command), payload)
        assert not any(
            security.accepts(_own_id(command), _payload(other["Data"])) for other in commands if other is not command
        )


def test_without_security_every_command_keeps_the_one_path_and_no_server(comexio_api: ComexioAPI) -> None:
    commands = comexio_api.build_webio_commands(SERVER, PARSED)

    assert {c["Parameter"] for c in commands} == {"/api/webhook/comexio_srv"}
    assert not any("server=" in c["Data"] for c in commands)


def test_generated_class_json_carries_the_ids_too(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    comexio_api.webhook_security = _security(monkeypatch)

    template = json.loads(comexio_api.generate_webio_json(SERVER, "HA [M]", PARSED, WebioClass.MARKER))

    assert len(template["commands"]) == 2
    assert len({c["Parameter"] for c in template["commands"]}) == 2
    for command in template["commands"]:
        payload = _payload(command["Data"])
        # Review (Sourcery, #279): accepts() lets a payload without a server pass, so the field is asserted itself
        assert payload["server"] == SERVER
        # every path is the id of the command whose payload it carries, so the handler accepts the push
        assert comexio_api.webhook_security.accepts(_own_id(command), payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "marker", "id": "5"},
        {"id": "5"},  # a payload from before the type field names a marker
        {"type": "knx", "id": "5"},
        {"type": "quickbus", "id": "5"},
        {"type": "io", "ext": "BASE", "io": "Q1"},
    ],
)
def test_a_payload_names_the_key_its_command_was_derived_from(payload: dict[str, str]) -> None:
    key = ws.payload_key(payload)

    item = {"id": "5", "ext_name": "BASE", "identifier": "Q1"}
    assert key == ws.command_key(WebioClass(payload.get("type", "marker")), item)


@pytest.mark.parametrize("payload", [{}, {"type": "io", "ext": "BASE"}, {"type": "knx"}, {"value": 1}])
def test_a_payload_without_an_address_names_no_key(payload: dict[str, str]) -> None:
    assert ws.payload_key(payload) is None


def test_the_old_id_carries_any_value_only_while_it_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    old = ws.legacy_webhook_id(SERVER)

    assert security.accepts(old, {"type": "marker", "id": "5"})
    security.legacy_enabled = False
    assert not security.accepts(old, {"type": "marker", "id": "5"})


def test_a_forged_id_a_foreign_server_and_a_non_ascii_id_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    own = ws.derive_webhook_id(security.secret, "marker:5")

    assert security.accepts(own, {"type": "marker", "id": "5", "server": SERVER})
    assert not security.accepts(own, {"type": "marker", "id": "5", "server": "other"})
    assert not security.accepts(own, {"type": "marker", "id": "6"})
    assert not security.accepts("é" * 32, {"type": "marker", "id": "5"})


def test_the_first_load_creates_and_saves_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    security.secret = ""
    monkeypatch.setattr(ws.webhook, "async_generate_id", lambda: "f1" * 32)

    _run(security.async_load())

    assert security.secret == "f1" * 32
    assert security.legacy_enabled
    assert security._store.stored["secret"] == security.secret


def test_a_second_load_keeps_secret_ledger_and_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"secret": "abc", "legacy": False, "migrated": ["HA M5", 7], "last_write": NOW.isoformat()}
    security = _security(monkeypatch, stored)

    _run(security.async_load())

    assert security.secret == "abc"
    assert security.legacy_enabled is False
    assert security.migrated == {"HA M5"}
    assert security.last_write == NOW


def test_a_damaged_store_gets_a_fresh_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch, {"secret": 5, "legacy": False})
    monkeypatch.setattr(ws.webhook, "async_generate_id", lambda: "e2" * 32)

    _run(security.async_load())

    assert security.secret == "e2" * 32
    assert security._store.stored["secret"] == "e2" * 32


def test_a_saved_command_with_its_own_path_enters_the_ledger(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    _use_client(comexio_api, SimpleNamespace(save_webio_command=AsyncMock()))
    own = comexio_api.build_webio_commands(SERVER, PARSED)[0]
    old = {"Name": "HA old", "Parameter": "/api/webhook/comexio_srv"}

    assert _run(comexio_api.save_single_command(1, 2, old, existing_cmd_id=4))
    assert not security.migrated
    assert _run(comexio_api.save_single_command(1, 2, own, existing_cmd_id=3))

    assert security.migrated == {own["Name"]}
    assert security.last_write is not None


def test_a_failed_save_does_not_enter_the_ledger(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    _use_client(comexio_api, SimpleNamespace(save_webio_command=AsyncMock(side_effect=ComexioConnectionError("down"))))

    assert not _run(comexio_api.save_single_command(1, 2, comexio_api.build_webio_commands(SERVER, PARSED)[0]))

    assert not security.migrated


def test_an_uploaded_class_enters_the_ledger(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    _use_client(comexio_api, SimpleNamespace(upload_webio_class=AsyncMock(return_value="9")))
    template = comexio_api.generate_webio_json(SERVER, "HA [M]", PARSED, WebioClass.MARKER)

    assert _run(comexio_api.upload_web_io(SERVER, "HA [M]", template)) == (True, "9")

    assert security.migrated == {"HA M5 Licht", "HA M6 Soll"}


def _coordinator(monkeypatch: pytest.MonkeyPatch, *, boot: datetime | None, written: datetime | None) -> Any:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = SERVER
    coordinator.hass = MagicMock()
    security = _security(monkeypatch)
    security.last_write = written
    security.async_save = AsyncMock()  # type: ignore[method-assign]
    coordinator.api = SimpleNamespace(webhook_security=security, get_webio_command_form=AsyncMock(return_value=None))
    coordinator.system_info = SimpleNamespace(boot_time=boot)
    coordinator.sync_webhooks = MagicMock()
    # nothing stored to verify: the finish step reads no form
    coordinator.data = {"webio_commands": {}, "webio_devices": {}}
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    coordinator._webio_range_targets = dict
    return coordinator


def test_the_old_id_stays_until_comexio_restarted_after_the_last_write(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=NOW - timedelta(hours=1), written=NOW)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert coordinator.api.webhook_security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()


def test_an_unknown_boot_time_does_not_switch_the_old_id_off(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=NOW)

    assert _run(coordinator._async_finish_webhook_migration()) == "needs_restart"

    assert coordinator.api.webhook_security.legacy_enabled


def test_the_old_id_goes_off_after_a_restart_following_the_last_write(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=NOW + timedelta(hours=1), written=NOW)
    security = coordinator.api.webhook_security
    saved: list[bool] = []

    async def record_save() -> None:
        saved.append(security.legacy_enabled)

    security.async_save.side_effect = record_save

    assert _run(coordinator._async_finish_webhook_migration()) == "done"

    assert not security.legacy_enabled
    # Review (Sourcery, #279): what is saved is the switched-off state; a save before the flip would reload as on
    assert saved
    assert saved[-1] is False
    coordinator.sync_webhooks.assert_called_once_with()


def test_nothing_ever_written_switches_the_old_id_off_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)

    assert _run(coordinator._async_finish_webhook_migration()) == "done"


def test_only_commands_without_their_own_path_are_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.api.webhook_security.migrated = {"HA done"}
    coordinator.data = {
        "webio_commands": {
            "HA done": {"cmdId": "1", "webioClass": "marker"},
            "HA todo": {"cmdId": "2", "webioClass": "marker"},
            "HA no id": {"cmdId": "", "webioClass": "marker"},
            "HA no target": {"cmdId": "4", "webioClass": "marker"},
        },
        "webio_devices": {"marker": {"device_id": "7", "base_id": "8"}},
    }
    target = {"Name": "x"}
    coordinator._webio_range_targets = lambda: {"HA done": target, "HA todo": target, "HA no id": target}

    assert [name for name, *_ in coordinator._pending_webhook_migrations()] == ["HA todo"]


def test_a_run_counts_rewritten_and_failed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: False)
    coordinator._sync_lock = _NoLock()
    device = {"device_id": "7", "base_id": "8"}
    pending = [(f"HA {n}", {"cmdId": str(n)}, device, {"Name": f"HA {n}"}) for n in (1, 2)]
    coordinator._pending_webhook_migrations = lambda: pending
    coordinator.api.save_single_command = AsyncMock(side_effect=[True, False])

    assert _run(coordinator._async_rewrite_pending_webhooks(coordinator.api.webhook_security)) == (1, 1)


def test_a_run_stops_while_a_function_plan_restore_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _coordinator(monkeypatch, boot=None, written=None)
    coordinator.unloading = False
    coordinator._restore_lock = SimpleNamespace(locked=lambda: True)
    coordinator._sync_lock = _NoLock()
    coordinator._pending_webhook_migrations = lambda: [("HA 1", {"cmdId": "1"}, {}, {})]
    coordinator.api.save_single_command = AsyncMock()

    assert _run(coordinator._async_rewrite_pending_webhooks(coordinator.api.webhook_security)) == (0, 0)

    coordinator.api.save_single_command.assert_not_awaited()


@pytest.mark.parametrize("stamp", ["garbage", None])
def test_a_lost_write_stamp_with_a_ledger_counts_as_written_now(monkeypatch: pytest.MonkeyPatch, stamp: Any) -> None:
    security = _security(monkeypatch, {"secret": "abc", "migrated": ["HA M5"], "last_write": stamp})

    _run(security.async_load())

    assert security.last_write is not None  # the old id waits for a Comexio restart from now on


def test_no_stamp_and_no_ledger_stays_unstamped(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch, {"secret": "abc", "migrated": []})

    _run(security.async_load())

    assert security.last_write is None


def test_two_addresses_that_differ_only_in_where_a_colon_falls_get_different_ids() -> None:
    """(Sourcery, dg #279) `(A:B, C)` and `(A, B:C)` must not share an HMAC input."""
    first = {"ext_name": "A:B", "identifier": "C"}
    second = {"ext_name": "A", "identifier": "B:C"}

    assert ws.command_key(WebioClass.IO, first) != ws.command_key(WebioClass.IO, second)
    assert ws.payload_key({"type": "io", "ext": "A:B", "io": "C"}) == ws.command_key(WebioClass.IO, first)
    assert ws.payload_key({"type": "io", "ext": "A", "io": "B:C"}) == ws.command_key(WebioClass.IO, second)


def test_the_key_of_an_address_without_colons_is_unchanged_so_migrated_ids_stay_valid() -> None:
    assert ws.command_key(WebioClass.IO, {"ext_name": "BASE", "identifier": "Q1"}) == "io:BASE:Q1"
    assert ws.command_key(WebioClass.MARKER, {"id": "5"}) == "marker:5"


def test_the_escape_keeps_a_literal_escape_sequence_apart_from_the_colon_it_stands_for() -> None:
    literal = {"ext_name": "A%3AB", "identifier": "C"}
    colon = {"ext_name": "A:B", "identifier": "C"}

    assert ws.command_key(WebioClass.IO, literal) != ws.command_key(WebioClass.IO, colon)


def test_the_item_id_of_marker_quickbus_and_knx_keys_is_escaped_the_same_way_on_both_sides() -> None:
    assert ws.payload_key({"type": "knx", "id": "a:b"}) == "knx:a%3Ab"
    assert ws.payload_key({"id": "a:b"}) == ws.command_key(WebioClass.MARKER, {"id": "a:b"})
    assert ws.payload_key({"type": "quickbus", "id": "a:b"}) == ws.command_key(WebioClass.QUICKBUS, {"id": "a:b"})
