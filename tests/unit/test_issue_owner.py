"""Every repair issue is linked to its config entry, so Home Assistant removes it together with the entry.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue; before that the keyword does not
exist. The helper issue_owner passes it only when the running version accepts it and the entry still
exists (HA refuses a link to an unknown entry). The guard below fails on a create call that does not
splat issue_owner with the code path's own entry id, so a new repair cannot miss the link.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
import pytest

from custom_components.comexio import issue_owner as owner_module
from custom_components.comexio.issue_owner import issue_owner

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
ENTRY_ID = "entry-1"
CREATE_NAMES = {"async_create_issue", "create_issue"}
# The ways the integration's code paths reach their own entry id. A new form is added here on purpose.
OWN_ENTRY_IDS = {"entry_id", "entry.entry_id", "self._entry_id", "self.config_entry.entry_id"}


OTHER_ENTRY_ID = "entry-2"


def _hass(*entry_ids: str) -> SimpleNamespace:
    """A hass whose config entries are exactly entry_ids."""
    entries = dict.fromkeys(entry_ids, object())
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=entries.get))


@pytest.mark.skipif((MAJOR_VERSION, MINOR_VERSION) < (2026, 11), reason="config_entry_id arrived in HA 2026.11")
def test_home_assistant_accepts_the_owner_keyword() -> None:
    # The tests below patch the flag; this one fails if Home Assistant renames the keyword, which would
    # otherwise turn the link off for good without any test noticing.
    assert owner_module.OWNER_KEYWORD_SUPPORTED


def test_links_the_issue_to_its_entry_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", True)
    hass = _hass(ENTRY_ID, OTHER_ENTRY_ID)
    # Two entries side by side: each issue is linked to the entry it was raised for, not to a fixed one.
    assert [issue_owner(hass, entry_id) for entry_id in (ENTRY_ID, OTHER_ENTRY_ID)] == [
        {"config_entry_id": ENTRY_ID},
        {"config_entry_id": OTHER_ENTRY_ID},
    ]


def test_passes_nothing_before_home_assistant_knows_the_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", False)
    assert issue_owner(_hass(ENTRY_ID), ENTRY_ID) == {}


def test_leaves_the_issue_unlinked_once_the_entry_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Linking to an unknown entry raises HomeAssistantError; the issue is created unlinked instead. The other
    # entry still exists, so the check has to look up the id it was handed, not any entry.
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", True)
    hass = _hass(OTHER_ENTRY_ID)
    assert issue_owner(hass, ENTRY_ID) == {}
    assert issue_owner(hass, OTHER_ENTRY_ID) == {"config_entry_id": OTHER_ENTRY_ID}


def _name_of(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)


def _is_create_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _name_of(node.func) in CREATE_NAMES


def _passes_own_entry(owner_call: ast.Call) -> bool:
    # An AST cannot prove the id belongs to this code path, but it can pin the expression to the known forms
    # of the own entry id. A literal, an unrelated variable or another object's entry_id fails here.
    return len(owner_call.args) == 2 and ast.unparse(owner_call.args[1]) in OWN_ENTRY_IDS


def _splats_owner(call: ast.Call) -> bool:
    return any(
        keyword.arg is None
        and isinstance(keyword.value, ast.Call)
        and _name_of(keyword.value.func) == "issue_owner"
        and _passes_own_entry(keyword.value)
        for keyword in call.keywords
    )


def _unlinked_creates(source: str | ast.AST) -> list[int]:
    tree = ast.parse(source) if isinstance(source, str) else source
    return [node.lineno for node in ast.walk(tree) if _is_create_call(node) and not _splats_owner(node)]


@pytest.mark.parametrize("entry", sorted(OWN_ENTRY_IDS))
def test_detector_accepts_the_own_entry(entry: str) -> None:
    source = f"ir.async_create_issue(hass, D, 'x', **issue_owner(hass, {entry}), is_fixable=True)\n"
    assert not _unlinked_creates(source)


@pytest.mark.parametrize(
    "owner",
    [
        "",
        "**issue_owner(hass, 'entry-1'), ",
        "**issue_owner(hass, other), ",
        "**issue_owner(hass, self._other_id), ",
        "**issue_owner(hass, other_entry.entry_id), ",
        "**issue_owner(hass, self._coordinator.entry_id), ",
        "**issue_owner(hass), ",
        "**owner_for(hass, entry_id), ",
    ],
)
def test_detector_flags_missing_or_foreign_links(owner: str) -> None:
    source = f"ir.async_create_issue(hass, D, 'x', {owner}is_fixable=True)\n"
    assert _unlinked_creates(source) == [1]


def _integration_trees() -> dict[Path, ast.AST]:
    return {path: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(INTEGRATION.rglob("*.py"))}


def test_the_knx_cleanup_is_the_only_persistent_repair() -> None:
    # A persistent repair outlives restarts, so one raised before issue_owner existed never gets the entry link;
    # async_remove_entry deletes the KNX cleanup repair by id for that reason. One entry per is_persistent
    # keyword: a second persistent create, in any file, needs the same delete there before it is pinned here.
    persistent = [
        path.relative_to(INTEGRATION).as_posix()
        for path, tree in _integration_trees().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "is_persistent"
    ]
    assert persistent == ["coordinator.py"]


def test_every_repair_issue_is_linked_to_its_entry() -> None:
    trees = _integration_trees()
    # The guard must see the create calls it protects, or a moved directory or a broken detector turns it
    # vacuously green.
    assert any(_is_create_call(node) for tree in trees.values() for node in ast.walk(tree))
    unlinked = sorted(
        f"{path.relative_to(INTEGRATION).as_posix()}:{line}"
        for path, tree in trees.items()
        for line in _unlinked_creates(tree)
    )
    assert not unlinked, f"create calls without **issue_owner(hass, <own entry id>): {unlinked}"
