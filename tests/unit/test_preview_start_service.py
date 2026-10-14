"""Plan preview: the opening card renders the selection, a selection change follows it while a card is open."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import coordinator as coordinator_module, plan_preview
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.services import misc, plan_actions


def _run_start(
    *,
    armed: bool,
    selection: bool,
    render: AsyncMock | None = None,
    coordinator: object | None = None,
    data: dict | None = None,
    still_following: bool = True,
) -> tuple[dict, AsyncMock]:
    coordinator = coordinator or _follow_coordinator(following=False, preview_armed=armed)
    render = render or AsyncMock()
    call = SimpleNamespace(data=data or {})
    with (
        patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(misc, "preview_selection_available", return_value=selection),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    # Whatever the card finds, its later Plan/Backup picks follow until it closes.
    assert coordinator.preview_following is still_following
    return result, render


def test_opening_the_card_renders_the_selection() -> None:
    result, render = _run_start(armed=False, selection=True)
    assert result == {"success": True, "already_armed": False, "instance": _INSTANCE}
    render.assert_awaited_once()


def test_an_armed_preview_is_left_alone() -> None:
    result, render = _run_start(armed=True, selection=True)
    assert result == {"success": True, "already_armed": True, "instance": _INSTANCE}
    render.assert_not_called()


def test_nothing_to_preview_is_reported() -> None:
    result, render = _run_start(armed=False, selection=False)
    # Not armed, but answered by this instance: the card waits before retrying the start on it.
    assert result == {"success": False, "error": "No plan is selected for the preview.", "instance": _INSTANCE}
    render.assert_not_called()


def test_the_open_cards_keepalive_renews_the_following_without_a_render() -> None:
    """A frozen orphaned-plan render arms nothing: the card keeps its selection changes following."""
    coordinator = _follow_coordinator(following=False)
    render = AsyncMock()
    call = SimpleNamespace(data={"keepalive": True})
    with (
        patch.object(misc, "_resolve_coordinator", MagicMock(return_value=coordinator)) as resolve,
        patch.object(misc, "preview_selection_available", return_value=True),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert result == {"success": True, "keepalive": True, "instance": _INSTANCE}
    assert coordinator.preview_following is True
    render.assert_not_called()
    assert resolve.call_args.kwargs == {"quiet": True}


def _keepalive_instance(coordinator: object) -> str:
    call = SimpleNamespace(data={"keepalive": True})
    with patch.object(misc, "_resolve_coordinator", MagicMock(return_value=coordinator)):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    return result["instance"]


def test_a_reloaded_entry_answers_the_keepalive_as_a_new_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """(ba): after a config-entry reload the open card's keepalive must reveal the new, unarmed coordinator.

    The card compares the instance against its opening start's answer and re-sends that start, so the
    live values resume. An auto-stop on the same coordinator keeps the instance: the keepalive must not
    re-arm a preview the auto-stop ended (a forgotten browser tab).
    """
    monkeypatch.setattr(coordinator_module, "async_call_later", MagicMock())
    before_reload, after_reload = (ComexioCoordinator(MagicMock(), MagicMock(), MagicMock()) for _ in range(2))
    opened = _keepalive_instance(before_reload)

    before_reload._preview_plan_cache = _armed_cache(1)
    asyncio.run(before_reload._async_auto_stop_preview(None))
    assert _keepalive_instance(before_reload) == opened
    assert before_reload.preview_armed is False

    assert _keepalive_instance(after_reload) != opened
    # A keepalive alone never arms — the card's re-sent start does.
    assert after_reload.preview_armed is False


def test_the_keepalive_sends_no_comexio_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """It repeats every 30 s per open card: a Comexio request there would load the server for nothing."""
    coordinator = _real_coordinator(monkeypatch)
    calls_before = list(coordinator.api.mock_calls)
    _keepalive_instance(coordinator)
    assert coordinator.api.mock_calls == calls_before


def _real_coordinator(monkeypatch: pytest.MonkeyPatch) -> ComexioCoordinator:
    """A real coordinator, so a renamed preview_instance_id attribute cannot hide behind a test double."""
    monkeypatch.setattr(coordinator_module, "async_call_later", MagicMock())
    return ComexioCoordinator(MagicMock(), MagicMock(), MagicMock())


def test_the_preview_instance_id_is_a_non_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _real_coordinator(monkeypatch).preview_instance_id
    assert isinstance(instance, str)
    assert instance


def test_every_successful_answer_names_the_real_coordinators_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh start, already armed and keepalive all carry the coordinator's own preview_instance_id."""
    coordinator = _real_coordinator(monkeypatch)
    expected = coordinator.preview_instance_id

    fresh, _ = _run_start(armed=False, selection=True, coordinator=coordinator)
    assert fresh == {"success": True, "already_armed": False, "instance": expected}

    coordinator._preview_plan_cache = _armed_cache(1)
    armed, render = _run_start(armed=True, selection=True, coordinator=coordinator)
    assert armed == {"success": True, "already_armed": True, "instance": expected}
    render.assert_not_called()

    assert _keepalive_instance(coordinator) == expected


def test_the_instance_survives_a_stop_and_a_restart_after_an_auto_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a new coordinator (a reload) may change the id — else every keepalive would restart the preview."""
    coordinator = _real_coordinator(monkeypatch)
    opened = coordinator.preview_instance_id

    coordinator._preview_plan_cache = _armed_cache(1)
    assert coordinator.stop_preview() is True
    assert _keepalive_instance(coordinator) == opened

    coordinator._preview_plan_cache = _armed_cache(1)
    asyncio.run(coordinator._async_auto_stop_preview(None))
    restarted, render = _run_start(armed=False, selection=True, coordinator=coordinator)
    assert restarted == {"success": True, "already_armed": False, "instance": opened}
    render.assert_awaited_once()


def test_an_unresolved_instance_answers_the_start_without_an_instance() -> None:
    call = SimpleNamespace(data={})
    with patch.object(misc, "_async_get_service_context", AsyncMock(return_value=None)):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert result == {"success": False, "error": misc._INSTANCE_NOT_RESOLVED_ERR}


def test_an_unresolved_instance_answers_the_keepalive_without_an_instance() -> None:
    call = SimpleNamespace(data={"keepalive": True})
    with patch.object(misc, "_resolve_coordinator", MagicMock(return_value=None)):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert result == {"success": False, "error": misc._INSTANCE_NOT_RESOLVED_ERR}


def test_closing_the_card_ends_the_following() -> None:
    coordinator = _follow_coordinator(following=True)
    coordinator.stop_preview_for_closed_cards = MagicMock(return_value=False)
    with patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))):
        asyncio.run(misc._handle_function_plan_preview_stop(SimpleNamespace(), SimpleNamespace(data={})))  # type: ignore[arg-type]
    assert coordinator.preview_following is False
    coordinator.stop_preview_for_closed_cards.assert_called_once_with()


def test_an_opened_card_follows_within_the_auto_stop_window_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed browser tab sends no preview_stop: without an armed preview the following must run out."""
    now = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator._preview_plan_cache = None
    coordinator._preview_following_until = 0.0
    coordinator._preview_cards = {}
    coordinator._preview_cards_dropped = {}
    coordinator._preview_card_sweep_cancel = None
    coordinator._preview_auto_stop_minutes = coordinator_module._PREVIEW_AUTO_STOP_DEFAULT_MINUTES
    coordinator.preview_stop_generation = 0
    assert coordinator.preview_following is False
    coordinator.start_preview_following()
    assert coordinator.preview_following is True
    now[0] += coordinator_module._PREVIEW_AUTO_STOP_DEFAULT_MINUTES * 60
    assert coordinator.preview_following is False
    coordinator.start_preview_following()  # the open card's keepalive renews the run-out window
    assert coordinator.preview_following is True
    now[0] += coordinator_module._PREVIEW_AUTO_STOP_DEFAULT_MINUTES * 60 - 1
    assert coordinator.preview_following is True
    coordinator.end_preview_following()
    assert coordinator.preview_following is False
    coordinator._preview_plan_cache = {"fub_id": 19}  # an armed preview follows as before
    assert coordinator.preview_following is True


def test_a_failed_render_is_not_reported_as_armed() -> None:
    """E.g. an expired session: the plan does not load, so the card must not be told the poll runs."""
    render = AsyncMock(side_effect=HomeAssistantError("The plan 'X' (ID 3) could not be loaded from Comexio."))
    result, _ = _run_start(armed=False, selection=True, render=render)
    assert result == {
        "success": False,
        "error": "The plan 'X' (ID 3) could not be loaded from Comexio.",
        "instance": _INSTANCE,
    }


def test_two_cards_recovering_from_one_reload_render_once() -> None:
    """Review (Sourcery, #171): both starts pass the armed check before the lock; the second must not render."""
    coordinator = _follow_coordinator(following=False)

    async def render(c: SimpleNamespace) -> None:
        await asyncio.sleep(0)
        c.preview_armed = True

    render_mock = AsyncMock(side_effect=render)
    call = SimpleNamespace(data={})

    async def scenario() -> list[dict]:
        with (
            patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
            patch.object(misc, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", render_mock),
        ):
            return await asyncio.gather(
                misc._handle_function_plan_preview_start(SimpleNamespace(), call),  # type: ignore[arg-type]
                misc._handle_function_plan_preview_start(SimpleNamespace(), call),  # type: ignore[arg-type]
            )

    results = asyncio.run(scenario())
    render_mock.assert_awaited_once()
    assert [r["already_armed"] for r in results] == [False, True]
    assert all(r["success"] is True and r["instance"] == _INSTANCE for r in results)


def test_a_start_waiting_behind_a_failed_render_renders_itself() -> None:
    """The re-check under the lock skips only an armed preview: a failed first render leaves the second to try."""
    coordinator = _follow_coordinator(following=False)
    attempts: list[int] = []

    async def render(c: SimpleNamespace) -> None:
        attempts.append(len(attempts))
        await asyncio.sleep(0)
        if len(attempts) == 1:
            raise HomeAssistantError("plan not loadable")
        c.preview_armed = True

    call = SimpleNamespace(data={})

    async def scenario() -> list[dict]:
        with (
            patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
            patch.object(misc, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", AsyncMock(side_effect=render)),
        ):
            return await asyncio.gather(
                misc._handle_function_plan_preview_start(SimpleNamespace(), call),  # type: ignore[arg-type]
                misc._handle_function_plan_preview_start(SimpleNamespace(), call),  # type: ignore[arg-type]
            )

    first, second = asyncio.run(scenario())
    assert len(attempts) == 2
    assert first["success"] is False
    assert second == {"success": True, "already_armed": False, "instance": _INSTANCE}


class _PreviewCoordinator(SimpleNamespace):
    @property
    def preview_card_open(self) -> bool:
        return self.preview_following

    def start_preview_following(self, card_id: str | None = None) -> bool:
        self.preview_following = True
        return False

    def end_preview_following(self, card_id: str | None = None) -> bool:
        self.preview_following = False
        self.preview_stop_generation += 1
        return True

    def stop_preview(self) -> bool:
        self.stops.append(self.preview_armed)
        self.preview_armed = False
        return True


_INSTANCE = "coordinator-1"


def _follow_coordinator(*, following: bool, preview_armed: bool = False) -> _PreviewCoordinator:
    return _PreviewCoordinator(
        preview_instance_id=_INSTANCE,
        preview_armed=preview_armed,
        preview_following=following,
        server_id="iosrv1",
        preview_follow_lock=asyncio.Lock(),
        preview_follow_generation=0,
        preview_stop_generation=0,
        stops=[],
    )


def _run_follow(*, following: bool, selection: bool, render: AsyncMock) -> None:
    # preview_armed stays off: a frozen orphaned-plan render leaves the poll off while the card shows it.
    coordinator = _follow_coordinator(following=following)
    with (
        patch.object(plan_preview, "preview_selection_available", return_value=selection),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        asyncio.run(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]


def test_a_selection_change_follows_an_open_card() -> None:
    """Review (Copilot, #132): also after a frozen orphaned-plan render, which arms no poll."""
    render = AsyncMock()
    _run_follow(following=True, selection=True, render=render)
    render.assert_awaited_once()


@pytest.mark.parametrize(
    ("following", "selection"), [(False, True), (True, False)], ids=["no-card-open", "nothing-selected"]
)
def test_a_selection_change_renders_nothing_without_an_open_card_or_a_plan(following: bool, selection: bool) -> None:
    """Picking a plan on the device page must not start the poll."""
    render = AsyncMock()
    _run_follow(following=following, selection=selection, render=render)
    render.assert_not_called()


def test_a_failed_follow_render_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    render = AsyncMock(side_effect=HomeAssistantError("backup vanished"))
    with caplog.at_level(logging.WARNING):
        _run_follow(following=True, selection=True, render=render)
    assert "backup vanished" in caplog.text


def test_an_unexpected_follow_error_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """The follow runs as a background task: an error must end in our log, not in asyncio's."""
    render = AsyncMock(side_effect=OSError("disk full"))
    with caplog.at_level(logging.ERROR):
        _run_follow(following=True, selection=True, render=render)
    assert "failed to follow the new selection" in caplog.text
    assert "disk full" in caplog.text


def test_a_selection_changed_during_the_opening_render_is_shown_after_it() -> None:
    """Review (Copilot, #132): the card's opening render must not overwrite a selection made meanwhile."""
    coordinator = _follow_coordinator(following=True)
    coordinator.selection = "A"
    rendered: list[str] = []

    async def scenario() -> None:
        release = asyncio.Event()

        async def render(c: SimpleNamespace) -> None:
            selection = c.selection
            if selection == "A":
                await release.wait()
            rendered.append(selection)

        with (
            patch.object(plan_preview, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", render),
        ):
            opening = asyncio.ensure_future(plan_preview.async_render_opened_preview(coordinator))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            coordinator.selection = "B"
            follow = asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(opening, follow)

    asyncio.run(scenario())
    assert rendered == ["A", "B"]


def test_a_selection_followed_before_the_opening_start_is_kept() -> None:
    """Review (Sourcery, #171): an opening start that finds the preview armed must not supersede a queued follow."""
    coordinator = _follow_coordinator(following=True)
    coordinator.selection = "A"
    rendered: list[str] = []

    async def scenario() -> bool:
        release = asyncio.Event()

        async def render(c: SimpleNamespace) -> None:
            selection = c.selection
            if selection == "A":
                await release.wait()
            rendered.append(selection)
            c.preview_armed = True

        with (
            patch.object(plan_preview, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", render),
        ):
            first = asyncio.ensure_future(plan_preview.async_render_opened_preview(coordinator))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            coordinator.selection = "B"
            follow = asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
            second = asyncio.ensure_future(plan_preview.async_render_opened_preview(coordinator))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            release.set()
            _, _, second_rendered = await asyncio.gather(first, follow, second)
            return second_rendered

    assert asyncio.run(scenario()) is False
    assert rendered == ["A", "B"]


def test_follows_scheduled_in_the_same_tick_render_once() -> None:
    """Review: a plan change makes the 'Backup' and the 'Plan' selector follow — one render, not two."""
    coordinator = _follow_coordinator(following=True)
    render = AsyncMock()

    async def scenario() -> None:
        with (
            patch.object(plan_preview, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", render),
        ):
            backup_follow = asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
            plan_follow = asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
            await asyncio.gather(backup_follow, plan_follow)

    asyncio.run(scenario())
    render.assert_awaited_once()


def test_rapid_selection_changes_end_on_the_latest_selection() -> None:
    """Review (Copilot, #132): a slow render of an older selection must not publish after a newer one."""
    coordinator = _follow_coordinator(following=True)
    coordinator.selection = "A"
    rendered: list[str] = []

    async def scenario() -> None:
        release = asyncio.Event()

        async def render(c: SimpleNamespace) -> None:
            selection = c.selection
            if selection == "A":
                await release.wait()
            rendered.append(selection)

        with (
            patch.object(plan_preview, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", render),
        ):
            follows = [asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))]  # type: ignore[arg-type]
            await asyncio.sleep(0)
            for selection in ("B", "C"):
                coordinator.selection = selection
                follows.append(asyncio.ensure_future(plan_preview.async_follow_selection(coordinator)))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*follows)

    asyncio.run(scenario())
    # "B" was superseded while it waited; "C" renders after "A" finished.
    assert rendered == ["A", "C"]


def _armed_cache(fub_id: int) -> dict:
    return {
        "fub_id": fub_id,
        "plan_name": f"P{fub_id}",
        "elements": {},
        "connections": {},
        "snapshot_source": None,
        "label_metadata": None,
        "block_settings": None,
        "paper_format": None,
    }


def test_an_armed_re_render_never_publishes_over_a_newer_selection() -> None:
    """Review (Copilot, #132): a poll/webhook re-render of the old plan waits for the selection render.

    Once the lock is held the old plan is no longer armed: the re-render publishes nothing and
    does not hand the new plan the old plan's wire values.
    """
    published: list[int] = []

    async def generate(fub_id: int, *_args: object) -> None:
        published.append(fub_id)

    old_cache = _armed_cache(1)
    coordinator = SimpleNamespace(
        preview_follow_lock=asyncio.Lock(),
        _preview_plan_cache=old_cache,
        _connection_values={},
        async_generate_plan_preview=generate,
    )

    async def scenario() -> None:
        async with coordinator.preview_follow_lock:  # the selection render of plan 2 is under way
            re_render = asyncio.create_task(
                ComexioCoordinator._render_armed_preview(coordinator, old_cache, {"c1": 1})  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            coordinator._preview_plan_cache = _armed_cache(2)
            published.append(2)
        await re_render

    asyncio.run(scenario())
    assert published == [2]
    assert coordinator._connection_values == {}


def test_an_armed_re_render_takes_the_poll_values_of_its_own_plan() -> None:
    published: list[int] = []

    async def generate(fub_id: int, *_args: object) -> None:
        published.append(fub_id)

    cache = _armed_cache(1)
    coordinator = SimpleNamespace(
        preview_follow_lock=asyncio.Lock(),
        _preview_plan_cache=cache,
        _connection_values={},
        async_generate_plan_preview=generate,
    )
    asyncio.run(ComexioCoordinator._render_armed_preview(coordinator, cache, {"c1": 1}))  # type: ignore[arg-type]
    assert published == [1]
    assert coordinator._connection_values == {"c1": 1}


def _cards_coordinator(monkeypatch: pytest.MonkeyPatch, now: list[float]) -> ComexioCoordinator:
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])
    return _real_coordinator(monkeypatch)


def _run_stop(coordinator: object, data: dict) -> dict:
    call = SimpleNamespace(data=data)
    with patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))):
        return asyncio.run(misc._handle_function_plan_preview_stop(SimpleNamespace(), call))  # type: ignore[arg-type]


def test_one_card_closing_leaves_another_open_cards_preview_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #171): the stop was coordinator-wide, so closing one card froze the other's live values."""
    now = [1000.0]
    coordinator = _cards_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")
    coordinator.start_preview_following("card-b")
    coordinator._preview_plan_cache = _armed_cache(1)

    assert _run_stop(coordinator, {"card_id": "card-a"}) == {"success": True, "stopped": False}
    assert coordinator.preview_armed is True
    assert coordinator.preview_following is True
    assert coordinator.preview_stop_generation == 0

    assert _run_stop(coordinator, {"card_id": "card-b"}) == {"success": True}
    assert coordinator.preview_armed is False
    assert coordinator.preview_following is False
    assert coordinator.preview_stop_generation == 1


def test_a_card_gone_without_a_stop_does_not_keep_the_preview_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed browser tab sends no stop: once its keepalives stay away, the last open card's stop disarms."""
    now = [1000.0]
    coordinator = _cards_coordinator(monkeypatch, now)
    coordinator.start_preview_following("closed-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC - 1
    coordinator.start_preview_following("card-b")
    assert coordinator.end_preview_following("card-b") is False  # the tab is still within its keepalive window
    coordinator.start_preview_following("card-b")
    now[0] += 1
    assert coordinator.end_preview_following("card-b") is True


def test_a_stop_without_a_card_id_ends_the_preview_for_every_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """An older cached card or a script: the stop stays coordinator-wide, as before card ids existed."""
    now = [1000.0]
    coordinator = _cards_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")
    coordinator.start_preview_following("card-b")
    coordinator._preview_plan_cache = _armed_cache(1)
    assert _run_stop(coordinator, {}) == {"success": True}
    assert coordinator.preview_armed is False
    assert coordinator.preview_following is False
    assert coordinator._preview_cards == {}
    assert coordinator._preview_cards_dropped == {}


def test_the_auto_stop_ends_the_preview_for_every_open_card(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator = _cards_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")
    coordinator.start_preview_following("card-b")
    coordinator._preview_plan_cache = _armed_cache(1)
    asyncio.run(coordinator._async_auto_stop_preview(None))
    assert coordinator.preview_armed is False
    assert coordinator.preview_following is False
    assert coordinator._preview_cards == {}
    assert coordinator.preview_stop_generation == 1


def test_start_and_keepalive_register_the_calling_card(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator = _cards_coordinator(monkeypatch, now)
    _run_start(armed=False, selection=False, coordinator=coordinator, data={"card_id": "card-a"})
    call = SimpleNamespace(data={"keepalive": True, "card_id": "card-b"})
    with patch.object(misc, "_resolve_coordinator", MagicMock(return_value=coordinator)):
        asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert set(coordinator._preview_cards) == {"card-a", "card-b"}


def _close_the_last_card_while_rendering() -> AsyncMock:
    async def render(c: _PreviewCoordinator) -> None:
        c.end_preview_following()  # the last card's stop lands while Comexio answers
        c.preview_armed = True

    return AsyncMock(side_effect=render)


def test_a_start_whose_card_closed_during_the_render_disarms_and_says_so() -> None:
    """Review (#171): the stop found nothing armed yet, so the render armed a poll no card shows, reporting success."""
    coordinator = _follow_coordinator(following=False)
    result, _ = _run_start(
        armed=False,
        selection=True,
        render=_close_the_last_card_while_rendering(),
        coordinator=coordinator,
        still_following=False,
    )
    assert result == {"success": False, "error": "The preview was stopped while it rendered.", "instance": _INSTANCE}
    assert coordinator.preview_armed is False
    assert coordinator.stops == [True]


def _follow_with(render: AsyncMock) -> _PreviewCoordinator:
    coordinator = _follow_coordinator(following=True)
    with (
        patch.object(plan_preview, "preview_selection_available", return_value=True),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        asyncio.run(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
    return coordinator


def test_a_follow_whose_card_closed_during_the_render_disarms() -> None:
    coordinator = _follow_with(_close_the_last_card_while_rendering())
    assert coordinator.preview_armed is False
    assert coordinator.stops == [True]


def test_an_undisturbed_follow_keeps_its_preview_armed() -> None:
    async def render(c: _PreviewCoordinator) -> None:
        c.preview_armed = True

    coordinator = _follow_with(AsyncMock(side_effect=render))
    assert coordinator.preview_armed is True
    assert coordinator.stops == []


def test_the_svg_visualize_renders_under_the_selection_lock_and_supersedes_queued_follows() -> None:
    """Review (#171): it armed outside the lock, so a queued selection follow could overwrite it, or it a newer one."""
    coordinator = _follow_coordinator(following=True)
    seen: list[tuple[bool, int]] = []

    async def generate(*_args: object) -> str:
        seen.append((coordinator.preview_follow_lock.locked(), coordinator.preview_follow_generation))
        return "/local/preview.svg"

    coordinator.async_generate_plan_preview = AsyncMock(side_effect=generate)
    coordinator.snapshot_block_settings_for_source = MagicMock(return_value=None)
    coordinator.snapshot_paper_format_for_source = MagicMock(return_value=None)
    source = (coordinator, None, 3, "Plan", {}, {}, "live", None)
    call = SimpleNamespace(data={"format": "svg"}, return_response=True)
    with (
        patch.object(plan_actions, "_resolve_visualize_live_source", AsyncMock(return_value=source)),
        patch.object(plan_actions.persistent_notification, "async_create"),
    ):
        result = asyncio.run(plan_actions.handle_function_plan_visualize(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert result == {"plan_name": "Plan", "url": "/local/preview.svg"}
    assert seen == [(True, 1)]


class _Timers:
    """async_call_later stand-in: records each timer so a test fires it when its time has come."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, object]] = []

    def __call__(self, _hass: object, delay: float, action: object) -> MagicMock:
        entry = (delay, action)
        self.pending.append(entry)
        return MagicMock(side_effect=lambda: self.pending.remove(entry) if entry in self.pending else None)

    def sweeps(self, coordinator: ComexioCoordinator) -> list[float]:
        return [d for d, a in self.pending if a == coordinator._async_sweep_preview_cards]

    def fire_sweep(self, coordinator: ComexioCoordinator) -> None:
        entry = next(e for e in self.pending if e[1] == coordinator._async_sweep_preview_cards)
        self.pending.remove(entry)
        asyncio.run(coordinator._async_sweep_preview_cards(None))


def _timed_coordinator(monkeypatch: pytest.MonkeyPatch, now: list[float]) -> tuple[ComexioCoordinator, _Timers]:
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])
    timers = _Timers()
    monkeypatch.setattr(coordinator_module, "async_call_later", timers)
    return ComexioCoordinator(MagicMock(), MagicMock(), MagicMock()), timers


def test_a_card_reloaded_without_a_stop_does_not_hold_the_preview_past_its_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review (#171): F5 sends no stop; the old card's id must not keep the poll running once the new card closes."""
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("before-f5")
    now[0] += 10
    coordinator.start_preview_following("after-f5")
    coordinator._preview_plan_cache = _armed_cache(1)

    assert _run_stop(coordinator, {"card_id": "after-f5"}) == {"success": True, "stopped": False}
    assert timers.sweeps(coordinator) == [coordinator_module.PREVIEW_CARD_ALIVE_SEC - 10]
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC - 10
    timers.fire_sweep(coordinator)
    assert coordinator.preview_armed is False
    assert coordinator.preview_card_open is False
    assert coordinator.preview_stop_generation == 1


def test_the_sweep_waits_while_the_remaining_card_keeps_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")
    coordinator.start_preview_following("card-b")
    coordinator._preview_plan_cache = _armed_cache(1)
    _run_stop(coordinator, {"card_id": "card-b"})

    now[0] += 100
    coordinator.start_preview_following("card-a")  # card-a's keepalive
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC - 100
    timers.fire_sweep(coordinator)
    assert coordinator.preview_armed is True
    assert timers.sweeps(coordinator) == [100]  # re-checks when card-a's latest keepalive runs out

    assert _run_stop(coordinator, {"card_id": "card-a"}) == {"success": True}
    assert timers.sweeps(coordinator) == []
    assert coordinator.preview_armed is False


def test_a_stalled_card_is_told_to_rearm_after_another_cards_stop_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (#171): a frozen background tab, dropped as gone, must not stay open without live values."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    coordinator._preview_plan_cache = _armed_cache(1)
    assert _run_stop(coordinator, {"card_id": "card-b"}) == {"success": True}

    call = SimpleNamespace(data={"keepalive": True, "card_id": "frozen-tab"})
    with patch.object(misc, "_resolve_coordinator", MagicMock(return_value=coordinator)):
        first = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
        second = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    assert first["rearm"] is True
    assert "rearm" not in second  # told once; its re-sent start does the rest


def test_a_dropped_card_is_not_told_to_rearm_while_the_preview_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    coordinator.start_preview_following("card-c")
    coordinator._preview_plan_cache = _armed_cache(1)
    assert _run_stop(coordinator, {"card_id": "card-b"}) == {"success": True, "stopped": False}
    assert "rearm" not in _keepalive(coordinator, "frozen-tab")


def test_the_auto_stop_is_not_undone_by_a_dropped_cards_rearm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #171): the frozen tab's keepalive after an auto-stop must not ask for a new start."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    coordinator.start_preview_following("card-c")
    coordinator._preview_plan_cache = _armed_cache(1)
    _run_stop(coordinator, {"card_id": "card-b"})  # drops frozen-tab, card-c keeps the preview
    assert "frozen-tab" in coordinator._preview_cards_dropped
    asyncio.run(coordinator._async_auto_stop_preview(None))
    assert coordinator.preview_armed is False
    assert "rearm" not in _keepalive(coordinator, "frozen-tab")


def test_a_dropped_card_is_remembered_while_an_extended_preview_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #171): an extended preview outlasts the default window, its dropped cards must too."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator._preview_plan_cache = _armed_cache(1)
    assert coordinator.set_preview_auto_stop_extension(60) is True
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    coordinator.start_preview_following("card-c")
    _run_stop(coordinator, {"card_id": "card-b"})  # drops frozen-tab
    now[0] += 30 * 60  # past the default window, within the extension
    coordinator.start_preview_following("card-c")
    assert _run_stop(coordinator, {"card_id": "card-c"}) == {"success": True}
    assert _keepalive(coordinator, "frozen-tab")["rearm"] is True


def _keepalive(coordinator: object, card_id: str) -> dict:
    call = SimpleNamespace(data={"keepalive": True, "card_id": card_id})
    with patch.object(misc, "_resolve_coordinator", MagicMock(return_value=coordinator)):
        return asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]


def test_a_stop_from_an_unknown_card_after_a_restart_disarms(monkeypatch: pytest.MonkeyPatch) -> None:
    """After an HA restart no card is registered yet: a surviving card's stop must still disarm."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator._preview_plan_cache = _armed_cache(1)
    assert _run_stop(coordinator, {"card_id": "survivor"}) == {"success": True}
    assert coordinator.preview_armed is False
    assert coordinator.preview_stop_generation == 1


@pytest.mark.parametrize("card_id", ["", None])
def test_a_missing_card_id_registers_no_card(monkeypatch: pytest.MonkeyPatch, card_id: str | None) -> None:
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    _run_start(armed=False, selection=False, coordinator=coordinator, data={"card_id": card_id})
    assert coordinator._preview_cards == {}


def test_one_cards_stop_during_another_cards_render_keeps_the_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the last card's stop may disarm a render: a stop while another card is alive changes nothing."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")

    async def render(c: ComexioCoordinator) -> None:
        assert c.end_preview_following("card-a") is False
        c._preview_plan_cache = _armed_cache(1)

    result, _ = _run_start(
        armed=False,
        selection=True,
        render=AsyncMock(side_effect=render),
        coordinator=coordinator,
        data={"card_id": "card-b"},
    )
    assert result["success"] is True
    assert coordinator.preview_armed is True
    assert coordinator.preview_stop_generation == 0


def test_a_card_reopened_during_the_render_keeps_what_it_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (#171): the last card closed, a new one opened before the render ended — its preview must stay armed."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)

    async def render(c: ComexioCoordinator) -> None:
        assert c.end_preview_following("card-a") is True
        c.start_preview_following("card-b")
        c._preview_plan_cache = _armed_cache(1)

    result, _ = _run_start(
        armed=False,
        selection=True,
        render=AsyncMock(side_effect=render),
        coordinator=coordinator,
        data={"card_id": "card-a"},
    )
    assert result["success"] is True
    assert coordinator.preview_armed is True


def test_an_auto_stop_during_the_opening_render_disarms(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)

    async def render(c: ComexioCoordinator) -> None:
        c._preview_plan_cache = _armed_cache(1)
        await c._async_auto_stop_preview(None)
        c._preview_plan_cache = _armed_cache(1)  # the render's own arm lands after the auto-stop

    result, _ = _run_start(
        armed=False,
        selection=True,
        render=AsyncMock(side_effect=render),
        coordinator=coordinator,
        data={"card_id": "card-a"},
        still_following=False,
    )
    assert result["success"] is False
    assert coordinator.preview_armed is False


def test_a_start_stopped_while_rendering_logs_quietly(caplog: pytest.LogCaptureFixture) -> None:
    """Leaving the dashboard during the first render is routine: no warning in the HA log."""
    coordinator = _follow_coordinator(following=False)
    with caplog.at_level(logging.DEBUG, logger=misc.__name__):
        _run_start(
            armed=False,
            selection=True,
            render=_close_the_last_card_while_rendering(),
            coordinator=coordinator,
            still_following=False,
        )
    assert [r.levelno for r in caplog.records if r.name == misc.__name__ and r.levelno >= logging.WARNING] == []


def test_a_follow_queued_before_a_visualize_never_renders_after_it() -> None:
    """The svg visualize is the newest request: a selection follow that waited on the lock must not overwrite it."""
    coordinator = _follow_coordinator(following=True)
    order: list[str] = []
    follow_render = AsyncMock(side_effect=lambda _c: order.append("follow"))

    async def scenario() -> None:
        await coordinator.preview_follow_lock.acquire()  # a render in progress
        with (
            patch.object(plan_preview, "preview_selection_available", return_value=True),
            patch.object(plan_preview, "async_render_selected_preview", follow_render),
            patch.object(plan_actions, "_resolve_visualize_live_source", AsyncMock(return_value=source)),
            patch.object(plan_actions.persistent_notification, "async_create"),
        ):
            follow = asyncio.ensure_future(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            visualize = asyncio.ensure_future(plan_actions.handle_function_plan_visualize(SimpleNamespace(), call))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            coordinator.preview_follow_lock.release()
            await asyncio.gather(follow, visualize)

    async def generate(*_args: object) -> str:
        order.append("visualize")
        return "/local/preview.svg"

    coordinator.async_generate_plan_preview = AsyncMock(side_effect=generate)
    coordinator.snapshot_block_settings_for_source = MagicMock(return_value=None)
    coordinator.snapshot_paper_format_for_source = MagicMock(return_value=None)
    source = (coordinator, None, 3, "Plan", {}, {}, "live", None)
    call = SimpleNamespace(data={"format": "svg"}, return_response=True)
    asyncio.run(scenario())
    assert order[-1] == "visualize"


def _drop_frozen_tab_and_close_b(coordinator: ComexioCoordinator, now: list[float], *, keep_c: bool) -> dict:
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    if keep_c:
        coordinator.start_preview_following("card-c")
    coordinator._preview_plan_cache = _armed_cache(1)
    return _run_stop(coordinator, {"card_id": "card-b"})


def test_a_sweep_survives_a_disarm_that_ends_no_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (#171): an orphaned-plan render or the poll breaker disarms, a follow re-arms: the sweep still runs."""
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    _drop_frozen_tab_and_close_b(coordinator, now, keep_c=True)
    assert len(timers.sweeps(coordinator)) == 1
    coordinator.stop_preview()  # e.g. the 'Backup' selector switched to a deleted plan's frozen render
    assert len(timers.sweeps(coordinator)) == 1
    coordinator._preview_plan_cache = _armed_cache(1)  # a follow back to a live plan re-arms
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    timers.fire_sweep(coordinator)
    assert coordinator.preview_armed is False


def test_a_last_stop_that_disarmed_nothing_gives_no_rearm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (#171): the last card's stop found it already auto-stopped, so the frozen tab resumes nothing."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("frozen-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    coordinator.start_preview_following("card-b")
    assert _run_stop(coordinator, {"card_id": "card-b"}) == {
        "success": False,
        "error": "No live plan preview is currently armed.",
    }
    assert coordinator.start_preview_following("frozen-tab") is False


def test_a_dropped_card_does_not_undo_the_poll_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    _drop_frozen_tab_and_close_b(coordinator, now, keep_c=False)
    coordinator._preview_plan_cache = _armed_cache(1)  # re-armed by a follow, then
    coordinator._disarm_preview_cache()  # the poll breaker disarms
    assert coordinator.start_preview_following("frozen-tab") is False


def test_drops_are_forgotten_after_the_auto_stop_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each page load brings a new card id: the drops of reloaded pages must not pile up for HA's lifetime."""
    now = [1000.0]
    coordinator, _ = _timed_coordinator(monkeypatch, now)
    _drop_frozen_tab_and_close_b(coordinator, now, keep_c=True)
    assert set(coordinator._preview_cards_dropped) == {"frozen-tab"}
    now[0] += coordinator_module._PREVIEW_AUTO_STOP_DEFAULT_MINUTES * 60 + 1
    coordinator.start_preview_following("card-c")
    assert coordinator.end_preview_following("card-c") is True
    assert coordinator._preview_cards_dropped == {}


def test_shutdown_cancels_a_pending_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    _drop_frozen_tab_and_close_b(coordinator, now, keep_c=True)
    coordinator._knx_dpt_reload_cancel = None
    with patch.object(coordinator_module.DataUpdateCoordinator, "async_shutdown", AsyncMock()):
        asyncio.run(coordinator.async_shutdown())
    assert timers.sweeps(coordinator) == []


def test_a_lone_card_gone_without_a_stop_is_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Sourcery, #171): with no stop at all, a closed tab's card must not stay registered and polling."""
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("closed-tab")
    coordinator._preview_plan_cache = _armed_cache(1)
    assert timers.sweeps(coordinator) == [coordinator_module.PREVIEW_CARD_ALIVE_SEC]
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    timers.fire_sweep(coordinator)
    assert coordinator._preview_cards == {}
    assert coordinator.preview_armed is False
    assert coordinator.preview_card_open is False
    assert _keepalive(coordinator, "closed-tab")["rearm"] is True  # a frozen tab that wakes up resumes


def test_keepalives_do_not_reschedule_a_pending_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep stays due when the oldest card runs out, not pushed back by a newer card's keepalive."""
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("card-a")
    now[0] += 10
    coordinator.start_preview_following("card-b")
    now[0] += 20
    coordinator.start_preview_following("card-b")
    assert timers.sweeps(coordinator) == [coordinator_module.PREVIEW_CARD_ALIVE_SEC]
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC - 30
    timers.fire_sweep(coordinator)
    assert set(coordinator._preview_cards) == {"card-b"}
    assert timers.sweeps(coordinator) == [30]


def test_a_lone_unarmed_card_gone_without_a_stop_ends_the_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card that armed nothing (empty selection, failed render) and vanished: following ends, no rearm."""
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("closed-tab")
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    timers.fire_sweep(coordinator)
    assert coordinator.preview_following is False
    assert coordinator.preview_stop_generation == 1
    assert coordinator._preview_cards_disarm_generation is None
    assert "rearm" not in _keepalive(coordinator, "closed-tab")


def test_a_sweep_that_disarms_logs_at_info(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    now = [1000.0]
    coordinator, timers = _timed_coordinator(monkeypatch, now)
    coordinator.start_preview_following("closed-tab")
    coordinator._preview_plan_cache = _armed_cache(1)
    now[0] += coordinator_module.PREVIEW_CARD_ALIVE_SEC
    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        timers.fire_sweep(coordinator)
    assert [r.levelno for r in caplog.records if "no open plan card answered" in r.getMessage()] == [logging.INFO]
