"""Every reason a repair flow aborts with has a translation, otherwise HA shows the raw reason key.

HA looks an abort reason up under `issues.<translation_key>.fix_flow.abort.<reason>` of the issue the
flow fixes. The reasons are read from repairs.py (every `self.async_abort(reason=...)` a flow step can
reach through `self.` calls and form steps), so a new abort is checked without touching this file.
"""

import ast
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[2] / "custom_components" / "comexio"
TRANSLATION_FILES = ["strings.json", "en.json", "de.json", "fr.json", "es.json"]

# translation_key of each fixable issue → the flow step async_step_init routes it to (by issue_id prefix).
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

# Fixable issues raised with a computed translation_key: (file, expression) → the keys it can take.
DYNAMIC_FIXABLE_KEYS = {
    ("coordinator.py", "translation_key"): {"uninstall_cleanup", "knx_prerelease_cleanup"},
    ("plan_watchdog.py", "self._issue_translation_key(fub_id)"): {
        "function_plan_stopped",
        "function_plan_stopped_user",
        "function_plan_auto_start_suspended",
    },
}


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, to resolve `reason=ABORT_...`."""
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


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


def _is_self_call(node: ast.AST, name: str | None = None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and (name is None or node.func.attr == name)
    )


def _flow_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per method of the repair flow: the abort reasons it returns and the methods it leads to."""
    tree = ast.parse((COMPONENT / "repairs.py").read_text(encoding="utf-8"))
    constants = _module_constants(tree)
    flow = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ComexioRepairFlow")
    reasons: dict[str, set[str]] = {}
    edges: dict[str, set[str]] = {}
    for method in flow.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
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


def _fix_flow_aborts(file_name: str) -> dict[str, set[str]]:
    data = json.loads((COMPONENT / "translations" / file_name).read_text(encoding="utf-8"))
    return {
        key: set(issue["fix_flow"].get("abort", {})) for key, issue in data["issues"].items() if "fix_flow" in issue
    }


def _fixable_issue_keys() -> set[str]:
    """translation_key of every `is_fixable=True` issue the integration raises."""
    trees = {
        path.relative_to(COMPONENT).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in COMPONENT.rglob("*.py")
    }
    constants: dict[str, str] = {}
    for tree in trees.values():
        constants |= _module_constants(tree)
    keys: set[str] = set()
    for file_name, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            fixable = keywords.get("is_fixable")
            if not (isinstance(fixable, ast.Constant) and fixable.value is True) or "translation_key" not in keywords:
                continue
            value = keywords["translation_key"]
            dynamic = (file_name, ast.unparse(value))
            if dynamic in DYNAMIC_FIXABLE_KEYS:
                keys |= DYNAMIC_FIXABLE_KEYS[dynamic]
            else:
                keys |= _reason_values(value, constants)
    return keys


def test_every_fixable_issue_is_routed_in_this_test() -> None:
    """A new fixable issue needs its entry step here, or its abort reasons go unchecked."""
    assert sorted(_fixable_issue_keys()) == sorted(ENTRY_STEP_BY_ISSUE)
    assert sorted(_fix_flow_aborts("strings.json")) == sorted(ENTRY_STEP_BY_ISSUE)


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


@pytest.mark.parametrize("file_name", TRANSLATION_FILES)
def test_every_repair_abort_reason_has_a_translation(file_name: str) -> None:
    translated = _fix_flow_aborts(file_name)
    missing = {
        issue: sorted(_reachable_reasons(step) - translated.get(issue, set()))
        for issue, step in ENTRY_STEP_BY_ISSUE.items()
    }

    assert {issue: reasons for issue, reasons in missing.items() if reasons} == {}
