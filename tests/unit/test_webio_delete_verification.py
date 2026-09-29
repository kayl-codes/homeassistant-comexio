"""Uninstall cleanup (coordinator._delete_webio_class_entry): a Web-IO delete only counts once a lookup confirms it."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.comexio.button import _confirm_webio_device_created, _delete_old_webio_class
from custom_components.comexio.coordinator import ComexioCoordinator, webio_still_present

NAMES = ("HomeAssistant [M]", "HomeAssistant [M]")


class _FakeApi:
    def __init__(self, *, device_error=None, base_error=None, device_left=None, base_left=None) -> None:
        self.webio_device_delete_error = AsyncMock(return_value=device_error)
        self.webio_base_delete_error = AsyncMock(return_value=base_error)
        self.get_webio_device_info = AsyncMock(side_effect=_lookup(device_left))
        self.get_webio_base_info = AsyncMock(side_effect=_lookup(base_left))


def _lookup(result: Any) -> Any:
    if isinstance(result, Exception):
        return result
    return lambda _name: result


def _run(api: _FakeApi, device_id: Any = "12", base_id: Any = "7") -> tuple[dict, dict, dict, dict]:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.api = api  # type: ignore[assignment]
    coordinator.server_id = "iosrv1"
    results: tuple[dict, dict, dict, dict] = ({}, {}, {}, {})
    asyncio.run(coordinator._delete_webio_class_entry("marker", NAMES, device_id, base_id, results))
    return results


def test_confirmed_deletes_are_counted() -> None:
    devices, classes, failed, skipped = _run(_FakeApi())
    assert (devices, classes, failed, skipped) == ({"marker": "12"}, {"marker": "7"}, {}, {})


def test_device_refusal_keeps_its_reason() -> None:
    # Regression (n): the report said only "delete_webio_device failed".
    api = _FakeApi(device_error="still used in a function plan")
    devices, classes, failed, skipped = _run(api)
    assert skipped == {"marker": "Web-IO device 12 not deleted: still used in a function plan"}
    assert not devices
    assert not classes
    api.webio_base_delete_error.assert_not_awaited()


def test_device_still_listed_is_not_counted() -> None:
    devices, _classes, _failed, skipped = _run(_FakeApi(device_left="12"))
    assert not devices
    assert skipped == {"marker": "Web-IO device 12 not deleted: still present after the delete request"}


def test_class_still_listed_is_a_failure() -> None:
    # Comexio accepts a class delete silently while a device of the class is left.
    _devices, classes, failed, _skipped = _run(_FakeApi(base_left=("7", False)))
    assert not classes
    assert failed == {"marker": "Web-IO class 7 not deleted: still present after the delete request"}


def test_unverifiable_class_delete_is_a_failure() -> None:
    _devices, classes, failed, _skipped = _run(_FakeApi(base_left=RuntimeError("HTTP 500")))
    assert not classes
    assert failed == {"marker": "Web-IO class 7 not deleted: could not be verified (HTTP 500)"}


def test_class_without_device_is_still_deleted() -> None:
    api = _FakeApi()
    devices, classes, _failed, _skipped = _run(api, device_id=None)
    assert not devices
    assert classes == {"marker": "7"}
    api.webio_device_delete_error.assert_not_awaited()


@pytest.mark.parametrize(
    ("found", "expected"),
    [(None, None), ("12", "still present after the delete request"), (RuntimeError("x"), "could not be verified (x)")],
)
def test_webio_still_present(found: Any, expected: str | None) -> None:
    lookup = AsyncMock(side_effect=_lookup(found))
    assert asyncio.run(webio_still_present(lookup, "HomeAssistant [M]")) == expected
    lookup.assert_awaited_once_with("HomeAssistant [M]")


def test_recreate_does_not_take_the_old_device_for_the_new_one() -> None:
    # Review: the lookup by name also finds the old device if Comexio refused the create.
    api = SimpleNamespace(get_webio_device_info=AsyncMock(return_value="12"))
    confirm = _confirm_webio_device_created(api, "HomeAssistant [M]", "12", "Marker")
    with pytest.raises(RuntimeError, match="not confirmed"):
        asyncio.run(confirm)
    api.get_webio_device_info = AsyncMock(return_value=None)
    confirm = _confirm_webio_device_created(api, "HomeAssistant [M]", None, "Marker")
    with pytest.raises(RuntimeError, match="not confirmed"):
        asyncio.run(confirm)
    api.get_webio_device_info = AsyncMock(return_value="13")
    asyncio.run(_confirm_webio_device_created(api, "HomeAssistant [M]", "12", "Marker"))


@pytest.mark.parametrize(("deleted", "left", "match"), [(False, None, "failed"), (True, ("7", True), "still present")])
def test_recreate_aborts_unless_the_old_class_is_gone(deleted: bool, left: Any, match: str) -> None:
    api = SimpleNamespace(
        delete_webio_base=AsyncMock(return_value=deleted), get_webio_base_info=AsyncMock(return_value=left)
    )
    delete = _delete_old_webio_class(api, "7", "HomeAssistant [M]", "Marker")
    with patch("custom_components.comexio.button.asyncio.sleep", AsyncMock()), pytest.raises(RuntimeError, match=match):
        asyncio.run(delete)
