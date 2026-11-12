"""set_value target parsing: '<Extension>#<IO>' with spaces in the extension name (#259), repetition (#289)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.comexio.const import MarkerKind
from custom_components.comexio.services import misc
from custom_components.comexio.services.misc import _resolve_set_value_target

_DATA: dict[str, Any] = {
    "io": [
        {"id": "58", "ext_name": "IO-Extension A2", "identifier": "AO4", "is_input": False, "is_binary": False},
        {"id": "3", "ext_name": "IOX2", "identifier": "Q3", "is_input": False, "is_binary": True},
    ],
    "markers": [
        {"id": "107", "type": "digital", "kind": MarkerKind.NORMAL},
        {"id": "108", "type": "digital", "kind": MarkerKind.TRIGGER},
        {"id": "109", "type": "analog", "kind": MarkerKind.KNX_BRIDGE},
        {"id": "110", "type": "digital"},
    ],
}


@pytest.mark.parametrize(
    ("target", "io"),
    [
        ("IO-Extension A2#AO4", ("58", "IO-Extension A2", "AO4")),
        ("io-extension a2#ao4", ("58", "IO-Extension A2", "AO4")),
        ("  IO-Extension A2 # AO4  ", ("58", "IO-Extension A2", "AO4")),
        ("IOX2#Q3", ("3", "IOX2", "Q3")),
    ],
)
def test_io_target_with_spaces_in_the_extension_name(target: str, io: tuple[str, str, str]) -> None:
    kwargs, error = _resolve_set_value_target(target, 1.0, _DATA)

    io_id, ext, identifier = io
    expected = {"target_type": "io", "target_id": io_id, "ext": ext, "identifier": identifier, "repeat": True}
    assert (error, kwargs) == ("", expected)


@pytest.mark.parametrize("target", ["#AO4", "   #AO4", "IOX2#", "IOX2#Q 3", "A#B#C", "IOX2Q3"])
def test_malformed_io_target_is_rejected(target: str) -> None:
    kwargs, error = _resolve_set_value_target(target, 1.0, _DATA)

    assert kwargs is None
    assert error.startswith("Invalid target")


@pytest.mark.parametrize(("marker_id", "repeat"), [("107", True), ("108", False), ("109", False), ("110", False)])
def test_only_a_plain_marker_target_is_sent_again_after_a_drop(marker_id: str, repeat: bool) -> None:
    # A plain marker takes the value as a state. A trigger marker's plan input toggles on every edge, a KNX bridge
    # marker may carry a relative DPT3 move step, and a marker of unknown kind may be either: each is sent once.
    kwargs, error = _resolve_set_value_target(f"M{marker_id}", 1.0, _DATA)

    assert (error, kwargs) == ("", {"target_type": "marker", "target_id": marker_id, "repeat": repeat})


@pytest.mark.parametrize(
    ("target", "written", "repeat"),
    [
        ("IOX2#Q3", {"target_type": "io", "target_id": "3", "ext": "IOX2", "identifier": "Q3"}, True),
        ("M107", {"target_type": "marker", "target_id": "107"}, True),
        ("M108", {"target_type": "marker", "target_id": "108"}, False),
        ("M109", {"target_type": "marker", "target_id": "109"}, False),
    ],
)
def test_the_set_value_service_repeats_a_dropped_write_unless_it_hits_a_trigger(
    monkeypatch: pytest.MonkeyPatch, target: str, written: dict[str, Any], repeat: bool
) -> None:
    api = MagicMock(set_value=AsyncMock(return_value=True))
    coordinator = MagicMock(data=_DATA)
    monkeypatch.setattr(misc, "_async_get_service_context", AsyncMock(return_value=(coordinator, api, None)))

    response = asyncio.run(misc._handle_set_value(MagicMock(), MagicMock(data={"target": target, "value": "1"})))

    assert response is not None
    assert response["success"] is True
    api.set_value.assert_awaited_once_with(**written, value=1, repeat=repeat)


def test_the_set_value_service_sends_a_target_that_does_not_say_whether_it_repeats_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = MagicMock(set_value=AsyncMock(return_value=True))
    monkeypatch.setattr(misc, "_async_get_service_context", AsyncMock(return_value=(MagicMock(), api, None)))
    monkeypatch.setattr(misc, "_resolve_set_value_target", lambda *_: ({"target_type": "marker", "target_id": "1"}, ""))

    asyncio.run(misc._handle_set_value(MagicMock(), MagicMock(data={"target": "M1", "value": "1"})))

    api.set_value.assert_awaited_once_with(target_type="marker", target_id="1", value=1, repeat=False)
