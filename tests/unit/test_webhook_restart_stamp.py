"""A new command and a plain rename ask for no Comexio restart; every other write still does (measured 2026-11-22).

Live on iosrv1: a marker renamed in Comexio and a marker created after that, each synced by the integration, pushed
to Home Assistant within seconds, with no Comexio restart in between. Only a command whose path, server field or
protocol changes is sent by Comexio after a restart, so only that write moves the stamp the "restart needed" notice
and the switch-off of the old id wait for.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio import webhook_security as ws
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.button import ComexioSyncButton
from custom_components.comexio.coordinator import ComexioCoordinator
from tests.unit.test_api_helpers import PARSED
from tests.unit.test_webhook_security import SERVER, _security, _use_client

_run = asyncio.run
RENAMED = "HA M5 Umbenannt"


def _api(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, stored: Any = None) -> tuple[ComexioAPI, Any]:
    comexio_api.webhook_security = security = _security(monkeypatch, stored)
    _use_client(comexio_api, SimpleNamespace(save_webio_command=AsyncMock()))
    return comexio_api, security


def _own(api: ComexioAPI, name: str | None = None, *, https: bool = False) -> dict[str, Any]:
    command = dict(api.build_webio_commands(SERVER, PARSED)[0])
    command["Protocol"] = ws.WEBIO_PROTOCOL_HTTPS if https else 0
    if name:
        command["Name"] = name
    return command


def test_a_new_command_enters_the_ledger_without_a_restart_stamp(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)

    assert _run(api.save_single_command(1, 2, command))

    assert security.migrated == {command["Name"]}
    assert security.last_write is None
    assert security.protocol_write is None
    assert not security.protocol_moved


def test_a_ledger_of_new_commands_keeps_its_missing_stamp_over_a_restart_of_home_assistant(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    _run(api.save_single_command(1, 2, _own(api)))

    reloaded = _security(monkeypatch, security._data())
    _run(reloaded.async_load())

    assert reloaded.migrated == security.migrated
    assert reloaded.last_write is None


def test_a_plain_rename_of_a_migrated_command_leaves_the_stamp_alone(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated = {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces=old["Name"]))

    assert security.migrated == {old["Name"], RENAMED}
    assert security.last_write is None
    assert security.protocol_write is None
    assert not security.protocol_moved


def test_a_rename_leaves_a_stamp_that_is_still_waiting_for_a_restart_where_it_is(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    written = datetime(2026, 11, 20, 1, 30, tzinfo=UTC)
    security.migrated, security.last_write = {old["Name"]}, written

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write == written


def test_a_rename_of_a_command_the_ledger_does_not_know_is_a_rewrite(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces="HA M5 Alt"))

    assert security.last_write is not None


def test_a_rename_that_changes_the_protocol_is_a_rewrite(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated, security.https_written = {old["Name"]}, {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is not None


def test_an_update_that_is_no_rename_is_still_a_rewrite(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nightly rewrite of a path, a type fix and a range correction send no previous name."""
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=3))

    assert security.last_write is not None


def test_an_uploaded_class_still_asks_for_a_restart(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    api, security = _api(comexio_api, monkeypatch)

    api._note_upload_written(json.dumps({"commands": [_own(api)]}))

    assert security.last_write is not None


@pytest.mark.parametrize(
    ("stamped", "restarted_after", "pending"),
    [(False, True, False), (True, False, True), (True, True, False)],
)
def test_the_restart_notice_waits_only_for_a_stamped_write(
    comexio_api: ComexioAPI,
    monkeypatch: pytest.MonkeyPatch,
    stamped: bool,
    restarted_after: bool,
    pending: bool,
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    boot = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)
    if stamped:
        security.mark_written("HA M1 Pfad", boot + timedelta(hours=-1 if restarted_after else 1))
    _run(api.save_single_command(1, 2, _own(api, "HA M5 Neu")))
    coordinator = SimpleNamespace(system_info=SimpleNamespace(boot_time=boot))

    assert ComexioCoordinator._restart_pending(coordinator, security) is pending  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("task_type", "previous_name", "replaces", "existing"),
    [
        ("rename", "HA M1 Alt", "HA M1 Alt", "11"),
        ("rename", None, None, "11"),
        ("type", "HA M1 Alt", None, "11"),
        ("create", None, None, None),
    ],
)
def test_the_delta_sync_names_the_old_command_only_for_a_plain_rename(
    task_type: str, previous_name: str | None, replaces: str | None, existing: str | None
) -> None:
    api = MagicMock()
    api.save_single_command = AsyncMock(return_value=True)
    item = {"name": "HA M1 Neu", "id": "11", "payload": {"Name": "HA M1 Neu"}, "previous_name": previous_name}
    result = {"renamed": 0, "removed": 0, "updated": 0, "added": 0, "created_names": []}

    _run(ComexioSyncButton._apply_delta_task(api, "7", "3", {"type": task_type, "item": item}, result, []))

    api.save_single_command.assert_awaited_once_with(
        "7", "3", item["payload"], existing_cmd_id=existing, replaces=replaces
    )


def test_a_rename_that_stays_on_https_leaves_the_stamp_alone(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api, https=True)
    security.migrated, security.https_written = {old["Name"]}, {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED, https=True), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is None
    assert RENAMED in security.https_written
    assert security.protocol_write is None
    assert not security.protocol_moved


def test_a_rename_that_moves_to_https_is_a_rewrite(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated = {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED, https=True), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is not None


def test_a_created_command_that_changes_the_protocol_of_its_name_asks_for_a_restart(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command of that name was in the ledger with the other protocol: the move needs a restart like any other."""
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated, security.https_written = {command["Name"]}, {command["Name"]}

    assert _run(api.save_single_command(1, 2, command))

    assert security.last_write is not None
    assert security.protocol_write is not None
    assert command["Name"] in security.protocol_moved


@pytest.mark.parametrize("empty_id", ["", 0])
def test_a_save_with_an_empty_command_id_is_a_new_command(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, empty_id: Any
) -> None:
    """The client sends it as a new command (id None), so the ledger counts it as one too."""
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=empty_id))

    assert security.last_write is None
    assert api.client.save_webio_command.await_args.kwargs["command_id"] is None


def test_a_rename_that_leaves_https_records_the_move_of_the_old_name(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The protocol is compared with the old name's: the new name is not in the ledger yet."""
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api, https=True)
    security.migrated, security.https_written = {old["Name"]}, {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is not None
    assert security.protocol_write is not None
    assert security.protocol_moved == {RENAMED}
    assert RENAMED not in security.https_written


def test_a_plain_rename_keeps_the_pending_proof_of_a_move_to_http_under_the_new_name(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated, security.protocol_moved = {old["Name"]}, {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is None
    assert security.protocol_moved == {RENAMED}


def test_a_write_that_moved_the_stamp_waits_for_the_save_of_the_ledger(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored restart_free marker must not outlive the stamp that replaces it."""
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=3))

    security._store.async_save.assert_awaited_once()
    assert security._store.stored["last_write"] is not None
    assert security._store.stored["restart_free"] is False


@pytest.mark.parametrize("rename", [False, True])
def test_a_write_that_needs_no_restart_waits_for_no_save(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, rename: bool
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated = {old["Name"]}
    kwargs = {"existing_cmd_id": 3, "replaces": old["Name"]} if rename else {}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), **kwargs))

    security._store.async_save.assert_not_awaited()
    security._store.async_delay_save.assert_called_once()
    assert security.protocol_write is None
    assert not security.protocol_moved


def test_a_delayed_save_is_armed_again_after_the_awaited_save_of_a_moved_stamp(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA's Store.async_save cancels the delayed save and only logs a failed write: the next one is the retry."""
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}
    order: list[str] = []
    security._store.async_save.side_effect = lambda _data: order.append("save")
    security._store.async_delay_save.side_effect = lambda *_a: order.append("delay")

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=3))

    assert order == ["delay", "save", "delay"]


@pytest.mark.parametrize("error", [OSError("disk full"), TypeError("not serialisable")])
def test_an_error_of_the_save_after_a_moved_stamp_does_not_fail_the_write(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}
    security._store.async_save.side_effect = error

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=3))

    assert security.last_write is not None
    assert security._store.async_delay_save.call_count == 2  # the one of the write and the retry
    assert "Could not save the Web-IO ledger" in caplog.text
    assert str(error) not in caplog.text


def test_a_rename_whose_old_name_is_unknown_but_whose_new_name_is_known_compares_that_protocol(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    security.migrated, security.https_written = {RENAMED}, {RENAMED}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED), existing_cmd_id=3, replaces="HA M5 Alt"))

    assert security.last_write is not None
    assert security.protocol_write is not None
    assert RENAMED in security.protocol_moved


def test_an_update_that_names_its_own_name_as_the_old_one_is_no_rename(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}

    assert _run(api.save_single_command(1, 2, command, existing_cmd_id=3, replaces=command["Name"]))

    assert security.last_write is not None


def test_a_created_command_that_reuses_a_name_with_the_same_protocol_stays_free(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api)
    security.migrated = {command["Name"]}

    assert _run(api.save_single_command(1, 2, command))

    assert security.last_write is None
    assert security.protocol_write is None


def test_a_created_command_that_reuses_a_name_and_moves_to_https_asks_for_a_restart(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    command = _own(api, https=True)
    security.migrated = {command["Name"]}

    assert _run(api.save_single_command(1, 2, command))

    assert security.last_write is not None
    assert security.protocol_write is not None
    assert command["Name"] not in security.protocol_moved
    assert command["Name"] in security.https_written


def test_a_rename_of_a_command_with_a_pending_move_to_http_that_goes_to_https_ends_the_pending_proof(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)
    old = _own(api)
    security.migrated, security.protocol_moved = {old["Name"]}, {old["Name"]}

    assert _run(api.save_single_command(1, 2, _own(api, RENAMED, https=True), existing_cmd_id=3, replaces=old["Name"]))

    assert security.last_write is not None
    assert not security.protocol_moved
    assert RENAMED in security.https_written


def test_the_stamp_of_an_uploaded_class_is_reported_to_the_caller(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, security = _api(comexio_api, monkeypatch)

    assert api._note_upload_written(json.dumps({"commands": [_own(api)]})) is True
    assert api._note_upload_written(json.dumps({"commands": []})) is False


@pytest.mark.parametrize(
    ("migrated", "stamp", "restart_free"),
    [
        (set(), None, False),
        ({"HA M1"}, None, True),
        ({"HA M1"}, datetime(2026, 11, 22, tzinfo=UTC), False),
        (set(), datetime(2026, 11, 22, tzinfo=UTC), False),
    ],
)
def test_the_ledger_records_whether_a_missing_stamp_is_meant(
    monkeypatch: pytest.MonkeyPatch, migrated: set[str], stamp: datetime | None, restart_free: bool
) -> None:
    security = _security(monkeypatch)
    security.migrated, security.last_write = migrated, stamp

    assert security._data()["restart_free"] is restart_free


@pytest.mark.parametrize("flag", [{}, {"restart_free": False}, {"restart_free": 1}, {"restart_free": "yes"}])
def test_a_ledger_with_names_and_a_lost_stamp_is_stamped_as_now_unless_it_says_restart_free(
    monkeypatch: pytest.MonkeyPatch, flag: dict[str, Any]
) -> None:
    security = _security(monkeypatch, {"secret": "abc", "migrated": ["HA M1"], **flag})

    _run(security.async_load())

    assert security.last_write is not None


def test_a_restart_free_ledger_keeps_its_missing_stamp_on_load(monkeypatch: pytest.MonkeyPatch) -> None:
    security = _security(monkeypatch, {"secret": "abc", "migrated": ["HA M1"], "restart_free": True})

    _run(security.async_load())

    assert security.last_write is None
