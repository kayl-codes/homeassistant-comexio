"""Plan preview: the opening card renders the selection, a selection change follows it while a card is open."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import coordinator as coordinator_module, plan_preview
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.services import misc


def _run_start(*, armed: bool, selection: bool, render: AsyncMock | None = None) -> tuple[dict, AsyncMock]:
    coordinator = _follow_coordinator(following=False, preview_armed=armed)
    render = render or AsyncMock()
    call = SimpleNamespace(data={})
    with (
        patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(misc, "preview_selection_available", return_value=selection),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
    # Whatever the card finds, its later Plan/Backup picks follow until it closes.
    assert coordinator.preview_following is True
    return result, render


def test_opening_the_card_renders_the_selection() -> None:
    result, render = _run_start(armed=False, selection=True)
    assert result == {"success": True, "already_armed": False}
    render.assert_awaited_once()


def test_an_armed_preview_is_left_alone() -> None:
    result, render = _run_start(armed=True, selection=True)
    assert result == {"success": True, "already_armed": True}
    render.assert_not_called()


def test_nothing_to_preview_is_reported() -> None:
    result, render = _run_start(armed=False, selection=False)
    assert result["success"] is False
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
    assert result == {"success": True, "keepalive": True}
    assert coordinator.preview_following is True
    render.assert_not_called()
    assert resolve.call_args.kwargs == {"quiet": True}


def test_closing_the_card_ends_the_following() -> None:
    coordinator = _follow_coordinator(following=True)
    coordinator.stop_preview = MagicMock(return_value=False)
    with patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))):
        asyncio.run(misc._handle_function_plan_preview_stop(SimpleNamespace(), SimpleNamespace(data={})))  # type: ignore[arg-type]
    assert coordinator.preview_following is False
    coordinator.stop_preview.assert_called_once_with()


def test_an_opened_card_follows_within_the_auto_stop_window_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed browser tab sends no preview_stop: without an armed preview the following must run out."""
    now = [1000.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now[0])
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator._preview_plan_cache = None
    coordinator._preview_following_until = 0.0
    assert coordinator.preview_following is False
    coordinator.start_preview_following()
    assert coordinator.preview_following is True
    now[0] += coordinator_module._PREVIEW_AUTO_STOP_DEFAULT_MINUTES * 60
    assert coordinator.preview_following is False
    coordinator.start_preview_following()
    coordinator.end_preview_following()
    assert coordinator.preview_following is False
    coordinator._preview_plan_cache = {"fub_id": 19}  # an armed preview follows as before
    assert coordinator.preview_following is True


def test_a_failed_render_is_not_reported_as_armed() -> None:
    """E.g. an expired session: the plan does not load, so the card must not be told the poll runs."""
    render = AsyncMock(side_effect=HomeAssistantError("The plan 'X' (ID 3) could not be loaded from Comexio."))
    result, _ = _run_start(armed=False, selection=True, render=render)
    assert result == {"success": False, "error": "The plan 'X' (ID 3) could not be loaded from Comexio."}


class _PreviewCoordinator(SimpleNamespace):
    def start_preview_following(self) -> None:
        self.preview_following = True

    def end_preview_following(self) -> None:
        self.preview_following = False


def _follow_coordinator(*, following: bool, preview_armed: bool = False) -> _PreviewCoordinator:
    return _PreviewCoordinator(
        preview_armed=preview_armed,
        preview_following=following,
        server_id="iosrv1",
        preview_follow_lock=asyncio.Lock(),
        preview_follow_generation=0,
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
