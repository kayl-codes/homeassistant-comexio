"""Write the scene block / Quantum module configs a backup stores back onto a restored plan.

Comexio keeps these configs outside the plan (function_plan_module_configs), so neither run_fup
nor a plain add_element restores them:

- a rebuilt plan (restore as new plan / as copy) gets every scene block and Quantum module placed
  by place_module_block: a new scene or module, then its stored config, before any connection is
  drawn (a scene block has its pins from its config). A block whose config save fails is removed
  again rather than left with Comexio's defaults; a Quantum module the backup stores no config for
  is not placed at all (its module type is unknown, and add_element would read the old module id
  as a type).
- an in-place restore writes the configs onto the live plan's blocks (restore_in_place), matched
  to the snapshot by map_restored_elements; a Quantum module of another type cannot be changed.

Comexio has no request for a Quantum module's rules: check_rules reloads the config page and names
the rules that differ from the backup. The page reload also refreshes ComexioAPI.module_configs,
so the next backup cycle compares against what the restore wrote, not the page from before it.

Problems name a block a restore placed or wrote by its plan element id ("element #N"), one it
could not place by the backup's ("backup element #N").
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from aiocomexio import ComexioConnectionError, PlacedModuleBlock
import aiohttp

from .const import ICON_ERROR, ICON_SUCCESS, ICON_WARNING, one_line
from .function_plan_module_configs import (
    COMBINED_CENTRAL_TYPE,
    SCENE_TYPE,
    ModuleConfigs,
    quantum_settings_to_save,
    rules_not_matching,
    scene_block_ids,
    scene_config_to_save,
    snapshot_module_configs,
    uses_module_configs,
)

_LOGGER = logging.getLogger(__name__)

# What a restore aborts on (ComexioAPI's write adapters raise a transport failure as one of these).
TRANSPORT_ERRORS = (aiohttp.ClientError, TimeoutError, ComexioConnectionError)

MODULE_CONFIGS_NOT_IN_BACKUP = (
    f"Scene/Quantum configs: {ICON_WARNING} not in this backup — check scene blocks/Quantum modules in Comexio\n"
)

_QUANTUM_TYPES = ("20", COMBINED_CENTRAL_TYPE)
_CONFIG = "config"
_TYPE_ID = "type_id"
_NO_OBJECT = "Comexio named no scene/module for it"


@dataclass
class ModuleConfigRestore:
    """What a restore did with a snapshot's scene/Quantum configs (see module_configs_line).

    configs: the snapshot's, None for one stored before they were captured; configs_missing when
    such a snapshot's plan has scene blocks / Quantum modules (they keep Comexio's defaults).
    restored counts the blocks that now carry their stored config. quantum: {backup element id:
    (restored element id, element type, module id)} of the Quantum modules whose rules check_rules
    compares. rules: {restored element id: events of the rules that differ}; rules_unchecked when
    the config page could not be reloaded for that.
    """

    configs: ModuleConfigs | None
    restored: int = 0
    problems: list[str] = field(default_factory=list)
    quantum: dict[str, tuple[int, str, int]] = field(default_factory=dict)
    rules: dict[str, list[str]] = field(default_factory=dict)
    rules_unchecked: bool = False
    configs_missing: bool = False

    @classmethod
    def for_snapshot(cls, snapshot: Mapping[str, Any]) -> "ModuleConfigRestore":
        configs = snapshot_module_configs(snapshot)
        return cls(configs, configs_missing=configs is None and uses_module_configs(snapshot))

    def ok(self) -> bool:
        """False when a block does not carry its stored config (or that could not be checked)."""
        return not (
            self.problems
            or self.rules
            or self.rules_unchecked
            or self.configs_missing
            or self.restored < len(self.configs or {})
        )

    def summary(self) -> str:
        """One log line: counts, not the configs."""
        return (
            f"{self.restored}/{len(self.configs or {})} restored, {len(self.problems)} problem(s), "
            f"{len(self.rules)} module(s) with differing rules, rules_unchecked={self.rules_unchecked}, "
            f"configs_missing={self.configs_missing}"
        )


def is_module_block(elem_type: Any) -> bool:
    """Whether an element type is a scene block or Quantum module (place_module_block places those)."""
    return str(elem_type) in (SCENE_TYPE, *_QUANTUM_TYPES)


async def place_module_block(
    api, fub_id: int, old_id: str, elem: Mapping[str, Any], restore: ModuleConfigRestore
) -> int | None:
    """Place one snapshot scene block / Quantum module on a rebuilt plan with its stored config.

    Returns the new element id, or None when the block was not placed or was removed again
    (restore.problems says which and why). A failure while saving its config raises (the rebuild
    aborts) after the placed block was removed again; a refused save returns None.
    """
    elem_type = str((elem.get("reference") or {}).get("type"))
    entry = (restore.configs or {}).get(old_id)
    config = entry.get(_CONFIG) if isinstance(entry, Mapping) else None
    if elem_type != SCENE_TYPE:
        return await _place_quantum(api, fub_id, old_id, elem, elem_type, config, restore)
    placed = await api.function_plan_add_scene_block(
        fub_id, x=elem.get("position_x", 0.0), y=elem.get("position_y", 0.0)
    )
    if placed is None:
        return None
    if not isinstance(config, Mapping):
        return await _keep_default_scene(api, old_id, placed, entry, restore)
    return await _configure(
        api, old_id, placed, restore, lambda: api.function_plan_save_scene(placed.ref_id, scene_config_to_save(config))
    )


async def _keep_default_scene(
    api, old_id: str, placed: PlacedModuleBlock, entry: Any, restore: ModuleConfigRestore
) -> int | None:
    """A scene block placed without a stored config to save: its element id, or None after removing it.

    No stored config is a scene never saved in Comexio (or a backup without configs, which
    module_configs_line names): Comexio's default scene is what it was. A stored entry without a
    readable config, or a block Comexio named no scene for, would act differently, so it is removed.
    """
    if placed.ref_id <= 0:
        reason = _NO_OBJECT
    elif entry is not None:
        reason = "its stored config is unreadable"
    else:
        return placed.element_id
    await _remove_placed(api, old_id, placed.element_id, restore, reason)
    return None


async def _place_quantum(
    api,
    fub_id: int,
    old_id: str,
    elem: Mapping[str, Any],
    elem_type: str,
    config: Any,
    restore: ModuleConfigRestore,
) -> int | None:
    """place_module_block for a Quantum module: placed with its stored type, then its settings and name."""
    if not isinstance(config, Mapping) or (type_id := _positive(config.get(_TYPE_ID))) is None:
        restore.problems.append(
            f"backup element #{old_id}: Quantum module not recreated — the backup does not name its module type"
        )
        return None
    central = elem_type == COMBINED_CENTRAL_TYPE
    placed = await api.function_plan_add_quantum_module(
        fub_id, type_id, central=central, x=elem.get("position_x", 0.0), y=elem.get("position_y", 0.0)
    )
    if placed is None:
        return None
    new_id = await _configure(
        api, old_id, placed, restore, lambda: _save_quantum(api, placed, config, elem, central, restore)
    )
    if new_id is not None:
        restore.quantum[old_id] = (placed.element_id, elem_type, placed.ref_id)
    return new_id


async def _configure(
    api,
    old_id: str,
    placed: PlacedModuleBlock,
    restore: ModuleConfigRestore,
    save: Callable[[], Awaitable[bool]],
) -> int | None:
    """Save a placed block's stored config: its element id, or None after removing it again.

    A save that raises instead of refusing removes the block too, then re-raises.
    """
    if placed.ref_id <= 0:
        await _remove_placed(api, old_id, placed.element_id, restore, _NO_OBJECT)
        return None
    try:
        saved = await save()
    except TRANSPORT_ERRORS:
        await _remove_placed(api, old_id, placed.element_id, restore, "saving its config failed (connection)")
        raise
    except Exception:
        # Not a refusal the adapters turn into False: still no block with Comexio's defaults.
        await _remove_placed(api, old_id, placed.element_id, restore, "saving its config failed")
        raise
    if saved:
        restore.restored += 1
        return placed.element_id
    await _remove_placed(api, old_id, placed.element_id, restore, "its config could not be saved")
    return None


async def _save_quantum(
    api,
    placed: PlacedModuleBlock,
    config: Mapping[str, Any],
    elem: Mapping[str, Any],
    central: bool,
    restore: ModuleConfigRestore,
) -> bool:
    """Save a Quantum module's stored settings and name; True when it now carries them.

    A stored config without settings is the module type's defaults, which the module already has.
    Comexio saves the name only together with settings, so a named module with defaults keeps the
    name Comexio gave it (a problem, not a failed save).
    """
    settings = quantum_settings_to_save(config)
    name = None if central else (str(elem.get("name") or "").strip() or None)
    if not settings:
        if name is not None:
            restore.problems.append(
                f"element #{placed.element_id}: name '{one_line(name)}' not restored — Comexio saves a Quantum "
                "module's name only together with changed settings, rename it in Comexio Studio"
            )
        return True
    return await api.function_plan_save_quantum_settings(
        placed.element_id, placed.ref_id, settings, central=central, name=name
    )


async def _remove_placed(api, old_id: str, element_id: int, restore: ModuleConfigRestore, reason: str) -> None:
    """Remove a block whose stored config is not on it: with Comexio's defaults it would act differently.

    The delete adapter does not raise, so this also runs while a transport failure propagates.
    """
    removed = await api.function_plan_delete_elements([element_id])
    # Logged too: a propagating failure drops restore with its problems.
    _LOGGER.warning(
        "Function Plan Restore: backup element #%s: %s — removing the placed block #%s %s",
        old_id,
        reason,
        element_id,
        "succeeded" if removed else "failed, delete it in Comexio Studio",
    )
    if removed:
        restore.problems.append(
            f"backup element #{old_id}: {reason} — the placed block #{element_id} was removed again (see the log)"
        )
        return
    restore.problems.append(
        f"backup element #{old_id}: {reason}, and removing the placed block #{element_id} failed too — it has "
        "Comexio's defaults, delete it in Comexio Studio"
    )


async def restore_in_place(
    api, snapshot: Mapping[str, Any], element_map: Mapping[str, Any] | None, live_plan: Mapping[str, Any] | None
) -> ModuleConfigRestore:
    """Write a snapshot's scene/Quantum configs onto the blocks of the plan an in-place restore just applied.

    element_map / live_plan: the snapshot's elements matched to the reloaded plan's, and that plan;
    None when it could not be reloaded. The config page is reloaded first: a Quantum module's
    type can only be checked against it; when that reload fails, the scene configs (which need no
    table) are still written. A transport failure while writing stops the writes and is reported,
    so the plan the restore already applied stays reported as well.
    """
    restore = ModuleConfigRestore.for_snapshot(snapshot)
    configs = restore.configs
    unsaved_scenes = [] if configs is None else [s for s in scene_block_ids(snapshot) if s not in configs]
    if not configs and not unsaved_scenes:
        return restore
    if element_map is None or not live_plan:
        restore.problems.append("not restored — the plan could not be reloaded to match its blocks")
        return restore
    tables = await _reload_tables(api)
    await _write_all_in_place(api, configs or {}, element_map, live_plan.get("elements") or {}, tables, restore)
    _note_unsaved_scenes(restore, unsaved_scenes, element_map, tables)
    # Rules are compared on the page read before the writes: saving settings changes no rule
    # field the comparison looks at. A reload after writing refreshes the cached configs.
    _compare_rules(restore, tables)
    if restore.restored:
        await _reload_tables(api)
    return restore


async def _write_all_in_place(
    api,
    configs: ModuleConfigs,
    element_map: Mapping[str, Any],
    live_elements: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]] | None,
    restore: ModuleConfigRestore,
) -> None:
    """Write each stored config onto the live block it was matched to; a transport failure stops the writes."""
    for old_id, entry in configs.items():
        if (live_id := _positive(element_map.get(old_id))) is None:
            restore.problems.append(f"no restored element for backup element #{old_id}")
            continue
        ref = (live_elements.get(str(live_id)) or {}).get("reference") or {}
        try:
            await _write_in_place(
                api, old_id, live_id, ref, entry if isinstance(entry, Mapping) else {}, tables, restore
            )
        except TRANSPORT_ERRORS as err:
            _LOGGER.warning("Function Plan Restore: writing the config of element #%s failed: %r", live_id, err)
            restore.problems.append(
                f"element #{live_id}: writing its config failed (connection) — the remaining configs were not written"
            )
            break


def _note_unsaved_scenes(
    restore: ModuleConfigRestore,
    unsaved_scenes: list[str],
    element_map: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    """Name the scene blocks saved in Comexio since a backup that stores them unsaved (Comexio's default scene).

    Comexio cannot reset a scene to its default, and save_scene refuses an empty config. Without a
    readable scene table each of them is named unchecked.
    """
    scenes = None if tables is None else tables.get(SCENE_TYPE)
    for old_id in unsaved_scenes:
        if (live_id := _positive(element_map.get(old_id))) is None:
            continue
        if scenes is None:
            restore.problems.append(
                f"element #{live_id}: scene not checked for Comexio's default (the backup's, never saved) — "
                "Comexio's scene table could not be read"
            )
        elif str(live_id) in scenes:
            restore.problems.append(
                f"element #{live_id}: scene not reset to Comexio's default (the backup's, never saved) — "
                "reset it in Comexio Studio"
            )


async def _write_in_place(
    api,
    old_id: str,
    live_id: int,
    ref: Mapping[str, Any],
    entry: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]] | None,
    restore: ModuleConfigRestore,
) -> None:
    """Write one stored config onto the live block it was matched to (see restore_in_place)."""
    elem_type, config = str(entry.get("type")), entry.get(_CONFIG)
    ref_id = _positive(ref.get("ref_id"))
    if str(ref.get("type")) != elem_type or ref_id is None or not isinstance(config, Mapping):
        restore.problems.append(f"element #{live_id}: not restored — the block is not the backup's {_kind(elem_type)}")
        return
    if elem_type == SCENE_TYPE:
        if await api.function_plan_save_scene(ref_id, scene_config_to_save(config)):
            restore.restored += 1
        else:
            restore.problems.append(f"element #{live_id}: saving the scene config failed (see the log)")
        return
    if tables is None or (live := (tables.get(elem_type) or {}).get(str(ref_id))) is None:
        restore.problems.append(f"element #{live_id}: not restored — Comexio's Quantum module table could not be read")
        return
    if str(live.get(_TYPE_ID)) != str(config.get(_TYPE_ID)):
        restore.problems.append(
            f"element #{live_id}: not restored — the module is of type {live.get(_TYPE_ID)}, the backup's of type "
            f"{config.get(_TYPE_ID)}; replace the module in Comexio Studio"
        )
        return
    if await _write_quantum_in_place(api, live_id, ref_id, elem_type, config, live, restore):
        restore.quantum[old_id] = (live_id, elem_type, ref_id)


async def _write_quantum_in_place(
    api,
    live_id: int,
    ref_id: int,
    elem_type: str,
    config: Mapping[str, Any],
    live: Mapping[str, Any],
    restore: ModuleConfigRestore,
) -> bool:
    """Write a Quantum module's stored settings onto the live module of the same type; False if that failed.

    The name is part of the plan payload run_fup applied; only the settings are written here.
    """
    if settings := quantum_settings_to_save(config):
        central = elem_type == COMBINED_CENTRAL_TYPE
        if not await api.function_plan_save_quantum_settings(live_id, ref_id, settings, central=central, name=None):
            restore.problems.append(f"element #{live_id}: saving the Quantum module settings failed (see the log)")
            return False
        restore.restored += 1
    elif quantum_settings_to_save(live):
        restore.problems.append(
            f"element #{live_id}: settings not reset to the module type's defaults (the backup's) — Comexio cannot "
            "remove settings, reset them in Comexio Studio"
        )
    else:
        restore.restored += 1
    return True


def _kind(elem_type: str) -> str:
    return "scene block" if elem_type == SCENE_TYPE else "Quantum module"


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def check_rules(api, restore: ModuleConfigRestore) -> None:
    """Name the restored Quantum modules whose rules differ from the backup (after a rebuild).

    Reloads the config page once when a module was placed — that also refreshes the module
    configs a backup cycle compares against.
    """
    if restore.quantum:
        _compare_rules(restore, await _reload_tables(api))


def _compare_rules(restore: ModuleConfigRestore, tables: Mapping[str, Mapping[str, Any]] | None) -> None:
    configs = restore.configs or {}
    for old_id, (element_id, elem_type, module_id) in restore.quantum.items():
        live = None if tables is None else (tables.get(elem_type) or {}).get(str(module_id))
        if live is None:
            restore.rules_unchecked = True
            continue
        wanted = configs[old_id][_CONFIG]
        if events := rules_not_matching(wanted, live, quantum_settings_to_save(wanted)):
            restore.rules[str(element_id)] = events


async def _reload_tables(api) -> Mapping[str, Mapping[str, Any]] | None:
    """The module config tables of a freshly fetched config page; None when it could not be read.

    A transport failure is logged, not raised: the configs are written by then, only checking them
    (and refreshing the cache, which the next poll does too) failed.
    """
    try:
        await api.get_raw_config()
    except TRANSPORT_ERRORS as err:
        _LOGGER.warning("Function Plan Restore: reloading Comexio's config page failed: %r", err)
        return None
    return api.module_configs


def module_configs_line(snapshot: Mapping[str, Any], restore: ModuleConfigRestore) -> str:
    """Restore message line on the scene/Quantum configs ("" for a plan without such blocks)."""
    parts: list[str] = []
    if restore.configs is None:
        if uses_module_configs(snapshot):
            parts.append(MODULE_CONFIGS_NOT_IN_BACKUP.rstrip("\n"))
    elif restore.configs:
        icon = ICON_SUCCESS if restore.ok() else ICON_ERROR
        parts.append(f"Scene/Quantum configs: {icon} {restore.restored}/{len(restore.configs)} restored")
    parts.extend(f"{ICON_WARNING} {problem}" for problem in restore.problems)
    if restore.rules:
        differ = "; ".join(
            f"element #{elem_id} {', '.join(events)}" for elem_id, events in sorted(restore.rules.items())
        )
        parts.append(
            f"{ICON_WARNING} Quantum rules not restored (Comexio has no request for them), they differ from the "
            f"backup: {differ} — set them in Comexio Studio"
        )
    if restore.rules_unchecked:
        parts.append(f"{ICON_WARNING} Quantum rules not checked — Comexio's module table could not be reloaded")
    return " | ".join(parts) + "\n" if parts else ""
