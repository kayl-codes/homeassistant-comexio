"""Fresh mid-sync audits: one config fetch for the KNX step, reused by the trigger step only when unchanged."""

import asyncio
import datetime
from typing import Any

from custom_components.comexio.button import ComexioSyncButton, _SyncContext
from custom_components.comexio.const import WebioClass
from custom_components.comexio.coordinator import ComexioCoordinator

_PARSED = {
    "knx": [
        {"id": 1, "name": "K1", "title": "Light", "type": "digital", "type_raw": "1.001"},
        {"id": 2, "name": "K2", "title": "Blind", "type": "digital", "type_raw": "1.008"},
    ],
    "markers": [{"id": 7, "title": "Bridge K1"}],
}


class _FakeApi:
    def __init__(self, raw_config: dict[str, Any]) -> None:
        self.raw_config = raw_config
        self.fetches = 0

    async def get_raw_config(self) -> dict[str, Any]:
        self.fetches += 1
        return self.raw_config

    def parse_config(self, raw_config: dict[str, Any]) -> dict[str, Any]:
        return _PARSED


class _Coordinator(ComexioCoordinator):
    """ComexioCoordinator without HA: only what the fresh audits touch."""

    active_webio_classes = (WebioClass.MARKER, WebioClass.KNX)


def _coordinator(*, credentials: bool = True, plans_fresh: bool = True, raw_config=None) -> _Coordinator:
    coordinator = _Coordinator.__new__(_Coordinator)
    coordinator.server_id = "test"
    coordinator.api = _FakeApi({"config": 1} if raw_config is None else raw_config)
    coordinator.plan_refetches = []

    async def _ensure(fub_ids: set[int], *, force: bool = False) -> bool:
        coordinator.plan_refetches.append(force)
        return plans_fresh

    coordinator._ensure_relevant_plans_cached = _ensure
    coordinator._function_plan_check_fub_ids = lambda: {10, 11}
    coordinator._has_active_function_plan = lambda: True
    coordinator._api_credentials_configured = lambda: credentials
    coordinator.ignored_ids_for = lambda webio_class: set()
    # K1 is bridged (marker 7) and read-path wired once -> loopback missing; K2 has no bridge.
    coordinator._knx_bridge_marker_by_k_id = lambda: {"1": "7"}
    coordinator._wired_source_webio_pairs = lambda source_type: {("1", "500")}
    coordinator._trigger_ids_by_ref = lambda parsed: {2: [7]}
    coordinator._audit_all_trigger_pairs = lambda ids_by_ref: ({2: [7]}, {})
    return coordinator


def test_knx_audits_share_one_config_fetch_and_one_forced_plan_refetch() -> None:
    coordinator = _coordinator()

    bridge, loopback, snapshot = asyncio.run(coordinator.async_fresh_knx_audits())

    assert coordinator.api.fetches == 1
    assert coordinator.plan_refetches == [True]
    assert [item["ref_id"] for item in bridge] == ["2"]
    assert [item["ref_id"] for item in loopback] == ["1"]
    assert snapshot is _PARSED


def test_knx_bridge_audit_alone_keeps_the_cheap_cache_top_up_and_offers_no_snapshot() -> None:
    coordinator = _coordinator(credentials=False)

    bridge, loopback, snapshot = asyncio.run(coordinator.async_fresh_knx_audits())

    assert coordinator.plan_refetches == [False]
    assert [item["ref_id"] for item in bridge] == ["2"]
    assert loopback == []
    assert snapshot is None


def test_failed_forced_plan_refetch_skips_loopback_and_offers_no_snapshot() -> None:
    coordinator = _coordinator(plans_fresh=False)

    bridge, loopback, snapshot = asyncio.run(coordinator.async_fresh_knx_audits())

    assert [item["ref_id"] for item in bridge] == ["2"]
    assert loopback == []
    assert snapshot is None


def test_failed_config_fetch_skips_both_knx_audits() -> None:
    coordinator = _coordinator(raw_config={})

    assert asyncio.run(coordinator.async_fresh_knx_audits()) == ([], [], None)
    assert coordinator.plan_refetches == []


def test_trigger_audit_with_snapshot_fetches_nothing() -> None:
    coordinator = _coordinator()

    missing, orphan, markers = asyncio.run(coordinator.async_fresh_trigger_audit(_PARSED))

    assert (missing, orphan, markers) == ({2: [7]}, {}, _PARSED["markers"])
    assert coordinator.api.fetches == 0
    assert coordinator.plan_refetches == []


def test_trigger_audit_without_snapshot_fetches_config_and_forces_plans() -> None:
    coordinator = _coordinator()

    asyncio.run(coordinator.async_fresh_trigger_audit())

    assert coordinator.api.fetches == 1
    assert coordinator.plan_refetches == [True]


def test_trigger_audit_skips_when_the_forced_plan_refetch_fails() -> None:
    coordinator = _coordinator(plans_fresh=False)

    assert asyncio.run(coordinator.async_fresh_trigger_audit()) == ({}, {}, None)
    assert coordinator.plan_refetches == [True]


class _FakeSyncCoordinator:
    def __init__(self, bridge: list[dict[str, Any]]) -> None:
        self.bridge = bridge
        self.trigger_snapshots: list[dict[str, Any] | None] = []

    async def async_fresh_knx_audits(self):
        return self.bridge, [], _PARSED

    async def resolve_knx_clusters(self, k_ids: list[int]):
        return {}, [], []

    async def async_fresh_trigger_audit(self, snapshot=None):
        self.trigger_snapshots.append(snapshot)
        return {}, {}, None


def _sync_run(bridge: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Full-sync order: KNX step, then the trigger step twice (the second must not reuse)."""
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = _FakeSyncCoordinator(bridge)
    button.server_id = "test"
    ctx = _SyncContext(
        api=None,
        action="full_sync",
        ha_address="10.0.0.7:8123",
        webio_devices_audit={},
        start_time=datetime.datetime.now(),
        class_names={},
        update_status=lambda *args, **kwargs: None,
    )

    async def _run() -> None:
        await button._wire_knx_full(ctx, [], [], refresh_audit=True)
        await button._wire_trigger_pairs(ctx, refresh_audit=True)
        await button._wire_trigger_pairs(ctx, refresh_audit=True)

    asyncio.run(_run())
    return button.coordinator.trigger_snapshots


def test_trigger_step_reuses_the_knx_snapshot_once_when_nothing_was_written() -> None:
    assert _sync_run(bridge=[]) == [_PARSED, None]


def test_trigger_step_fetches_itself_when_the_knx_step_had_work() -> None:
    bridge = [{"name": "K2", "ref_id": "2", "title": "Blind", "type_raw": "1.008", "webio_class": "knx"}]

    assert _sync_run(bridge) == [None, None]
