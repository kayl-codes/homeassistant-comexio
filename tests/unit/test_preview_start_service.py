"""function_plan_preview_start: opening the plan card arms the live preview via the Preview button."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comexio.services import misc

BUTTON_EID = "button.iosrv1_plan_preview"


def _run(*, armed: bool, button_state: str | None) -> tuple[dict, AsyncMock]:
    coordinator = SimpleNamespace(preview_armed=armed, server_id="iosrv1")
    press = AsyncMock()
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: SimpleNamespace(state=button_state) if button_state and eid == BUTTON_EID else None
        ),
        services=SimpleNamespace(async_call=press),
    )
    registry = MagicMock()
    registry.async_get_entity_id.return_value = BUTTON_EID
    call = SimpleNamespace(data={})
    with (
        patch.object(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, None, None))),
        patch.object(misc.er, "async_get", return_value=registry),
    ):
        result = asyncio.run(misc._handle_function_plan_preview_start(hass, call))  # type: ignore[arg-type]
    registry_calls = registry.async_get_entity_id.call_args_list
    if registry_calls:
        assert registry_calls[0].args[2] == "comexio_iosrv1_plan_preview_btn"
    return result, press


def test_opening_the_card_presses_the_preview_button() -> None:
    result, press = _run(armed=False, button_state="unknown")
    assert result == {"success": True, "already_armed": False}
    press.assert_awaited_once_with("button", "press", {"entity_id": BUTTON_EID}, blocking=True)


def test_an_armed_preview_is_left_alone() -> None:
    result, press = _run(armed=True, button_state="unknown")
    assert result == {"success": True, "already_armed": True}
    press.assert_not_called()


@pytest.mark.parametrize("button_state", ["unavailable", None], ids=["no-plan-selected", "button-missing"])
def test_nothing_to_preview_is_reported(button_state: str | None) -> None:
    result, press = _run(armed=False, button_state=button_state)
    assert result["success"] is False
    press.assert_not_called()
