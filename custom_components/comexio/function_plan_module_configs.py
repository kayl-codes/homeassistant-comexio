"""Configs of scene blocks and Quantum modules in function plans.

Like the block settings (function_plan_block_settings), Comexio keeps these outside the plan,
so the run_fup payload a backup stores does not carry them:

- a scene block (element type 13) has its scenes in $CurrentSceneConfig, keyed by element id:
  {name, category, id, allow_save, common: {n: {value, id}}, scene: {n: {name, user_save,
  default_scene, id, data: {k: {value, common_id, id}}}}}
- a Quantum module (type 20) is the module its element's ref_id names in $FubModules["20"]
  ({Id, RandomId, TypeId, DefaultValue}); its settings and rules are the rows of
  $CombinedModuleSettingData {"settings": {row: {Id, CombinedModuleId, Name, Value}},
  "rules": {row: {Id, CombinedModuleId, Name, Freq, ByDay, Value, ...}}} with that module id
- a central Quantum module (type 21) likewise, from $FubModules["21"] and
  $CombinedModuleCentralSettingData

A snapshot keeps them per element under SNAPSHOT_MODULE_CONFIGS:
{element id: {"type": "13" | "20" | "21", "config": {...}}}. Restoring them is not done yet.
"""

from collections.abc import Iterable, Iterator, Mapping
import json
import logging
from typing import Any

from .const import one_line

_LOGGER = logging.getLogger(__name__)

# Snapshot key: {element id: {"type": element type, "config": ...}}. Snapshots stored before it existed lack it.
SNAPSHOT_MODULE_CONFIGS = "module_configs"

SCENE_TYPE = "13"
COMBINED_TYPE = "20"
COMBINED_CENTRAL_TYPE = "21"
_COMBINED_TABLES = {
    COMBINED_TYPE: "CombinedModuleSettingData",
    COMBINED_CENTRAL_TYPE: "CombinedModuleCentralSettingData",
}
_SCENE_TABLE = "CurrentSceneConfig"
_MODULE_ID = "CombinedModuleId"
# Rule fields the diff leaves out: the row id is the key, the module id is the element's
# ref_id, and RandomId is an internal token, not something a user set.
_RULE_FIELDS_NOT_DIFFED = frozenset({"Id", _MODULE_ID, "RandomId"})

# {element type: {key: config}} — scenes by element id, Quantum modules by module id. A type whose
# table the page did not carry (readably) is absent.
ModuleConfigTables = dict[str, dict[str, dict[str, Any]]]
ModuleConfigs = dict[str, dict[str, Any]]


def _rows(raw: Any) -> Iterable[Any] | None:
    """The rows of a PHP-serialized table: [] when empty, an object keyed by row id when filled."""
    if isinstance(raw, list):
        return raw
    return raw.values() if isinstance(raw, Mapping) else None


def parse_module_config_tables(variables: Mapping[str, Any]) -> ModuleConfigTables:
    """The scene and Quantum module tables of a config page (see the module docstring)."""
    tables: ModuleConfigTables = {}
    scenes = variables.get(_SCENE_TABLE)
    if isinstance(scenes, list) and not scenes:
        scenes = {}
    if isinstance(scenes, Mapping) and all(isinstance(scene, Mapping) for scene in scenes.values()):
        tables[SCENE_TYPE] = {str(elem_id): dict(scene) for elem_id, scene in scenes.items()}
    elif _SCENE_TABLE in variables:
        _LOGGER.warning("Module configs: $%s has an unknown format — scene blocks are not backed up", _SCENE_TABLE)
    fub_modules = variables.get("FubModules")
    modules: Mapping[str, Any] = fub_modules if isinstance(fub_modules, Mapping) else {}
    for elem_type, table in _COMBINED_TABLES.items():
        if (parsed := _parse_combined(modules.get(elem_type), variables.get(table), table)) is not None:
            tables[elem_type] = parsed
    return tables


def _parse_combined(modules: Any, data: Any, table: str) -> dict[str, dict[str, Any]] | None:
    """{module id: {"type_id", "default_value", "settings": {name: value}, "rules": {row id: row}}}."""
    # A type $FubModules does not list has no modules, like an empty one.
    module_rows = [] if modules is None else _rows(modules)
    if isinstance(data, list) and not data:
        data = {}
    # A missing sub-table is empty (the central table may carry no rules); a malformed one is unreadable.
    settings = _rows(data.get("settings", [])) if isinstance(data, Mapping) else None
    rules = _rows(data.get("rules", [])) if isinstance(data, Mapping) else None
    if module_rows is None or settings is None or rules is None:
        if data is not None:
            _LOGGER.warning("Module configs: $%s has an unknown format — Quantum modules are not backed up", table)
        elif module_rows:
            _LOGGER.warning("Module configs: $%s is missing — Quantum modules are not backed up", table)
        return None
    configs: dict[str, dict[str, Any]] = {
        str(module["Id"]): {
            "type_id": module.get("TypeId"),
            "default_value": module.get("DefaultValue"),
            "settings": {},
            "rules": {},
        }
        for module in module_rows
        if isinstance(module, Mapping) and module.get("Id") is not None
    }
    skipped = 0
    for row in settings:
        config = configs.get(str(row.get(_MODULE_ID))) if isinstance(row, Mapping) else None
        if config is None or not row.get("Name"):
            skipped += 1
            continue
        config["settings"][str(row["Name"])] = row.get("Value")
    for row in rules:
        config = configs.get(str(row.get(_MODULE_ID))) if isinstance(row, Mapping) else None
        if config is None or row.get("Id") is None:
            skipped += 1
            continue
        config["rules"][str(row["Id"])] = {key: value for key, value in row.items() if key != _MODULE_ID}
    if skipped:
        _LOGGER.warning("Module configs: %d row(s) of $%s without a known module, name or id ignored", skipped, table)
    return configs


def plan_module_configs(tables: Mapping[str, Mapping[str, Any]], elements: Mapping[str, Any]) -> ModuleConfigs | None:
    """The configs of one plan's scene blocks and Quantum modules; None when one of them was not readable.

    A scene block without a stored config is left out (Comexio creates it on the first save).
    """
    configs: ModuleConfigs = {}
    for elem_id, elem_type, ref in _module_elements(elements):
        if (table := tables.get(elem_type)) is None:
            return None
        key = str(elem_id) if elem_type == SCENE_TYPE else str(ref.get("ref_id"))
        if (config := table.get(key)) is not None:
            configs[str(elem_id)] = {"type": elem_type, "config": config}
        elif elem_type != SCENE_TYPE:
            _LOGGER.debug("Module configs: Quantum element %s names module %s, not listed by Comexio", elem_id, key)
    return configs


def uses_module_configs(snapshot: Mapping[str, Any]) -> bool:
    """Whether a snapshot's plan has a scene block or Quantum module."""
    return any(_module_elements(snapshot.get("elements") or {}))


def _module_elements(elements: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    """(element id, element type, reference) of the scene blocks and Quantum modules among elements."""
    for elem_id, elem in elements.items():
        ref = elem.get("reference") if isinstance(elem, Mapping) else None
        if isinstance(ref, Mapping) and (elem_type := str(ref.get("type"))) in (SCENE_TYPE, *_COMBINED_TABLES):
            yield str(elem_id), elem_type, ref


def snapshot_module_configs(snapshot: Mapping[str, Any]) -> ModuleConfigs | None:
    """A snapshot's stored module configs; None for a snapshot stored before they were captured."""
    stored = snapshot.get(SNAPSHOT_MODULE_CONFIGS)
    return stored if isinstance(stored, dict) else None


def module_configs_changed(previous: Mapping[str, Any], current: ModuleConfigs | None) -> bool:
    """Whether today's configs differ from the ones the previous snapshot stored.

    current None (not read this poll) never counts as a change. A previous snapshot without
    configs counts as one without any, so a plan with scene blocks or Quantum modules gets one
    new snapshot that carries them.
    """
    if current is None:
        return False
    return (snapshot_module_configs(previous) or {}) != current


def module_configs_diff(
    newer: Mapping[str, Any], older: Mapping[str, Any]
) -> list[tuple[str, str, str | None, str | None]] | None:
    """(element id, field, old value, new value) for every config field that changed between two snapshots.

    Only elements both snapshots contain count: an added or removed block is a change of the
    plan itself. None when either snapshot was stored without configs.
    """
    new_configs, old_configs = snapshot_module_configs(newer), snapshot_module_configs(older)
    if new_configs is None or old_configs is None:
        return None
    common = set(newer.get("elements") or {}) & set(older.get("elements") or {})
    changes: list[tuple[str, str, str | None, str | None]] = []
    for elem_id in sorted(common, key=_id_order):
        before, after = _flatten(old_configs.get(elem_id)), _flatten(new_configs.get(elem_id))
        changes.extend(
            (elem_id, field, before.get(field), after.get(field))
            for field in sorted(set(before) | set(after))
            if before.get(field) != after.get(field)
        )
    return changes


def _flatten(entry: Mapping[str, Any] | None) -> dict[str, str]:
    """{field label: value} of one element's config, the unit the diff compares."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("config"), Mapping):
        return {}
    if entry.get("type") == SCENE_TYPE:
        return _flatten_scene(entry["config"])
    return _flatten_combined(entry["config"])


def _flatten_scene(config: Mapping[str, Any]) -> dict[str, str]:
    fields = {
        "name": _text(config.get("name")),
        "user save": _text(config.get("allow_save")),
        "category": _text(config.get("category")),
    }
    for common_id, common in _items(config.get("common")):
        fields[f"channel {common_id}"] = _text(common.get("value"))
    for scene_id, scene in _items(config.get("scene")):
        prefix = f"scene {scene_id}"
        fields[f"{prefix} name"] = _text(scene.get("name"))
        fields[f"{prefix} user save"] = _text(scene.get("user_save"))
        fields[f"{prefix} default"] = _text(scene.get("default_scene"))
        for pos, value in _items(scene.get("data")):
            fields[f"{prefix} value {pos} (channel {_text(value.get('common_id'))})"] = _text(value.get("value"))
    return fields


def _flatten_combined(config: Mapping[str, Any]) -> dict[str, str]:
    fields = {"type": _text(config.get("type_id")), "default value": _text(config.get("default_value"))}
    for setting, value in (config.get("settings") or {}).items():
        fields[f"setting {setting}"] = _text(value)
    for rule_id, rule in _items(config.get("rules")):
        for key, value in rule.items():
            if key not in _RULE_FIELDS_NOT_DIFFED:
                fields[f"rule {rule_id} {key}"] = _text(value)
    return fields


def _items(raw: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """(key, row) of a table, rows only; a PHP list counts its position from 1 like Comexio's own keys."""
    if isinstance(raw, list):
        pairs: Iterable[tuple[Any, Any]] = enumerate(raw, start=1)
    elif isinstance(raw, Mapping):
        pairs = raw.items()
    else:
        return []
    return sorted(((str(key), row) for key, row in pairs if isinstance(row, Mapping)), key=lambda kv: _id_order(kv[0]))


def _text(value: Any) -> str:
    """A value as the diff shows it: strings on one line (scene and rule names come from Comexio), else JSON."""
    return one_line(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _id_order(key: str) -> tuple[int, str]:
    return (int(key), "") if key.isdigit() else (0, key)
