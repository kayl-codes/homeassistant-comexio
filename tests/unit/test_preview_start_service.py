"""Plan preview: the opening card renders the selection, a selection change follows it only while armed."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.comexio import plan_preview
from custom_components.comexio.services import misc


def _run_start(*, armed: bool, selection: bool, render: AsyncMock | None = None) -> tuple[dict, AsyncMock]:
    coordinator = SimpleNamespace(preview_armed=armed, server_id="iosrv1")
    render = render or AsyncMock()
    call = SimpleNamespace(data={})
    with (
        patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(misc, "preview_selection_available", return_value=selection),
        patch.object(misc, "async_render_selected_preview", render),
    ):
        result = asyncio.run(misc._handle_function_plan_preview_start(SimpleNamespace(), call))  # type: ignore[arg-type]
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


def test_a_failed_render_is_not_reported_as_armed() -> None:
    """E.g. an expired session: the plan does not load, so the card must not be told the poll runs."""
    render = AsyncMock(side_effect=HomeAssistantError("The plan 'X' (ID 3) could not be loaded from Comexio."))
    result, _ = _run_start(armed=False, selection=True, render=render)
    assert result == {"success": False, "error": "The plan 'X' (ID 3) could not be loaded from Comexio."}


def _run_follow(*, armed: bool, selection: bool, render: AsyncMock) -> None:
    coordinator = SimpleNamespace(preview_armed=armed, server_id="iosrv1")
    with (
        patch.object(plan_preview, "preview_selection_available", return_value=selection),
        patch.object(plan_preview, "async_render_selected_preview", render),
    ):
        asyncio.run(plan_preview.async_follow_selection(coordinator))  # type: ignore[arg-type]


def test_a_selection_change_follows_an_open_card() -> None:
    render = AsyncMock()
    _run_follow(armed=True, selection=True, render=render)
    render.assert_awaited_once()


@pytest.mark.parametrize(
    ("armed", "selection"), [(False, True), (True, False)], ids=["no-card-open", "nothing-selected"]
)
def test_a_selection_change_renders_nothing_without_an_open_card_or_a_plan(armed: bool, selection: bool) -> None:
    """Picking a plan on the device page must not start the poll."""
    render = AsyncMock()
    _run_follow(armed=armed, selection=selection, render=render)
    render.assert_not_called()


def test_a_failed_follow_render_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    render = AsyncMock(side_effect=HomeAssistantError("backup vanished"))
    with caplog.at_level(logging.WARNING):
        _run_follow(armed=True, selection=True, render=render)
    assert "backup vanished" in caplog.text


def test_an_unexpected_follow_error_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """The follow runs as a background task: an error must end in our log, not in asyncio's."""
    render = AsyncMock(side_effect=OSError("disk full"))
    with caplog.at_level(logging.ERROR):
        _run_follow(armed=True, selection=True, render=render)
    assert "failed to follow the new selection" in caplog.text
    assert "disk full" in caplog.text
