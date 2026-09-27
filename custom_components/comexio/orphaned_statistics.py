"""Pure selection logic for Comexio long-term statistics (no HA instance required)."""

from collections.abc import Callable, Iterable, Mapping
import re
from typing import Any


def legacy_statistic_prefixes(server_slug: str) -> tuple[str, str]:
    """Statistic_id prefixes of the old ``comexio_``-prefixed entity_id naming of one server.

    - ``sensor.comexio_{server}_…`` (v0.7.5 until the stable scheme)
    - ``sensor.comexio_server_{server}_…`` (pre-sub-device-grouping naming)
    """
    return (f"sensor.comexio_{server_slug}_", f"sensor.comexio_server_{server_slug}_")


def stable_statistic_id_pattern(server_slug: str, extension_slugs: Iterable[str]) -> re.Pattern[str]:
    """Pattern for sensor statistic_ids in the stable entity_id scheme of one server.

    The stable scheme (see const.stable_object_id) is the technical address without the
    ``comexio_`` prefix: ``sensor.iosrv1_m12``, ``sensor.iosrv1_k1_k2``,
    ``sensor.iosrv1_iox1_ai7``. IO ids are only matched for the given extensions, so a foreign
    ``sensor.iosrv1_…`` entity of another integration is not claimed. HA's ``_2`` collision
    suffix is accepted. Use with ``fullmatch``.
    """
    alternatives = [r"m\d+", r"k\d+(?:_k\d+)?"]
    ext = sorted({slug for slug in extension_slugs if slug}, key=len, reverse=True)
    if ext:
        alternatives.append(f"(?:{'|'.join(map(re.escape, ext))})_[a-z0-9]+(?:_[a-z0-9]+)*?")
    return re.compile(rf"sensor\.{re.escape(server_slug)}_(?:{'|'.join(alternatives)})(?:_\d+)?")


def find_orphaned_statistic_ids(
    statistic_ids: Iterable[str],
    *,
    live_entity_ids: set[str],
    known_entity_ids: set[str],
    legacy_prefixes: tuple[str, ...],
    protected_entity_ids: set[str],
    stable_id_pattern: re.Pattern[str] | None = None,
) -> list[str]:
    """Return the statistic_ids that belong to this integration but have no live entity anymore.

    Ownership is decided in three ways:

    - ``known_entity_ids``: entity_ids the entity registry still remembers as belonging to this
      config entry (deleted entries included). This is independent of how HA derived the
      entity_id — device-name-based ids like ``sensor.iosrv1_iox2_iox2_tl1_…`` carry no
      ``comexio_`` prefix at all.
    - ``legacy_prefixes``: fallback for statistics whose registry entry was already purged
      (HA drops deleted entries after a retention period), old ``comexio_`` naming.
    - ``stable_id_pattern``: the same fallback for the stable entity_id scheme
      (see stable_statistic_id_pattern).

    Statistics of live entities and of ``protected_entity_ids`` (temporarily offline extensions)
    are never returned.
    """
    return [
        sid
        for sid in statistic_ids
        if sid not in live_entity_ids
        and sid not in protected_entity_ids
        and (
            sid in known_entity_ids
            or sid.startswith(legacy_prefixes)
            or (stable_id_pattern is not None and stable_id_pattern.fullmatch(sid) is not None)
        )
    ]


def find_unit_mismatches(
    stats: Iterable[Mapping[str, Any]],
    *,
    owned_entity_ids: set[str],
    legacy_prefixes: tuple[str, ...],
    current_unit: Callable[[str], str | None],
) -> list[tuple[str, str]]:
    """Return (statistic_id, unit) for owned statistics stored without a unit the entity now has.

    Ownership comes from the entity registry (``owned_entity_ids`` = this config entry's
    entities), so it holds for every entity_id format; ``legacy_prefixes`` keeps old
    ``comexio_``-prefixed ids covered. ``current_unit`` returns the entity's current
    unit_of_measurement ("" for none) or None when its state is not available yet (skipped).
    """
    mismatches: list[tuple[str, str]] = []
    for stat in stats:
        stat_id = stat["statistic_id"]
        if stat_id not in owned_entity_ids and not stat_id.startswith(legacy_prefixes):
            continue
        unit = current_unit(stat_id)
        # HA 2024+ returns "statistics_unit_of_measurement"; older versions use "unit_of_measurement"
        stored_unit = stat.get("statistics_unit_of_measurement") or stat.get("unit_of_measurement") or ""
        if unit and not stored_unit:
            mismatches.append((stat_id, unit))
    return mismatches
