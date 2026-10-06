"""Ignore-lists never reach KNX bridge markers, and an ignored [TRIG] source loses its trigger pair (bj)."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio.config import MarkerKind
import pytest

from custom_components.comexio import coordinator as coordinator_module
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_IGNORED_KNX,
    CONF_IGNORED_MARKERS,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    WebioClass,
    source_category,
)
from custom_components.comexio.coordinator import ComexioCoordinator

BRIDGE_MARKER_ID = 333
TRIGGER_FUB_ID = 39
IMPORT_ALL = {"import_markers": True, "import_knx": True}
STALE_ID = "comexio_stale_ignored_marker_iosrv1"
PROTECTED_ID = "comexio_protected_ignored_marker_iosrv1"


def _coordinator(options: dict[str, Any], bridge_marker_ids: set[int] | None = None) -> ComexioCoordinator:
    coordinator = ComexioCoordinator.__new__(ComexioCoordinator)
    coordinator.server_id = "iosrv1"
    coordinator.hass = MagicMock()
    coordinator.config_entry = SimpleNamespace(data=dict(IMPORT_ALL), options=options)  # type: ignore[assignment]
    coordinator._knx_bridge_marker_ids = bridge_marker_ids or set()
    coordinator._last_poll_scraped = True
    return coordinator


def _element(ref_type: int, ref_id: int) -> dict[str, Any]:
    return {"reference": {"type": ref_type, "ref_id": ref_id}}


def test_bridge_markers_are_never_ignored() -> None:
    # A typo (333 for 33) that hits a KNX bridge marker must not ignore it: the cleanup would
    # delete the element that carries the write path to the KNX object.
    coordinator = _coordinator(
        {CONF_IGNORED_MARKERS: f"33,{BRIDGE_MARKER_ID}", CONF_IGNORED_KNX: f"{BRIDGE_MARKER_ID}"},
        {BRIDGE_MARKER_ID},
    )

    assert coordinator.ignored_marker_ids == {33}
    # Marker and KNX ids share one numeric space — a KNX object with the same id stays ignorable.
    assert coordinator.ignored_knx_ids == {BRIDGE_MARKER_ID}


def test_ignored_trigger_sources_are_no_trigger_sources() -> None:
    # An ignored [TRIG] source must turn its pair in "HA - TRIGGER" into an orphan (removed by
    # the sync) instead of a source the trigger audit keeps — or re-creates after a cleanup.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "7", CONF_IGNORED_KNX: "K12"})
    data = {
        "markers": [{"id": 7, "kind": MarkerKind.TRIGGER}, {"id": 8, "kind": MarkerKind.TRIGGER}, {"id": 9}],
        "knx": [{"id": 12, "kind": MarkerKind.TRIGGER}, {"id": 13, "kind": MarkerKind.TRIGGER}],
    }

    assert coordinator._trigger_ids_by_ref(data) == {2: [8], 11: [13]}


def _trigger_audit_coordinator(
    options: dict[str, Any], plan_elements: dict[str, Any], bridge_marker_by_k_id: dict[str, str]
) -> ComexioCoordinator:
    coordinator = _coordinator(
        {**options, CONF_FUNCTION_PLAN_PLAN_MAP: {FUNCTION_PLAN_TRIGGER_PLAN_NAME: TRIGGER_FUB_ID}}
    )
    coordinator.api = SimpleNamespace(  # type: ignore[assignment]
        flanke_ref_id=lambda: 99,
        fub_data={str(TRIGGER_FUB_ID): {"Name": FUNCTION_PLAN_TRIGGER_PLAN_NAME}},
        _function_plan_existing_refs=ComexioAPI._function_plan_existing_refs,
        _function_plan_trigger_wired_source_ids=ComexioAPI._function_plan_trigger_wired_source_ids,
    )
    coordinator.function_plan_plans = {TRIGGER_FUB_ID: {"elements": plan_elements, "connections": {}}}
    coordinator._knx_bridge_marker_by_k_id = lambda: bridge_marker_by_k_id  # type: ignore[method-assign]
    return coordinator


def test_ignored_trigger_marker_is_reported_as_an_orphaned_pair() -> None:
    coordinator = _trigger_audit_coordinator({CONF_IGNORED_MARKERS: "7"}, {"1": _element(2, 7)}, {})
    data = {"markers": [{"id": 7, "kind": MarkerKind.TRIGGER}], "knx": []}

    assert coordinator._audit_all_trigger_pairs(coordinator._trigger_ids_by_ref(data)) == ({}, {2: [7]})


def test_ignored_knx_trigger_is_reported_by_its_k_id_not_its_bridge_marker() -> None:
    # The wired element of a KNX trigger is its bridge marker (50 for K12): the orphan must be
    # reported as K12 and the bridge marker must not show up a second time as a marker orphan.
    coordinator = _trigger_audit_coordinator(
        {CONF_IGNORED_KNX: "K12"}, {"1": _element(2, 50)}, {"12": "50", "13": "51"}
    )
    data = {"markers": [], "knx": [{"id": 12, "kind": MarkerKind.TRIGGER}, {"id": 13, "kind": MarkerKind.TRIGGER}]}

    assert coordinator._audit_all_trigger_pairs(coordinator._trigger_ids_by_ref(data)) == ({11: [13]}, {11: [12]})


def _check_ignored(
    coordinator: ComexioCoordinator,
    final_data: dict[str, Any],
    webio_class: WebioClass = WebioClass.MARKER,
    plans: dict[int, Any] | None = None,
) -> MagicMock:
    """Run async_check_ignored_sources with every HA side effect stubbed; returns the notification mock."""
    coordinator._cleanup_entity_ids = []
    coordinator._cleanup_function_plan_count = 0
    coordinator._load_managed_plan_check_data = AsyncMock(return_value=plans or {})  # type: ignore[method-assign]
    coordinator.marker_entities_by_id = MagicMock(return_value={})  # type: ignore[method-assign]
    coordinator.request_options_update_without_reload = MagicMock()  # type: ignore[method-assign]
    conf = {**IMPORT_ALL, **coordinator.config_entry.options}
    with (
        patch.object(coordinator_module.ir, "async_delete_issue"),
        patch.object(coordinator_module.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(coordinator.async_check_ignored_sources(conf, final_data, webio_class))
    return notify


def _notifications(notify: MagicMock) -> dict[str, str]:
    return {call.kwargs["notification_id"]: call.args[1] for call in notify.call_args_list}


MARKERS = [
    {"id": 33, "name": "Licht"},
    {"id": BRIDGE_MARKER_ID, "name": "Bridge [K5]", "kind": MarkerKind.KNX_BRIDGE},
]


def test_ignored_bridge_marker_is_removed_from_the_option_and_never_cleaned_up() -> None:
    coordinator = _coordinator({CONF_IGNORED_MARKERS: f"33,{BRIDGE_MARKER_ID},999"}, {BRIDGE_MARKER_ID})
    # The bridge marker is wired in a managed plan: inspected as an ignored source, the cleanup
    # would delete its element and break the KNX write path.
    plans = {
        36: {
            "elements": {"1": _element(2, BRIDGE_MARKER_ID), "2": _element(11, 5)},
            "connections": {"c1": {"input": {"FubElementId": 1}, "output": [{"FubElementId": 2}]}},
        }
    }

    notify = _check_ignored(coordinator, {"markers": MARKERS}, plans=plans)

    # One options update drops both the stale id (999) and the bridge marker.
    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33"})
    notifications = _notifications(notify)
    assert set(notifications) == {STALE_ID, PROTECTED_ID}
    assert f"M{BRIDGE_MARKER_ID}" in notifications[PROTECTED_ID]
    assert "KNX bridge markers" in notifications[PROTECTED_ID]
    assert not coordinator._cleanup_entity_ids
    assert coordinator._cleanup_function_plan_count == 0


def test_only_unignorable_ids_drop_the_option_and_keep_other_options() -> None:
    coordinator = _coordinator({CONF_IGNORED_MARKERS: f"{BRIDGE_MARKER_ID}", "other_opt": 1}, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_called_once_with({"other_opt": 1})
    assert set(_notifications(notify)) == {PROTECTED_ID}


def test_stale_ids_keep_their_notification() -> None:
    category = source_category(WebioClass.MARKER)
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    notify = _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33"})
    assert _notifications(notify) == {
        STALE_ID: f"{category.label} IDs **M999** were automatically removed from `{category.ignored_conf_key}` "
        "because they no longer exist in Comexio. "
        "They will be created as entities again on the next integration restart."
    }


@pytest.mark.parametrize(
    ("webio_class", "options", "final_data"),
    [
        # Nothing to remove: no options write on every poll.
        (WebioClass.MARKER, {CONF_IGNORED_MARKERS: "33"}, {"markers": MARKERS}),
        # K333 stays ignored although marker 333 is a bridge marker — separate id spaces.
        (
            WebioClass.KNX,
            {CONF_IGNORED_KNX: f"{BRIDGE_MARKER_ID}"},
            {"knx": [{"id": BRIDGE_MARKER_ID, "name": "Licht"}]},
        ),
    ],
    ids=["nothing_to_remove", "knx_id_equal_to_a_bridge_marker"],
)
def test_valid_ignore_lists_stay_untouched(
    webio_class: WebioClass, options: dict[str, Any], final_data: dict[str, Any]
) -> None:
    coordinator = _coordinator(options, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, final_data, webio_class)

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_unreadable_config_leaves_the_ignore_list_alone() -> None:
    # get_raw_config returns {} on a transient failure: every ignored source looked gone and the
    # stale sweep removed the whole ignore list, so the next sync wired them up again.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33"})
    coordinator._last_poll_scraped = False

    notify = _check_ignored(coordinator, {"markers": []})

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_trigger_pairs_count_as_blocked_for_ignored_sources() -> None:
    # Without the Flanke the pair of an ignored [TRIG] source cannot be removed — sync must say so.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "7"})
    coordinator.api = SimpleNamespace(flanke_ref_id=lambda: None)  # type: ignore[assignment]
    coordinator.data = {"markers": [{"id": 7, "kind": MarkerKind.TRIGGER}], "knx": []}

    assert coordinator.trigger_pairs_blocked()
