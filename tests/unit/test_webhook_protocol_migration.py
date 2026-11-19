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

from aiocomexio.webio import WEBIO_PROTOCOL_HTTP, WEBIO_PROTOCOL_HTTPS
import pytest

from custom_components.comexio import button as button_module, webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import ComexioSyncButton
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
    security.protocol_write = NOW - timedelta(hours=2)  # moved from HTTPS to HTTP, the restart is still to come

    assert _run(coordinator._async_webhook_migration_tick()) == "pending"

    assert "1 could not be read" in caplog.text
    assert security.protocol_write is not None  # not ended, as the form proves nothing


def test_a_form_without_a_protocol_blocks_no_http_command_when_no_protocol_move_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=None, boot=NOW + timedelta(hours=1), target_protocol=WEBIO_PROTOCOL_HTTP
    )
    security = coordinator.api.webhook_security
    security.legacy_enabled = True  # a plain path migration: as it always was, the missing field blocks nothing

    assert _run(coordinator._async_webhook_migration_tick()) != "pending"
    assert security.protocol_write is None


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
    security = coordinator.api.webhook_security

    assert _run(coordinator.async_force_webhook_migration()) == "skipped"

    assert NAME in security.migrated  # the ledger was not touched
    coordinator.api.client.save_webio_command.assert_not_awaited()


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
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTP
    assert NAME not in security.migrated  # not recorded as moved
    assert security.protocol_write is not None  # the next night does not skip: it tries again

    coordinator.api.client.save_webio_command = send
    assert _run(coordinator._async_webhook_migration_tick()) == "needs_restart"
    assert held["Protocol"] == WEBIO_PROTOCOL_HTTPS


def test_an_uploaded_class_template_records_each_commands_protocol(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    comexio_api.webhook_security = security = _security(monkeypatch)
    template = json.dumps(
        {
            "commands": [
                {"Name": "HA s", "Parameter": "/api/webhook/a", "Protocol": WEBIO_PROTOCOL_HTTPS},
                {"Name": "HA p", "Parameter": "/api/webhook/b", "Protocol": WEBIO_PROTOCOL_HTTP},
                {"Name": "HA n", "Parameter": "/api/webhook/c"},
            ]
        }
    )

    comexio_api._note_upload_written(template)

    assert security.https_written == {"HA s"}


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
    coordinator.data["webio_commands"] = {}

    assert _run(coordinator.async_force_webhook_migration()) == "done"

    assert security.protocol_write is None  # not "the old id stays on" every night, with the old id long off
    assert _run(coordinator._async_webhook_migration_tick()) == "skipped"


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
    coordinator._async_save_webhook_security = AsyncMock(return_value=False)
    security.schedule_save = MagicMock()

    assert _run(coordinator._async_webhook_migration_tick()) == "done"

    assert security.protocol_write is None
    security.schedule_save.assert_called_once_with()


def test_the_forced_run_that_switches_the_old_id_off_for_lack_of_commands_clears_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _held = _protocol_coordinator(
        monkeypatch, held_protocol=WEBIO_PROTOCOL_HTTP, boot=NOW + timedelta(hours=1), legacy=True
    )
    security = coordinator.api.webhook_security
    coordinator.data["webio_commands"] = {}
    coordinator._legacy_webhook_id_unneeded = lambda _security: True

    assert _run(coordinator.async_force_webhook_migration()) == "done"

    assert not security.legacy_enabled
    assert security.protocol_write is None  # not left to raise a restart request after the next start


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

    coordinator = SimpleNamespace(
        api=SimpleNamespace(generate_webio_json=MagicMock(return_value="{}")),
        server_id="srv",
        config_entry=SimpleNamespace(data={}, options={}),
        required_webio_classes=("marker",),
        async_webhook_target=AsyncMock(),
        _sync_lock=lock,
        webio_source_data=dict,
        ignored_marker_ids=set(),
        ignored_knx_ids=set(),
    )
    monkeypatch.setattr(services_misc.persistent_notification, "async_create", MagicMock())
    monkeypatch.setattr(services_misc, "_upload_webio_class", upload)
    hass = SimpleNamespace(data={"comexio": {"e1": coordinator}})

    _run(services_misc.handle_generate_web_io(hass, SimpleNamespace(data={"config_entry": "e1", "upload": True})))

    assert held_during_upload == [True]
    assert not lock.locked()
