"""Block settings of function plan elements (Comexio's $FubBaseConfig).

Comexio keeps the settings of single blocks outside the plan, in a table of its own: one row
{Id, FubElementId, Name, Value} per element and setting. Shutter travel times, dimmer settings,
the values of hidden inputs and the extended view ("autohide") live there, so the run_fup
payload a backup stores does not carry them. A snapshot keeps them per element under
SNAPSHOT_BLOCK_SETTINGS; a restore writes them back, through the old -> new element id map when
the restore re-created the elements.
"""

from collections.abc import Mapping
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Snapshot key: {element id: {setting name: value}}. Snapshots stored before it existed lack it.
SNAPSHOT_BLOCK_SETTINGS = "block_settings"
# The extended view ("Alles anzeigen"): "1" shows the block's auto-hidden pins.
SETTING_AUTOHIDE = "autohide"
AUTOHIDE_EXPANDED = "1"

# Reference type of a logic block (the only elements with auto-hidden pins).
_BLOCK_TYPE = "5"
# Suffix of the catalog key an expanded block is rendered under (see expand_autohide_blocks).
_EXPANDED_KEY_SUFFIX = ":expanded"

BlockSettings = dict[str, dict[str, str]]


def parse_block_settings(raw: Any) -> BlockSettings | None:
    """{element id: {name: value}} from $FubBaseConfig; None when the page did not carry the table.

    PHP serializes an empty table as [] and a filled one as an object keyed by row id. Values
    are kept as the strings Comexio stores, so a restore writes back exactly what it read.
    A table with rows but not one usable row (changed row format) is None too: read as "no
    settings", it would rotate a settings-less backup in for every plan that has some.
    """
    if isinstance(raw, list):
        rows: Any = raw
    elif isinstance(raw, Mapping):
        rows = raw.values()
    else:
        return None
    settings: BlockSettings = {}
    skipped = 0
    for row in rows:
        if not isinstance(row, Mapping) or row.get("FubElementId") is None or not row.get("Name"):
            skipped += 1
            continue
        if row.get("Value") is None:
            skipped += 1
            continue
        settings.setdefault(str(row["FubElementId"]), {})[str(row["Name"])] = str(row["Value"])
    if skipped and not settings:
        _LOGGER.warning("Block settings: no usable row among the %d of $FubBaseConfig — not captured", skipped)
        return None
    if skipped:
        _LOGGER.warning("Block settings: %d row(s) of $FubBaseConfig without element, name or value ignored", skipped)
    return settings


def plan_block_settings(all_settings: Mapping[str, Mapping[str, str]], elements: Mapping[str, Any]) -> BlockSettings:
    """The settings of one plan's elements (elements without settings are left out)."""
    return {str(elem_id): dict(all_settings[str(elem_id)]) for elem_id in elements if str(elem_id) in all_settings}


def snapshot_block_settings(snapshot: Mapping[str, Any]) -> BlockSettings | None:
    """A snapshot's stored block settings; None for a snapshot stored before they were captured."""
    stored = snapshot.get(SNAPSHOT_BLOCK_SETTINGS)
    return stored if isinstance(stored, dict) else None


def block_settings_changed(previous: Mapping[str, Any], current: BlockSettings | None) -> bool:
    """Whether today's settings differ from the ones the previous snapshot stored.

    current None (not read this poll) never counts as a change. A previous snapshot without
    settings counts as one without any, so a plan whose blocks have settings gets one new
    snapshot that carries them.
    """
    if current is None:
        return False
    return (snapshot_block_settings(previous) or {}) != current


def map_restored_elements(snapshot_elements: Mapping[str, Any], live_elements: Mapping[str, Any]) -> dict[str, str]:
    """{snapshot element id: live element id} after a restore onto an existing plan.

    The restore puts every element back at its snapshot position, so reference + position
    decide first: the same id at that place, else the one live element of that reference
    there. Only an element no place match finds falls back to the same id with the same
    reference (its position may not have been restored). Without either it stays unmapped.
    An element whose place holds blocks of its type but no unique match (stacked blocks) gets
    no fallback: which of them it is stays unproven. A same id alone never decides: onto
    another plan (force_override) run_fup hands out fresh ids, which can name a different
    block of the same type.
    """
    by_place: dict[tuple[Any, ...], list[str]] = {}
    for live_id, live in live_elements.items():
        if isinstance(live, Mapping):
            by_place.setdefault(_place(live), []).append(str(live_id))
    mapping: dict[str, str] = {}
    placeless: list[str] = []
    for elem_id, elem in snapshot_elements.items():
        candidates = by_place.get(_place(elem), [])
        if str(elem_id) in candidates:
            mapping[str(elem_id)] = str(elem_id)
        elif len(candidates) == 1:
            mapping[str(elem_id)] = candidates[0]
        elif not candidates:
            placeless.append(str(elem_id))
    _drop_shared_targets(mapping)
    _map_by_same_id(snapshot_elements, live_elements, mapping, placeless)
    return mapping


def _drop_shared_targets(mapping: dict[str, str]) -> None:
    """Unmap snapshot elements stacked on one place: they cannot all own its single live element."""
    owners: dict[str, list[str]] = {}
    for elem_id, live_id in mapping.items():
        owners.setdefault(live_id, []).append(elem_id)
    for elem_ids in owners.values():
        if len(elem_ids) > 1:
            for elem_id in elem_ids:
                del mapping[elem_id]


def _map_by_same_id(
    snapshot_elements: Mapping[str, Any],
    live_elements: Mapping[str, Any],
    mapping: dict[str, str],
    placeless: list[str],
) -> None:
    """Map the placeless elements to the same id, when it names the same reference and is still free."""
    taken = set(mapping.values())
    for elem_id in placeless:
        live = live_elements.get(elem_id)
        if elem_id in taken or not isinstance(live, Mapping):
            continue
        if _reference(live) == _reference(snapshot_elements[elem_id]):
            mapping[elem_id] = elem_id
            taken.add(elem_id)


def _reference(elem: Mapping[str, Any]) -> tuple[str, str]:
    ref = elem.get("reference") or {}
    return str(ref.get("type")), str(ref.get("ref_id"))


def _place(elem: Mapping[str, Any]) -> tuple[Any, ...]:
    return (*_reference(elem), _coordinate(elem.get("position_x")), _coordinate(elem.get("position_y")))


def _coordinate(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def settings_to_write(
    wanted: Mapping[str, Mapping[str, str]], id_map: Mapping[str, Any]
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """({live element id: settings to write}, snapshot element ids whose settings have no target).

    Every stored setting is written, not only the ones that differ: the table HA holds is as
    old as the last poll, so comparing against it could skip a value changed since. Settings
    the live element has beyond the snapshot's stay — Comexio offers no way to remove one.
    """
    writes: dict[str, dict[str, str]] = {}
    unmapped: list[str] = []
    for old_id, settings in wanted.items():
        new_id = id_map.get(str(old_id))
        if new_id is None:
            unmapped.append(str(old_id))
        elif settings:
            writes[str(new_id)] = dict(settings)
    return writes, unmapped


def live_only_settings(
    wanted: Mapping[str, Mapping[str, str]],
    id_map: Mapping[str, str],
    live_settings: Mapping[str, Mapping[str, str]],
) -> dict[str, list[str]]:
    """{live element id: setting names it has that its snapshot element lacks}, for a restore in place.

    Comexio has no way to remove a setting, so these keep their live value and the restored
    plan differs from the backup there. live_settings is HA's last read of the table.
    """
    kept: dict[str, list[str]] = {}
    for old_id, new_id in id_map.items():
        if extra := sorted(set(live_settings.get(str(new_id), {})) - set(wanted.get(str(old_id), {}))):
            kept[str(new_id)] = extra
    return kept


def block_settings_form(element_id: int | str, settings: Mapping[str, str], timestamp: str) -> dict[str, str]:
    """The savefubbaseconfig form Comexio's editor sends: id, one element_<name> per setting, timestamp."""
    fields = {f"element_{name}": str(value) for name, value in settings.items()}
    return {"id": str(element_id)} | fields | {"timestamp": timestamp}


def block_settings_diff(
    newer: Mapping[str, Any], older: Mapping[str, Any]
) -> list[tuple[str, str, str | None, str | None]] | None:
    """(element id, setting, old value, new value) for every setting that changed between two snapshots.

    Only elements both snapshots contain count: an added or removed block is a change of the
    plan itself. None when either snapshot was stored without settings.
    """
    new_settings, old_settings = snapshot_block_settings(newer), snapshot_block_settings(older)
    if new_settings is None or old_settings is None:
        return None
    common = set(newer.get("elements") or {}) & set(older.get("elements") or {})
    changes: list[tuple[str, str, str | None, str | None]] = []
    for elem_id in sorted(common, key=_id_order):
        before, after = old_settings.get(elem_id, {}), new_settings.get(elem_id, {})
        changes.extend(
            (elem_id, name, before.get(name), after.get(name))
            for name in sorted(set(before) | set(after))
            if before.get(name) != after.get(name)
        )
    return changes


def _id_order(elem_id: str) -> tuple[int, str]:
    return (int(elem_id), "") if elem_id.isdigit() else (0, elem_id)


def expand_autohide_blocks(
    elements: dict[str, Any], catalog: dict[str, Any], settings: Mapping[str, Mapping[str, str]] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(elements, catalog) to render with the extended view of every block that has it switched on.

    The renderer collapses a block's auto-hidden pins by its catalog entry alone. Each expanded
    block is pointed at a copy of that entry without hidden pins, under its own catalog key;
    the inputs stay untouched.
    """
    fub_base = catalog.get("fub_base") or {}
    expanded = [
        elem_id
        for elem_id, values in (settings or {}).items()
        if values.get(SETTING_AUTOHIDE) == AUTOHIDE_EXPANDED and elem_id in elements
    ]
    if not expanded or not fub_base:
        return elements, catalog
    new_base = dict(fub_base)
    new_elements = dict(elements)
    for elem_id in expanded:
        elem = elements[elem_id]
        ref = elem.get("reference") or {}
        block = fub_base.get(str(ref.get("ref_id")))
        if str(ref.get("type")) != _BLOCK_TYPE or not isinstance(block, Mapping):
            continue
        if not (block.get("in_hide") or block.get("out_hide")):
            continue
        key = f"{ref.get('ref_id')}{_EXPANDED_KEY_SUFFIX}"
        new_base[key] = {**block, "in_hide": [], "out_hide": []}
        new_elements[elem_id] = {**elem, "reference": {**ref, "ref_id": key}}
    return new_elements, {**catalog, "fub_base": new_base}
