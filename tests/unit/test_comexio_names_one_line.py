"""Names from Comexio reach a log line or a notification only through const.one_line.

A plan, marker or IO name is edited in the Comexio admin and can hold line breaks or control characters, which
split a log entry or break a notification's layout. The integration's code is read for every `_LOGGER` call and
every `persistent_notification.async_create` that is handed a variable `name` or `plan_name` directly, as an
argument or as a value in an f-string, so a new raw name is caught without touching this file. A notification_id
is an identity and keeps the raw name. A name built into a message variable first is not followed; the review
checklist covers that case.
"""

import ast
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[2] / "custom_components" / "comexio"
NAME_VARIABLES = {"name", "plan_name"}
# Keyword arguments that identify rather than show: they keep the raw name.
IDENTITY_KEYWORDS = {"notification_id"}
SOURCE_FILES = sorted(COMPONENT.rglob("*.py"))


def _is_output_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    return func.value.id == "_LOGGER" or (func.value.id == "persistent_notification" and func.attr == "async_create")


def _raw_names(value: ast.expr) -> list[ast.Name]:
    """The name variables an argument hands over unfiltered: itself, or a value of an f-string."""
    candidates = (
        [part.value for part in value.values if isinstance(part, ast.FormattedValue)]
        if isinstance(value, ast.JoinedStr)
        else [value]
    )
    return [node for node in candidates if isinstance(node, ast.Name) and node.id in NAME_VARIABLES]


def raw_name_outputs(source: str) -> list[tuple[int, str]]:
    """(line, variable) of every name variable a log or notification call gets without one_line."""
    found = []
    for call in ast.walk(ast.parse(source)):
        if isinstance(call, ast.Call) and _is_output_call(call):
            shown = [keyword.value for keyword in call.keywords if keyword.arg not in IDENTITY_KEYWORDS]
            for value in [*call.args, *shown]:
                found.extend((node.lineno, node.id) for node in _raw_names(value))
    return sorted(found)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('_LOGGER.info("Plan %s", plan_name)', [(1, "plan_name")]),
        ('_LOGGER.warning(f"Plan {name!r} gone")', [(1, "name")]),
        ('persistent_notification.async_create(hass, f"Plan {plan_name}", title="x")', [(1, "plan_name")]),
        ("persistent_notification.async_create(hass, msg, title=name)", [(1, "name")]),
        ('_LOGGER.info("Plan %s", one_line(plan_name))', []),
        ('_LOGGER.info(f"Plan {one_line(name)!r}")', []),
        ("persistent_notification.async_create(hass, msg, title=one_line(name))", []),
        ('persistent_notification.async_create(hass, msg, notification_id=f"x_{plan_name}")', []),
        ('_LOGGER.debug(\n    "Plan %s",\n    plan_name,\n)', [(3, "plan_name")]),
        ('_LOGGER.info("Plan %s (ID %s)", shown, fub_id)', []),
        ("token = vanished_plan_token(fub_id, plan_name)", []),
    ],
)
def test_the_check_finds_a_raw_name(source: str, expected: list[tuple[int, str]]) -> None:
    assert raw_name_outputs(source) == expected


def test_the_check_reads_the_whole_integration() -> None:
    assert COMPONENT / "services" / "backup.py" in SOURCE_FILES


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: str(path.relative_to(COMPONENT)))
def test_no_log_line_or_notification_gets_a_raw_comexio_name(path: Path) -> None:
    raw = raw_name_outputs(path.read_text(encoding="utf-8"))
    assert not raw, f"wrap these names in const.one_line before output: {raw}"
