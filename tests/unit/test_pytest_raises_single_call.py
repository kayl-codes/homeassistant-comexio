"""A `pytest.raises` block makes exactly one call, so only the tested call can satisfy it (SonarCloud S5778).

With a second call in the block - a second statement, but also a nested call like `check(build(x))` - an unexpected
exception from the other call satisfies the expected one and hides a failure of the tested call. Ruff's PT012 only
counts statements; this test counts the calls, so the nested form is caught as well. Build the arguments before the
`with` and keep only the tested call inside it.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parents[1]


def _is_raises_call(node: ast.AST) -> bool:
    """`pytest.raises(...)` or, after `from pytest import raises`, `raises(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "raises") or (
        isinstance(func, ast.Name) and func.id == "raises"
    )


def _raises_blocks(source: str) -> list[tuple[int, int, str]]:
    """Return (line, call count, first statement) of each `with` block that expects a raise.

    A with-item counts when a raises call appears anywhere in it, so a conditional
    `pytest.raises(E) if fails else nullcontext()` is checked too.
    """
    blocks = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        if not any(_is_raises_call(sub) for item in node.items for sub in ast.walk(item.context_expr)):
            continue
        calls = [call for stmt in node.body for call in ast.walk(stmt) if isinstance(call, ast.Call)]
        blocks.append((node.lineno, len(calls), ast.unparse(node.body[0])))
    return blocks


def _extra_call_blocks(source: str) -> list[tuple[int, str]]:
    return [(line, stmt) for line, calls, stmt in _raises_blocks(source) if calls > 1]


def test_detector_flags_nested_and_sequential_calls() -> None:
    nested = "with pytest.raises(ValueError):\n    check(build(1))\n"
    sequential = "with pytest.raises(ValueError):\n    x = build(1)\n    check(x)\n"
    bare = "with raises(ValueError):\n    check(build(1))\n"
    conditional = "with pytest.raises(ValueError) if fails else nullcontext():\n    check(build(1))\n"
    in_async = "async def t():\n    async with pytest.raises(ValueError):\n        await check(build(1))\n"
    single = "x = build(1)\nwith pytest.raises(ValueError):\n    check(x)\n"
    other_context = "with patch('a.b'):\n    check(build(1))\n"

    assert _extra_call_blocks(nested) == [(1, "check(build(1))")]
    assert _extra_call_blocks(sequential) == [(1, "x = build(1)")]
    assert _extra_call_blocks(bare) == [(1, "check(build(1))")]
    assert _extra_call_blocks(conditional) == [(1, "check(build(1))")]
    assert _extra_call_blocks(in_async) == [(2, "await check(build(1))")]
    assert not _extra_call_blocks(single)
    assert not _extra_call_blocks(other_context)


def test_every_pytest_raises_block_makes_one_call() -> None:
    files = sorted(TESTS.rglob("test_*.py"))
    blocks = [(path, block) for path in files for block in _raises_blocks(path.read_text(encoding="utf-8"))]
    offenders = [
        f"{path.relative_to(TESTS.parent)}:{line}: {stmt}" for path, (line, calls, stmt) in blocks if calls > 1
    ]

    # The scan must see the tests it guards, or a moved directory turns it vacuously green.
    assert files
    assert blocks
    assert not offenders, "pytest.raises block with more than one call:\n" + "\n".join(offenders)
