"""The connect service skips markers the ignore-list covers, ranges included however wide."""

from custom_components.comexio.const import CONF_IGNORED_MARKERS
from custom_components.comexio.services.connect import _parse_ignored_marker_ids, _resolve_requested_marker_ids


def test_a_marker_inside_a_wide_ignored_range_is_not_connected() -> None:
    ignored = _parse_ignored_marker_ids({CONF_IGNORED_MARKERS: "M3, 100-1000000000"})
    markers_by_id = {2: {}, 3: {}, 99: {}, 100: {}, 5000: {}}

    assert _resolve_requested_marker_ids("*", False, markers_by_id, ignored) == ([2, 99], [])
    assert _resolve_requested_marker_ids("2,M3,5000,99", False, markers_by_id, ignored) == ([2, 99], [])
