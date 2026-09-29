"""Service instance resolution (services/_context.py): no loaded instance is not "multiple instances"."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.comexio.const import DOMAIN
from custom_components.comexio.coordinator import ComexioCoordinator
from custom_components.comexio.services import _context


def _coordinator() -> ComexioCoordinator:
    return ComexioCoordinator.__new__(ComexioCoordinator)


def _resolve(domain_data: dict, call_data: dict | None = None) -> tuple[object, MagicMock]:
    hass = SimpleNamespace(data={DOMAIN: domain_data})
    call = SimpleNamespace(data=call_data or {})
    with patch.object(_context.persistent_notification, "async_create") as notify:
        result = _context._resolve_coordinator(hass, call, "Title")  # type: ignore[arg-type]
    return result, notify


def test_the_only_instance_is_used() -> None:
    coordinator = _coordinator()
    result, notify = _resolve({"entry1": coordinator, "other": object()})
    assert result is coordinator
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("domain_data", "message"),
    [
        # Regression (l): a call during the reload after a sync read "multiple instances".
        ({}, _context._NO_INSTANCE_MSG),
        ({"a": _coordinator(), "b": _coordinator()}, _context._MULTI_INSTANCE_MSG),
    ],
    ids=["none", "several"],
)
def test_no_unique_instance_is_reported(domain_data: dict, message: str) -> None:
    result, notify = _resolve(domain_data)
    assert result is None
    assert notify.call_args.args[1] == message


def test_unknown_config_entry_is_reported() -> None:
    result, notify = _resolve({"a": _coordinator()}, {"config_entry": "gone"})
    assert result is None
    assert "`gone` not found" in notify.call_args.args[1]
