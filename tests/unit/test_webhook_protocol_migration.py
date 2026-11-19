"""The move of the Web-IO commands to the protocol Home Assistant is reached with (#165), end to end.

Comexio's scrape does not show a command's protocol, so the ledger (``https_written``) is the record, the nightly run
rewrites the commands whose recorded protocol is not the target's, and the read-back of the migration checks the
protocol of the command in Comexio.
"""

from datetime import timedelta
import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiocomexio import ComexioError
from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
import pytest

from custom_components.comexio import button as button_module, webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.coordinator import ComexioCoordinator
from tests.unit.test_api_helpers import PARSED
from tests.unit.test_webhook_security import NOW, SERVER, _run, _security, _use_client
from tests.unit.test_webhook_security_flow import _tick_coordinator

NAME = "HA m"


def _protocol_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    held_protocol: int | None,
    boot: Any,
    legacy: bool = False,
    target_protocol: int = WEBIO_PROTOCOL_HTTPS,
) -> tuple[Any, dict[str, Any]]:
    """One command with its own path, held in Comexio with held_protocol; HA is on target_protocol."""
    coordinator = _tick_coordinator(monkeypatch, cmd_id="9", boot=boot)
    security = coordinator.api.webhook_security
    security.legacy_enabled = legacy
    security.migrated.add(NAME)
    target = {**coordinator._webio_range_targets()[NAME], "Protocol": target_protocol}
    coordinator._webio_range_targets = lambda: {NAME: target}
    held = {"Parameter": target["Parameter"], "Data": target["Data"], "Protocol": held_protocol}

    async def _send(_device: Any, payload: dict[str, Any], **_k: Any) -> None:
        held.update(Parameter=payload["Parameter"], Data=payload["Data"], Protocol=payload["Protocol"])

    coordinator.api.client.save_webio_command = AsyncMock(side_effect=_send)
    coordinator.api.get_webio_command_form = AsyncMock(
        side_effect=lambda *_a, **_k: SimpleNamespace(
            parameter=held["Parameter"], data=held["Data"], protocol=held["Protocol"]
        )
    )
    return coordinator, held


def test_a_written_command_is_recorded_with_its_protocol_and_a_rewrite_to_another_one_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _security(monkeypatch)

    security.mark_written("HA a", NOW, https=True)
    assert security.carries_https("HA a")
    assert security.protocol_write is None  # a new command needs no protocol move

    security.mark_written("HA a", NOW + timedelta(hours=1), https=True)
    assert security.protocol_write is None  # the same protocol again moves nothing

    security.mark_written("HA a", NOW + timedelta(hours=2), https=False)
    assert not security.carries_https("HA a")
    assert security.protocol_write == NOW + timedelta(hours=2)


def test_save_single_command_records_the_protocol_the_command_is_written_with(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.comexio.api.dt_util.utcnow", lambda: NOW)
    comexio_api.webhook_security = security = _security(monkeypatch)
    _use_client(comexio_api, SimpleNamespace(save_webio_command=AsyncMock()))
    comexio_api.webhook_https = True
    https_command = comexio_api.build_webio_commands(SERVER, PARSED)[0]
    comexio_api.webhook_https = False
    http_command = comexio_api.build_webio_commands(SERVER, PARSED)[1]

    assert _run(comexio_api.save_single_command(1, 2, https_command))
    assert _run(comexio_api.save_single_command(1, 2, http_command))

    assert security.carries_https(https_command["Name"])
    assert not security.carries_https(http_command["Name"])


def test_the_ledger_keeps_the_protocols_and_the_marker_across_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch)
    security.legacy_enabled = False
    security.mark_written("HA m", NOW, https=False)
    security.mark_written("HA m", NOW, https=True)
    _run(security.async_save())
    again = _security(monkeypatch, security._store.stored)

    _run(again.async_load())

    assert again.https_written == {"HA m"}
    assert again.protocol_write == NOW


@pytest.mark.parametrize("stored", [None, "HA m", 5, ["ok", 1]])
def test_a_hand_edited_protocol_ledger_is_read_without_failing(monkeypatch: pytest.MonkeyPatch, stored: Any) -> None:
    security = _security(monkeypatch, {"secret": "abc", "https_written": stored, "protocol_write": "garbage"})

    _run(security.async_load())

    assert security.https_written == ({"ok"} if stored == ["ok", 1] else set())
    assert security.protocol_write is None


def test_the_protocol_checks_of_the_read_back() -> None:
    https = {"Protocol": WEBIO_PROTOCOL_HTTPS}
    http = {"Protocol": WEBIO_PROTOCOL_HTTP}

    assert ws.protocol_deviation(https, WEBIO_PROTOCOL_HTTPS) is None
    assert ws.protocol_deviation(https, WEBIO_PROTOCOL_HTTP) == "protocol is not the one Home Assistant is reached with"
    assert ws.protocol_deviation(http, WEBIO_PROTOCOL_HTTPS)
    assert ws.protocol_deviation(https, None) is None  # unknown is no deviation: see protocol_unproven
    assert ws.protocol_unproven(https, None)
    assert not ws.protocol_unproven(http, None)  # as it always was: a form without the field does not block HTTP
    assert not ws.protocol_unproven({}, None)
    assert not ws.protocol_unproven(https, WEBIO_PROTOCOL_HTTPS)
    assert ws.protocol_unproven(http, None, moving=True)  # a move to HTTP waits for proof that HTTPS is gone
    assert not ws.protocol_unproven(http, WEBIO_PROTOCOL_HTTP, moving=True)
    assert not ws.protocol_unproven(http, WEBIO_PROTOCOL_HTTPS, moving=True)  # that one deviates instead


def test_the_nightly_run_moves_a_command_to_https_when_the_old_id_is_off_and_waits_for_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS  # the command really went out with the target's protocol
    assert security.carries_https(NAME)
    assert security.protocol_write == NOW
    assert not security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()

    # Comexio has not restarted: the next night asks again instead of ending, nothing is rewritten twice
    coordinator.api.client.save_webio_command.reset_mock()
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    coordinator.api.client.save_webio_command.assert_not_awaited()

    coordinator.system_info.boot_time = security.last_write + timedelta(minutes=5)

    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert security.protocol_write is None  # read back right: nothing waits for a restart any more
    assert security.protocol_moved == set()
    assert not security.legacy_enabled
    coordinator.sync_webhooks.assert_not_called()  # the old id was off already
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"  # and now the night has nothing to do


def test_the_nightly_run_leaves_a_command_alone_that_carries_the_target_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTPS, boot=NOW + timedelta(hours=1)
    )
    coordinator.api.webhook_security.https_written.add(NAME)

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_a_command_in_comexio_that_is_not_https_after_the_restart_goes_back_to_the_ledger_gap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ledger says HTTPS, Comexio holds HTTP (a hand edit): the read-back finds it and the next run rewrites it."""
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)  # a move that waited for the restart, which has happened

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert "protocol is not the one Home Assistant is reached with" in caplog.text
    assert NAME not in security.migrated
    assert security.protocol_write is not None  # still going: the read-back did not come out clean
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTP

    # rewritten; Comexio booted after the first write, so the read-back of this run is clean and it ends
    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS


def test_a_form_without_a_protocol_does_not_prove_an_https_command_and_is_not_rewritten_over_and_over(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, held = _protocol_coordinator(monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert "1 could not be read" in caplog.text
    assert NAME in security.migrated  # unreadable is no deviation: nothing is taken out of the ledger
    coordinator.api.client.save_webio_command.assert_not_awaited()
    assert held["Protocol"] is None


def test_a_form_without_a_protocol_does_not_prove_the_move_of_a_command_to_http(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security
    security.protocol_write = NOW - timedelta(hours=2)  # moved from HTTPS to HTTP, Comexio restarted since
    security.protocol_moved = {NAME}  # this entry rewrote it from HTTPS to HTTP

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert "1 could not be read" in caplog.text
    assert security.protocol_write is not None  # not ended, as the form proves nothing
    assert NAME in security.migrated  # unreadable is no deviation: nothing is taken out of the ledger
    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_a_form_that_shows_http_proves_the_move_of_a_command_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch,
        held_protocol=WEBIO_PROTOCOL_HTTP,
        boot=NOW + timedelta(hours=1),
        target_protocol=WEBIO_PROTOCOL_HTTP,
    )
    security = coordinator.api.webhook_security
    security.protocol_write = NOW - timedelta(hours=2)

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert security.protocol_write is None
    coordinator.api.get_webio_command_form.assert_awaited()


def test_a_form_without_a_protocol_blocks_no_http_command_when_no_protocol_move_waits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security
    security.legacy_enabled = True  # a plain path migration: as it always was, the missing field blocks nothing
    security.last_write = NOW - timedelta(hours=2)  # Comexio booted after the last write

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert not security.legacy_enabled  # verified (the read-back ran) and the old id is off
    coordinator.api.get_webio_command_form.assert_awaited()
    assert "could not be read" not in caplog.text


def test_the_forced_run_keeps_a_command_without_ids_in_the_ledger_and_does_not_end_while_its_protocol_is_wrong(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""  # no ids: the run cannot rewrite it

    assert _run(coordinator.async_force_webhook_migration()) == "pending"  # not "done"

    assert NAME in security.migrated  # still tracked, so the next night looks at it again
    assert security.protocol_write is not None
    assert NAME in caplog.text


def test_a_command_without_ids_that_needs_another_protocol_is_named_and_keeps_the_run_pending(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert "cannot be moved to the protocol Home Assistant is reached with" in caplog.text
    assert NAME in caplog.text
    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_the_forced_run_rewrites_every_command_even_when_the_ledger_says_it_is_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)  # the ledger believes HTTPS, Comexio holds HTTP

    assert _run(coordinator.async_force_webhook_migration()) == "needs_restart"

    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS
    assert security.protocol_write is not None  # the read-back after the restart follows, as after the nightly run
    coordinator.api.client.save_webio_command.assert_awaited_once()


@pytest.mark.parametrize("busy", ["_sync_lock", "_restore_lock"])
def test_the_forced_run_does_not_start_while_a_sync_or_a_restore_runs(
    monkeypatch: pytest.MonkeyPatch, busy: str
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    setattr(coordinator, busy, SimpleNamespace(locked=lambda: True))
    coordinator.async_webhook_target = AsyncMock()
    security = coordinator.api.webhook_security

    assert _run(coordinator.async_force_webhook_migration()) == "skipped"

    assert NAME in security.migrated  # the ledger was not touched
    coordinator.api.client.save_webio_command.assert_not_awaited()
    # resolving rewrites the protocol every builder follows: not while a sync or a restore is under way
    coordinator.async_webhook_target.assert_not_awaited()


@pytest.mark.parametrize("taken", ["unloading", "_webhook_migration_running", "_sync_lock", "_restore_lock"])
def test_the_forced_run_checks_again_after_it_resolved_the_target(monkeypatch: pytest.MonkeyPatch, taken: str) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )

    async def _resolve(*_a: Any) -> None:
        # the await is where an unload, a sync or a restore can come in
        if taken in {"unloading", "_webhook_migration_running"}:
            setattr(coordinator, taken if taken == "_webhook_migration_running" else "unloading", True)
        else:
            setattr(coordinator, taken, SimpleNamespace(locked=lambda: True))

    coordinator.async_webhook_target = AsyncMock(side_effect=_resolve)
    security = coordinator.api.webhook_security

    assert _run(coordinator.async_force_webhook_migration()) == "skipped"

    coordinator.async_webhook_target.assert_awaited_once()
    assert NAME in security.migrated  # the ledger was not emptied
    coordinator.api.client.save_webio_command.assert_not_awaited()


@pytest.mark.parametrize("busy", ["_sync_lock", "_restore_lock", "_webhook_migration_running"])
def test_the_nightly_run_neither_resolves_nor_runs_while_a_sync_a_restore_or_a_run_is_going_on(
    monkeypatch: pytest.MonkeyPatch, busy: str
) -> None:
    """A run that went on with an unresolved target would write with the protocol of before (#165)."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    if busy == "_webhook_migration_running":
        coordinator._webhook_migration_running = True
    else:
        setattr(coordinator, busy, SimpleNamespace(locked=lambda: True))
    coordinator.async_webhook_target = AsyncMock()
    coordinator._async_run_webhook_migration = AsyncMock(return_value="done")

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator.async_webhook_target.assert_not_awaited()
    coordinator._async_run_webhook_migration.assert_not_awaited()


@pytest.mark.parametrize("taken", ["unloading", "_sync_lock", "_restore_lock", "_webhook_migration_running"])
def test_the_nightly_run_checks_again_after_it_resolved_the_target(monkeypatch: pytest.MonkeyPatch, taken: str) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )

    async def _resolve(*_a: Any) -> None:
        # the await is where an unload, a sync, a restore or a forced run can come in
        if taken in {"unloading", "_webhook_migration_running"}:
            setattr(coordinator, taken, True)
        else:
            setattr(coordinator, taken, SimpleNamespace(locked=lambda: True))

    coordinator.async_webhook_target = AsyncMock(side_effect=_resolve)
    security = coordinator.api.webhook_security

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator.async_webhook_target.assert_awaited_once()
    assert NAME in security.migrated  # the ledger was not emptied
    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_the_nightly_target_is_not_resolved_for_an_entry_that_is_already_unloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    coordinator.unloading = True
    coordinator.async_webhook_target = AsyncMock()

    assert _run(coordinator._async_resolve_webhook_target_for_nightly_run()) is False

    coordinator.async_webhook_target.assert_not_awaited()


def test_the_restart_request_after_a_start_waits_for_the_boot_time_while_a_protocol_move_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=None)
    security = coordinator.api.webhook_security
    security.protocol_write = NOW
    security.last_write = NOW
    coordinator._webhook_notices_restored = False
    notified = []
    coordinator._notify_webhook_restart = lambda: notified.append(True)
    coordinator._dismiss_webhook_restart = lambda: notified.append(False)

    coordinator._restore_webhook_migration_notices()
    assert notified == []
    assert not coordinator._webhook_notices_restored  # waits for the first reading of Comexio's boot time

    coordinator.system_info.boot_time = NOW - timedelta(hours=1)
    coordinator._restore_webhook_migration_notices()
    assert notified == [True]
    assert coordinator._webhook_notices_restored


def test_an_old_install_without_the_protocol_keys_rewrites_nothing_on_an_http_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upgrade case: HA on HTTP as ever, the form has no protocol field: nothing blocks, nothing is rewritten."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security

    assert security.https_written == set()
    assert security.protocol_write is None
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"
    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_a_command_moves_back_to_http_when_home_assistant_is_reached_with_http_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch,
        held_protocol=WEBIO_PROTOCOL_HTTPS,
        boot=NOW - timedelta(hours=1),
        target_protocol=WEBIO_PROTOCOL_HTTP,
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)

    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    assert held["Protocol"] == WEBIO_PROTOCOL_HTTP
    assert not security.carries_https(NAME)
    assert security.protocol_write == NOW

    coordinator.system_info.boot_time = security.last_write + timedelta(minutes=5)

    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert security.protocol_write is None


def test_only_the_commands_with_another_protocol_are_rewritten_and_the_others_stay_in_the_ledger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    base = coordinator._webio_range_targets()[NAME]
    coordinator._webio_range_targets = lambda: {
        NAME: base,
        "HA ok": {**base, "Name": "HA ok"},
        "HA nocmd": {**base, "Name": "HA nocmd"},
    }
    coordinator.data["webio_commands"]["HA ok"] = {"cmdId": "10", "webioClass": "marker"}
    coordinator.data["webio_commands"]["HA nocmd"] = {"cmdId": "", "webioClass": "marker"}
    security.migrated.update({"HA ok", "HA nocmd"})
    security.https_written.add("HA ok")  # right already

    state = _run(coordinator._async_webhook_migration_tick())

    assert state == "pending"  # the command without ids still waits
    sent = [call.args[1]["Name"] for call in coordinator.api.client.save_webio_command.await_args_list]
    assert sent == [NAME]
    assert "HA ok" in security.migrated
    assert security.carries_https("HA ok")
    assert "HA nocmd" in caplog.text


def test_the_old_id_goes_off_with_the_protocol_move_in_one_run_and_the_marker_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1), legacy=True
    )
    security = coordinator.api.webhook_security
    security.protocol_moved = {NAME}  # must go with the marker
    security.protocol_write = NOW - timedelta(hours=2)
    security.last_write = NOW - timedelta(hours=2)
    security.https_written.add(NAME)
    coordinator.api.get_webio_command_form = AsyncMock(
        return_value=SimpleNamespace(
            parameter=coordinator._webio_range_targets()[NAME]["Parameter"],
            data=coordinator._webio_range_targets()[NAME]["Data"],
            protocol=WEBIO_PROTOCOL_HTTPS,
        )
    )

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert not security.legacy_enabled
    assert security.protocol_write is None
    assert security.protocol_moved == set()
    coordinator.sync_webhooks.assert_called()


def test_a_failed_write_of_the_forced_run_leaves_the_move_open_for_the_next_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    send = coordinator.api.client.save_webio_command
    coordinator.api.client.save_webio_command = AsyncMock(side_effect=ConnectionError("down"))

    state = _run(coordinator.async_force_webhook_migration())

    assert state != "done"
    # The forced run did try the write (a run that skips it also leaves everything below as it is, and the night
    # after would then do the write this test expects).
    coordinator.api.client.save_webio_command.assert_awaited_once()
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTP
    assert NAME not in security.migrated  # not recorded as moved
    assert security.protocol_write is not None  # the next night does not skip: it tries again

    coordinator.api.client.save_webio_command = send
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS


_UPLOAD_TEMPLATE = json.dumps(
    {
        "commands": [
            {"Name": "HA s", "Parameter": "/api/webhook/a", "Protocol": WEBIO_PROTOCOL_HTTPS},
            {"Name": "HA p", "Parameter": "/api/webhook/b", "Protocol": WEBIO_PROTOCOL_HTTP},
            {"Name": "HA n", "Parameter": "/api/webhook/c"},
        ]
    }
)


def test_an_uploaded_class_template_records_each_commands_protocol(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through upload_web_io itself, so a successful upload that stops recording fails this."""
    comexio_api.webhook_security = security = _security(monkeypatch)
    upload = AsyncMock(return_value="7")
    _use_client(comexio_api, SimpleNamespace(upload_webio_class=upload))

    assert _run(comexio_api.upload_web_io(SERVER, "Cls", _UPLOAD_TEMPLATE)) == (True, "7")

    upload.assert_awaited_once()
    assert security.https_written == {"HA s"}


@pytest.mark.parametrize("outcome", ["error", "no_base_id"])
def test_a_failed_class_upload_records_no_protocol(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    upload = AsyncMock(side_effect=ComexioError("refused")) if outcome == "error" else AsyncMock(return_value="")
    _use_client(comexio_api, SimpleNamespace(upload_webio_class=upload))

    ok, _why = _run(comexio_api.upload_web_io(SERVER, "Cls", _UPLOAD_TEMPLATE))

    assert not ok
    assert security.https_written == set()  # Comexio holds none of these commands


def test_a_second_run_while_one_is_going_on_is_skipped_and_leaves_the_ledger_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    coordinator._webhook_migration_running = True  # the nightly run is under way

    assert _run(coordinator.async_force_webhook_migration()) == "skipped"
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    assert NAME in security.migrated  # the forced run did not empty it under the first one
    assert security.protocol_write is None
    coordinator.api.client.save_webio_command.assert_not_awaited()


def test_the_flag_of_a_run_is_down_again_after_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    coordinator._async_rewrite_pending_webhooks = AsyncMock(side_effect=RuntimeError("boom"))

    forced = coordinator.async_force_webhook_migration()
    with pytest.raises(RuntimeError):
        _run(forced)

    assert not coordinator._webhook_migration_running


def test_the_forced_run_resolves_the_target_before_it_empties_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    coordinator.async_webhook_target = AsyncMock(side_effect=OSError("adapters"))

    forced = coordinator.async_force_webhook_migration()
    with pytest.raises(OSError, match="adapters"):
        _run(forced)

    assert NAME in security.migrated  # nothing was cleared that no save would bring back
    assert security.protocol_write is None


def test_a_forced_run_on_an_entry_without_stored_commands_ends_when_the_old_id_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.protocol_moved = {NAME}  # must go with the marker
    coordinator.data["webio_commands"] = {}
    coordinator._last_poll_scraped = True
    coordinator._own_web_device_count = 0  # `$WebDevices` was read and holds no device of ours
    for attr, value in _SCRAPE_STATE.items():
        setattr(coordinator, attr, value)

    assert _run(coordinator.async_force_webhook_migration()) == "done"

    assert security.protocol_write is None  # not "the old id stays on" every night, with the old id long off
    assert security.protocol_moved == set()
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"


_NO_PROOF = [(False, 0), (False, None), (True, None), (True, 1)]
_SCRAPE_STATE = {"last_update_success": True, "_polls_open": 0, "_poll_overlap": False}


@pytest.mark.parametrize(("scraped", "own_devices"), _NO_PROOF)
@pytest.mark.parametrize("path", ["forced", "nightly"])
def test_an_empty_command_list_does_not_end_a_protocol_move_without_the_proof_that_none_exists(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
    scraped: bool,
    own_devices: int | None,
) -> None:
    """An incomplete scrape (no `$WebDevices`, or a device of ours) must not clear the marker and the moved set."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.protocol_moved = {NAME}
    security.protocol_write = NOW  # the move is under way (a forced run sets it itself)
    coordinator.data["webio_commands"] = {}
    coordinator._last_poll_scraped = scraped
    coordinator._own_web_device_count = own_devices
    for attr, value in _SCRAPE_STATE.items():
        setattr(coordinator, attr, value)
    coordinator._clear_webhook_migration_notices = MagicMock()
    coordinator._async_save_webhook_security = AsyncMock(return_value=True)

    run = coordinator.async_force_webhook_migration if path == "forced" else coordinator._async_webhook_migration_tick
    assert _run(run()) == "pending"

    assert security.protocol_write is not None  # still waiting: a later complete scrape is read back
    assert security.protocol_moved == {NAME}
    assert not security.legacy_enabled
    coordinator._clear_webhook_migration_notices.assert_not_called()  # the repairs and notices stay
    assert "The protocol move is not over" in caplog.text
    assert "Every Web-IO command carries the protocol" not in caplog.text

    # A later scrape that read `$WebDevices` without a device of ours ends it.
    coordinator._last_poll_scraped = True
    coordinator._own_web_device_count = 0
    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert security.protocol_write is None
    assert security.protocol_moved == set()


@pytest.mark.parametrize(
    "state", [{"_polls_open": 1}, {"_poll_overlap": True}, {"last_update_success": False}], ids=str
)
def test_the_proof_that_no_device_exists_is_not_taken_from_a_poll_that_is_not_through(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> None:
    """The scrape flags are set before a poll commits its data: they do not belong to the data of this run yet."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.protocol_moved = {NAME}
    coordinator.data["webio_commands"] = {}
    coordinator._last_poll_scraped = True
    coordinator._own_web_device_count = 0
    for attr, value in {**_SCRAPE_STATE, **state}.items():
        setattr(coordinator, attr, value)

    assert _run(coordinator.async_force_webhook_migration()) == "pending"

    assert security.protocol_moved == {NAME}


def test_a_command_that_could_not_be_moved_after_the_old_id_is_off_raises_no_repair_about_the_old_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""  # no ids: cannot be rewritten
    coordinator._report_unrewritable_webhooks = MagicMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    coordinator._report_unrewritable_webhooks.assert_not_called()  # its text says the old id stays on
    assert NAME in caplog.text  # but the command is named


def test_a_failed_save_at_the_end_of_a_protocol_move_is_retried_by_a_delayed_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTPS, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)
    security.protocol_moved = {NAME}
    coordinator._async_save_webhook_security = AsyncMock(return_value=False)
    security.schedule_save = MagicMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert security.protocol_write is None
    assert security.protocol_moved == set()
    security.schedule_save.assert_called_once_with()


@pytest.mark.parametrize(("https", "saved"), [(False, True), (True, True), (False, False)])
def test_a_protocol_move_is_not_over_when_a_sync_writes_a_command_while_the_end_is_saved(
    monkeypatch: pytest.MonkeyPatch, https: bool, saved: bool
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTPS, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)
    written = NOW + timedelta(hours=2)

    async def _save(_security: Any) -> bool:
        # the await is where a sync can write: back to HTTP (a move) or again with HTTPS; Comexio restarts for both
        security.mark_written(NAME, written, https=https)
        return saved

    coordinator._async_save_webhook_security = AsyncMock(side_effect=_save)
    coordinator._notify_webhook_restart = MagicMock()
    coordinator._clear_webhook_migration_notices = MagicMock()
    security.schedule_save = MagicMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    # The marker is the sync's write, after the end cleared it, not the one the run began with: also for a write that
    # keeps HTTPS (it sets none itself), or the next night would skip and nothing would take the request back once
    # Comexio has restarted. It is saved again (a failed save is retried as well).
    assert security.protocol_write == written
    assert security.protocol_moved == (set() if https else {NAME})
    coordinator._notify_webhook_restart.assert_called_once_with()
    coordinator._clear_webhook_migration_notices.assert_not_called()
    # the end and the re-armed marker are each saved by an awaited save; only a failed one falls back to a delayed save
    assert coordinator._async_save_webhook_security.await_count == 2
    assert security.schedule_save.call_count == (0 if saved else 2)


def test_the_night_after_a_sync_wrote_at_the_end_of_a_protocol_move_ends_it_once_comexio_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTPS, boot=NOW + timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)
    written = NOW + timedelta(hours=2)

    async def _save(_security: Any) -> bool:
        security.mark_written(NAME, written, https=True)  # the sync writes the command as it is: no protocol move
        return True

    coordinator._async_save_webhook_security = AsyncMock(side_effect=_save)
    coordinator._notify_webhook_restart = MagicMock()
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    coordinator._notify_webhook_restart.assert_called_once_with()  # the request stands until Comexio restarted

    coordinator._async_save_webhook_security = AsyncMock(return_value=True)
    coordinator.system_info.boot_time = written + timedelta(hours=1)  # Comexio restarted
    coordinator._clear_webhook_migration_notices = MagicMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert security.protocol_write is None
    coordinator._clear_webhook_migration_notices.assert_called_once_with()


def test_the_forced_run_that_switches_the_old_id_off_for_lack_of_commands_clears_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1), legacy=True
    )
    security = coordinator.api.webhook_security
    coordinator.data["webio_commands"] = {}
    coordinator._legacy_webhook_id_unneeded = lambda _security: True
    security.protocol_moved = {NAME}

    assert _run(coordinator.async_force_webhook_migration()) == "done"

    assert not security.legacy_enabled
    assert security.protocol_write is None  # not left to raise a restart request after the next start
    assert security.protocol_moved == set()


def test_a_command_without_ids_does_not_hold_back_the_restart_request_when_the_old_id_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It holds its right path and protocol, so the forced run leaves it in the ledger; the others are rewritten."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    base = coordinator._webio_range_targets()[NAME]
    coordinator._webio_range_targets = lambda: {NAME: base, "HA nocmd": {**base, "Name": "HA nocmd"}}
    coordinator.data["webio_commands"]["HA nocmd"] = {"cmdId": "", "webioClass": "marker"}
    coordinator.api.webhook_security.https_written.add("HA nocmd")  # its protocol is right already
    coordinator.api.webhook_security.migrated.add("HA nocmd")
    notified: list[bool] = []
    coordinator._notify_webhook_restart = lambda: notified.append(True)

    assert _run(coordinator.async_force_webhook_migration()) == "needs_restart"

    assert notified == [True]  # the restart for the rewritten command is asked for
    assert "HA nocmd" in coordinator.api.webhook_security.migrated  # not forgotten: nothing to move, nothing to warn
    assert "HA nocmd" not in caplog.text


def _button(forced: AsyncMock) -> ComexioSyncButton:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(async_force_webhook_migration=forced)  # type: ignore[assignment]
    button.server_id = "cx1"
    button.async_handle_press = AsyncMock()  # type: ignore[method-assign]
    button.hass = MagicMock()
    return button


def test_press_action_webhook_migration_runs_outside_the_sync() -> None:
    forced = AsyncMock(return_value="done")
    button = _button(forced)
    _run(button.async_press_action("webhook_migration"))
    forced.assert_awaited_once_with()
    button.async_handle_press.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize("action", ["full_sync", "update_ip", "create_missing"])
def test_press_action_hands_every_other_action_to_the_sync(action: str) -> None:
    forced = AsyncMock()
    button = _button(forced)
    _run(button.async_press_action(action))
    button.async_handle_press.assert_awaited_once_with(action)  # type: ignore[attr-defined]
    forced.assert_not_awaited()


def test_the_press_action_service_is_served_by_the_dispatcher() -> None:
    registered = inspect.getsource(button_module.async_setup_entry)
    assert '"async_press_action",' in registered
    assert '"async_handle_press",' not in registered


@pytest.mark.parametrize("state", ["done", "needs_restart", "pending", "failed", "skipped"])
def test_the_forced_migration_tells_the_user_its_result(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    created = MagicMock()
    monkeypatch.setattr(button_module.persistent_notification, "async_create", created)
    button = _button(AsyncMock(return_value=state))

    _run(button.async_press_action("webhook_migration"))

    created.assert_called_once()
    message = created.call_args.args[1]
    assert message.startswith(button_module.WEBHOOK_MIGRATION_RESULT_TEXT[state])
    assert "duration" in message
    assert created.call_args.kwargs["notification_id"] == "comexio_webhook_migration_cx1"


def test_the_generate_web_io_upload_replaces_a_class_under_the_sync_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The migration rewrites the commands of a class under that lock: it must not meet one that is deleted."""
    import asyncio

    from custom_components.comexio.services import misc as services_misc

    lock = asyncio.Lock()
    held_during_upload: list[bool] = []

    async def upload(*_a: Any, **_k: Any) -> str:
        held_during_upload.append(lock.locked())
        return "ok"

    held_during_build: list[bool] = []

    def build(*_a: Any, **_k: Any) -> str:
        held_during_build.append(lock.locked())
        return "{}"

    coordinator = SimpleNamespace(
        api=SimpleNamespace(generate_webio_json=build),
        server_id="srv",
        config_entry=SimpleNamespace(data={}, options={}),
        required_webio_classes=("marker",),
        async_webhook_target=AsyncMock(),
        _sync_lock=lock,
        _restore_lock=SimpleNamespace(locked=lambda: False),
        unloading=False,
        webio_source_data=dict,
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
    )
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    hass = SimpleNamespace(data={"comexio": {"e1": coordinator}})

    _run(services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": True})))

    assert held_during_build == [True]  # the JSON is built from the source data of the moment the lock is held
    assert held_during_upload == [True]
    assert not lock.locked()


def test_the_generate_web_io_upload_waits_for_a_run_that_holds_the_sync_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from custom_components.comexio.services import misc as services_misc

    uploaded: list[bool] = []

    async def upload(*_a: Any, **_k: Any) -> str:
        uploaded.append(True)
        return "ok"

    async def scenario() -> tuple[list[bool], list[bool]]:
        lock = asyncio.Lock()
        coordinator = SimpleNamespace(
            api=SimpleNamespace(generate_webio_json=MagicMock(return_value="{}")),
            server_id="srv",
            config_entry=SimpleNamespace(data={}, options={}),
            required_webio_classes=("marker",),
            async_webhook_target=AsyncMock(),
            _sync_lock=lock,
            _restore_lock=SimpleNamespace(locked=lambda: False),
            unloading=False,
            webio_source_data=dict,
            ignored_marker_ids=set(),
            ignored_knx_ids=set(),
        )
        hass = SimpleNamespace(data={"comexio": {"e1": coordinator}})
        await lock.acquire()  # a sync or a migration command holds it
        task = asyncio.ensure_future(
            services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": True}))
        )
        await asyncio.sleep(0.01)
        before = list(uploaded)
        built_while_waiting = coordinator.api.generate_webio_json.call_count
        lock.release()
        await task
        assert built_while_waiting == 0  # what the class describes is read after the wait, not before it
        assert coordinator.api.generate_webio_json.call_count == 1
        return before, list(uploaded)

    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)

    before_release, after_release = asyncio.run(scenario())

    assert before_release == []  # waited, did not skip or run beside it
    assert after_release == [True]


def test_a_form_without_a_protocol_blocks_no_command_that_was_never_moved_to_http_even_when_a_move_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP-only install under a forced run: nothing left HTTPS, so the missing field proves nothing wrong."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security
    security.protocol_write = NOW - timedelta(hours=2)  # a forced run set the marker, no command carried HTTPS

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert security.protocol_write is None


def test_the_protocol_move_evidence_is_recorded_persisted_and_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch, {"secret": "abc"})
    security.mark_written(NAME, NOW, https=True)
    assert security.protocol_moved == set()  # a move to HTTPS needs no proof of the missing field

    security.mark_written(NAME, NOW, https=False)

    assert security.protocol_moved == {NAME}
    _run(security.async_save())
    again = _security(monkeypatch, security._store.stored)
    _run(again.async_load())
    assert again.protocol_moved == {NAME}

    again.end_protocol_move()

    assert again.protocol_write is None
    assert again.protocol_moved == set()


@pytest.mark.parametrize("stored", [None, NAME, 5, [NAME, 1]])
def test_a_hand_edited_protocol_moved_entry_is_read_without_failing(
    monkeypatch: pytest.MonkeyPatch, stored: Any
) -> None:
    security = _security(monkeypatch, {"secret": "abc", "protocol_moved": stored})

    _run(security.async_load())

    assert security.protocol_moved == ({NAME} if stored == [NAME, 1] else set())


def test_the_forced_run_forgets_a_command_without_ids_while_the_old_id_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the old id on, such a command holds it on and raises the repair: the ledger must not hide it."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1), legacy=True
    )
    security = coordinator.api.webhook_security
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""

    _run(coordinator.async_force_webhook_migration())

    assert NAME not in security.migrated


def test_the_forced_run_drops_ledger_names_the_data_no_longer_has(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.migrated.add("HA gone")  # a command that was deleted in Comexio meanwhile
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""

    _run(coordinator.async_force_webhook_migration())

    assert "HA gone" not in security.migrated
    assert NAME in security.migrated


def test_a_command_the_ledger_never_held_is_rewritten_when_the_old_id_is_off_and_read_back_after_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old id off, no marker, nothing in the ledger to move: a rewritable command that is not in it still runs."""
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.migrated.discard(NAME)  # created in Comexio by hand: never written by this entry

    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS  # really written, not skipped
    assert NAME in security.migrated
    assert security.protocol_write == NOW  # kept, so that the next night waits for the restart and reads it back

    coordinator.system_info.boot_time = security.last_write + timedelta(minutes=5)
    assert _run(coordinator._async_webhook_migration_tick()) == "done"
    assert security.protocol_write is None
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"


def test_a_never_held_command_whose_write_fails_sets_no_marker_and_is_tried_again_the_next_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.migrated.discard(NAME)
    send = coordinator.api.client.save_webio_command
    coordinator.api.client.save_webio_command = AsyncMock(side_effect=ConnectionError("down"))

    assert _run(coordinator._async_webhook_migration_tick()) == "failed"

    coordinator.api.client.save_webio_command.assert_awaited_once()  # tried, not skipped
    assert security.protocol_write is None  # nothing was written, so nothing waits for a restart
    assert NAME not in security.migrated

    coordinator.api.client.save_webio_command = send
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"  # the next night does not skip
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS


def test_a_never_held_command_is_read_back_and_not_written_twice_while_comexio_has_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.migrated.discard(NAME)
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    # Comexio has not restarted: the next night asks again, rewrites nothing and keeps the marker.
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    coordinator.api.client.save_webio_command.assert_awaited_once()
    assert security.protocol_write == NOW

    # After the restart the command is read back: one that still shows the old protocol is not accepted.
    coordinator.system_info.boot_time = security.last_write + timedelta(minutes=5)
    held["Protocol"] = WEBIO_PROTOCOL_HTTP
    assert _run(coordinator._async_webhook_migration_tick()) == "pending"
    coordinator.api.get_webio_command_form.assert_awaited()
    assert NAME not in security.migrated  # back in the ledger gap, the next night rewrites it


def test_a_never_held_command_with_the_old_id_on_sets_no_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the old id on, the old id itself keeps the run going: the marker belongs to a protocol move only."""
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1), legacy=True
    )
    security = coordinator.api.webhook_security
    security.migrated.discard(NAME)

    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"

    assert NAME in security.migrated  # written
    assert security.protocol_write is None


def test_the_forced_run_keeps_its_marker_for_a_never_held_command_when_the_old_id_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.migrated.discard(NAME)

    assert _run(coordinator.async_force_webhook_migration()) == "needs_restart"

    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS
    assert security.protocol_write == security.last_write == NOW  # moved forward by the write, never back


@pytest.mark.parametrize("missing", ["cmd_id", "device_id", "target"])
def test_a_never_held_command_that_cannot_be_rewritten_for_any_reason_leaves_the_night_skipped(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    coordinator.api.webhook_security.migrated.discard(NAME)
    if missing == "cmd_id":
        coordinator.data["webio_commands"][NAME]["cmdId"] = ""
    elif missing == "device_id":
        for device in coordinator.data["webio_devices"].values():
            device["device_id"] = ""
    else:
        coordinator._webio_range_targets = dict

    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"

    coordinator.api.client.save_webio_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("legacy", "last_write", "expected"), [(True, NOW, None), (False, None, NOW - timedelta(days=1))]
)
def test_the_marker_of_a_written_command_is_only_set_never_cleared_and_not_while_the_old_id_is_on(
    monkeypatch: pytest.MonkeyPatch, legacy: bool, last_write: Any, expected: Any
) -> None:
    security = _security(monkeypatch)
    security.legacy_enabled = legacy
    security.last_write = last_write
    security.protocol_write = expected  # what a forced run set before, or nothing

    ComexioCoordinator._keep_the_request_for_the_restart(security)

    assert security.protocol_write == expected


def test_the_restart_request_stays_when_only_commands_without_ids_are_left(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    security = coordinator.api.webhook_security
    security.last_write = NOW  # another command was rewritten, Comexio has not restarted since
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""
    notified: list[bool] = []
    coordinator._notify_webhook_restart = lambda: notified.append(True)
    coordinator._dismiss_webhook_restart = lambda: notified.append(False)

    _run(coordinator._async_webhook_migration_tick())

    assert notified[-1:] == [True]


def _upload_coordinator(*, unloading: bool) -> Any:
    import asyncio

    return SimpleNamespace(
        api=SimpleNamespace(generate_webio_json=MagicMock(return_value="{}")),
        server_id="srv",
        async_webhook_target=AsyncMock(),
        _sync_lock=asyncio.Lock(),
        _restore_lock=asyncio.Lock(),
        unloading=unloading,
        webio_source_data=dict,
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
    )


def test_the_upload_of_a_class_is_skipped_once_the_entry_unloads(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.comexio.services import misc as services_misc

    upload = AsyncMock(return_value="ok")
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    coordinator = _upload_coordinator(unloading=True)

    line = _run(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))

    assert "skipped" in line
    upload.assert_not_awaited()
    coordinator.async_webhook_target.assert_not_awaited()  # no adapter lookup for a class that is not built
    coordinator.api.generate_webio_json.assert_not_called()


def test_the_upload_of_a_class_is_skipped_while_a_function_plan_restore_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.comexio.services import misc as services_misc

    upload = AsyncMock(return_value="ok")
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    coordinator = _upload_coordinator(unloading=False)

    async def _during_a_restore() -> str:
        async with coordinator._restore_lock:
            return await services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker")

    line = _run(_during_a_restore())

    assert "a function plan restore is running" in line
    upload.assert_not_awaited()  # the old class is not touched
    coordinator.async_webhook_target.assert_not_awaited()
    coordinator.api.generate_webio_json.assert_not_called()
    assert not coordinator._sync_lock.locked()


def test_a_failing_upload_of_one_class_becomes_its_result_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from custom_components.comexio.services import misc as services_misc

    monkeypatch.setattr(services_misc, "_upload_webio_class", AsyncMock(side_effect=ValueError("secret detail")))
    coordinator = _upload_coordinator(unloading=False)

    line = _run(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))

    assert line.startswith("Cls: failed (ValueError)")  # the class of the error only, never its text
    assert "secret detail" not in line
    assert "may be missing" in line
    assert "Uploading the Web-IO class 'Cls' failed" in caplog.text
    assert not coordinator._sync_lock.locked()


def _restart_flags(coordinator: Any) -> list[bool]:
    flags: list[bool] = []
    coordinator._notify_webhook_restart = lambda: flags.append(True)
    coordinator._dismiss_webhook_restart = lambda: flags.append(False)
    return flags


def test_no_restart_request_with_the_old_id_on_and_a_command_without_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1), legacy=True
    )
    coordinator.api.webhook_security.last_write = NOW
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""
    flags = _restart_flags(coordinator)

    _run(coordinator._async_webhook_migration_tick())

    assert flags[-1:] == [False]


def test_no_restart_request_while_a_command_can_still_be_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW - timedelta(hours=1)
    )
    coordinator.api.webhook_security.last_write = NOW
    coordinator.api.client.save_webio_command = AsyncMock(side_effect=RuntimeError("write failed"))
    flags = _restart_flags(coordinator)

    _run(coordinator._async_webhook_migration_tick())

    assert flags[-1:] == [False]


def test_no_restart_request_once_comexio_restarted_after_the_last_write(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1)
    )
    coordinator.api.webhook_security.last_write = NOW
    coordinator.data["webio_commands"][NAME]["cmdId"] = ""
    flags = _restart_flags(coordinator)

    _run(coordinator._async_webhook_migration_tick())

    assert flags[-1:] == [False]


def test_the_upload_of_a_class_resolves_the_target_under_the_lock_right_before_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.comexio.services import misc as services_misc

    monkeypatch.setattr(services_misc, "_upload_webio_class", AsyncMock(return_value="ok"))
    coordinator = _upload_coordinator(unloading=False)
    steps: list[str] = []
    coordinator.async_webhook_target = AsyncMock(
        side_effect=lambda *_a: steps.append(f"target (locked={coordinator._sync_lock.locked()})")
    )
    coordinator.api.generate_webio_json = MagicMock(side_effect=lambda *_a, **_k: steps.append("build") or "{}")

    _run(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))

    assert steps == ["target (locked=True)", "build"]


def test_the_upload_of_a_class_resolves_the_target_only_after_the_lock_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from custom_components.comexio.services import misc as services_misc

    monkeypatch.setattr(services_misc, "_upload_webio_class", AsyncMock(return_value="ok"))

    async def scenario() -> tuple[int, list[str]]:
        coordinator = _upload_coordinator(unloading=False)
        steps: list[str] = []
        coordinator.async_webhook_target = AsyncMock(side_effect=lambda *_a: steps.append("target"))
        coordinator.api.generate_webio_json = MagicMock(side_effect=lambda *_a, **_k: steps.append("build") or "{}")
        await coordinator._sync_lock.acquire()  # a sync or a migration command holds it
        task = asyncio.ensure_future(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))
        await asyncio.sleep(0.01)
        resolved_while_waiting = coordinator.async_webhook_target.await_count
        coordinator._sync_lock.release()
        await task
        return resolved_while_waiting, steps

    resolved_while_waiting, steps = _run(scenario())

    assert resolved_while_waiting == 0  # the target of before the wait would be the stale one
    assert steps == ["target", "build"]


def test_the_upload_of_several_classes_resolves_the_target_once_per_class_and_not_up_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.comexio.services import misc as services_misc

    monkeypatch.setattr(services_misc, "_upload_webio_class", AsyncMock(return_value="ok"))
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    coordinator = _upload_coordinator(unloading=False)
    coordinator.config_entry = SimpleNamespace(data={}, options={})
    coordinator.required_webio_classes = ("marker", "io")
    steps: list[str] = []
    coordinator.async_webhook_target = AsyncMock(side_effect=lambda *_a: steps.append("target"))
    coordinator.api.generate_webio_json = MagicMock(side_effect=lambda *_a, **_k: steps.append("build") or "{}")
    hass = SimpleNamespace(data={"comexio": {"e1": coordinator}})

    _run(services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": True})))

    assert steps == ["target", "build", "target", "build"]


def test_the_preview_resolves_the_target_before_it_builds_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.comexio.services import misc as services_misc

    coordinator = _upload_coordinator(unloading=False)
    coordinator.config_entry = SimpleNamespace(data={}, options={})
    coordinator.required_webio_classes = ("marker",)
    steps: list[str] = []
    coordinator.async_webhook_target = AsyncMock(side_effect=lambda *_a: steps.append("target"))
    coordinator.api.generate_webio_json = MagicMock(side_effect=lambda *_a, **_k: steps.append("build") or "{}")
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    hass = SimpleNamespace(data={"comexio": {"e1": coordinator}})

    _run(services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": False})))

    assert steps == ["target", "build"]


def test_a_target_that_cannot_be_resolved_is_the_result_line_of_that_class_and_uploads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.comexio.services import misc as services_misc

    upload = AsyncMock(return_value="ok")
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    coordinator = _upload_coordinator(unloading=False)
    coordinator.async_webhook_target = AsyncMock(side_effect=OSError("adapters"))

    line = _run(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))

    assert line.startswith("Cls: failed (OSError)")
    assert "nothing changed on Comexio" in line  # the old class is untouched: no hint to the sync button
    assert "may be missing" not in line
    upload.assert_not_awaited()
    coordinator.api.generate_webio_json.assert_not_called()
    assert not coordinator._sync_lock.locked()


def test_a_drifted_body_is_a_deviation_even_when_the_form_shows_no_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, held = _protocol_coordinator(monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    held["Data"] = held["Data"].replace('server="', 'server="other-')  # another server field than the target's

    deviating, unreadable, complete = _run(coordinator._async_verify_webhook_commands(security))

    assert list(deviating) == [NAME]
    assert unreadable == []
    assert complete


def test_a_drifted_path_is_a_deviation_for_a_command_moved_to_http_whose_form_shows_no_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security
    security.protocol_write = NOW - timedelta(hours=2)
    security.protocol_moved = {NAME}
    held["Parameter"] = "/api/webhook/somewhere-else"

    deviating, unreadable, _complete = _run(coordinator._async_verify_webhook_commands(security))

    assert list(deviating) == [NAME]
    assert unreadable == []


def test_an_unchanged_command_whose_form_shows_no_protocol_stays_unreadable_not_deviating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)

    deviating, unreadable, complete = _run(coordinator._async_verify_webhook_commands(security))

    assert deviating == {}
    assert unreadable == [NAME]
    assert complete


def test_a_drifted_path_is_a_deviation_even_when_the_form_shows_no_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, held = _protocol_coordinator(monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1))
    security = coordinator.api.webhook_security
    security.https_written.add(NAME)
    security.protocol_write = NOW - timedelta(hours=2)
    held["Parameter"] = "/api/webhook/somewhere-else"  # edited in Comexio; the form carries no protocol field

    deviating, unreadable, complete = _run(coordinator._async_verify_webhook_commands(security))

    assert list(deviating) == [NAME]
    assert unreadable == []
    assert complete


def test_a_failing_build_of_a_class_is_its_result_line_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.comexio.services import misc as services_misc

    upload = AsyncMock(return_value="ok")
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    coordinator = _upload_coordinator(unloading=False)
    coordinator.api.generate_webio_json = MagicMock(side_effect=KeyError("x"))

    line = _run(services_misc._upload_class_under_sync_lock(coordinator, "Cls", "marker"))

    assert line.startswith("Cls: failed (KeyError)")
    assert "nothing changed on Comexio" in line
    upload.assert_not_awaited()
    assert not coordinator._sync_lock.locked()


def test_a_move_to_http_of_a_command_the_ledger_did_not_hold_marks_the_move_and_leaves_it_on_a_move_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _security(monkeypatch, {"secret": "abc"})
    security.https_written.add(NAME)  # carries HTTPS, but is not in the ledger of migrated commands

    security.mark_written(NAME, NOW, https=False)

    assert security.protocol_write == NOW  # a restart and a read-back have to follow
    assert security.protocol_moved == {NAME}

    security.mark_written(NAME, NOW, https=True)

    assert security.protocol_moved == set()
