"""Restores honour auto_start: as a new plan and in place (both used to start the plan always)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.comexio.services import backup

SNAPSHOT = {"plan_name": "Kitch", "elements": {}, "connections": {}}


def _run(auto_start: bool, run_fup: AsyncMock | None = None) -> tuple[AsyncMock, MagicMock, SimpleNamespace]:
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=run_fup or AsyncMock(return_value=True),
    )
    coordinator = SimpleNamespace(
        server_id="iosrv1",
        function_plan_backup=SimpleNamespace(async_rekey_fub_id=AsyncMock()),
        async_repoint_function_plan_fub_id=AsyncMock(return_value=[]),
        note_plan_restored_as_new=MagicMock(),
    )
    with patch.object(backup.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup._restore_plan_as_new(MagicMock(), coordinator, api, 42, SNAPSHOT, "auto", 0, auto_start=auto_start)
        )
    return api.function_plan_run_fup, notify, coordinator


@pytest.mark.parametrize(
    ("auto_start", "started", "activation"), [(True, True, "active"), (False, False, "not started")]
)
def test_restore_as_new_starts_the_plan_only_with_auto_start(auto_start: bool, started: bool, activation: str) -> None:
    run_fup, notify, _coordinator = _run(auto_start)
    assert run_fup.await_count == int(started)
    assert (
        f"Activation: {backup.ICON_SUCCESS if started else backup.ICON_INACTIVE} {activation}"
        in (notify.call_args.args[1])
    )


START_FAILED = f"Activation: {backup.ICON_WARNING} starting it failed ("


@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("odd reply")])
def test_a_restore_as_new_whose_start_fails_still_counts_as_restored(error: Exception) -> None:
    """(Sourcery) A start failing after the build was reported as a failed restore: a retry built it twice.

    Any error, not only the network ones: the plan exists either way.
    """
    _run_fup, notify, coordinator = _run(True, AsyncMock(side_effect=error))
    coordinator.note_plan_restored_as_new.assert_called_once_with(42, "Kitch", 50, complete=True)
    coordinator.function_plan_backup.async_rekey_fub_id.assert_awaited_once()  # the lineage moved to the new ID
    # (local review) Named as a failed start, not as Comexio's routine "created inactive".
    assert f"{START_FAILED}{type(error).__name__}" in notify.call_args.args[1]
    assert notify.call_args.kwargs["title"] != backup._TITLE_RESTORE_ERR


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientError("gone"), RuntimeError("odd reply")])
def test_a_restore_as_copy_whose_start_fails_keeps_the_copy(error: Exception) -> None:
    api = SimpleNamespace(
        create_fup=AsyncMock(return_value=50),
        function_plan_rebuild_plan_from_snapshot=AsyncMock(return_value=({}, 0, [])),
        function_plan_run_fup=AsyncMock(side_effect=error),
    )
    with patch.object(backup.persistent_notification, "async_create") as notify:
        asyncio.run(
            backup._restore_plan_as_copy(
                MagicMock(), SimpleNamespace(server_id="iosrv1"), api, 42, SNAPSHOT, "auto", 0, "Kitch copy", True
            )
        )
    assert notify.call_args.kwargs["title"] != backup._TITLE_RESTORE_ERR
    assert f"Activation: {backup.ICON_WARNING} starting it failed ({type(error).__name__}" in notify.call_args.args[1]


@pytest.mark.parametrize("auto_start", [True, False])
def test_restore_in_place_gets_the_callers_auto_start(auto_start: bool) -> None:
    # The in-place restore handles auto_start, but the service never passed it on.
    live = {"Name": "Kitch"}
    api = SimpleNamespace(
        login=AsyncMock(return_value=True),
        get_raw_config=AsyncMock(return_value={"Fubs": {"42": live}}),
        update_fub_cache_entry=MagicMock(),
        comexio_version=None,
    )
    call = SimpleNamespace(data={"confirm": True, "auto_start": auto_start})
    with (
        patch.object(backup, "_resolve_restore_params", AsyncMock(return_value=(42, "auto", 0, "Kitch"))),
        patch.object(backup, "_resolve_backup_identity", AsyncMock(return_value=("Kitch", None))),
        patch.object(backup, "_resolve_restore_snapshot", AsyncMock(return_value=SNAPSHOT)),
        patch.object(backup, "_refresh_service_descriptions", AsyncMock()),
        patch.object(backup, "_restore_plan_in_place", AsyncMock()) as in_place,
    ):
        asyncio.run(
            backup._run_function_plan_restore(
                MagicMock(),
                call,
                SimpleNamespace(server_id="iosrv1", vanished_restore_running=lambda *_: False),
                api,
                None,
            )
        )
    assert in_place.await_args.kwargs["auto_start"] is auto_start
