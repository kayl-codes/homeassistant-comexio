"""A `pytest.raises` block makes exactly one call, so only the tested call can satisfy it (SonarCloud S5778).

With a second call in the block - a second statement, but also a nested call like `check(build(x))` - an unexpected
exception from the other call satisfies the expected one and hides a failure of the tested call. Ruff's PT012 only
counts statements; this test counts the calls, so the nested form is caught as well. Build the arguments before the
`with` and keep only the tested call inside it.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).parents[1]


def _raises_blocks_with_extra_calls(source: str) -> list[tuple[int, str]]:
    """Return (line, first statement) of each `pytest.raises` block that makes more than one call."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        is_raises = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "raises"
            for item in node.items
        )
        calls = [call for stmt in node.body for call in ast.walk(stmt) if isinstance(call, ast.Call)]
        if is_raises and len(calls) > 1:
            found.append((node.lineno, ast.unparse(node.body[0])))
    return found


def test_detector_flags_nested_and_sequential_calls() -> None:
    nested = "with pytest.raises(ValueError):\n    check(build(1))\n"
    sequential = "with pytest.raises(ValueError):\n    x = build(1)\n    check(x)\n"
    single = "x = build(1)\nwith pytest.raises(ValueError):\n    check(x)\n"
    other_context = "with patch('a.b'):\n    check(build(1))\n"

    assert _raises_blocks_with_extra_calls(nested) == [(1, "check(build(1))")]
    assert _raises_blocks_with_extra_calls(sequential) == [(1, "x = build(1)")]
    assert not _raises_blocks_with_extra_calls(single)
    assert not _raises_blocks_with_extra_calls(other_context)


def test_every_pytest_raises_block_makes_one_call() -> None:
    offenders = [
        f"{path.relative_to(TESTS.parent)}:{line}: {stmt}"
        for path in sorted(TESTS.rglob("test_*.py"))
        for line, stmt in _raises_blocks_with_extra_calls(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "pytest.raises block with more than one call:\n" + "\n".join(offenders)
