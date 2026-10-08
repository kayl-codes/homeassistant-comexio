"""Every reason a repair flow aborts with has a translation, otherwise HA shows the raw reason key.

HA looks an abort reason up under `issues.<translation_key>.fix_flow.abort.<reason>` of the issue the
flow fixes. The reasons are read from repairs.py (every `self.async_abort(reason=...)` a flow step can
reach through `self.` calls and form steps), so a new abort is checked without touching this file.
Which step a fixable issue lands in is read from the source as well: the issue_id it is raised with
is run through the if-chain of async_step_init.
"""

import ast
from collections.abc import Iterator
from functools import cache
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[2] / "custom_components" / "comexio"
TRANSLATION_FILES = ["strings.json", "en.json", "de.json", "fr.json", "es.json"]

# translation_key of each fixable issue → the flow step async_step_init routes its issue_id to.
ENTRY_STEP_BY_ISSUE = {
    "sync_mismatch": "async_step_select_action",
    "missing_webio_class": "async_step_select_action",
    "entity_id_mismatch": "async_step_entity_id_fix",
    "statistics_orphaned": "async_step_statistics_cleanup",
    "uninstall_cleanup": "async_step_uninstall_cleanup",
    "knx_prerelease_cleanup": "async_step_uninstall_cleanup",
    "knx_dpt_ambiguous": "async_step_knx_dpt_suffix",
    "orphaned_plan_backups": "async_step_orphaned_backups",
    "function_plan_stopped": "async_step_function_plan_stopped",
    "function_plan_stopped_user": "async_step_function_plan_stopped",
    "function_plan_auto_start_suspended": "async_step_function_plan_stopped",
}

# Call-site arguments that only hand an already raised issue's own translation_key back (the repair
# flow re-raising the issue it fixes), so they cannot add a new key: (file, calling function, argument).
PASS_THROUGH_KEYS = {("repairs.py", "_reraise_cleanup_issue", "translation_key")}

ISSUE_CREATORS = {"async_create_issue", "create_issue"}
ISSUE_ID = "self.issue_id"

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
# Known start of an issue_id, and whether that is the whole issue_id.
Prefix = tuple[str, bool]


@cache
def _trees() -> dict[str, ast.Module]:
    return {
        path.relative_to(COMPONENT).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in COMPONENT.rglob("*.py")
    }


@cache
def _parents(file_name: str) -> dict[ast.AST, ast.AST]:
    tree = _trees()[file_name]
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _enclosing_function(file_name: str, node: ast.AST) -> FunctionNode | None:
    parent = _parents(file_name).get(node)
    while parent is not None and not isinstance(parent, FunctionNode):
        parent = _parents(file_name).get(parent)
    return parent


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, to resolve `reason=ABORT_...`."""
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


@cache
def _all_constants() -> dict[str, str]:
    constants: dict[str, str] = {}
    for file_name, tree in _trees().items():
        for name, value in _module_constants(tree).items():
            assert constants.get(name, value) == value, f"{file_name}: {name} has another value in another module"
            constants[name] = value
    return constants


def _reason_values(node: ast.expr, constants: dict[str, str]) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in constants:
        return {constants[node.id]}
    if isinstance(node, ast.IfExp):
        return _reason_values(node.body, constants) | _reason_values(node.orelse, constants)
    raise AssertionError(
        f"line {node.lineno}: a reason or translation_key the test cannot resolve: {ast.unparse(node)}"
    )


def _string(node: ast.expr) -> str:
    """A string literal, a constant or an f-string of both, as used in the issue_id checks."""
    if isinstance(node, ast.JoinedStr):
        return "".join(_string(part.value if isinstance(part, ast.FormattedValue) else part) for part in node.values)
    (value,) = _reason_values(node, _all_constants())
    return value


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return node.func.id if isinstance(node.func, ast.Name) else None


def _is_self_call(node: ast.AST, name: str | None = None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and (name is None or node.func.attr == name)
    )


def _function_def(file_name: str, name: str) -> tuple[str, FunctionNode]:
    """The one function or method called `name`: in `file_name`, or else anywhere in the integration."""
    for scope in ([file_name], list(_trees())):
        found = [
            (file, node)
            for file in scope
            for node in ast.walk(_trees()[file])
            if isinstance(node, FunctionNode) and node.name == name
        ]
        if found:
            assert len(found) == 1, f"{name} is defined more than once, the test cannot tell which one is called"
            return found[0]
    raise AssertionError(f"{file_name}: no definition of {name}")


def _flow_class() -> ast.ClassDef:
    tree = _trees()["repairs.py"]
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ComexioRepairFlow")


def _flow_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per method of the repair flow: the abort reasons it returns and the methods it leads to."""
    constants = _module_constants(_trees()["repairs.py"])
    reasons: dict[str, set[str]] = {}
    edges: dict[str, set[str]] = {}
    for method in _flow_class().body:
        if not isinstance(method, FunctionNode):
            continue
        reasons[method.name] = set()
        edges[method.name] = set()
        for node in ast.walk(method):
            if not _is_self_call(node):
                continue
            assert isinstance(node, ast.Call)
            assert isinstance(node.func, ast.Attribute)
            edges[method.name].add(node.func.attr)
            for keyword in node.keywords:
                if node.func.attr == "async_abort" and keyword.arg == "reason":
                    reasons[method.name] |= _reason_values(keyword.value, constants)
                elif node.func.attr == "async_show_form" and keyword.arg == "step_id":
                    # Submitting the form runs async_step_<step_id>.
                    edges[method.name] |= {f"async_step_{step}" for step in _reason_values(keyword.value, constants)}
    return reasons, edges


def _reachable_reasons(entry_step: str) -> set[str]:
    reasons, edges = _flow_graph()
    seen: set[str] = set()
    todo = [entry_step]
    while todo:
        method = todo.pop()
        if method in seen or method not in edges:
            continue
        seen.add(method)
        todo.extend(edges[method])
    return set().union(*(reasons[method] for method in seen))


def _issue_id_matches(test: ast.expr, issue_id: str) -> bool:
    """Evaluate one routing condition of async_step_init (`"x" in self.issue_id` or `.startswith(...)`)."""
    if isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.In) and len(test.ops) == 1:
        assert ast.unparse(test.comparators[0]) == ISSUE_ID, ast.unparse(test)
        return _string(test.left) in issue_id
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "startswith":
        assert ast.unparse(test.func.value) == ISSUE_ID, ast.unparse(test)
        (arg,) = test.args
        prefixes = tuple(_string(elt) for elt in arg.elts) if isinstance(arg, ast.Tuple) else (_string(arg),)
        return issue_id.startswith(prefixes)
    raise AssertionError(f"line {test.lineno}: a routing condition the test cannot evaluate: {ast.unparse(test)}")


def _returned_step(statement: ast.stmt) -> str:
    assert isinstance(statement, ast.Return), ast.unparse(statement)
    call = statement.value.value if isinstance(statement.value, ast.Await) else statement.value
    assert _is_self_call(call), ast.unparse(statement)
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    return call.func.attr


def _routed_step(issue_id: str) -> str:
    """The step async_step_init routes an issue_id to, following its if-chain in source order."""
    init = next(n for n in _flow_class().body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_step_init")
    for statement in init.body:
        if isinstance(statement, ast.If):
            assert not statement.orelse, f"line {statement.lineno}: elif/else routing is not evaluated by this test"
            if _issue_id_matches(statement.test, issue_id):
                return _returned_step(statement.body[-1])
        elif isinstance(statement, ast.Return):
            return _returned_step(statement)
        else:
            # Only logging may sit between the routing branches; anything else could route unseen.
            assert isinstance(statement, ast.Expr), f"line {statement.lineno}: a statement the test cannot follow"
    raise AssertionError(f"async_step_init routes no step for {issue_id}")


def _fix_flow_aborts(file_name: str) -> dict[str, set[str]]:
    """Per issue with a fix flow: the abort reasons that have a non-empty text (an empty one shows nothing)."""
    data = json.loads((COMPONENT / "translations" / file_name).read_text(encoding="utf-8"))
    return {
        key: {
            reason
            for reason, text in issue["fix_flow"].get("abort", {}).items()
            if isinstance(text, str) and text.strip()
        }
        for key, issue in data["issues"].items()
        if "fix_flow" in issue
    }


def _create_issue_calls() -> Iterator[tuple[str, FunctionNode, ast.Call]]:
    """(file, enclosing function, call) of every call that raises a repair issue."""
    for file_name, tree in _trees().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in ISSUE_CREATORS and all(kw.arg != "is_fixable" for kw in node.keywords):
                continue
            function = _enclosing_function(file_name, node)
            assert function is not None, f"{file_name}:{node.lineno}: an issue raised outside a function"
            yield file_name, function, node


def _method_return_keys(file_name: str, name: str) -> set[str]:
    """The translation keys a `self.<name>(...)` helper returns."""
    _, method = _function_def(file_name, name)
    returns = [n.value for n in ast.walk(method) if isinstance(n, ast.Return) and n.value is not None]
    assert returns, f"{file_name}: {name} returns nothing the test can read"
    return set().union(*(_reason_values(value, _all_constants()) for value in returns))


def _argument(call: ast.Call, function: FunctionNode, parameter: str) -> ast.expr | None:
    """The argument a call passes for `parameter` of `function` (a method: `self` is not passed)."""
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if parameter in keywords:
        return keywords[parameter]
    names = [arg.arg for arg in function.args.args if arg.arg != "self"]
    index = names.index(parameter)
    return call.args[index] if index < len(call.args) else None


def _is_pass_through(file_name: str, call: ast.Call, argument: ast.expr) -> bool:
    caller = _enclosing_function(file_name, call)
    return (file_name, caller.name if caller else "", ast.unparse(argument)) in PASS_THROUGH_KEYS


def _parameter_keys(function: FunctionNode, parameter: str) -> set[str]:
    """The translation keys the callers of `function` pass for `parameter`."""
    keys: set[str] = set()
    call_sites = 0
    for file_name, tree in _trees().items():
        for node in ast.walk(tree):
            if _call_name(node) != function.name:
                continue
            assert isinstance(node, ast.Call)
            call_sites += 1
            argument = _argument(node, function, parameter)
            assert argument is not None, f"{file_name}:{node.lineno}: no {parameter} passed to {function.name}"
            if not _is_pass_through(file_name, node, argument):
                keys |= _reason_values(argument, _all_constants())
    assert call_sites, f"{function.name} raises a fixable issue but has no caller the test can find"
    return keys


def _translation_keys(file_name: str, function: FunctionNode, value: ast.expr) -> tuple[set[str], str | None]:
    """The keys a translation_key expression can take, and the parameter name if it is passed in."""
    if _is_self_call(value):
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Attribute)
        return _method_return_keys(file_name, value.func.attr), None
    parameters = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
    if isinstance(value, ast.Name) and value.id in parameters:
        return _parameter_keys(function, value.id), value.id
    return _reason_values(value, _all_constants()), None


def _local_value(function: FunctionNode, name: str) -> ast.expr | None:
    """The value of the one `name = ...` assignment in `function`, if there is one."""
    values = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == [name]
    ]
    assert len(values) <= 1, f"{function.name}: {name} is assigned more than once"
    return values[0] if values else None


def _joined_prefix(file_name: str, function: FunctionNode, node: ast.JoinedStr, bindings: dict[str, str]) -> Prefix:
    text = ""
    for part in node.values:
        value, complete = _issue_id_prefix(
            file_name, function, part.value if isinstance(part, ast.FormattedValue) else part, bindings
        )
        text += value
        if not complete:
            return text, False
    return text, True


def _issue_id_prefix(file_name: str, function: FunctionNode, node: ast.expr, bindings: dict[str, str]) -> Prefix:
    """The known start of an issue_id expression; stops at the first part only known at runtime (server_id…)."""
    if isinstance(node, ast.JoinedStr):
        return _joined_prefix(file_name, function, node, bindings)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.Name):
        if node.id in bindings:
            return bindings[node.id], True
        if node.id in _all_constants():
            return _all_constants()[node.id], True
        value = _local_value(function, node.id)
        return _issue_id_prefix(file_name, function, value, bindings) if value is not None else ("", False)
    name = _call_name(node)
    if name is not None:
        helper_file, helper = _function_def(file_name, name)
        (returned,) = [n.value for n in ast.walk(helper) if isinstance(n, ast.Return) and n.value is not None]
        return _issue_id_prefix(helper_file, helper, returned, {})
    return "", False


def _fixable_issue_prefixes() -> dict[str, set[str]]:
    """translation_key of every `is_fixable=True` issue the integration raises → its issue_id prefixes."""
    prefixes: dict[str, set[str]] = {}
    for file_name, function, call in _create_issue_calls():
        where = f"{file_name}:{call.lineno}"
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert None not in keywords, f"{where}: **kwargs hide is_fixable/translation_key from the test"
        fixable = keywords.get("is_fixable")
        not_literal = f"{where}: is_fixable must be a literal True/False for the test to know whether a flow fixes it"
        assert isinstance(fixable, ast.Constant), not_literal
        assert isinstance(fixable.value, bool), not_literal
        if not fixable.value:
            continue
        assert "translation_key" in keywords, f"{where}: a fixable issue without translation_key"
        issue_id = call.args[2] if len(call.args) > 2 else keywords.get("issue_id")
        assert issue_id is not None, f"{where}: a fixable issue without an issue_id the test can read"
        keys, parameter = _translation_keys(file_name, function, keywords["translation_key"])
        for key in keys:
            prefix, _ = _issue_id_prefix(file_name, function, issue_id, {parameter: key} if parameter else {})
            assert prefix, f"{where}: the issue_id has no fixed start to route by: {ast.unparse(issue_id)}"
            prefixes.setdefault(key, set()).add(prefix)
    return prefixes


def test_every_fixable_issue_is_routed_in_this_test() -> None:
    """A new fixable issue needs its entry step here, or its abort reasons go unchecked."""
    assert sorted(_fixable_issue_prefixes()) == sorted(ENTRY_STEP_BY_ISSUE)
    assert sorted(_fix_flow_aborts("strings.json")) == sorted(ENTRY_STEP_BY_ISSUE)


def test_async_step_init_routes_every_fixable_issue_to_its_step() -> None:
    """The issue_id each issue is raised with reaches its step; swapped branches or a renamed issue_id would
    send it to another flow, though the set of steps stays the same."""
    routed = {
        (key, prefix): _routed_step(f"{prefix}srv1")
        for key, prefixes in _fixable_issue_prefixes().items()
        for prefix in prefixes
    }

    assert routed == {(key, prefix): ENTRY_STEP_BY_ISSUE[key] for key, prefix in routed}


def test_the_routing_table_matches_async_step_init() -> None:
    """A renamed or new routing branch in async_step_init must show up here; init itself never aborts."""
    reasons, edges = _flow_graph()

    assert edges["async_step_init"] == set(ENTRY_STEP_BY_ISSUE.values())
    assert reasons["async_step_init"] == set()
    assert all(_reachable_reasons(step) for step in ENTRY_STEP_BY_ISSUE.values())


def test_the_flow_graph_finds_the_known_aborts() -> None:
    """Guards against a discovery that silently finds nothing (a renamed class or step)."""
    assert {"entry_not_found", "sync_failed", "already_in_sync"} <= _reachable_reasons("async_step_select_action")
    assert "sync_running" in _reachable_reasons("async_step_uninstall_cleanup")


def test_computed_translation_keys_and_issue_ids_are_discovered() -> None:
    """Guards the helper, caller and local-variable lookups against silently finding nothing."""
    prefixes = _fixable_issue_prefixes()

    assert prefixes["uninstall_cleanup"] == {"uninstall_cleanup_"}
    assert prefixes["knx_prerelease_cleanup"] == {"knx_prerelease_cleanup_"}
    assert prefixes["missing_webio_class"] == {"sync_mismatch_"}
    assert prefixes["function_plan_auto_start_suspended"] == {"function_plan_stopped_"}
    assert prefixes["orphaned_plan_backups"] == {"orphaned_plan_backups_"}


@pytest.mark.parametrize(("file_name", "function_name", "argument"), sorted(PASS_THROUGH_KEYS))
def test_every_pass_through_entry_still_matches_a_call(file_name: str, function_name: str, argument: str) -> None:
    """A renamed re-raise path must not leave a stale entry that would excuse another call later."""
    _, function = _function_def(file_name, function_name)

    assert any(
        isinstance(node, ast.Call) and argument in {ast.unparse(arg) for arg in node.args}
        for node in ast.walk(function)
    )


@pytest.mark.parametrize("file_name", TRANSLATION_FILES)
def test_every_repair_abort_reason_has_a_translation(file_name: str) -> None:
    translated = _fix_flow_aborts(file_name)
    missing = {
        issue: sorted(_reachable_reasons(step) - translated.get(issue, set()))
        for issue, step in ENTRY_STEP_BY_ISSUE.items()
    }

    assert {issue: reasons for issue, reasons in missing.items() if reasons} == {}
