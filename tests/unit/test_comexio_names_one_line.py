"""Names from Comexio reach a log line, a notification or a message only through const.one_line.

A plan, marker, IO or KNX name is edited in the Comexio admin and can hold line breaks or control characters,
which split a log entry or break a notification's layout. The integration's code is read for every place that turns a
Comexio name - a variable in NAME_VARIABLES, or a mapping read like `item["name"]` / `fub.get("Name")` - into text: a
value in an f-string, a string built with `+`, `%` or `.format()`, and an argument of a `_LOGGER` call or of
`persistent_notification.async_create`, directly or through `str()` / `repr()`. So a message built first and output
later is caught as well as a new raw log argument, without touching this file.

Text that identifies rather than shows keeps the raw name: a `notification_id` or a `value` (keyword or dict key), an
f-string assigned to a variable in IDENTITY_TARGETS, and the f-strings of the functions in IDENTITY_FUNCTIONS (their
output calls are still checked). A name held in a variable named otherwise, or joined into a list first, is not
followed; the review checklist covers that case.
"""

import ast
from pathlib import Path

import pytest

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
}
# Mapping keys that hold a Comexio name: item["name"], fub.get("Name").
NAME_KEYS = {"name", "Name"}
# Keyword arguments and dict keys that identify rather than show: they keep the raw name.
IDENTITY_KEYS = {"notification_id", "value"}
# Variables that hold an identity built from a name: the Web-IO command name Comexio must report back. Building
# them keeps the raw name; they are name variables too, so showing them still needs one_line.
IDENTITY_TARGETS = {"expected_cmd_name"}
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
    },
    "function_plan_block_keys.py": {"_unresolved_element"},  # the element name written into the plan
    "function_plan_block_settings.py": {"block_settings_form"},  # the form field names Comexio's editor sends
    "select.py": {"_plan_option_label"},  # a select option, parsed back into the plan's ID
    "services/_context.py": {"format_plan_label"},  # the same option format
    "coordinator.py": {
        "async_newest_backup_field",  # the restore action's snapshot value
        "_build_ha_audit_map",  # the command names the audit compares with Comexio's
    },
}
STRING_WRAPPERS = {"str", "repr"}
SOURCE_FILES = sorted(COMPONENT.rglob("*.py"))


def _is_name(node: ast.AST) -> bool:
    """A name variable, or a name read from a mapping: item["name"] or fub.get("Name")."""
    if isinstance(node, ast.Name):
        return node.id in NAME_VARIABLES
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in NAME_KEYS
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
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


def _shown_name(value: ast.expr) -> ast.expr | None:
    """The name variable an output argument hands over as it is: itself, or wrapped in str()/repr()."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in STRING_WRAPPERS:
        value = value.args[0] if value.args else value
    return value if _is_name(value) else None


def _is_text(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _is_identity(node: ast.AST, parents: dict[ast.AST, ast.AST], identity_functions: set[str]) -> bool:
    """True for text passed as an identity key, or built inside an IDENTITY_FUNCTIONS function but not for output."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword) and parent.arg in IDENTITY_KEYS:
        return True
    if isinstance(parent, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in IDENTITY_TARGETS for target in parent.targets
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


def _raw_in_text(node: ast.AST) -> list[ast.expr]:
    """The names an f-string, a `+` / `%` string build or a `.format()` call puts into its text unfiltered."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return [arg for arg in [*node.args, *(keyword.value for keyword in node.keywords)] if _is_name(arg)]
    if isinstance(node, ast.JoinedStr):
        return [part.value for part in node.values if isinstance(part, ast.FormattedValue) and _is_name(part.value)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        if isinstance(node.op, ast.Mod) and _is_text(node.left):
            operands = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return [operand for operand in operands if _is_name(operand)]
        if isinstance(node.op, ast.Add) and (_is_text(node.left) or _is_text(node.right)):
            return [operand for operand in (node.left, node.right) if _is_name(operand)]
    return []


def _raw_in_output(call: ast.Call) -> list[ast.expr]:
    shown = [keyword.value for keyword in call.keywords if keyword.arg not in IDENTITY_KEYS]
    return [name for value in [*call.args, *shown] if (name := _shown_name(value)) is not None]


def raw_name_outputs(source: str, identity_functions: set[str] | None = None) -> list[tuple[int, str]]:
    """(line, source) of every name turned into text or output without one_line."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_output_call(node):
            found.extend((name.lineno, ast.unparse(name)) for name in _raw_in_output(node))
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
