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
{element id: {"type": "13" | "20" | "21", "config": {...}}}. A restore writes them back
(function_plan_module_restore) with scene_config_to_save / quantum_settings_to_save; Comexio has
no request for a Quantum module's rules, rules_not_matching names the ones that differ.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
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
# The central table may carry no rules sub-table at all; the module table always has one.
_RULES_OPTIONAL = frozenset({_COMBINED_TABLES[COMBINED_CENTRAL_TYPE]})
_SCENE_TABLE = "CurrentSceneConfig"
_MODULE_ID = "CombinedModuleId"
# Rule fields the diff leaves out: the row id is the key, the module id is the element's
# ref_id, and RandomId is an internal token, not something a user set.
_RULE_FIELDS_NOT_DIFFED = frozenset({"Id", _MODULE_ID, "RandomId"})
# The field that keys a row of each Quantum sub-table in a module's config.
_ROW_KEYS = {"settings": "Name", "rules": "Id"}
# A row field that is missing, null or empty names nothing.
_BLANK = (None, "")
# Comexio's own id of a scene's channel, scene and value rows.
_ROW_ID = "id"

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
    if (scenes := _scene_table(variables)) is not None:
        tables[SCENE_TYPE] = scenes
    fub_modules = variables.get("FubModules")
    if not isinstance(fub_modules, Mapping):
        # Without the module list no Quantum element can be matched to its config.
        if any(table in variables for table in _COMBINED_TABLES.values()):
            _LOGGER.warning(
                "Module configs: $FubModules is %s — Quantum modules are not backed up",
                "missing" if fub_modules is None else "of an unknown format",
            )
        return tables
    for elem_type, table in _COMBINED_TABLES.items():
        if (configs := _module_list(fub_modules.get(elem_type), elem_type)) is None:
            continue
        if (parsed := _parse_combined(configs, variables.get(table), table)) is not None:
            tables[elem_type] = parsed
    return tables


def _scene_table(variables: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """{element id: scene config} of $CurrentSceneConfig; None when the page lacks it or it is malformed."""
    scenes = variables.get(_SCENE_TABLE)
    if isinstance(scenes, list) and not scenes:
        scenes = {}
    if not isinstance(scenes, Mapping):
        if _SCENE_TABLE in variables:
            _LOGGER.warning("Module configs: $%s has an unknown format — scene blocks are not backed up", _SCENE_TABLE)
        return None
    # One unreadable scene makes the table unreadable, like a malformed Quantum row does.
    if (bad := next((elem_id for elem_id, scene in scenes.items() if not _readable_scene(scene)), None)) is not None:
        _LOGGER.warning(
            "Module configs: $%s has an unknown format for element %s — scene blocks are not backed up",
            _SCENE_TABLE,
            bad,
        )
        return None
    return {str(elem_id): dict(scene) for elem_id, scene in scenes.items()}


def _readable_scene(scene: Any) -> bool:
    """Whether a scene config's channel, scene and per-scene value tables are all readable rows.

    A partial view would hide a change confined to the unreadable part, so one bad table rejects it.
    """
    if not (isinstance(scene, Mapping) and _is_table(scene.get("common")) and _is_table(scene.get("scene"))):
        return False
    return all(_is_table(row.get("data")) for row in _rows(scene["scene"]) or ())


def _is_table(raw: Any) -> bool:
    """Whether raw is a PHP-serialized table whose rows are all objects."""
    rows = _rows(raw)
    return rows is not None and all(isinstance(row, Mapping) for row in rows)


def _module_list(modules: Any, elem_type: str) -> dict[str, dict[str, Any]] | None:
    """{module id: config without settings and rules yet} of one type's $FubModules rows; None when malformed."""
    # A type $FubModules does not list has no modules, like an empty one.
    rows = [] if modules is None else _rows(modules)
    # Id keys the module, TypeId is part of its config: a row without either is not a readable module.
    if rows is None or not all(
        isinstance(row, Mapping) and all(row.get(field) not in _BLANK for field in ("Id", "TypeId")) for row in rows
    ):
        _LOGGER.warning(
            "Module configs: $FubModules[%s] has an unknown format — Quantum modules are not backed up", elem_type
        )
        return None
    return {
        str(row["Id"]): {
            "type_id": row.get("TypeId"),
            "default_value": row.get("DefaultValue"),
            "settings": {},
            "rules": {},
        }
        for row in rows
    }


def _parse_combined(configs: dict[str, dict[str, Any]], data: Any, table: str) -> dict[str, dict[str, Any]] | None:
    """{module id: {"type_id", "default_value", "settings": {name: value}, "rules": {row id: row}}}."""
    settings, rules = _sub_tables(data, table)
    if data is not None and (settings is None or rules is None):
        _LOGGER.warning("Module configs: $%s has an unknown format — Quantum modules are not backed up", table)
        return None
    if settings is None or rules is None:
        if configs:
            _LOGGER.warning("Module configs: $%s is missing — Quantum modules are not backed up", table)
        return None
    if not (_add_rows(configs, settings, "settings", table) and _add_rows(configs, rules, "rules", table)):
        return None
    return configs


def _sub_tables(data: Any, table: str) -> tuple[Iterable[Any] | None, Iterable[Any] | None]:
    """The settings and rules rows of a Quantum table; None for a missing or malformed one."""
    if isinstance(data, list) and not data:
        return [], []
    if not isinstance(data, Mapping):
        return None, None
    # A sub-table the table must carry is unreadable when missing, never read as empty.
    return _rows(data.get("settings")), _rows(data.get("rules", [] if table in _RULES_OPTIONAL else None))


def _add_rows(configs: dict[str, dict[str, Any]], rows: Iterable[Any], part: str, table: str) -> bool:
    """Files each row under its module's config; False when a row is malformed (the table is unreadable).

    A row of a module $FubModules does not list belongs to no plan element (plan_module_configs
    treats an element naming an unlisted module as unreadable), so it is only logged at debug.
    """
    key_field = _ROW_KEYS[part]
    orphans = 0
    for row in rows:
        if (missing := _missing_field(row, key_field, part)) is not None:
            _LOGGER.warning(
                "Module configs: $%s has a %s row without %s — Quantum modules are not backed up",
                table,
                part,
                missing,
            )
            return False
        if (config := configs.get(str(row.get(_MODULE_ID)))) is None:
            orphans += 1
            continue
        key = str(row[key_field])
        config[part][key] = (
            row.get("Value") if part == "settings" else {k: v for k, v in row.items() if k != _MODULE_ID}
        )
    if orphans:
        _LOGGER.debug("Module configs: %d %s row(s) of $%s name no listed module, ignored", orphans, part, table)
    return True


def _missing_field(row: Any, key_field: str, part: str) -> str | None:
    """The field a Quantum row lacks to be filed (its key, its module id, a setting's Value); None if complete."""
    if not isinstance(row, Mapping):
        return key_field
    if (missing := next((field for field in (key_field, _MODULE_ID) if row.get(field) in _BLANK), None)) is not None:
        return missing
    # A setting's value may be empty, but a row without the field is truncated, not a setting set to nothing.
    return "Value" if part == "settings" and "Value" not in row else None


def plan_module_configs(tables: Mapping[str, Mapping[str, Any]], elements: Mapping[str, Any]) -> ModuleConfigs | None:
    """The configs of one plan's scene blocks and Quantum modules; None when one of them was not readable.

    A scene block without a stored config is left out (Comexio creates it on the first save); a
    Quantum element whose module Comexio does not list counts as unreadable.
    """
    configs: ModuleConfigs = {}
    for elem_id, elem_type, ref in _module_elements(elements):
        if (table := tables.get(elem_type)) is None:
            _LOGGER.warning(
                "Module configs: no readable config table for element %s (type %s) — the plan's scene/Quantum "
                "configs are not backed up",
                elem_id,
                elem_type,
            )
            return None
        key = str(elem_id) if elem_type == SCENE_TYPE else str(ref.get("ref_id"))
        if (config := table.get(key)) is not None:
            configs[str(elem_id)] = {"type": elem_type, "config": config}
        elif elem_type != SCENE_TYPE:
            _LOGGER.warning(
                "Module configs: Quantum element %s names module %s, not listed by Comexio — the plan's "
                "scene/Quantum configs are not backed up",
                elem_id,
                key,
            )
            return None
    return configs


def uses_module_configs(snapshot: Mapping[str, Any]) -> bool:
    """Whether a snapshot's plan has a scene block or Quantum module."""
    return any(_module_elements(snapshot.get("elements") or {}))


def scene_block_ids(snapshot: Mapping[str, Any]) -> list[str]:
    """Element ids of a snapshot's scene blocks."""
    return [
        elem_id
        for elem_id, elem_type, _ref in _module_elements(snapshot.get("elements") or {})
        if elem_type == SCENE_TYPE
    ]


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

    Only the fields the diff shows count. current None (not read this poll) never counts as a
    change. A previous snapshot without
    configs counts as one without any, so a plan with scene blocks or Quantum modules gets one
    new snapshot that carries them.
    """
    if current is None:
        return False
    return _comparable(snapshot_module_configs(previous) or {}) != _comparable(current)


def _comparable(configs: ModuleConfigs) -> dict[str, tuple[Any, dict[str, str]]]:
    """Configs as the diff compares them, so ids Comexio regenerates on a save (scene rows, rule
    RandomId) never count as a change the diff cannot show."""
    return {
        elem_id: (entry.get("type") if isinstance(entry, Mapping) else None, _flatten(entry))
        for elem_id, entry in configs.items()
    }


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
    settings = config.get("settings")
    for setting, value in (settings if isinstance(settings, Mapping) else {}).items():
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


def scene_config_to_save(config: Mapping[str, Any]) -> dict[str, Any]:
    """A stored scene config as a restore saves it onto a scene: without its row ids.

    The ids are rows of the scene the backup was read from. Without them the save replaces the
    target scene's rows with new ones; a value row names its channel by the channel's position
    (common_id, the key of the common table), not by a row id, so the pairing survives.
    """
    saved = {key: value for key, value in config.items() if key != _ROW_ID}
    if "common" in saved:
        saved["common"] = _map_rows(saved["common"], _without_row_id)
    if "scene" in saved:
        saved["scene"] = _map_rows(saved["scene"], _scene_without_row_ids)
    return saved


def _scene_without_row_ids(row: Mapping[str, Any]) -> dict[str, Any]:
    scene = _without_row_id(row)
    if "data" in scene:
        scene["data"] = _map_rows(scene["data"], _without_row_id)
    return scene


def _without_row_id(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != _ROW_ID}


def _map_rows(raw: Any, convert: Callable[[Mapping[str, Any]], dict[str, Any]]) -> Any:
    """raw with convert applied to each object row, in its own shape (PHP list or object keyed by row)."""
    if isinstance(raw, list):
        return [convert(row) if isinstance(row, Mapping) else row for row in raw]
    if isinstance(raw, Mapping):
        return {key: convert(row) if isinstance(row, Mapping) else row for key, row in raw.items()}
    return raw


def quantum_settings_to_save(config: Mapping[str, Any]) -> dict[str, str]:
    """{setting: value} of a stored Quantum config as Comexio's module dialog sends them (strings)."""
    settings = config.get("settings")
    if not isinstance(settings, Mapping):
        return {}
    return {str(name): _setting_text(value) for name, value in settings.items()}


def _setting_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def rules_not_matching(wanted: Mapping[str, Any], live: Mapping[str, Any], settings: Iterable[str]) -> list[str]:
    """The events (ByDay) of the rules that differ between a stored Quantum config and the live one.

    Rules are matched by the event they react to: a module created by a restore numbers its rule
    rows anew. A rule only one side has counts. The name of a rule whose event is one of the
    settings (user_event_N) is left out: it follows that setting, which the restore writes.
    """
    named = frozenset(settings)
    want, have = _rules_by_event(wanted), _rules_by_event(live)
    return sorted(
        (
            event
            for event in set(want) | set(have)
            if _rule(want.get(event), event in named) != _rule(have.get(event), event in named)
        ),
        key=_id_order,
    )


def _rules_by_event(config: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(rule.get("ByDay") or f"#{row_id}"): rule for row_id, rule in _items(config.get("rules"))}


def _rule(rule: Mapping[str, Any] | None, name_follows_setting: bool) -> dict[str, str] | None:
    """A rule's fields as compared; null and empty count alike (Comexio stores either for an unset group)."""
    if rule is None:
        return None
    return {
        key: "" if value in _BLANK else _text(value)
        for key, value in rule.items()
        if key not in _RULE_FIELDS_NOT_DIFFED and not (name_follows_setting and key == "Name")
    }
