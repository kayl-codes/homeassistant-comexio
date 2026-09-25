"""Tests for the orphaned long-term statistics selection."""

from custom_components.comexio.orphaned_statistics import find_orphaned_statistic_ids

LEGACY_PREFIXES = ("sensor.comexio_iosrv1_", "sensor.comexio_server_iosrv1_")


def _find(statistic_ids, *, live=(), known=(), protected=()):
    return find_orphaned_statistic_ids(
        statistic_ids,
        live_entity_ids=set(live),
        known_entity_ids=set(known),
        legacy_prefixes=LEGACY_PREFIXES,
        protected_entity_ids=set(protected),
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
