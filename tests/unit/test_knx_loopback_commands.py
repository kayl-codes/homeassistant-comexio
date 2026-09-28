"""Per-command progress of the API-Loopback Web-IO command creation."""

import asyncio
from typing import Any

import pytest

from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import knx_loopback_command_name

DEVICE_ID = 7
BRIDGES = [(51, 350, False), (52, 351, True), (53, 352, False)]


@pytest.fixture
def saved(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every save_single_command call instead of hitting the network."""
    calls: list[Any] = []

    async def _save(_base_id: Any, _device_id: Any, command: Any) -> bool:
        calls.append(command)
        return True

    monkeypatch.setattr(comexio_api, "save_single_command", _save)
    return calls


def _run(api: ComexioAPI, existing: set[str], freshly_created: bool, events: list[tuple[int, ...]]):
    return asyncio.run(
        api._ensure_knx_loopback_commands(
            BRIDGES, DEVICE_ID, 3, existing, freshly_created, lambda *args: events.append(args)
        )
    )


def test_progress_counts_only_commands_actually_saved(comexio_api: ComexioAPI, saved: list[Any]) -> None:
    existing = {f"{DEVICE_ID}. {knx_loopback_command_name(52, 351)}"}
    events: list[tuple[int, ...]] = []

    pending, _, errors = _run(comexio_api, existing, False, events)

    assert events == [(1, 2, 51, 350), (2, 2, 53, 352)]
    assert len(saved) == 2
    assert len(pending) == 3
    assert not errors


def test_no_progress_for_bulk_embedded_commands(comexio_api: ComexioAPI, saved: list[Any]) -> None:
    events: list[tuple[int, ...]] = []

    _run(comexio_api, set(), True, events)

    assert not events
    assert not saved


def test_preembedded_commands_are_confirmed_not_saved(comexio_api: ComexioAPI, saved: list[Any]) -> None:
    preembedded = {knx_loopback_command_name(51, 350)}
    events: list[tuple[int, ...]] = []

    pending, to_confirm, errors = asyncio.run(
        comexio_api._ensure_knx_loopback_commands(
            BRIDGES, DEVICE_ID, 3, set(), False, lambda *args: events.append(args), preembedded
        )
    )

    assert len(saved) == 2
    assert events == [(1, 2, 52, 351), (2, 2, 53, 352)]
    assert f"{DEVICE_ID}. {knx_loopback_command_name(51, 350)}" in to_confirm
    assert len(pending) == 3
    assert not errors


def test_allocate_knx_bridge_markers_maps_every_created_marker(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _prepare(_titles: Any) -> tuple[dict, list[int], None]:
        return {"2": {}}, [], None

    async def _create(k_id: int, *_args: Any) -> tuple[int, str] | None:
        return None if k_id == 52 else (300 + k_id, f"K{k_id}")

    monkeypatch.setattr(comexio_api, "_prepare_knx_bridge_batch", _prepare)
    monkeypatch.setattr(comexio_api, "create_knx_bridge_marker", _create)
    monkeypatch.setattr(comexio_api, "io_types", {"1": {"binary": True}})
    items = [
        {"ref_id": "53", "title": "Licht", "type_raw": "2"},
        {"ref_id": "51", "title": "Schalter", "type_raw": "1"},
        {"ref_id": "52", "title": "Dimmer", "type_raw": "2"},
    ]

    allocated, errors = asyncio.run(comexio_api.allocate_knx_bridge_markers(items))

    assert allocated == {51: (351, True), 53: (353, False)}
    assert len(errors) == 1
    assert "K52" in errors[0]


def test_allocate_knx_bridge_markers_reports_prepare_failure(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _prepare(_titles: Any) -> tuple[None, list[int], str]:
        return None, [], "boom"

    monkeypatch.setattr(comexio_api, "_prepare_knx_bridge_batch", _prepare)

    allocated, errors = asyncio.run(
        comexio_api.allocate_knx_bridge_markers([{"ref_id": "51", "title": "x", "type_raw": "1"}])
    )

    assert not allocated
    assert errors == ["boom"]


@pytest.mark.parametrize("bootstrap", [None, ("3", False)])
def test_prestage_uploads_nothing_unless_class_is_fresh(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch, bootstrap: Any
) -> None:
    async def _ensure(*_args: Any) -> Any:
        return bootstrap

    monkeypatch.setattr(comexio_api, "ensure_knx_loopback_webio", _ensure)

    assert asyncio.run(comexio_api.prestage_knx_loopback_class("u", "p", BRIDGES)) == set()


def test_prestage_returns_every_embedded_command_name(comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    uploaded: list[Any] = []

    async def _ensure(_user: str, _pass: str, bridges: Any) -> tuple[str, bool]:
        uploaded.extend(bridges)
        return "3", True

    async def _device(_name: str) -> int:
        return DEVICE_ID

    async def _wait(*_args: Any) -> dict:
        return {}

    monkeypatch.setattr(comexio_api, "ensure_knx_loopback_webio", _ensure)
    monkeypatch.setattr(comexio_api, "get_webio_device_info", _device)
    monkeypatch.setattr(comexio_api, "_reload_config_until_commands_ready", _wait)

    names = asyncio.run(comexio_api.prestage_knx_loopback_class("u", "p", list(reversed(BRIDGES))))

    assert uploaded == BRIDGES
    assert names == {knx_loopback_command_name(k, m) for k, m, _ in BRIDGES}


def test_prestage_keeps_names_when_device_lookup_fails(
    comexio_api: ComexioAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ensure(*_args: Any) -> tuple[str, bool]:
        return "3", True

    async def _device(_name: str) -> None:
        raise TimeoutError

    monkeypatch.setattr(comexio_api, "ensure_knx_loopback_webio", _ensure)
    monkeypatch.setattr(comexio_api, "get_webio_device_info", _device)

    names = asyncio.run(comexio_api.prestage_knx_loopback_class("u", "p", BRIDGES))

    assert names == {knx_loopback_command_name(k, m) for k, m, _ in BRIDGES}
