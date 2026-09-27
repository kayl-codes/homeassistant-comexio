"""Tests for the orphaned long-term statistics selection."""

from custom_components.comexio.orphaned_statistics import (
    find_orphaned_statistic_ids,
    find_unit_mismatches,
    legacy_statistic_prefixes,
    stable_statistic_id_pattern,
)

LEGACY_PREFIXES = ("sensor.comexio_iosrv1_", "sensor.comexio_server_iosrv1_")


def _find(statistic_ids, *, live=(), known=(), protected=(), pattern=None):
    return find_orphaned_statistic_ids(
        statistic_ids,
        live_entity_ids=set(live),
        known_entity_ids=set(known),
        legacy_prefixes=LEGACY_PREFIXES,
        protected_entity_ids=set(protected),
        stable_id_pattern=pattern,
    )


def test_device_name_based_entity_id_is_detected_via_registry() -> None:
    """Regression: deleted entities with device-name-based ids (no comexio_ prefix) were missed."""
    stat_id = "sensor.iosrv1_iox2_iox2_tl1_onboard_temperatur_1"
    assert _find([stat_id], known=[stat_id]) == [stat_id]


def test_legacy_prefix_detected_without_registry_entry() -> None:
    """Statistics whose deleted registry entry was already purged still match by prefix."""
    stat_ids = ["sensor.comexio_iosrv1_base_ai1", "sensor.comexio_server_iosrv1_m12"]
    assert _find(stat_ids) == stat_ids


def test_live_entity_is_never_orphaned() -> None:
    stat_id = "sensor.comexio_iosrv1_iox2_ai2_helligkeit"
    assert _find([stat_id], live=[stat_id], known=[stat_id]) == []


def test_offline_extension_statistics_are_protected() -> None:
    stat_ids = ["sensor.iosrv1_ud1_ud1_ul1_versorgungsspannung", "sensor.comexio_iosrv1_ud1_ai1"]
    assert _find(stat_ids, known=stat_ids[:1], protected=stat_ids) == []


def test_foreign_statistics_are_ignored() -> None:
    """Statistics neither known to the registry for this entry nor matching a prefix stay untouched."""
    stat_ids = ["sensor.outdoor_temperature", "sensor.comexio_iosrv2_base_ai1", "sensor.iosrv1_other_integration"]
    assert _find(stat_ids) == []


# --- stable entity_id scheme (sensor.iosrv1_m12 / _iox1_ai7) ---------------------------------

STABLE = stable_statistic_id_pattern("iosrv1", ["base", "iox1", "ud_1"])


def test_stable_scheme_detected_without_registry_entry() -> None:
    """Regression: purged stable-scheme ids were missed — the fallback only knew comexio_ prefixes."""
    stat_ids = [
        "sensor.iosrv1_m12",
        "sensor.iosrv1_m12_2",
        "sensor.iosrv1_k3_k4",
        "sensor.iosrv1_base_ai2",
        "sensor.iosrv1_iox1_tl1",
        "sensor.iosrv1_ud_1_ul1",
    ]
    assert _find(stat_ids, pattern=STABLE) == stat_ids


def test_stable_scheme_ignores_foreign_and_unknown_extensions() -> None:
    stat_ids = [
        "sensor.iosrv1_other_integration",
        "sensor.iosrv1_iox9_ai1",
        "sensor.iosrv2_base_ai1",
        "sensor.iosrv1_mode",
        "sensor.xiosrv1_m12",
    ]
    assert _find(stat_ids, pattern=STABLE) == []


def test_stable_scheme_live_and_protected_are_never_orphaned() -> None:
    stat_ids = ["sensor.iosrv1_base_ai2", "sensor.iosrv1_ud_1_ai1"]
    assert _find(stat_ids, live=stat_ids[:1], protected=stat_ids[1:], pattern=STABLE) == []


def test_legacy_statistic_prefixes() -> None:
    assert legacy_statistic_prefixes("iosrv1") == LEGACY_PREFIXES


# --- unit mismatch selection ---------------------------------------------------------------------


def _mismatches(stats, *, owned=(), units=None):
    units = units or {}
    return find_unit_mismatches(
        stats,
        owned_entity_ids=set(owned),
        legacy_prefixes=LEGACY_PREFIXES,
        current_unit=units.get,
    )


def test_unit_mismatch_found_for_registry_owned_stable_id() -> None:
    """Regression: stable ids (no comexio_ prefix) were never selected, so no unit got fixed."""
    stats = [{"statistic_id": "sensor.iosrv1_base_ai2", "statistics_unit_of_measurement": None}]
    result = _mismatches(stats, owned=["sensor.iosrv1_base_ai2"], units={"sensor.iosrv1_base_ai2": "V"})
    assert result == [("sensor.iosrv1_base_ai2", "V")]


def test_unit_mismatch_legacy_prefix_still_selected() -> None:
    stats = [{"statistic_id": "sensor.comexio_iosrv1_base_ai2", "unit_of_measurement": ""}]
    assert _mismatches(stats, units={"sensor.comexio_iosrv1_base_ai2": "°C"}) == [
        ("sensor.comexio_iosrv1_base_ai2", "°C")
    ]


def test_unit_mismatch_skips_foreign_matching_and_unavailable() -> None:
    stats = [
        {"statistic_id": "sensor.outdoor_temperature", "statistics_unit_of_measurement": ""},
        {"statistic_id": "sensor.iosrv1_base_ai1", "statistics_unit_of_measurement": "V"},
        {"statistic_id": "sensor.iosrv1_base_ai3", "statistics_unit_of_measurement": ""},
        {"statistic_id": "sensor.iosrv1_base_ai4", "statistics_unit_of_measurement": ""},
    ]
    units = {"sensor.outdoor_temperature": "°C", "sensor.iosrv1_base_ai1": "V", "sensor.iosrv1_base_ai4": ""}
    owned = ["sensor.iosrv1_base_ai1", "sensor.iosrv1_base_ai3", "sensor.iosrv1_base_ai4"]
    assert _mismatches(stats, owned=owned, units=units) == []


def test_stable_scheme_derived_sensor_with_state_is_not_orphaned() -> None:
    """A YAML sensor named after a Comexio IO has statistics but no registry entry.

    It matches the stable pattern, so only the live-state gate (coordinator passes every sensor
    with a current state as live) keeps its statistics from being offered for deletion.
    """
    stat_id = "sensor.iosrv1_base_ai2_daily"
    assert STABLE.fullmatch(stat_id) is not None
    assert _find([stat_id], live=[stat_id], pattern=STABLE) == []
