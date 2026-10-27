"""Ignore-lists never reach KNX bridge markers, and an ignored [TRIG] source loses its trigger pair (bj)."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiocomexio.config import MarkerKind
import pytest

from custom_components.comexio import coordinator as coordinator_module, switch
from custom_components.comexio.api import ComexioAPI
from custom_components.comexio.const import (
    CONF_FUNCTION_PLAN_PLAN_MAP,
    CONF_IGNORED_KNX,
    CONF_IGNORED_MARKERS,
    DOMAIN,
    FUNCTION_PLAN_TRIGGER_PLAN_NAME,
    IgnoredIds,
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
    import_flags: dict[str, bool] | None = None,
    load_plans: AsyncMock | None = None,
    entities_by_id: dict[int, list[str]] | None = None,
    after_poll_read: Callable[[], None] | None = None,
) -> MagicMock:
    """Run async_check_ignored_sources with every HA side effect stubbed; returns the notification mock.

    after_poll_read runs between the poll reading the options and the audit starting.
    """
    coordinator._cleanup_entity_ids = []
    coordinator._cleanup_function_plan_count = 0
    coordinator._load_managed_plan_check_data = load_plans or AsyncMock(return_value=plans or {})  # type: ignore[method-assign]
    coordinator.marker_entities_by_id = MagicMock(return_value=entities_by_id or {})  # type: ignore[method-assign]
    coordinator.request_options_update_without_reload = MagicMock()  # type: ignore[method-assign]
    options_at_poll = coordinator.config_entry.options
    conf = {**(import_flags or IMPORT_ALL), **options_at_poll}
    if after_poll_read:
        after_poll_read()
    with (
        patch.object(coordinator_module.ir, "async_delete_issue"),
        patch.object(coordinator_module.persistent_notification, "async_create") as notify,
    ):
        asyncio.run(coordinator.async_check_ignored_sources(conf, final_data, webio_class, options_at_poll))
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
    ("webio_class", "options", "final_data", "import_flags"),
    [
        # Nothing to remove: no options write on every poll.
        (WebioClass.MARKER, {CONF_IGNORED_MARKERS: "33"}, {"markers": MARKERS}, IMPORT_ALL),
        # K333 stays ignored although marker 333 is a bridge marker — separate id spaces.
        (
            WebioClass.KNX,
            {CONF_IGNORED_KNX: f"{BRIDGE_MARKER_ID}"},
            {"knx": [{"id": BRIDGE_MARKER_ID, "name": "Licht"}]},
            IMPORT_ALL,
        ),
        # Opting a category out leaves its dormant list alone — 999 is not swept as stale.
        (
            WebioClass.MARKER,
            {CONF_IGNORED_MARKERS: "33,999"},
            {"markers": []},
            {"import_markers": False, "import_knx": True},
        ),
    ],
    ids=["nothing_to_remove", "knx_id_equal_to_a_bridge_marker", "category_opted_out"],
)
def test_valid_ignore_lists_stay_untouched(
    webio_class: WebioClass, options: dict[str, Any], final_data: dict[str, Any], import_flags: dict[str, bool]
) -> None:
    coordinator = _coordinator(options, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, final_data, webio_class, import_flags=import_flags)

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


def test_ignore_entries_saved_during_the_audit_survive_the_pruning() -> None:
    # An options-flow save while the audit awaits the plan data must not be overwritten with
    # the poll's conf snapshot: the newly ignored 44 would be dropped and its source reactivated.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    async def _options_saved_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33,44,999"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(
        coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_options_saved_meanwhile)
    )

    # The save is not overwritten; 999 was judged against the old list, so its pruning waits.
    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()

    notify = _check_ignored(coordinator, {"markers": [*MARKERS, {"id": 44, "name": "Rollo"}]})

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33,44"})
    assert set(_notifications(notify)) == {STALE_ID}


def test_bridge_markers_are_pruned_while_the_category_is_opted_out() -> None:
    # Bridge markers come from the unfiltered scrape: the protected id is removed even with marker
    # import off, while the dormant list gets no stale sweep (999 stays, the audit never runs).
    coordinator = _coordinator({CONF_IGNORED_MARKERS: f"33,{BRIDGE_MARKER_ID},999"}, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, {"markers": []}, import_flags={"import_markers": False, "import_knx": True})

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33,999"})
    assert set(_notifications(notify)) == {PROTECTED_ID}
    coordinator._load_managed_plan_check_data.assert_not_awaited()


def test_stale_id_removed_by_the_user_during_the_audit_is_not_reported() -> None:
    # 999 was stale in the poll's snapshot, but the user dropped it meanwhile: no write and no
    # "automatically removed" notification for an id that is no longer in the list.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    async def _options_saved_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(
        coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_options_saved_meanwhile)
    )

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_ranges_survive_the_pruning_with_gaps_and_bridge_markers_in_them() -> None:
    # 330-340 covers a bridge marker and IDs no source has (yet): the pruning flattened such a
    # range into single IDs on every poll and dropped the gaps, so a marker created there later
    # was no longer ignored.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,330-340"}, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()
    # The bridge marker in the range stays unignored all the same.
    assert BRIDGE_MARKER_ID not in coordinator.ignored_marker_ids
    assert {33, 330, 340} <= coordinator.ignored_marker_ids


def test_only_single_ids_are_pruned_and_ranges_are_written_back_as_ranges() -> None:
    coordinator = _coordinator({CONF_IGNORED_MARKERS: f"33,400-410,999,{BRIDGE_MARKER_ID}"}, {BRIDGE_MARKER_ID})

    notify = _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33,400-410"})
    notifications = _notifications(notify)
    assert "**M999**" in notifications[STALE_ID]
    assert f"**M{BRIDGE_MARKER_ID}**" in notifications[PROTECTED_ID]


def test_a_stale_single_inside_a_range_is_not_reported_as_removed() -> None:
    # Hand-edited "330-340,335": the range keeps ignoring 335, so no "removed" notification.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "330-340,335"})

    notify = _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_an_existing_source_in_a_range_is_still_cleaned_up() -> None:
    # Gaps in a range are skipped, its existing sources are not: leftover entities get cleaned up.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "330-340"})
    markers = [*MARKERS, {"id": 335, "name": "Rollo"}]
    _check_ignored(coordinator, {"markers": markers}, entities_by_id={335: ["switch.iosrv1_m335"]})

    assert coordinator._cleanup_entity_ids == [(WebioClass.MARKER.value, 335)]
    coordinator.request_options_update_without_reload.assert_not_called()


def test_a_source_created_later_in_a_range_gets_no_entity() -> None:
    # The range was saved before marker 337 existed: the platform setup must still skip it.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "330-340"}, {BRIDGE_MARKER_ID})
    coordinator.data = {
        "markers": [{"id": str(marker_id), "type": "digital", "kind": MarkerKind.NORMAL} for marker_id in (337, 341)]
    }
    entry = SimpleNamespace(entry_id="e1", data={"import_markers": True}, options=coordinator.config_entry.options)
    hass = SimpleNamespace(data={DOMAIN: {"e1": coordinator}})
    added: list[Any] = []

    with (
        patch.object(switch, "ComexioMarkerSwitch", lambda _coordinator, _server_id, marker: marker["id"]),
        patch.object(switch, "ComexioPlanAutoStartSwitch"),
        patch.object(switch, "ComexioUserPlanAutoStartSwitch"),
    ):
        asyncio.run(switch.async_setup_entry(hass, entry, added.extend))  # type: ignore[arg-type]

    assert [entity for entity in added if isinstance(entity, str)] == ["341"]


def test_knx_ranges_survive_and_only_stale_k_singles_are_pruned() -> None:
    coordinator = _coordinator({CONF_IGNORED_KNX: "K5,K10-12,K999"})

    notify = _check_ignored(coordinator, {"knx": [{"id": 5, "name": "Licht"}]}, WebioClass.KNX)

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_KNX: "5,10-12"})
    assert "**K999**" in next(iter(_notifications(notify).values()))


def test_a_stale_single_turned_into_a_range_during_the_audit_is_kept() -> None:
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    async def _options_saved_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33,990-1000"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(
        coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_options_saved_meanwhile)
    )

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_a_save_during_the_audit_defers_the_stale_pruning_to_the_next_poll() -> None:
    # 999 was stale in the poll's snapshot, but the user saved the list meanwhile (e.g. after
    # creating marker 999 in Comexio): the audit judged the old list, so it must not prune the new.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    async def _options_saved_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33,400-410,999"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(
        coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_options_saved_meanwhile)
    )

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_a_save_that_restores_the_audited_list_still_defers_the_stale_pruning() -> None:
    # "33,999" -> "33" -> "33,999" while the audit awaited: the user re-added 999 (e.g. after
    # creating it in Comexio), so the old verdict must not remove it although the string matches.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    async def _saved_twice_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33"}  # type: ignore[misc]
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33,999"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_saved_twice_meanwhile))

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_a_save_between_the_poll_read_and_the_audit_still_defers_the_stale_pruning() -> None:
    # Same restore ("33,999" -> "33" -> "33,999"), but before the audit started: the baseline is
    # the options the poll built conf from, not the ones present when the audit begins.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"})

    def _saved_twice_meanwhile() -> None:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33"}  # type: ignore[misc]
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33,999"}  # type: ignore[misc]

    notify = _check_ignored(coordinator, {"markers": MARKERS}, after_poll_read=_saved_twice_meanwhile)

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_a_save_during_the_audit_still_drops_a_bridge_marker() -> None:
    # Bridge markers are known from the scrape, not from the audited list: they go regardless.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,999"}, {BRIDGE_MARKER_ID})

    async def _options_saved_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: f"33,400-410,999,{BRIDGE_MARKER_ID}"}  # type: ignore[misc]
        return {}

    notify = _check_ignored(
        coordinator, {"markers": MARKERS}, load_plans=AsyncMock(side_effect=_options_saved_meanwhile)
    )

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33,400-410,999"})
    assert set(_notifications(notify)) == {PROTECTED_ID}


def test_the_audit_walks_only_the_sources_a_range_covers_not_every_id_in_it() -> None:
    # A wide range must not be expanded per poll: only sources Comexio has in it are inspected.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "330-340"}, {BRIDGE_MARKER_ID})

    _check_ignored(coordinator, {"markers": [*MARKERS, {"id": 335, "name": "Rollo"}]})

    (ids, _infix), _kwargs = coordinator.marker_entities_by_id.call_args
    assert sorted(ids) == [BRIDGE_MARKER_ID, 335]


def _no_expansion(*_args: object) -> None:
    raise AssertionError("ignore range expanded")


def test_a_user_typed_wide_range_passes_the_whole_audit_without_being_expanded() -> None:
    # Every coordinator reader of the ignore-list (filter, audit, pruning) must stay on membership:
    # expanding "100-1000000000" would hang or exhaust HA on every poll.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,100-1000000000"}, {BRIDGE_MARKER_ID})

    with (
        patch.object(IgnoredIds, "__iter__", _no_expansion),
        patch.object(coordinator_module, "range", _no_expansion, create=True),
    ):
        notify = _check_ignored(coordinator, {"markers": [*MARKERS, {"id": 5000, "name": "Rollo"}]})
        ignored = coordinator.ignored_marker_ids

        assert {33, 5000, 1000000000} <= ignored
        assert BRIDGE_MARKER_ID not in ignored

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_an_unnamed_source_in_a_range_still_gets_its_leftovers_cleaned_up() -> None:
    # The range keeps ignoring the unnamed marker, so its old entity must be cleaned up — as a
    # single it would be pruned as stale instead, and the range is never written back.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "330-340,336"})
    markers = [*MARKERS, {"id": 335, "name": " "}, {"id": 336, "name": ""}]

    notify = _check_ignored(
        coordinator, {"markers": markers}, entities_by_id={335: ["switch.iosrv1_m335"], 336: ["switch.iosrv1_m336"]}
    )

    assert coordinator._cleanup_entity_ids == [(WebioClass.MARKER.value, 335), (WebioClass.MARKER.value, 336)]
    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()


def test_a_rewrite_that_drops_an_invalid_hand_edited_entry_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,abc,999"})

    _check_ignored(coordinator, {"markers": MARKERS})

    coordinator.request_options_update_without_reload.assert_called_once_with({CONF_IGNORED_MARKERS: "33"})
    assert "'abc'" in caplog.text


def test_an_unnamed_single_source_stays_ignored_and_is_not_pruned() -> None:
    # Only a missing source makes a single ID stale: an existing unnamed one ("#nn") must stay
    # ignored, or its entity would come back after the prune.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,336"})
    markers = [*MARKERS, {"id": 336, "name": " "}]

    notify = _check_ignored(coordinator, {"markers": markers}, entities_by_id={336: ["switch.iosrv1_m336"]})

    coordinator.request_options_update_without_reload.assert_not_called()
    notify.assert_not_called()
    assert coordinator._cleanup_entity_ids == [(WebioClass.MARKER.value, 336)]


def test_a_source_unignored_during_the_audit_is_not_flagged_for_cleanup() -> None:
    # The audit read "33,335"; the user removed 335 meanwhile: its leftover entity is wanted now,
    # so the cleanup must not offer to delete it — the next poll re-judges the saved list.
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "33,335"})

    async def _unignored_meanwhile(**_kwargs: Any) -> dict[int, Any]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33"}  # type: ignore[misc]
        return {}

    _check_ignored(
        coordinator,
        {"markers": [*MARKERS, {"id": 335, "name": "Rollo"}]},
        load_plans=AsyncMock(side_effect=_unignored_meanwhile),
        entities_by_id={335: ["switch.iosrv1_m335"]},
    )

    assert coordinator._cleanup_entity_ids == []
    assert coordinator._cleanup_function_plan_count == 0


class _AuditReached(Exception):
    """Stops the poll once both ignore audits have received their arguments."""


def test_the_poll_hands_both_audits_the_options_it_built_conf_from() -> None:
    # A save during the config fetch replaces config_entry.options; the audits must still get the
    # mapping conf came from, or _ignore_list_saved_since would compare against the post-save one.
    polled = {CONF_IGNORED_MARKERS: "33,999"}
    coordinator = _coordinator(polled)
    coordinator.function_plan_catalog = MagicMock(async_update_from_raw_config=AsyncMock())
    coordinator.reference_monitor = MagicMock(async_check=AsyncMock())
    coordinator.api = MagicMock(comexio_version="2.0")
    coordinator.async_check_orphaned_statistics = AsyncMock()
    for name in (
        "_prune_deleted_plans_from_snapshot",
        "_merge_polled_states",
        "_merge_polled_knx_states",
        "_track_offline_extensions",
        "_update_entity_id_mismatch_issue",
    ):
        setattr(coordinator, name, MagicMock())

    async def _fetch_while_the_user_saves(_conf: dict[str, Any]) -> tuple[dict, dict, dict]:
        coordinator.config_entry.options = {CONF_IGNORED_MARKERS: "33"}  # type: ignore[misc]
        return {}, {"markers": [], "io": [], "knx": []}, {}

    coordinator._async_fetch_parsed_config = _fetch_while_the_user_saves  # type: ignore[method-assign]
    coordinator.async_check_ignored_markers = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_check_ignored_knx = AsyncMock(side_effect=_AuditReached)  # type: ignore[method-assign]

    poll = coordinator._async_poll_and_audit()
    with pytest.raises(_AuditReached):
        asyncio.run(poll)

    for audit in (coordinator.async_check_ignored_markers, coordinator.async_check_ignored_knx):
        conf, _final_data, options_at_poll = audit.await_args.args
        assert options_at_poll is polled
        assert conf[CONF_IGNORED_MARKERS] == "33,999"


def test_the_audit_loads_the_plans_whose_list_entry_is_unreadable_too() -> None:
    # Managed or not is unknown for them: a leftover wired there stays a finding (the cleanup reports them skipped).
    coordinator = _coordinator({CONF_IGNORED_MARKERS: "335"})
    _check_ignored(coordinator, {"markers": [*MARKERS, {"id": 335, "name": "Rollo"}]})

    coordinator._load_managed_plan_check_data.assert_awaited_once_with(include_unknown=True)
