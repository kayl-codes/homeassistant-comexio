"""set_value target parsing: '<Extension>#<IO>' with spaces in the extension name (#259)."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.comexio.services.misc import _resolve_set_value_target

_DATA: dict[str, Any] = {
    "io": [
        {"id": "58", "ext_name": "IO-Extension A2", "identifier": "AO4", "is_input": False, "is_binary": False},
        {"id": "3", "ext_name": "IOX2", "identifier": "Q3", "is_input": False, "is_binary": True},
    ],
    "markers": [],
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
    assert (error, kwargs) == ("", {"target_type": "io", "target_id": io_id, "ext": ext, "identifier": identifier})


@pytest.mark.parametrize("target", ["#AO4", "   #AO4", "IOX2#", "IOX2#Q 3", "A#B#C", "IOX2Q3"])
def test_malformed_io_target_is_rejected(target: str) -> None:
    kwargs, error = _resolve_set_value_target(target, 1.0, _DATA)

    assert kwargs is None
    assert error.startswith("Invalid target")
