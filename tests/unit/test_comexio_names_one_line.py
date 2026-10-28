"""Names from Comexio reach a log line, a notification or a message only through const.one_line.

A plan, marker, IO or KNX name is edited in the Comexio admin and can hold line breaks or control characters,
which split a log entry or break a notification's layout. The integration's code is read for every place that turns a
Comexio name - a variable in NAME_VARIABLES, a mapping read like `item["name"]` / `fub.get("Name")`, or a call in
NAME_CALLS - into text: a
value in an f-string, a string built with `+`, `%` or `.format()`, and an argument of a `_LOGGER` call or of
`persistent_notification.async_create`, directly or through `str()` / `repr()`, a string method or a slice, also
after it was copied to another variable of the same function (also as `name or fallback`) or built into a message
held there. So a message built first and output later is caught as
well as a new raw log argument, without touching this file.

Text that identifies rather than shows keeps the raw name: a `notification_id` or a `value` (keyword or dict key), an
f-string assigned to a variable in IDENTITY_TARGETS, and the f-strings of the functions in IDENTITY_FUNCTIONS (their
output calls are still checked). A name collected into a list and joined, copied by anything but a (possibly
annotated) assignment, or copied outside a function, is not followed; the review checklist covers that case. An alias
read in a nested function, or reassigned to `one_line(...)`, is still flagged: rename it rather than exempt it.
"""

import ast
from pathlib import Path

import pytest

from custom_components.comexio.const import (
    distinct_one_line,
    extension_firmware_update_unique_id,
    io_command_name,
    io_plan_label,
    io_unique_id,
)
from custom_components.comexio.select import _plan_option_label
from custom_components.comexio.services._context import _resolve_fub_id
from custom_components.comexio.services._yaml_sync import _build_restore_options, _build_snapshot_options

COMPONENT = Path(__file__).parents[2] / "custom_components" / "comexio"
NAME_VARIABLES = {
    "name",
    "plan_name",
    "plan_name_s",
    "new_plan_name",
    "expected_name",
    "expected_cmd_name",
    "source_name",
    "snapshot_name",
    "live_name",
    "k_title",
    "ext_name",
    "option_value",
    "identifier",
}
# Mapping keys that hold a Comexio name: item["name"], fub.get("Name"), entry.get("plan_name"), io["identifier"].
NAME_KEYS = {"name", "Name", "plan_name", "ext_name", "identifier"}
# Calls that return a Comexio name: api.function_plan_name(fub_id).
NAME_CALLS = {"function_plan_name"}
# Keyword arguments and dict keys that identify rather than show: they keep the raw name.
IDENTITY_KEYS = {"notification_id", "value"}
# Variables and attributes that hold an identity built from a name: the Web-IO command name Comexio must report back,
# an option value parsed back, an entity's unique_id. Building them keeps the raw name; the variables are name
# variables too, so showing them needs one_line.
IDENTITY_TARGETS = {"expected_cmd_name", "option_value", "_attr_unique_id"}
# Functions whose f-strings build an identity from a name, so one_line would change what they match or store,
# per file of the integration.
IDENTITY_FUNCTIONS = {
    "api.py": {
        "prestage_knx_loopback_class",  # the Web-IO command names Comexio must report back
        "_expected_names",  # the same command names
        "_knx_bridge_title",  # the bridge marker's name written to Comexio
    },
    "const.py": {
        "watchdog_user_plan_pick",  # the stored pick, parsed back into ID and name
        "hub_era_object_id",  # an entity's object id
        "io_audit_key",  # the audit map's key of an IO
        "extension_firmware_update_unique_id",  # an entity's unique_id
        "extension_firmware_object_id",  # an entity's object id
        "io_unique_id",  # an entity's unique_id
        "io_command_name",  # the Web-IO command name Comexio must report back
    },
    "entity.py": {"extension_device_identifier"},  # a device registry identifier
    "function_plan_block_keys.py": {"_unresolved_element"},  # the element name written into the plan
    "function_plan_block_settings.py": {"block_settings_form"},  # the form field names Comexio's editor sends
    "coordinator.py": {
        "async_newest_backup_field",  # the restore action's snapshot value
        "_build_ha_audit_map",  # the command names the audit compares with Comexio's
        # unique_ids and device identifiers from extension names; the device name matches the one setup gives
        "_migrate_extension_entities",
        "update_io_by_name",  # the event label the plan card matches with the plan's element labels
    },
}
# Keyword arguments whose dict values a repair issue or a flow step shows as text.
PLACEHOLDER_KEYWORDS = {"translation_placeholders", "description_placeholders"}
STRING_WRAPPERS = {"str", "repr"}
# String methods that keep a name's line breaks: name.strip(), name.upper().
STRING_METHODS = {"strip", "lstrip", "rstrip", "upper", "lower", "title", "capitalize", "casefold", "replace"}
SOURCE_FILES = sorted(COMPONENT.rglob("*.py"))
# Set on a variable read that holds a name copied from another one: label = fub.get("Name").
ALIAS_MARK = "comexio_name_alias"
# Set on a variable read that holds a collection of names: names.append(item["name"]).
COLLECTION_MARK = "comexio_name_collection"
COLLECTION_ADDERS = {"append", "add"}
# Calls that hand a collection's items over unchanged: ", ".join(sorted(names)).
COLLECTION_WRAPPERS = {"sorted", "list", "set", "tuple", "reversed"}


def _is_name(node: ast.AST) -> bool:
    """A name variable or its alias, or a name read from a mapping: item["name"] or fub.get("Name")."""
    if isinstance(node, ast.Name):
        return node.id in NAME_VARIABLES or getattr(node, ALIAS_MARK, False)
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in NAME_KEYS
    if not isinstance(node, ast.Call):
        return False
    called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if called in NAME_CALLS:
        return True
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in NAME_KEYS
    )


def _is_output_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    return func.value.id == "_LOGGER" or (func.value.id == "persistent_notification" and func.attr == "async_create")


def _shown_name(value: ast.AST) -> ast.expr | None:
    """The name an expression hands over unfiltered: itself, or through str()/repr(), a string method or a slice."""
    if _is_name(value):
        return value
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in STRING_WRAPPERS:
        return _shown_name(value.args[0]) if value.args else None
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr in STRING_METHODS:
        return _shown_name(value.func.value)
    if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Slice):
        return _shown_name(value.value)
    if isinstance(value, ast.BoolOp | ast.IfExp):  # name or fallback, name if x else fallback
        branches = value.values if isinstance(value, ast.BoolOp) else [value.body, value.orelse]
        return next(iter(_shown_names(branches)), None)
    return None


def _shown_names(values: list[ast.expr]) -> list[ast.expr]:
    return [name for value in values if (name := _shown_name(value)) is not None]


def _is_text(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _is_identity(node: ast.AST, parents: dict[ast.AST, ast.AST], identity_functions: set[str]) -> bool:
    """True for text passed as an identity key, or built inside an IDENTITY_FUNCTIONS function but not for output."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword) and parent.arg in IDENTITY_KEYS:
        return True
    if isinstance(parent, ast.Assign) and any(
        (target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)) in IDENTITY_TARGETS
        for target in parent.targets
    ):
        return True
    if isinstance(parent, ast.Dict) and node in parent.values:
        key = parent.keys[parent.values.index(node)]
        if isinstance(key, ast.Constant) and key.value in IDENTITY_KEYS:
            return True
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call) and _is_output_call(node):
            return False
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in identity_functions:
            return True
    return False


def _joined_names(items: ast.expr) -> list[ast.expr]:
    """The names a `", ".join(items)` puts into its text: a collection of names, or one built in place."""
    if isinstance(items, ast.Name):
        return [items] if getattr(items, COLLECTION_MARK, False) else []
    if isinstance(items, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return _shown_names([items.elt])
    if isinstance(items, ast.List | ast.Tuple | ast.Set):
        return _shown_names(items.elts)
    if isinstance(items, ast.Call) and isinstance(items.func, ast.Name) and items.func.id in COLLECTION_WRAPPERS:
        return _joined_names(items.args[0]) if items.args else []
    return []


def _raw_in_text(node: ast.AST) -> list[ast.expr]:
    """The names an f-string, a `+` / `%` string build, a `.format()` or a `.join()` call puts into its text."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _shown_names([*node.args, *(keyword.value for keyword in node.keywords)])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        return _joined_names(node.args[0]) if _is_text(node.func.value) and node.args else []
    if isinstance(node, ast.JoinedStr):
        return _shown_names([part.value for part in node.values if isinstance(part, ast.FormattedValue)])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        if isinstance(node.op, ast.Mod) and _is_text(node.left):
            right = node.right
            operands = (
                right.elts if isinstance(right, ast.Tuple) else right.values if isinstance(right, ast.Dict) else [right]
            )
            return _shown_names(operands)
        if isinstance(node.op, ast.Add) and (_is_text(node.left) or _is_text(node.right)):
            return _shown_names([node.left, node.right])
    return []


def _raw_in_output(call: ast.Call) -> list[ast.expr]:
    shown = [keyword.value for keyword in call.keywords if keyword.arg not in IDENTITY_KEYS]
    return _shown_names([*call.args, *shown])


def _raw_in_placeholders(call: ast.Call) -> list[ast.expr]:
    """The names a repair issue or a flow step puts into its text through a placeholders dict."""
    placeholders = [kw.value for kw in call.keywords if kw.arg in PLACEHOLDER_KEYWORDS]
    held = [value for value in placeholders if isinstance(value, ast.Name) and getattr(value, COLLECTION_MARK, False)]
    dicts = [value for value in placeholders if isinstance(value, ast.Dict)]
    return [*held, *_shown_names([value for literal in dicts for value in literal.values])]


def _name_aliases(scope: ast.AST) -> dict[str, int]:
    """{variable: first line} of the plain assignments of a name as it is in scope: `label = item["name"]`."""
    aliases: dict[str, int] = {}
    for assign in ast.walk(scope):
        if isinstance(assign, ast.Assign) and len(assign.targets) == 1:
            target, value = assign.targets[0], assign.value
        elif isinstance(assign, ast.AnnAssign) and assign.value is not None:
            target, value = assign.target, assign.value
        else:
            continue
        holds_name = _shown_name(value) is not None or bool(_raw_in_text(value))  # a name, or text built from one
        if isinstance(target, ast.Name) and target.id not in IDENTITY_TARGETS and holds_name:
            aliases[target.id] = min(assign.lineno, aliases.get(target.id, assign.lineno))
    return aliases


def _collected_into(node: ast.AST) -> str | None:
    """The variable a statement fills with names: `names.append(item["name"])`, `names = [f["Name"] for f in fubs]`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id if node.func.attr in COLLECTION_ADDERS and _shown_names(node.args) else None
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        value = node.value
        holds_names = _shown_names(value.values) if isinstance(value, ast.Dict) else _joined_names(value)
        return node.targets[0].id if not isinstance(value, ast.Name) and holds_names else None
    return None


def _name_collections(scope: ast.AST) -> dict[str, int]:
    """{variable: first line} of the collections a function fills with names."""
    collections: dict[str, int] = {}
    for node in ast.walk(scope):
        if (name := _collected_into(node)) is not None:
            collections[name] = min(node.lineno, collections.get(name, node.lineno))
    return collections


def _mark_collections(tree: ast.Module) -> None:
    """Mark the reads of a collection of names, per function, from the line it is first filled on."""
    for scope in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)):
        collections = _name_collections(scope)
        for read in ast.walk(scope):
            if isinstance(read, ast.Name) and read.id in collections and read.lineno >= collections[read.id]:
                setattr(read, COLLECTION_MARK, True)


def _mark_aliases(tree: ast.Module) -> None:
    """Mark the reads of a name alias after its first assignment, per function, as names themselves."""
    for scope in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)):
        aliases: dict[str, int] = {}
        while (found := _name_aliases(scope)) != aliases:  # an alias of an alias needs another round
            aliases = found
            reads = (node for node in ast.walk(scope) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))
            for read in reads:
                if read.id in aliases and read.lineno > aliases[read.id]:
                    setattr(read, ALIAS_MARK, True)


def raw_name_outputs(source: str, identity_functions: set[str] | None = None) -> list[tuple[int, str]]:
    """(line, source) of every name turned into text or output without one_line."""
    tree = ast.parse(source)
    _mark_aliases(tree)
    _mark_collections(tree)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_output_call(node):
            found.extend((name.lineno, ast.unparse(name)) for name in _raw_in_output(node))
        elif isinstance(node, ast.Call) and _raw_in_placeholders(node):
            found.extend((name.lineno, ast.unparse(name)) for name in _raw_in_placeholders(node))
        elif not _is_identity(node, parents, identity_functions or set()):
            found.extend((name.lineno, ast.unparse(name)) for name in _raw_in_text(node))
    return sorted(found)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('_LOGGER.info("Plan %s", plan_name)', [(1, "plan_name")]),
        ('_LOGGER.warning(f"Plan {name!r} gone")', [(1, "name")]),
        ('persistent_notification.async_create(hass, f"Plan {plan_name}", title="x")', [(1, "plan_name")]),
        ("persistent_notification.async_create(hass, msg, title=name)", [(1, "name")]),
        ('_LOGGER.info("Plan %s", str(plan_name))', [(1, "plan_name")]),
        ('_LOGGER.info("Plan %r", repr(name))', [(1, "name")]),
        ('msg = f"Plan {plan_name} restored"', [(1, "plan_name")]),
        ('lines = [f"Plan {plan_name_s} stopped" for plan_name_s in plans]', [(1, "plan_name_s")]),
        ('msg = "Plan " + plan_name', [(1, "plan_name")]),
        ('msg = "Plan %s (%s)" % (plan_name, fub_id)', [(1, "plan_name")]),
        ('_LOGGER.debug(\n    "Plan %s",\n    plan_name,\n)', [(3, "plan_name")]),
        ('_LOGGER.info("Plan %s", one_line(plan_name))', []),
        ('_LOGGER.info(f"Plan {one_line(name)!r}")', []),
        ('msg = f"Plan {one_line(plan_name)} restored"', []),
        ("persistent_notification.async_create(hass, msg, title=one_line(name))", []),
        ('persistent_notification.async_create(hass, msg, notification_id=f"x_{plan_name}")', []),
        ('option = {"label": one_line(name), "value": f"{fub_id}:{name}"}', []),
        ('SelectOptionDict(value=f"{fub_id}:{name}", label=f"{one_line(name)} (ID {fub_id})")', []),
        ('_LOGGER.info("Plan %s (ID %s)", shown, fub_id)', []),
        ('msg = f"Plan {shown} restored"', []),
        ('_LOGGER.info("   -> %s", item["name"])', [(1, "item['name']")]),
        ("msg = f\"Plan {fub.get('Name', '?')} gone\"", [(1, "fub.get('Name', '?')")]),
        ('msg = "Plan {} gone".format(plan_name)', [(1, "plan_name")]),
        ('msg = "Plan {name} gone".format(name=k_title)', [(1, "k_title")]),
        ('_LOGGER.info("   -> %s", one_line(item["name"]))', []),
        ("expected_cmd_name = f\"HA {marker['name']}\"", []),
        ('_LOGGER.warning("Web-IO %s not found", expected_cmd_name)', [(1, "expected_cmd_name")]),
        ("msg = f\"Web-IO '{expected_cmd_name}' not found\"", [(1, "expected_cmd_name")]),
        ("cmd = webio_commands.get(expected_cmd_name)", []),
        ("msg = f\"Command {item['id']}\"", []),
        ('msg = f"Plan {plan_name.strip()} gone"', [(1, "plan_name")]),
        ('msg = f"Plan {str(name)!r} gone"', [(1, "name")]),
        ('msg = "Plan {}".format(plan_name[:40])', [(1, "plan_name")]),
        ("_LOGGER.info(\"Plan %s\", fub.get('Name', '?').upper())", [(1, "fub.get('Name', '?')")]),
        ('msg = f"Plan {one_line(plan_name).strip()} gone"', []),
        ('def f(fub):\n    label = fub.get("Name")\n    _LOGGER.info("Plan %s", label)', [(3, "label")]),
        ('def f(item):\n    shown = item["name"]\n    label = shown\n    msg = f"Plan {label} gone"', [(4, "label")]),
        ('def f(fub):\n    label = one_line(fub.get("Name"))\n    _LOGGER.info("Plan %s", label)', []),
        ('def f(fub):\n    _LOGGER.info("Plan %s", label)\n    label = fub.get("Name")', []),
        ('def f(fub):\n    label = fub.get("Name")\ndef g(label):\n    _LOGGER.info("Plan %s", label)', []),
        (
            'def f(fub):\n    label = fub["Name"]\n    _LOGGER.info("%s", label)\n    label = fub["Name"]',
            [(3, "label")],
        ),
        ('def f(fub):\n    label: str = fub.get("Name")\n    _LOGGER.info("Plan %s", label)', [(3, "label")]),
        ('def f(a):\n    b = a["name"]\n    c = b\n    d = c\n    msg = f"Plan {d}"', [(5, "d")]),
        ("option_value = f\"{fub_id}:{entry.get('plan_name')}\"", []),
        ('_LOGGER.info("Option %s", option_value)', [(1, "option_value")]),
        ('value = f"Plan {name} gone"', [(1, "name")]),
        ('def f(m):\n    label = m.get("name") or "M1"\n    _LOGGER.debug("%s", label)', [(3, "label")]),
        ('msg = f"Plan {name if name else fub_id} gone"', [(1, "name")]),
        ('_LOGGER.debug("%s", fallback or item["name"])', [(1, "item['name']")]),
        ('msg = "Plan %(plan)s gone" % {"plan": plan_name}', [(1, "plan_name")]),
        (
            'def f(items):\n    names = []\n    for i in items:\n        names.append(i["name"])'
            '\n    msg = ", ".join(names)',
            [(5, "names")],
        ),
        (
            'def f(items):\n    names = [i["name"] for i in items]\n    _LOGGER.info("%s", ", ".join(sorted(names)))',
            [(3, "names")],
        ),
        ('msg = ", ".join(f["Name"] for f in fubs)', [(1, "f['Name']")]),
        ('msg = ", ".join(one_line(f["Name"]) for f in fubs)', []),
        ('def f(items):\n    names = [one_line(i["name"]) for i in items]\n    msg = ", ".join(names)', []),
        (
            'def f(n):\n    d = {"plan_name": n["name"]}'
            '\n    ir.async_create_issue(h, D, "x", translation_placeholders=d)',
            [(3, "d")],
        ),
        (
            'def f(n):\n    d = {"plan_name": n["name"]}'
            '\n    ir.async_create_issue(h, D, "x", translation_placeholders=one_line_values(d))',
            [],
        ),
        ('self.async_show_form(step_id="x", description_placeholders={"name": item["name"]})', [(1, "item['name']")]),
        ('ir.async_create_issue(hass, DOMAIN, "x", translation_placeholders={"plan": plan_name})', [(1, "plan_name")]),
        ('self.async_show_form(step_id="x", description_placeholders={"name": one_line(item["name"])})', []),
        ('def f(m):\n    label = one_line(m.get("name") or "M1")\n    _LOGGER.debug("%s", label)', []),
        ('def f(name):\n    msg = f"Plan {one_line(name)}"\n    _LOGGER.info(msg)', []),
        ('_LOGGER.info("Plan %s", self.api.function_plan_name(fid))', [(1, "self.api.function_plan_name(fid)")]),
        ('names = ", ".join(f"\'{function_plan_name(fid)}\'" for fid in ids)', [(1, "function_plan_name(fid)")]),
        ('_LOGGER.warning("Unknown IO %s %s", ext_name, identifier)', [(1, "ext_name"), (1, "identifier")]),
        ('_LOGGER.debug("%s %s", io["ext_name"], io["identifier"])', [(1, "io['ext_name']"), (1, "io['identifier']")]),
        ("label = f\"{io.get('ext_name')} {one_line(io['identifier'])}\"", [(1, "io.get('ext_name')")]),
        ("label = f\"{one_line(io['ext_name'])} {one_line(io['identifier'])}\"", []),
        ('self._attr_unique_id = f"comexio_{server_id}_{ext_name}_x"', []),
        ("total = fub_id + name_count", []),
        ("token = vanished_plan_token(fub_id, plan_name)", []),
    ],
)
def test_the_check_finds_a_raw_name(source: str, expected: list[tuple[int, str]]) -> None:
    assert raw_name_outputs(source) == expected


_PICK = 'def pick(fub_id, name):\n    _LOGGER.info("Picked %s", name)\n    return f"{fub_id}:{name}"'


def test_an_identity_function_keeps_its_f_strings_raw_but_not_its_log_calls() -> None:
    assert raw_name_outputs(_PICK, {"pick"}) == [(2, "name")]


def test_an_identity_function_still_checks_f_strings_it_outputs() -> None:
    source = 'def pick(fub_id, name):\n    _LOGGER.info(f"Picked {name}")\n    return f"{fub_id}:{name}"'
    assert raw_name_outputs(source, {"pick"}) == [(2, "name")]


def test_an_identity_function_still_checks_a_message_it_builds_first() -> None:
    source = 'def pick(fub_id, name):\n    msg = f"Picked {name}"\n    _LOGGER.info(msg)\n    return f"{fub_id}:{name}"'
    assert raw_name_outputs(source, {"pick"}) == [(3, "msg")]


def test_an_identity_exemption_covers_only_the_named_function() -> None:
    assert raw_name_outputs(_PICK, {"other"}) == [(2, "name"), (3, "name")]


def test_the_check_reads_the_whole_integration() -> None:
    assert COMPONENT / "services" / "backup.py" in SOURCE_FILES


@pytest.mark.parametrize(("file", "functions"), sorted(IDENTITY_FUNCTIONS.items()))
def test_every_identity_function_exists_in_its_file(file: str, functions: set[str]) -> None:
    """An exemption whose function was renamed, moved or removed would exempt nothing, or a new namesake."""
    defined = {
        node.name
        for node in ast.walk(ast.parse((COMPONENT / file).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert functions <= defined, functions - defined


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: path.relative_to(COMPONENT).as_posix())
def test_no_log_line_notification_or_message_gets_a_raw_comexio_name(path: Path) -> None:
    identity_functions = IDENTITY_FUNCTIONS.get(path.relative_to(COMPONENT).as_posix(), set())
    raw = raw_name_outputs(path.read_text(encoding="utf-8"), identity_functions)
    assert not raw, f"wrap these names in const.one_line before output: {raw}"


def test_a_plan_label_is_one_line_and_resolves_back_to_its_plan() -> None:
    """The selector label shows the name on one line; the "(ID n)" suffix still names the plan."""
    fub_data = {"7": {"Name": "Hall\nway", "Active": True}, "8": {"Name": "Hall\nway", "Active": False}}
    labels = [_plan_option_label(fid, fub) for fid, fub in fub_data.items()]
    assert labels == ["Hall way (ID 7)", "⏸ Hall way (ID 8)"]
    assert [_resolve_fub_id(label, fub_data) for label in labels] == [7, 8]


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Hall way", "Hall way"),
        (None, "None"),
        ("Hall\nway", r"Hall\nway"),
        ("Hall\tway", r"Hall\tway"),
        ("Hall  way", r"Hall \x20way"),
        (" Hall ", r"\x20Hall\x20"),
        ("Hall\\nway", r"Hall\\nway"),
        ("Hall\ud800", r"Hall\ud800"),
    ],
)
def test_distinct_one_line_escapes_what_one_line_would_hide(name: object, label: str) -> None:
    assert distinct_one_line(name) == label


def test_distinct_one_line_never_gives_two_names_one_label() -> None:
    """Also not a name that spells another one's escape: the backslash itself is escaped."""
    names = ["Hall way", "Hall\nway", "Hall\\nway", "Hall  way", "Hall \\x20way", "Hall way ", "Hall way"]
    assert len({distinct_one_line(name) for name in names}) == len(names)


def test_restore_and_snapshot_options_tell_apart_names_that_coincide_on_one_line() -> None:
    names = ["Hall\nway", "Hall way"]
    restore = _build_restore_options([(4, name, False, 1) for name in names])
    snapshots = _build_snapshot_options({"auto": [{"fub_id": 4, "slot": 0, "plan_name": name} for name in names]})
    for options in (restore, snapshots):
        assert len({option["label"] for option in options}) == 2
        assert all("\n" not in option["label"] for option in options)
    # The values are parsed back into the plan: they keep the raw name.
    assert {option["value"] for option in restore} == {"4:Hall\nway", "4:Hall way"}
    assert {option["value"] for option in snapshots} == {"4:auto:0:Hall\nway", "4:auto:0:Hall way"}


@pytest.mark.parametrize(
    ("ext_name", "unique_id"),
    [("Ext_01", "comexio_abc_ext_01_firmware_update"), ("EXT\n01", "comexio_abc_ext\n01_firmware_update")],
)
def test_the_firmware_update_unique_id_keeps_the_raw_extension_name(ext_name: str, unique_id: str) -> None:
    """An identity: one_line here would orphan (and the cleanup remove) every existing firmware update entity."""
    assert extension_firmware_update_unique_id("abc", ext_name) == unique_id


def test_an_io_keeps_its_raw_names_in_its_identities_but_shows_them_on_one_line() -> None:
    """unique_id and Web-IO command name are identities: one_line there would orphan entities and commands."""
    io = {"ext_name": "EXT\n01", "identifier": "I\t1", "quickbus": None}
    assert io_unique_id("IOSrv1", io) == "comexio_iosrv1_ext\n01_i\t1"
    assert io_unique_id("IOSrv1", {"ext_name": "BASE", "identifier": "I1"}) == "comexio_iosrv1_base_i1"
    assert io_command_name(io) == "HA IO EXT\n01 I\t1"
    assert io_plan_label(io) == "EXT 01 I 1"


def test_a_quickbus_io_label_is_one_line() -> None:
    io = {"ext_name": "BASE", "identifier": "PIR\t1", "quickbus": {"device_id": 3, "name": "BM\nDiele"}}
    assert io_plan_label(io) == "QB3 BM Diele PIR 1"
