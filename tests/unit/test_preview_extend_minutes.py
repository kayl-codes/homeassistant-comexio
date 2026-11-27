"""`function_plan_preview_extend` takes only whole minutes: int() alone let 1.9 through as 1 and True as 1."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio.services import misc


def _run_extend(minutes: object) -> tuple[dict, MagicMock]:
    coordinator = MagicMock()
    coordinator.set_preview_auto_stop_extension.return_value = True
    call = SimpleNamespace(data={"minutes": minutes})
    with patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))):
        result = asyncio.run(misc._handle_function_plan_preview_extend(SimpleNamespace(), call))  # type: ignore[arg-type]
    return result, coordinator


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(15, 15), ("15", 15), (15.0, 15), (" 30 ", 30), (1, 1), (1440, 1440)],
    ids=["int", "numeric-string", "whole-float", "padded-string", "min", "max"],
)
def test_whole_minutes_are_accepted(minutes: object, expected: int) -> None:
    result, coordinator = _run_extend(minutes)
    assert result == {"success": True, "minutes": expected}
    coordinator.set_preview_auto_stop_extension.assert_called_once_with(expected)


@pytest.mark.parametrize(
    "minutes",
    [1.9, 15.5, True, False, "1.9", "abc", None, 0, 1441, -5],
    ids=["fraction", "fraction-2", "true", "false", "string-fraction", "text", "none", "zero", "too-big", "negative"],
)
def test_anything_but_whole_minutes_is_turned_away_without_extending(minutes: object) -> None:
    result, coordinator = _run_extend(minutes)
    assert result["success"] is False
    coordinator.set_preview_auto_stop_extension.assert_not_called()
