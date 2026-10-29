"""Sync button lookups: the KNX bridge Marker by its title, and the split of gap items into sources and IOs."""

import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.comexio.button import ComexioSyncButton, _SyncContext
from custom_components.comexio.const import MarkerKind, WebioClass


def _marker(marker_id: int, title: str | None, kind: MarkerKind = MarkerKind.KNX_BRIDGE) -> dict[str, Any]:
    return {"id": marker_id, "kind": kind, "title": title}


_CACHED_BRIDGE = _marker(9, "Cached [K1]")


def _button(markers: list[dict[str, Any]], fresh_markers: list[dict[str, Any]] | None) -> tuple:
    button = ComexioSyncButton.__new__(ComexioSyncButton)
    button.coordinator = SimpleNamespace(data={"markers": markers})
    ctx = _SyncContext(
        api=None,
        action="full_sync",
        ha_address="10.0.0.7:8123",
        webio_devices_audit={},
        start_time=datetime.datetime.now(),
        class_names={},
        update_status=lambda *args, **kwargs: None,
        fresh_markers=fresh_markers,
    )
    return button, ctx


@pytest.mark.parametrize(
    ("fresh_markers", "expected"),
    [
        (
            [
                _marker(1, "Light [K1] old"),  # suffix not at the end
                _marker(2, "Light [K1]", MarkerKind.NORMAL),  # not a bridge Marker
                _marker(3, "Light [K11]"),  # other K id
                _marker(4, "Light [K1]  "),  # trailing blanks
                _marker(5, "Dimmer [K1]"),
            ],
            4,
        ),
        ([_marker(6, None)], None),
        ([], None),
        (None, 9),  # no fresh list: the coordinator's cached one
    ],
)
def test_knx_bridge_marker_is_the_first_bridge_whose_title_ends_in_its_k_id(
    fresh_markers: list[dict[str, Any]] | None, expected: int | None
) -> None:
    button, ctx = _button([_CACHED_BRIDGE], fresh_markers)

    assert button._knx_bridge_marker_id_by_title(ctx, 1) == expected


def test_gap_items_carrying_a_source_key_fold_into_their_category_and_only_io_items_are_left() -> None:
    gap_keys: dict[str, Any] = {
        "marker_id": SimpleNamespace(key=WebioClass.MARKER),
        "knx_id": SimpleNamespace(key=WebioClass.KNX),
    }
    source_ids: dict[WebioClass, list[int]] = {WebioClass.MARKER: [1]}
    gap_items = [
        {"marker_id": 5},
        {"io": "A1"},
        {"marker_id": 6, "knx_id": 7},  # both keys: counted for each, never left over as an IO
        {"knx_id": 8},
        {"io": "B2"},
    ]

    leftover = ComexioSyncButton._merge_gap_source_ids(source_ids, gap_items, gap_keys)

    assert leftover == [{"io": "A1"}, {"io": "B2"}]
    assert source_ids == {WebioClass.MARKER: [1, 5, 6], WebioClass.KNX: [7, 8]}
