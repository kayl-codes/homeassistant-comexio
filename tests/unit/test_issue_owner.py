"""Every repair issue belongs to its config entry: raised only while the entry exists, removed together with it.

Home Assistant 2026.11 added config_entry_id to ir.async_create_issue; before that the keyword does not exist.
async_create_entry_issue passes it only when the running version accepts it, and skips the issue once the entry
is gone (HA refuses a link to an unknown entry). The guard below keeps it the only way the integration raises a
repair: it fails on any other reference to ir.async_create_issue, on the issue registry used other than as
ir.<attribute>, on any lookup by a name built at runtime (getattr with a computed name outside a short per-file
list, importlib, __import__, sys.modules, vars, __dict__, eval ...), on the wrapper used other than as the plain
name imported from issue_owner, on a wrapper call that does not hand over the code path's own entry id, and on a
** or * that would hide what the call passes.
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
WRAPPER_MODULE = "issue_owner"
WRAPPER_FILE = f"{WRAPPER_MODULE}.py"
WRAPPER = "async_create_entry_issue"
CREATE_NAMES = {"async_create_issue", "create_issue"}
REGISTRY = "ir"
REGISTRY_MODULE = "issue_registry"
GETATTR = "getattr"
# The names the scan follows: the wrapper, the issue registry module and the modules that hold it under another
# handle are imported under these only.
IMPORTED_AS = {WRAPPER: WRAPPER, REGISTRY_MODULE: REGISTRY, "sys": "sys", "helpers": "helpers"}
# attribute -> base: sys.modules and helpers.issue_registry hold the registry module under a handle other than ir.
OTHER_HANDLES = {"modules": "sys", REGISTRY_MODULE: "helpers"}
# The ways other than getattr to reach a name built at runtime: dynamic imports, namespace dicts, callers by name
# and code from strings. The integration uses none of them, so the guard can refuse them all.
DYNAMIC_LOOKUPS = {
    "__builtins__",
    "__dict__",
    "__getattr__",
    "__getattribute__",
    "__globals__",
    "__import__",
    "attrgetter",
    "eval",
    "exec",
    "getmembers",
    "globals",
    "import_module",
    "importlib",
    "locals",
    "methodcaller",
    "vars",
}
# Strings that reach the create function, the wrapper, the issue registry module or a dynamic lookup around the
# scan, e.g. getattr(sys, "modules") or getattr(builtins, "__import__").
HIDDEN_ACCESS = CREATE_NAMES | DYNAMIC_LOOKUPS | set(OTHER_HANDLES) | {WRAPPER, "homeassistant.helpers.issue_registry"}
# Per file, the one getattr with a computed name it may hold, and the written-out names it computes. The object
# may well lead to the issue registry (plan_watchdog.py imports ir), so the names are pinned, not the object.
COMPUTED_GETATTR_BY_FILE = {
    "ha_address.py": ("getattr(socket, name, None)", {"EAI_NONAME", "EAI_NODATA"}),
    "switch.py": (
        "getattr(self.coordinator.plan_watchdog, self._watchdog_flag)",
        {"auto_restart", "auto_restart_user"},
    ),
}
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
    """The name a node reads, or binds by def, class or parameter (which would shadow an imported one)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    return node.arg if isinstance(node, ast.arg) else None


def _links_own_entry(wrapper_call: ast.Call, own_entry_id: str | None) -> bool:
    # An AST cannot prove the id belongs to this code path, but it can pin the expression to the one form of the
    # own entry id in that file. A literal, an unrelated variable or another object's entry_id fails here, and so
    # does a ** or * that would hide the arguments (an is_persistent=True among them).
    hidden = any(isinstance(arg, ast.Starred) for arg in wrapper_call.args) or any(
        keyword.arg is None for keyword in wrapper_call.keywords
    )
    return not hidden and len(wrapper_call.args) >= 3 and ast.unparse(wrapper_call.args[1]) == own_entry_id


def _bad_import(node: ast.Import | ast.ImportFrom) -> bool:
    """Imports ir.async_create_issue or a dynamic lookup, the wrapper from elsewhere, or a name the scan misses."""
    source = node.module if isinstance(node, ast.ImportFrom) else None
    for alias in node.names:
        name = alias.name.rsplit(".", 1)[-1]
        bound = alias.asname or alias.name
        if (
            name in CREATE_NAMES
            or name in DYNAMIC_LOOKUPS
            or bound != IMPORTED_AS.get(name, bound)
            or (name == WRAPPER and not _imports_wrapper(node))
            or (name in OTHER_HANDLES and OTHER_HANDLES[name] == source)
        ):
            return True
    return False


def _imports_wrapper(node: ast.AST) -> bool:
    """from .issue_owner import async_create_entry_issue, the one import of the wrapper the guard follows."""
    return (
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == WRAPPER_MODULE
        and any(alias.name == WRAPPER for alias in node.names)
    )


def _is_registry(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == REGISTRY


def _is_wrapper_call(node: ast.AST) -> bool:
    # Only by its imported name: x.async_create_entry_issue may be any object's method of that name.
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == WRAPPER


def _is_plain_getattr(node: ast.AST, computed_getattr: str | None) -> bool:
    """getattr with a written-out name, or the one getattr with a computed name the file may hold."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == GETATTR):
        return False
    written_out = len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
    return written_out or ast.unparse(node) == computed_getattr


def _followed_uses(tree: ast.AST, computed_getattr: str | None) -> set[int]:
    """The only uses the checks see through: the wrapper and getattr as plain callees, ir as ir.<attribute>."""
    followed: set[int] = set()
    for node in ast.walk(tree):
        if _is_wrapper_call(node) or _is_plain_getattr(node, computed_getattr):
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
    if _is_wrapper_call(node):
        return not _links_own_entry(node, own_entry_id)
    name = _name_of(node)
    # Passed around, rebound, shadowed or reached through another object, the wrapper escapes the entry id check;
    # handed to getattr, the registry gives its create function to a name built at runtime, and so does a getattr
    # with a computed name on any object that leads there.
    if _is_registry(node) or name in {WRAPPER, GETATTR}:
        return id(node) not in followed
    if isinstance(node, ast.Attribute) and node.attr in OTHER_HANDLES:
        return _name_of(node.value) == OTHER_HANDLES[node.attr]
    return name in CREATE_NAMES or name in DYNAMIC_LOOKUPS


def _violations(
    source: str | ast.AST, own_entry_id: str | None = OWN_ENTRY_ID, computed_getattr: str | None = None
) -> list[int]:
    tree = ast.parse(source) if isinstance(source, str) else source
    followed = _followed_uses(tree, computed_getattr)
    return sorted({node.lineno for node in ast.walk(tree) if _violates(node, followed, own_entry_id)})


@pytest.mark.parametrize(
    "source",
    [
        "async_create_entry_issue(hass, self._entry_id, 'x', is_fixable=True)\n",
        "from .issue_owner import async_create_entry_issue\n",
        "ir.async_delete_issue(hass, D, 'x')\n",
        "import sys\n",
        "from homeassistant import helpers\n",
        "severity = ir.IssueSeverity.WARNING\n",
        "issue_registry = ir.async_get(hass)\n",
        "self.issue_registry.async_get_issue(D, 'x')\n",
        "self.modules.clear()\n",
        "from homeassistant.helpers import entity_registry as er, issue_registry as ir\n",
        "other(hass, *arguments, **options)\n",
        "value = getattr(obj, 'x', None)\n",
        "flag = getattr(self.coordinator.plan_watchdog, self._watchdog_flag)\n",
    ],
)
def test_detector_accepts_the_wrapper_with_the_own_entry_and_plain_registry_use(source: str) -> None:
    assert not _violations(source, computed_getattr=_computed_getattr("switch.py"))


def test_detector_allows_a_computed_getattr_only_in_its_own_file() -> None:
    source = "flag = getattr(self.coordinator.plan_watchdog, self._watchdog_flag)\n"
    assert _violations(source) == [1]
    assert _violations(source, computed_getattr=_computed_getattr("ha_address.py")) == [1]


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
        # The wrapper reached other than as the plain imported name, or shadowed by a local of that name.
        "issue_owner.async_create_entry_issue(hass, self._entry_id, 'x', is_fixable=True)\n",
        "other.async_create_entry_issue(hass, self._entry_id, 'x', is_fixable=True)\n",
        "from .other import async_create_entry_issue\n",
        "from custom_components.comexio.issue_owner import async_create_entry_issue\n",
        "def async_create_entry_issue(hass, entry_id, issue_id): pass\n",
        "class async_create_entry_issue: pass\n",
        "def raise_repair(async_create_entry_issue): pass\n",
        # Lookups by a name built at runtime.
        "getattr(module, name)(hass, D, 'x')\n",
        "raise_it = reduce(getattr, path, module)\n",
        "importlib.import_module('homeassistant.' + 'helpers.issue_registry')\n",
        "import importlib\n",
        "from importlib import import_module\n",
        "__import__(name)\n",
        "sys.modules[name]\n",
        "vars(helpers)[name]\n",
        "globals()[name]\n",
        "module.__dict__[name]\n",
        "operator.attrgetter(name)(module)\n",
        "from operator import attrgetter\n",
        "eval(code)\n",
        "def __getattr__(name): pass\n",
        # The same lookups spelled as a string to getattr, or reached through an aliased module.
        "getattr(sys, 'modules')[name]\n",
        "getattr(builtins, '__import__')(name)\n",
        "getattr(module, '__dict__')[name]\n",
        "getattr(obj, 'eval')(code)\n",
        "getattr(helpers, 'issue_registry')\n",
        "import sys as system\n",
        "from sys import modules\n",
        "from homeassistant import helpers as ha_helpers\n",
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
    raising = {name for name, tree in trees.items() if any(_is_wrapper_call(node) for node in ast.walk(tree))}
    # The guard must see the calls it protects (a moved directory or a broken detector would turn it vacuously
    # green), and a file that no longer raises repairs leaves the map.
    assert raising == set(OWN_ENTRY_ID_BY_FILE)
    importing = {name for name, tree in trees.items() if any(_imports_wrapper(node) for node in ast.walk(tree))}
    assert importing == raising
    violations = sorted(
        f"{name}:{line}"
        for name, tree in trees.items()
        for line in _violations(tree, OWN_ENTRY_ID_BY_FILE.get(name), _computed_getattr(name))
    )
    assert not violations, f"repairs raised other than by {WRAPPER}(hass, <own entry id>, issue_id, ...): {violations}"


def _computed_getattr(file_name: str) -> str | None:
    entry = COMPUTED_GETATTR_BY_FILE.get(file_name)
    return entry[0] if entry else None


# The scopes Python resolves a plain name in; a class body is skipped for the code nested in it.
NAME_SCOPES = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
)


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """The nodes of scope itself: a nested scope is listed, what it holds is not (its parameters bind there)."""
    own: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        own.append(node)
        if not isinstance(node, (*NAME_SCOPES, ast.ClassDef)):
            pending.extend(ast.iter_child_nodes(node))
    return own


def _binds(node: ast.AST, key: str, attribute: bool) -> bool:
    """Whether node binds key: a stored name, a parameter, a def or class, and for self.<key> a stored attribute."""
    if isinstance(node, ast.Attribute) and not attribute:
        return False
    if isinstance(node, ast.Name | ast.Attribute) and not isinstance(node.ctx, ast.Store):
        return False
    return _name_of(node) == key


def _bound_value(target: ast.AST, parent: ast.AST) -> ast.AST:
    """What a binding gives its name: the assigned value or the loop's iterable, else the binding itself."""
    if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr) and parent.value is not None:
        return parent.value
    if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension) and parent.target is target:
        # The loop binds each element of what it iterates; a literal tuple gives its strings.
        return parent.iter
    # Augmented, unpacked, with/except targets, parameters, defs: no value written out here.
    return target


def _name_targets(call: ast.AST, key: str, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    """The bindings of a plain name in the innermost scope around call that binds it, as Python resolves it."""
    node = call
    while node in parents:
        node = parents[node]
        if isinstance(node, NAME_SCOPES) and (
            targets := [own for own in _own_nodes(node) if _binds(own, key, attribute=False)]
        ):
            return targets
    return []


def _opaque_binding(tree: ast.AST, key: str) -> bool:
    """A binding of key the scope walk does not resolve: global/nonlocal, an import, except-as or a match capture."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            bound = node.names
        elif isinstance(node, ast.alias):
            bound = [node.asname or node.name.split(".")[0]]
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            bound = [node.name]
        elif isinstance(node, ast.MatchMapping):
            bound = [node.rest]
        else:
            continue
        if key in bound:
            return True
    return False


def _literal_strings(node: ast.AST) -> set[str] | None:
    elements = node.elts if isinstance(node, ast.Tuple | ast.List | ast.Set) else [node]
    if all(isinstance(element, ast.Constant) and isinstance(element.value, str) for element in elements):
        return {element.value for element in elements}
    return None


def _computed_names(tree: ast.AST, call_text: str) -> set[str] | None:
    """The names the listed getattr computes, or None when it is gone or reads a name not written out in the file."""
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node) == call_text]
    if len(calls) != 1 or len(calls[0].args) < 2:
        return None
    argument = calls[0].args[1]
    key = _name_of(argument)
    if key is None or _opaque_binding(tree, key):
        return None
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    if isinstance(argument, ast.Attribute):
        # self.<key>: set in a class body or on the instance, anywhere in the file.
        targets = [node for node in ast.walk(tree) if _binds(node, key, attribute=True)]
    else:
        targets = _name_targets(calls[0], key, parents)
    values = [_literal_strings(_bound_value(target, parents[target])) for target in targets]
    if not values or None in values:
        return None
    return set().union(*values)


CALL = "getattr(socket, name, None)"
ATTRIBUTE_CALL = "getattr(watchdog, self.flag)"


@pytest.mark.parametrize(
    ("source", "call", "names"),
    [
        (f"for name in ('A', 'B'):\n    {CALL}\n", CALL, {"A", "B"}),
        (f"codes = [{CALL} for name in ['A']]\n", CALL, {"A"}),
        (f"def lookup():\n    name = 'A'\n    return {CALL}\n", CALL, {"A"}),
        # Another function's own name of the same spelling does not count; the real ha_address.py case.
        (f"def other(name):\n    return name\nfor name in ('A',):\n    {CALL}\n", CALL, {"A"}),
        (f"class S:\n    flag = 'A'\nclass T(S):\n    flag = 'B'\n{ATTRIBUTE_CALL}\n", ATTRIBUTE_CALL, {"A", "B"}),
        (f"class S:\n    flag = 'A'\nself.flag = build()\n{ATTRIBUTE_CALL}\n", ATTRIBUTE_CALL, None),
        (f"name = build()\n{CALL}\n", CALL, None),
        (f"name = build()\ndef lookup():\n    return {CALL}\n", CALL, None),
        (f"for name in names:\n    {CALL}\n", CALL, None),
        (f"name = 'A'\nname += suffix\n{CALL}\n", CALL, None),
        (f"name, other = 'A', 'B'\n{CALL}\n", CALL, None),
        (f"with open(path) as name:\n    {CALL}\n", CALL, None),
        (f"def lookup(name):\n    return {CALL}\n", CALL, None),
        (f"name = 'A'\nif (name := build()):\n    {CALL}\n", CALL, None),
        (f"{CALL}\n", CALL, None),
        ("name = 'A'\ngetattr(socket, name)\n", CALL, None),
        # Bindings the scope walk does not resolve count as not written out, wherever they are in the file.
        (f"name = 'A'\ndef rebind():\n    global name\n    name = build()\n{CALL}\n", CALL, None),
        (f"def outer():\n    name = 'A'\n    def rebind():\n        nonlocal name\n    return {CALL}\n", CALL, None),
        (f"from config import name\nname = 'A'\n{CALL}\n", CALL, None),
        (f"name = 'A'\ntry:\n    pass\nexcept Exception as name:\n    pass\n{CALL}\n", CALL, None),
        (f"name = 'A'\nmatch value:\n    case name:\n        pass\n{CALL}\n", CALL, None),
        (f"name = 'A'\nmatch value:\n    case [*name]:\n        pass\n{CALL}\n", CALL, None),
        (f"name = 'A'\nmatch value:\n    case {{**name}}:\n        pass\n{CALL}\n", CALL, None),
    ],
)
def test_detects_the_names_a_computed_getattr_reads(source: str, call: str, names: set[str] | None) -> None:
    assert _computed_names(ast.parse(source), call) == names


@pytest.mark.parametrize("file_name", sorted(COMPUTED_GETATTR_BY_FILE))
def test_a_listed_computed_getattr_reads_only_its_written_out_names(file_name: str) -> None:
    # The allowed getattr may sit on an object that leads to the issue registry; it stays harmless only while every
    # name it reads is written out in the file. A moved file or a changed call fails here as well.
    call_text, names = COMPUTED_GETATTR_BY_FILE[file_name]
    tree = _integration_trees().get(file_name, ast.Module(body=[], type_ignores=[]))
    assert _computed_names(tree, call_text) == names
