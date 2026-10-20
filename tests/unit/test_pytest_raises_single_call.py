"""A `pytest.raises` block makes exactly one call, so only the tested call can satisfy it (SonarCloud S5778).

With a second call in the block - a second statement, but also a nested call like `check(build(x))` or a context
manager entered after `pytest.raises` in the same `with` - an unexpected exception from the other call satisfies the
expected one and hides a failure of the tested call. Ruff's PT012 only counts statements; this test counts the calls,
so the nested form is caught as well. Build the arguments before the `with` and keep only the tested call inside it.

A function or lambda defined in the block is not run by defining it, so the calls in its body do not count (its
decorators, defaults and annotations do); a lambda passed as a callback runs inside the tested call and is part of
it. A generator expression's calls do count - stricter than needed, but it fails loudly. Not followed: a
`pytest.raises(...)` bound to a name or a parametrize argument first and entered as `with expectation:`, and a local
function called in the block - the review checklist covers those.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parents[1]
# pytest's default python_files patterns.
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
_DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _raises_names(tree: ast.Module) -> set[str]:
    """Bare names `pytest.raises` is bound to: `raises`, plus any `from pytest import raises as <alias>`."""
    names = {"raises"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pytest":
            names.update(alias.asname for alias in node.names if alias.name == "raises" and alias.asname)
    return names


def _is_raises_call(node: ast.AST, raises_names: set[str]) -> bool:
    """`pytest.raises(...)` (any module alias) or a bare name bound to it."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "raises") or (
        isinstance(func, ast.Name) and func.id in raises_names
    )


def _executed_calls(node: ast.AST) -> int:
    """Count the calls run when `node` runs, skipping the bodies of functions and lambdas it only defines.

    Their decorators, default values and annotations do run at definition, and a lambda called on the spot runs
    its body.
    """
    if isinstance(node, _DEFERRED):
        run_now = [*node.args.defaults, *(default for default in node.args.kw_defaults if default)]
        applied = 0
        if not isinstance(node, ast.Lambda):
            args = node.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            annotations = [param.annotation for param in params if param and param.annotation]
            run_now += [*node.decorator_list, *annotations, *([node.returns] if node.returns else [])]
            applied = len(node.decorator_list)  # applying a decorator is a call too
        return applied + sum(_executed_calls(expr) for expr in run_now)
    calls = sum(_executed_calls(child) for child in ast.iter_child_nodes(node))
    if isinstance(node, ast.Call):
        calls += 1
        if isinstance(node.func, ast.Lambda):
            calls += _executed_calls(node.func.body)
    return calls


def _raises_blocks(source: str) -> list[tuple[int, int, str]]:
    """Return (line, call count, first statement) of each `with` block that expects a raise.

    A with-item counts when a raises call appears anywhere in it, so a conditional
    `pytest.raises(E) if fails else nullcontext()` is checked too. The count covers the body and the with-items
    entered after that item, since pytest.raises catches what they raise as well.
    """
    tree = ast.parse(source)
    raises_names = _raises_names(tree)
    blocks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        raises_at = next(
            (
                index
                for index, item in enumerate(node.items)
                if any(_is_raises_call(sub, raises_names) for sub in ast.walk(item.context_expr))
            ),
            None,
        )
        if raises_at is None:
            continue
        later_items = node.items[raises_at + 1 :]
        calls = sum(_executed_calls(item.context_expr) for item in later_items)
        calls += sum(_executed_calls(stmt) for stmt in node.body)
        blocks.append((node.lineno, calls, ast.unparse(node.body[0])))
    return blocks


def _extra_call_blocks(source: str) -> list[tuple[int, str]]:
    return [(line, stmt) for line, calls, stmt in _raises_blocks(source) if calls > 1]


def test_detector_flags_nested_and_sequential_calls() -> None:
    nested = "with pytest.raises(ValueError):\n    check(build(1))\n"
    sequential = "with pytest.raises(ValueError):\n    x = build(1)\n    check(x)\n"
    bare = "with raises(ValueError):\n    check(build(1))\n"
    aliased = "from pytest import raises as expect\nwith expect(ValueError):\n    check(build(1))\n"
    conditional = "with pytest.raises(ValueError) if fails else nullcontext():\n    check(build(1))\n"
    later_item = "with pytest.raises(ValueError), pause():\n    check(x)\n"
    in_async = "async def t():\n    async with pytest.raises(ValueError):\n        await check(build(1))\n"
    single = "x = build(1)\nwith pytest.raises(ValueError):\n    check(x)\n"
    earlier_item = "with pause(), pytest.raises(ValueError):\n    check(x)\n"
    deferred = "with pytest.raises(ValueError):\n    check(lambda: build(1))\n"
    called_lambda = "with pytest.raises(ValueError):\n    (lambda: build(1))()\n"
    default_arg = "with pytest.raises(ValueError):\n    def g(x=build(1)):\n        pass\n    check(g)\n"
    decorated = "with pytest.raises(ValueError):\n    @wrap(1)\n    def g():\n        pass\n"
    annotated = "with pytest.raises(ValueError):\n    def g(x: kind(1)) -> kind(2):\n        pass\n"
    other_context = "with patch('a.b'):\n    check(build(1))\n"

    assert _extra_call_blocks(nested) == [(1, "check(build(1))")]
    assert _extra_call_blocks(sequential) == [(1, "x = build(1)")]
    assert _extra_call_blocks(bare) == [(1, "check(build(1))")]
    assert _extra_call_blocks(aliased) == [(2, "check(build(1))")]
    assert _extra_call_blocks(conditional) == [(1, "check(build(1))")]
    assert _extra_call_blocks(later_item) == [(1, "check(x)")]
    assert _extra_call_blocks(in_async) == [(2, "await check(build(1))")]
    assert _extra_call_blocks(called_lambda) == [(1, "(lambda: build(1))()")]
    assert [line for line, _ in _extra_call_blocks(default_arg)] == [1]
    assert [line for line, _ in _extra_call_blocks(decorated)] == [1]
    assert [line for line, _ in _extra_call_blocks(annotated)] == [1]
    assert not _extra_call_blocks(single)
    assert not _extra_call_blocks(earlier_item)
    assert not _extra_call_blocks(deferred)
    assert not _extra_call_blocks(other_context)


def test_every_pytest_raises_block_makes_one_call() -> None:
    files = sorted({path for pattern in TEST_FILE_PATTERNS for path in TESTS.rglob(pattern)})
    blocks = [(path, block) for path in files for block in _raises_blocks(path.read_text(encoding="utf-8"))]
    offenders = [
        f"{path.relative_to(TESTS.parent)}:{line}: {stmt}" for path, (line, calls, stmt) in blocks if calls > 1
    ]

    # The scan must see the tests it guards, or a moved directory turns it vacuously green.
    assert files
    assert blocks
    assert not offenders, "pytest.raises block with more than one call:\n" + "\n".join(offenders)
