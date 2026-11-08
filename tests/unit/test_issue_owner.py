"""Every repair issue belongs to its config entry: raised only while the entry exists, removed together with it.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue; before that the keyword does not exist.
async_create_entry_issue passes it only when the running version accepts it, and skips the issue once the entry
is gone (HA refuses a link to an unknown entry). The guard below keeps it the only way the integration raises a
repair: it fails on any other reference to ir.async_create_issue, on the issue registry used other than as
ir.<attribute> (getattr with a built name), on a wrapper call that does not hand over the code path's own entry
id, and on a ** or * that would hide what the call passes.
"""

import ast
from functools import cache
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.comexio import issue_owner as owner_module
from custom_components.comexio.const import DOMAIN
from custom_components.comexio.issue_owner import async_create_entry_issue

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "comexio"
WRAPPER_FILE = "issue_owner.py"
WRAPPER = "async_create_entry_issue"
CREATE_NAMES = {"async_create_issue", "create_issue"}
REGISTRY = "ir"
REGISTRY_MODULE = "issue_registry"
# Strings that reach the create function, the wrapper or the issue registry module around the scan (getattr,
# importlib, sys.modules).
HIDDEN_ACCESS = CREATE_NAMES | {WRAPPER, "homeassistant.helpers.issue_registry"}
# The names the scan follows: the wrapper and the issue registry module are imported under these only.
IMPORTED_AS = {WRAPPER: WRAPPER, REGISTRY_MODULE: REGISTRY}
# Per file, the one expression its code paths reach their own entry id by: the coordinator its config entry, the
# monitors the id the coordinator built them with, the free functions the entry (id) handed in. A new file that
# raises repairs is added here on purpose; another variable that happens to be called entry_id fails.
OWN_ENTRY_ID_BY_FILE = {
    "certificate.py": "entry.entry_id",
    "coordinator.py": "self.config_entry.entry_id",
    "orphaned_backups.py": "entry_id",
    "plan_watchdog.py": "self._entry_id",
    "reference_monitor.py": "self._entry_id",
    "unsupported_ios.py": "self._entry_id",
}
OWN_ENTRY_ID = "self._entry_id"  # the one the detector cases below are checked against
ENTRY_ID = "entry-1"
OTHER_ENTRY_ID = "entry-2"
# Distinct stand-ins for every keyword the wrapper takes, so a keyword it drops or mixes up shows in the call.
ISSUE_KEYWORDS = {
    name: f"<{name}>"
    for name, parameter in inspect.signature(async_create_entry_issue).parameters.items()
    if parameter.kind is inspect.Parameter.KEYWORD_ONLY
}


def _hass(*entry_ids: str) -> SimpleNamespace:
    """A hass whose config entries are exactly entry_ids."""
    entries = dict.fromkeys(entry_ids, object())
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=entries.get))


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    registry = MagicMock()
    monkeypatch.setattr(owner_module, "ir", registry)
    return registry


def _create_before_the_link(hass, domain, issue_id, *, is_fixable, severity, translation_key) -> None:
    """The signature of ir.async_create_issue up to HA 2026.10, reduced to the keywords that matter here."""


def _create_with_the_link(
    hass, domain, issue_id, *, is_fixable, severity, translation_key, config_entry_id=None
) -> None:
    """The signature from HA 2026.11 on."""


@pytest.mark.parametrize(
    ("create_issue", "supported"), [(_create_before_the_link, False), (_create_with_the_link, True)]
)
def test_detects_the_owner_keyword_from_the_signature(create_issue, supported: bool) -> None:
    assert owner_module.accepts_owner_keyword(create_issue) is supported


def test_the_owner_keyword_is_used_exactly_from_home_assistant_2026_11() -> None:
    # The tests below patch the flag; this one checks it against the Home Assistant installed. On 2026.8–2026.10
    # a wrongly set flag would make every repair raise TypeError; from 2026.11 on a renamed keyword would turn
    # the link off without any test noticing.
    expected = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11)
    assert owner_module.accepts_owner_keyword(ir.async_create_issue) is expected
    assert owner_module.OWNER_KEYWORD_SUPPORTED is expected


def test_forwards_every_keyword_and_no_link_before_home_assistant_knows_it(
    monkeypatch: pytest.MonkeyPatch, registry: MagicMock
) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", False)
    hass = _hass(ENTRY_ID)
    async_create_entry_issue(hass, ENTRY_ID, "one", **ISSUE_KEYWORDS)
    assert registry.async_create_issue.call_args_list == [call(hass, DOMAIN, "one", **ISSUE_KEYWORDS)]


def test_links_the_issue_to_its_entry_when_supported(monkeypatch: pytest.MonkeyPatch, registry: MagicMock) -> None:
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", True)
    hass = _hass(ENTRY_ID, OTHER_ENTRY_ID)
    # Two entries side by side: each issue is linked to the entry it was raised for, not to a fixed one.
    async_create_entry_issue(hass, ENTRY_ID, "one", **ISSUE_KEYWORDS)
    async_create_entry_issue(hass, OTHER_ENTRY_ID, "two", **ISSUE_KEYWORDS)
    assert registry.async_create_issue.call_args_list == [
        call(hass, DOMAIN, "one", **ISSUE_KEYWORDS, config_entry_id=ENTRY_ID),
        call(hass, DOMAIN, "two", **ISSUE_KEYWORDS, config_entry_id=OTHER_ENTRY_ID),
    ]


@pytest.mark.parametrize("supported", [True, False])
def test_skips_the_issue_once_the_entry_is_gone(
    monkeypatch: pytest.MonkeyPatch, registry: MagicMock, caplog: pytest.LogCaptureFixture, supported: bool
) -> None:
    # A poll or monitor finishing after the removal: no repair without its entry, on any Home Assistant version.
    # The other entry still exists, so the check has to look up the id it was handed, not any entry.
    monkeypatch.setattr(owner_module, "OWNER_KEYWORD_SUPPORTED", supported)
    hass = _hass(OTHER_ENTRY_ID)
    with caplog.at_level(logging.DEBUG, logger=owner_module.__name__):
        async_create_entry_issue(hass, ENTRY_ID, "gone", **ISSUE_KEYWORDS)
        async_create_entry_issue(hass, OTHER_ENTRY_ID, "kept", **ISSUE_KEYWORDS)
    assert [created.args[2] for created in registry.async_create_issue.call_args_list] == ["kept"]
    assert [record.getMessage() for record in caplog.records] == [
        f"Repair gone is not raised: config entry {ENTRY_ID} was removed"
    ]


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _links_own_entry(wrapper_call: ast.Call, own_entry_id: str | None) -> bool:
    # An AST cannot prove the id belongs to this code path, but it can pin the expression to the one form of the
    # own entry id in that file. A literal, an unrelated variable or another object's entry_id fails here, and so
    # does a ** or * that would hide the arguments (an is_persistent=True among them).
    hidden = any(isinstance(arg, ast.Starred) for arg in wrapper_call.args) or any(
        keyword.arg is None for keyword in wrapper_call.keywords
    )
    return not hidden and len(wrapper_call.args) >= 3 and ast.unparse(wrapper_call.args[1]) == own_entry_id


def _bad_import(node: ast.Import | ast.ImportFrom) -> bool:
    """Imports ir.async_create_issue itself, or the wrapper or the issue registry under a name the scan misses."""
    for alias in node.names:
        name = alias.name.rsplit(".", 1)[-1]
        bound = alias.asname or alias.name
        if name in CREATE_NAMES or bound != IMPORTED_AS.get(name, bound):
            return True
    return False


def _is_registry(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == REGISTRY


def _followed_uses(tree: ast.AST) -> set[int]:
    """The wrapper as a callee and ir as the base of ir.<attribute>: the only uses the checks see through."""
    followed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name_of(node.func) == WRAPPER:
            followed.add(id(node.func))
        elif isinstance(node, ast.Attribute) and _is_registry(node.value) and not node.attr.startswith("__"):
            # ir.__dict__ / ir.__getattribute__ reach any function by a built name, like getattr(ir, ...).
            followed.add(id(node.value))
    return followed


def _violates(node: ast.AST, followed: set[int], own_entry_id: str | None) -> bool:
    if isinstance(node, ast.Import | ast.ImportFrom):
        return _bad_import(node)
    if isinstance(node, ast.Constant):
        return node.value in HIDDEN_ACCESS
    if isinstance(node, ast.Call) and _name_of(node.func) == WRAPPER:
        return not _links_own_entry(node, own_entry_id)
    # Passed around, rebound or handed to getattr, the wrapper or the registry escapes the checks here: its
    # calls skip the entry id check, its create function is reached by a name built at runtime.
    if _is_registry(node) or _name_of(node) == WRAPPER:
        return id(node) not in followed
    if isinstance(node, ast.Attribute) and node.attr == REGISTRY_MODULE:
        # helpers.issue_registry: the module under a handle other than ir (self.issue_registry is not it).
        return _name_of(node.value) == "helpers"
    return _name_of(node) in CREATE_NAMES


def _violations(source: str | ast.AST, own_entry_id: str | None = OWN_ENTRY_ID) -> list[int]:
    tree = ast.parse(source) if isinstance(source, str) else source
    followed = _followed_uses(tree)
    return sorted({node.lineno for node in ast.walk(tree) if _violates(node, followed, own_entry_id)})


@pytest.mark.parametrize(
    "source",
    [
        "async_create_entry_issue(hass, self._entry_id, 'x', is_fixable=True)\n",
        "issue_owner.async_create_entry_issue(hass, self._entry_id, 'x', is_fixable=True)\n",
        "from .issue_owner import async_create_entry_issue\n",
        "ir.async_delete_issue(hass, D, 'x')\n",
        "severity = ir.IssueSeverity.WARNING\n",
        "issue_registry = ir.async_get(hass)\n",
        "self.issue_registry.async_get_issue(D, 'x')\n",
        "from homeassistant.helpers import entity_registry as er, issue_registry as ir\n",
        "other(hass, *arguments, **options)\n",
    ],
)
def test_detector_accepts_the_wrapper_with_the_own_entry_and_plain_registry_use(source: str) -> None:
    assert not _violations(source)


@pytest.mark.parametrize("own_entry_id", sorted(set(OWN_ENTRY_ID_BY_FILE.values())))
def test_detector_accepts_exactly_the_files_own_entry_form(own_entry_id: str) -> None:
    sources = {
        entry: f"async_create_entry_issue(hass, {entry}, 'x', is_fixable=True)\n"
        for entry in OWN_ENTRY_ID_BY_FILE.values()
    }
    accepted = [entry for entry, source in sources.items() if not _violations(source, own_entry_id)]
    assert set(accepted) == {own_entry_id}


def test_detector_flags_a_wrapper_call_in_a_file_without_an_own_entry_form() -> None:
    assert _violations("async_create_entry_issue(hass, self._entry_id, 'x')\n", None) == [1]


@pytest.mark.parametrize(
    "source",
    [
        "ir.async_create_issue(hass, D, 'x', is_fixable=True)\n",
        "create_issue(hass, D, 'x')\n",
        "raise_it = ir.async_create_issue\n",
        "getattr(ir, 'async_create_issue')(hass, D, 'x')\n",
        "getattr(ir, 'async_create' + '_issue')(hass, D, 'x')\n",
        "ir.__getattribute__('async_create' + '_issue')(hass, D, 'x')\n",
        "ir.__dict__['async_create' + '_issue'](hass, D, 'x')\n",
        "getattr(helpers.issue_registry, 'async_create' + '_issue')(hass, D, 'x')\n",
        "getattr(issue_owner, 'async_create_entry_issue')(hass, other, 'x')\n",
        "registry = ir\n",
        "from homeassistant.helpers import issue_registry\n",
        "from homeassistant.helpers import issue_registry as registry\n",
        "import homeassistant.helpers.issue_registry\n",
        "importlib.import_module('homeassistant.helpers.issue_registry')\n",
        "from homeassistant.helpers.issue_registry import async_create_issue\n",
        "from .issue_owner import async_create_entry_issue as raise_repair\n",
        "raise_repair = async_create_entry_issue\n",
        "async_create_entry_issue(hass, 'entry-1', 'x')\n",
        "async_create_entry_issue(hass, other, 'x')\n",
        "async_create_entry_issue(hass, entry_id, 'x')\n",
        "async_create_entry_issue(hass, self._other_id, 'x')\n",
        "async_create_entry_issue(hass, other_entry.entry_id, 'x')\n",
        "async_create_entry_issue(hass, self._coordinator.entry_id, 'x')\n",
        "async_create_entry_issue(hass, self._entry_id)\n",
        "async_create_entry_issue(hass, self._entry_id, issue_id='x')\n",
        "async_create_entry_issue(hass, entry_id=self._entry_id, issue_id='x')\n",
        "async_create_entry_issue(hass, self._entry_id, 'x', **options)\n",
        "async_create_entry_issue(hass, *arguments, 'x')\n",
        "getattr(homeassistant.helpers.issue_registry, 'async_create' + '_issue')(hass, D, 'x')\n",
    ],
)
def test_detector_flags_other_ways_to_raise_a_repair(source: str) -> None:
    assert _violations(source) == [1]


@cache
def _integration_trees() -> dict[str, ast.Module]:
    return {
        path.relative_to(INTEGRATION).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(INTEGRATION.rglob("*.py"))
    }


def _create_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _name_of(node.func) in CREATE_NAMES]


def test_the_wrapper_holds_the_one_direct_create() -> None:
    # The guard below skips issue_owner.py, so a second direct create there would go unseen without this.
    tree = _integration_trees()[WRAPPER_FILE]
    (wrapper,) = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == WRAPPER]
    assert len(_create_calls(tree)) == len(_create_calls(wrapper)) == 1


def test_the_knx_cleanup_is_the_only_persistent_repair() -> None:
    # A persistent repair outlives restarts, so one raised before the entry link existed never gets it;
    # async_remove_entry deletes the KNX cleanup repair by id for that reason. One entry per is_persistent
    # keyword: a second persistent create, in any file, needs the same delete there before it is pinned here.
    # The wrapper only forwards the keyword.
    persistent = [
        file_name
        for file_name, tree in _integration_trees().items()
        if file_name != WRAPPER_FILE
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "is_persistent"
    ]
    assert persistent == ["coordinator.py"]


def test_every_repair_issue_goes_through_the_wrapper() -> None:
    trees = {name: tree for name, tree in _integration_trees().items() if name != WRAPPER_FILE}
    raising = {
        name
        for name, tree in trees.items()
        if any(isinstance(node, ast.Call) and _name_of(node.func) == WRAPPER for node in ast.walk(tree))
    }
    # The guard must see the calls it protects (a moved directory or a broken detector would turn it vacuously
    # green), and a file that no longer raises repairs leaves the map.
    assert raising == set(OWN_ENTRY_ID_BY_FILE)
    violations = sorted(
        f"{name}:{line}" for name, tree in trees.items() for line in _violations(tree, OWN_ENTRY_ID_BY_FILE.get(name))
    )
    assert not violations, f"repairs raised other than by {WRAPPER}(hass, <own entry id>, issue_id, ...): {violations}"
