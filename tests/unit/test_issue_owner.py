"""Every repair issue is linked to its config entry, so Home Assistant removes it together with the entry.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue; before that the keyword does not
exist. The helper issue_owner passes it only when the running version accepts it and the entry still
exists (HA refuses a link to an unknown entry). The guard below fails on a create call that does not
splat issue_owner, so a new repair cannot miss the link.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.comexio import issue_owner as owner_module
from custom_components.comexio.issue_owner import issue_owner

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
ENTRY_ID = "entry-1"
CREATE_NAMES = {"async_create_issue", "create_issue"}


def _hass(*, entry_exists: bool) -> SimpleNamespace:
    entries = {ENTRY_ID: object()} if entry_exists else {}
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=entries.get))


def test_links_the_issue_to_its_entry_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", True)
    assert issue_owner(_hass(entry_exists=True), ENTRY_ID) == {"config_entry_id": ENTRY_ID}


def test_passes_nothing_before_home_assistant_knows_the_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", False)
    assert issue_owner(_hass(entry_exists=True), ENTRY_ID) == {}


def test_leaves_the_issue_unlinked_once_the_entry_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Linking to an unknown entry raises HomeAssistantError; the issue is created unlinked instead.
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", True)
    assert issue_owner(_hass(entry_exists=False), ENTRY_ID) == {}


def _is_create_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in CREATE_NAMES


def _splats_owner(call: ast.Call) -> bool:
    return any(
        keyword.arg is None
        and isinstance(keyword.value, ast.Call)
        and getattr(keyword.value.func, "id", getattr(keyword.value.func, "attr", None)) == "issue_owner"
        for keyword in call.keywords
    )


def _unlinked_creates(source: str) -> list[int]:
    return [node.lineno for node in ast.walk(ast.parse(source)) if _is_create_call(node) and not _splats_owner(node)]


def test_detector_sees_linked_and_unlinked_creates() -> None:
    linked = "ir.async_create_issue(hass, D, 'x', **issue_owner(hass, entry), is_fixable=True)\n"
    unlinked = "ir.async_create_issue(hass, D, 'x', is_fixable=True)\n"
    assert not _unlinked_creates(linked)
    assert _unlinked_creates(unlinked) == [1]


def test_every_repair_issue_is_linked_to_its_entry() -> None:
    files = sorted(INTEGRATION.rglob("*.py"))
    # The guard must see the integration it protects, or a moved directory turns it vacuously green.
    assert files
    unlinked = sorted(
        f"{path.relative_to(INTEGRATION).as_posix()}:{line}"
        for path in files
        for line in _unlinked_creates(path.read_text(encoding="utf-8"))
    )
    assert not unlinked, f"create calls without **issue_owner(hass, entry_id): {unlinked}"
