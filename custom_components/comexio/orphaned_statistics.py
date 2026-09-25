"""Pure selection logic for orphaned long-term statistics (no HA instance required)."""

from collections.abc import Iterable


def find_orphaned_statistic_ids(
    statistic_ids: Iterable[str],
    *,
    live_entity_ids: set[str],
    known_entity_ids: set[str],
    legacy_prefixes: tuple[str, ...],
    protected_entity_ids: set[str],
) -> list[str]:
    """Return the statistic_ids that belong to this integration but have no live entity anymore.

    Ownership is decided in two ways:

    - ``known_entity_ids``: entity_ids the entity registry still remembers as belonging to this
      config entry (deleted entries included). This is independent of how HA derived the
      entity_id — device-name-based ids like ``sensor.iosrv1_iox2_iox2_tl1_…`` carry no
      ``comexio_`` prefix at all.
    - ``legacy_prefixes``: fallback for statistics whose registry entry was already purged
      (HA drops deleted entries after a retention period).

    Statistics of live entities and of ``protected_entity_ids`` (temporarily offline extensions)
    are never returned.
    """
    return [
        sid
        for sid in statistic_ids
        if sid not in live_entity_ids
        and sid not in protected_entity_ids
        and (sid in known_entity_ids or sid.startswith(legacy_prefixes))
    ]
