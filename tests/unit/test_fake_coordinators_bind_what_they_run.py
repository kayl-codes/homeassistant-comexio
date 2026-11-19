"""A test double of the coordinator binds every coordinator method that the real methods it runs call.

Some tests run real `ComexioCoordinator` methods on a stand-in (`class _FakeCoordinator(SimpleNamespace)` with
`some_method = ComexioCoordinator.some_method` in its body). A real method that calls `self.other_method()` needs
`other_method` on the stand-in too. Where it is missing, the test fails with an AttributeError far from its cause
- or, on a path with `except Exception` (a restore, a tick), it ends green for the wrong reason. This happened 20
times (the counter of the open point "Fake-Bindungstest"); every case was one short fix round, so this test
only moves the failure to where the cause is named.

The scan reads every class in every Python module of `tests/unit` (`conftest.py` and support modules included) that
binds a coordinator method in its body (also through a module-level alias of the class, or behind a module:
`coordinator_module.ComexioCoordinator.method`). For each bound method it takes the `self.<name>` uses in the
coordinator source and checks that every `<name>` that the real coordinator has as a method, classmethod or property
(its own or Home Assistant's) is supplied in that test file: bound or defined in the class body (a bare annotation
without a value defines nothing), or given elsewhere in the file - an attribute assigned (`fake.name = ...`), a
`setattr` (a `patch.object` only with `create=True`, a `monkeypatch.setattr` only with `raising=False`, as keyword or
in the 5th / 4th position: without that they fail on a name the object lacks and supply nothing) with a literal
name, or a keyword argument of a call that builds a double (a call of the double's class, or of a function whose name
says coordinator: `_coordinator(api, other=...)`). A name that the method calls (`self.name()`) must be given
something that can be called: a literal, a container, a computed number or text (`None`, `[]`, `a + b`, `1 if x else 2`)
is not enough. What else is given (a name, a call, a `@property`) counts as callable: its value is not known here.
In a class body a later assignment replaces an earlier one (`other = AsyncMock()`, then `other = None`). Only a direct
call counts: `self.name.attr()` reads `name`, and so does `callback = self.name` or `schedule(self.name)`, so a literal
passes there (not followed: it would flag every data attribute that is handed on as an argument).

An assigned attribute, a `setattr` or a keyword at a builder function counts for every double of the file, because the
object it goes to is not known; only a keyword at a call of a double's class counts for that class alone (by its name:
two doubles of one name in one file share it). So a double that lacks a name which another double of the same file
gets that way is not found, and an assignment that shadows a method the class defines (`fake.other = None`) does not
take it away: it may hold for one test only, and the order of assignments in a test is not followed either. A
`patch.object` whose patcher is never entered or started counts as supplied all the same.

Not followed: a double that is built without a class (`SimpleNamespace(...)` or `MagicMock()` passed to
`ComexioCoordinator.method(fake)`), a method inherited from Home Assistant's coordinator that a double binds (no source
here to read), a name read through `getattr` with a computed name, a `setattr` with a computed name. These stay the
reviewer's job.

A finding is fixed by binding the method in the double, or - where the test never reaches the call - listed in
ALLOWED with the reason.
"""

import ast
import inspect
from pathlib import Path

import pytest

from custom_components.comexio.coordinator import ComexioCoordinator

TESTS = Path(__file__).parent
COORDINATOR = "ComexioCoordinator"
BUILDER_WORD = "coordinator"  # a function with this in its name builds a double: `_coordinator(api, other=...)`
NEW_VALUE = ("new", "value")  # keyword spellings of the value in `patch.object` and `monkeypatch.setattr`
NOT_CALLABLE = (  # what no call can be made on, also when it holds names (`[mock]`, `f"{x}"`, `a + b`)
    ast.List,
    ast.Dict,
    ast.Set,
    ast.Tuple,
    ast.JoinedStr,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)

# (test file, double class, bound method, name it does not supply) -> why no test reaches the call.
ALLOWED: dict[tuple[str, str, str, str], str] = {}


def _real_members() -> frozenset[str]:
    """Names the real coordinator has as a method, classmethod, staticmethod or property, inherited ones included."""
    statics = {name: inspect.getattr_static(ComexioCoordinator, name) for name in dir(ComexioCoordinator)}
    kinds = (property, classmethod, staticmethod)
    return frozenset(name for name, member in statics.items() if isinstance(member, kinds) or callable(member))


def _coordinator_methods() -> dict[str, ast.AST]:
    source = Path(inspect.getsourcefile(ComexioCoordinator)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == COORDINATOR:
            return {
                child.name: child for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    msg = f"{COORDINATOR} not found"
    raise AssertionError(msg)


def _self_uses(method: ast.AST) -> set[str]:
    """Every `self.<name>` in the method, nested functions included."""
    return {
        node.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
    }


def _names_the_class(expr: ast.expr, aliases: set[str]) -> bool:
    """The coordinator class by one of its names, bare or behind a module (`coordinator_module.ComexioCoordinator`)."""
    return (isinstance(expr, ast.Name) and expr.id in aliases) or (
        isinstance(expr, ast.Attribute) and expr.attr in aliases
    )


def _class_aliases(tree: ast.Module) -> set[str]:
    """The coordinator class under every name the module gives it (`import ... as`, `Alias = ComexioCoordinator`)."""
    aliases = {COORDINATOR}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            aliases.update(alias.asname for alias in statement.names if alias.name == COORDINATOR and alias.asname)
        elif isinstance(statement, ast.Assign) and _names_the_class(statement.value, aliases):
            aliases.update(target.id for target in statement.targets if isinstance(target, ast.Name))
    return aliases


def _bound_coordinator_method(statement: ast.stmt, aliases: set[str]) -> tuple[str, str] | None:
    """(name in the double, coordinator method) for `name = ComexioCoordinator.method` in a class body."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value = statement.targets[0], statement.value
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        target, value = statement.target, statement.value
    else:
        return None
    if not (isinstance(target, ast.Name) and isinstance(value, ast.Attribute)):
        return None
    if _names_the_class(value.value, aliases):
        return target.id, value.attr
    return None


def _self_calls(method: ast.AST) -> set[str]:
    """Every `self.<name>(...)` in the method: the names that have to be callable, not just present."""
    return {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }


# A supply maps a name to whether what it is given could be called. A literal (`other = None`) cannot; a bare
# annotation (`other: object`) gives nothing at all, Python creates no attribute for it.
Supply = dict[str, bool]


def _callable(value: ast.expr) -> bool:
    """False for a literal (`None`, `1`, `-1`, `[]`, `{}`), a container, a computed number or text: no call possible.

    A conditional can be called when either branch can (`AsyncMock() if x else None`).
    """
    if isinstance(value, NOT_CALLABLE):
        return False
    if isinstance(value, ast.IfExp):
        return _callable(value.body) or _callable(value.orelse)
    try:
        ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return True
    return False


def _give(supply: Supply, name: str, may_be_called: bool) -> None:
    supply[name] = supply.get(name, False) or may_be_called


def _merged(*supplies: Supply) -> Supply:
    merged: Supply = {}
    for supply in supplies:
        for name, may_be_called in supply.items():
            _give(merged, name, may_be_called)
    return merged


def _defined_in_body(double: ast.ClassDef) -> Supply:
    """What the class body defines itself: assignments (annotated ones only with a value) and methods.

    The body runs top down, so a later definition of a name replaces an earlier one.
    """
    supply: Supply = {}
    for statement in double.body:
        if isinstance(statement, ast.Assign):
            supply.update(
                {target.id: _callable(statement.value) for target in statement.targets if isinstance(target, ast.Name)}
            )
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
            supply[statement.target.id] = _callable(statement.value)
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            supply[statement.name] = True
    return supply


def _builds_a_double(call: ast.Call, doubles: set[str]) -> bool:
    """A call of a double's class, or of a helper whose name says it builds one (`_coordinator(api, **kwargs)`)."""
    name = ast.unparse(call.func).rsplit(".", 1)[-1]
    return name in doubles or BUILDER_WORD in name.lower()


def _argument(call: ast.Call, index: int, *names: str) -> ast.expr | None:
    """The argument at position `index` or under one of the keywords `names`; None where the call gives neither."""
    if index < len(call.args):
        return call.args[index]
    return next((keyword.value for keyword in call.keywords if keyword.arg in names), None)


def _creates_the_attribute(call: ast.Call) -> bool:
    """`setattr` always does. `patch.object` and `monkeypatch.setattr` fail on a name the object lacks, unless told.

    `create` is the fifth parameter of `patch.object`, `raising` the fourth of `monkeypatch.setattr`.
    """
    func = ast.unparse(call.func)
    if func == "setattr":
        return True
    if func.endswith("patch.object"):
        flag, expected = _argument(call, 4, "create"), True
    elif func.endswith(".setattr"):
        flag, expected = _argument(call, 3, "raising"), False
    else:
        return False
    return isinstance(flag, ast.Constant) and flag.value is expected


def _set_by_call(call: ast.Call) -> tuple[str, bool] | None:
    """(name, may be called) for a call that creates an attribute with a literal name, else None."""
    if not _creates_the_attribute(call):
        return None
    name = _argument(call, 1, "attribute", "name")
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    given = _argument(call, 2, *NEW_VALUE)
    if given is None:
        return name.value, True  # `patch.object(obj, "name", create=True)` puts a MagicMock there
    return name.value, _callable(given)


def _supplied_in_file(tree: ast.AST, doubles: set[str]) -> tuple[Supply, dict[str, Supply]]:
    """What a test of the file hands to a double: (for any double, per double class).

    Assigned attributes, setattr and keywords at a call that builds a double (by helper name) count for every double of
    the file: the object they are given to is not known. A keyword at a call of a double's class counts for that class.
    """
    shared: Supply = {}
    per_class: dict[str, Supply] = {name: {} for name in doubles}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _note_call(node, per_class, shared)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            _note_assignment(node, shared)
    return shared, per_class


def _note_call(call: ast.Call, per_class: dict[str, Supply], shared: Supply) -> None:
    """Keywords of a call that builds a double, and the attribute a `setattr`-like call creates."""
    if _builds_a_double(call, set(per_class)):
        name = ast.unparse(call.func).rsplit(".", 1)[-1]
        target = per_class.get(name, shared)
        for keyword in call.keywords:
            if keyword.arg:
                _give(target, keyword.arg, _callable(keyword.value))
    if (given := _set_by_call(call)) is not None:
        _give(shared, *given)


def _note_assignment(node: ast.Assign | ast.AnnAssign, shared: Supply) -> None:
    """`fake.name = value` and `fake.name: type = value`; an annotation without a value assigns nothing."""
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Attribute):
            _give(shared, target.attr, _callable(node.value))


def missing_bindings(
    source: str, methods: dict[str, ast.AST], members: frozenset[str]
) -> dict[tuple[str, str, str], str]:
    """(double class, bound method, missing name) -> coordinator method, for every double of the source."""
    tree = ast.parse(source)
    aliases = _class_aliases(tree)
    # By class definition, not by name: two doubles of one name (in two tests) keep their own bindings.
    bound_of = [(node, _bindings(node, aliases)) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    shared, per_class = _supplied_in_file(tree, {double.name for double, bound in bound_of if bound})
    found: dict[tuple[str, str, str], str] = {}
    for double, bound in bound_of:
        have = _merged(
            {name: True for name, _method in bound}, _defined_in_body(double), shared, per_class.get(double.name, {})
        )
        for name, method in bound:
            if method not in methods:
                continue  # inherited from Home Assistant's coordinator: no source here to follow
            for needed in _lacking(have, methods[method], members):
                found[(double.name, name, needed)] = method
    return found


def _bindings(double: ast.ClassDef, aliases: set[str]) -> list[tuple[str, str]]:
    """(name in the double, coordinator method) for each coordinator method the class body binds."""
    pairs = (_bound_coordinator_method(statement, aliases) for statement in double.body)
    return [pair for pair in pairs if pair]


def _lacking(have: Supply, method: ast.AST, members: frozenset[str]) -> list[str]:
    """The names the method uses on `self` that the double does not supply (or not callably, where it calls them)."""
    calls = _self_calls(method)
    return [
        needed
        for needed in sorted(_self_uses(method) & members)
        if not (have.get(needed, False) if needed in calls else needed in have)
    ]


def _scanned_files(root: Path) -> list[Path]:
    """Every Python module below `root` (nested folders, `conftest.py`, support modules), but not this one."""
    return [path for path in sorted(root.rglob("*.py")) if path != Path(__file__)]


def _findings(root: Path = TESTS) -> dict[tuple[str, str, str, str], str]:
    methods, members = _coordinator_methods(), _real_members()
    found: dict[tuple[str, str, str, str], str] = {}
    for path in _scanned_files(root):
        for (double, bound, needed), method in missing_bindings(
            path.read_text(encoding="utf-8"), methods, members
        ).items():
            found[(path.relative_to(root).as_posix(), double, bound, needed)] = method
    return found


def problems(found: dict[tuple[str, str, str, str], str], allowed: dict[tuple[str, str, str, str], str]) -> list[str]:
    """What the guard reports: findings that are not allowed, and allowed entries that no longer apply."""
    lines = [
        f"{file}: {double}.{bound} (ComexioCoordinator.{method}) calls self.{needed}, which the double does not "
        f"supply - bind it (`{needed} = ComexioCoordinator.{needed}`) or mock it, or list it in ALLOWED with the reason"
        for (file, double, bound, needed), method in sorted(found.items())
        if (file, double, bound, needed) not in allowed
    ]
    lines.extend(f"ALLOWED entry that no longer applies, remove it: {key}" for key in sorted(set(allowed) - set(found)))
    return lines


def test_every_double_binds_what_its_methods_call() -> None:
    assert not problems(_findings(), ALLOWED)


FAKE = "_Fake"
OTHER = "other"
RUN = "run"
HELPER = "helper"
PROPERTY = "other_property"
NO_TEST = "no test reaches it"

DOUBLE = """
class _Fake(SimpleNamespace):
    run = ComexioCoordinator.run
    helper = ComexioCoordinator.helper
"""

METHODS = {
    node.name: node
    for node in ast.parse(
        "class ComexioCoordinator:\n"
        "    def run(self):\n        self.helper()\n        self.other()\n        return self.data\n"
        "    def helper(self):\n        return self.other_property\n"
        "    def other(self):\n        pass\n"
    )
    .body[0]
    .body
}
MEMBERS = frozenset({RUN, HELPER, OTHER, PROPERTY})
FINDING = {(FAKE, RUN, OTHER): RUN, (FAKE, HELPER, PROPERTY): HELPER}
CALLED = {(FAKE, RUN, OTHER): RUN}  # `other` is called by `run`, so a literal does not do
ASSIGNED_LITERALS = "\ndef test_a(fake):\n    fake.other = None\n    fake.other_property = 1\n"


def test_detector_finds_a_method_the_double_lacks() -> None:
    assert missing_bindings(DOUBLE, METHODS, MEMBERS) == FINDING


@pytest.mark.parametrize(
    "supply",
    [
        "    other = ComexioCoordinator.other\n    other_property = None\n",
        "    def other(self): ...\n    other_property = None\n",
        "    other: object = AsyncMock()\n    other_property: int = 1\n",
        "\ndef test_a(fake):\n    fake.other = AsyncMock()\n    fake.other_property = 1\n",
        "\ndef test_a(fake):\n    fake.other: int = AsyncMock()\n    fake.other_property = 1\n",
        "\ndef test_a():\n    _Fake(other=AsyncMock(), other_property=2)\n",
        "\ndef test_a():\n    _coordinator(api, other=AsyncMock(), other_property=2)\n",
        "\ndef test_a(fake):\n    setattr(fake, 'other', AsyncMock())\n    setattr(fake, 'other_property', 2)\n",
        "\ndef test_a(fake):\n    patch.object(fake, 'other', create=True)\n"
        "    patch.object(fake, 'other_property', 2, create=True)\n",
        "\ndef test_a(fake, monkeypatch):\n    monkeypatch.setattr(fake, 'other', AsyncMock(), raising=False)\n"
        "    monkeypatch.setattr(fake, 'other_property', 2, raising=False)\n",
        "\ndef test_a(fake, monkeypatch):\n    monkeypatch.setattr(fake, 'other', AsyncMock(), False)\n"
        "    monkeypatch.setattr(fake, 'other_property', 2, False)\n",
        "\ndef test_a(fake):\n    patch.object(target=fake, attribute='other', new=AsyncMock(), create=True)\n"
        "    patch.object(fake, 'other_property', 2, None, True)\n",
    ],
    ids=[
        "bound",
        "defined",
        "annotated",
        "assigned",
        "annotated attribute",
        "keyword of the class",
        "keyword of a builder",
        "setattr",
        "patch.object with create",
        "monkeypatch.setattr with raising False",
        "monkeypatch.setattr with raising as a positional argument",
        "patch.object by keyword and with create as a positional argument",
    ],
)
def test_detector_accepts_every_way_to_supply_a_name(supply: str) -> None:
    assert missing_bindings(DOUBLE + supply, METHODS, MEMBERS) == {}


@pytest.mark.parametrize(
    "elsewhere",
    [
        "\ndef test_a():\n    Other(other=AsyncMock(), other_property=2)\n",
        "\ndef test_a():\n    make_fake_api(other=AsyncMock(), other_property=2)\n",
        "\ndef test_a(fake, name):\n    setattr(fake, name, 1)\n    patch.object(fake, name, 2, create=True)\n",
        # a patch of a name the object lacks fails by itself: it supplies nothing (a never started one even less)
        "\ndef test_a(fake):\n    patch.object(fake, 'other', AsyncMock())\n"
        "    patch.object(fake, 'other_property', 2)\n",
        "\ndef test_a(fake, monkeypatch):\n    monkeypatch.setattr(fake, 'other', AsyncMock())\n"
        "    monkeypatch.setattr(fake, 'other_property', 2)\n",
        # `+=` needs the attribute to exist already, it creates nothing
        "\ndef test_a(fake):\n    fake.other += 1\n    fake.other_property += 1\n",
        "\ndef test_a(fake, monkeypatch):\n    monkeypatch.setattr(fake, 'other', AsyncMock(), True)\n"
        "    monkeypatch.setattr(fake, 'other_property', 2, raising=True)\n",
        "\ndef test_a(fake):\n    patch.object(target=fake, attribute='other', new=AsyncMock(), create=False)\n"
        "    patch.object(fake, 'other_property', 2, None, False)\n",
    ],
    ids=[
        "keyword of a call that builds no double",
        "keyword of a helper that only says fake",
        "setattr with a computed name",
        "patch.object without create",
        "monkeypatch.setattr without raising False",
        "augmented assignment",
        "monkeypatch.setattr with raising True",
        "patch.object with create False",
    ],
)
def test_detector_does_not_take_a_foreign_keyword_a_computed_name_or_a_plain_patch_for_a_supply(
    elsewhere: str,
) -> None:
    assert missing_bindings(DOUBLE + elsewhere, METHODS, MEMBERS) == FINDING


@pytest.mark.parametrize(
    "supply",
    [
        "    other: object\n    other_property: int\n",
        "\ndef test_a(fake):\n    fake.other: object\n    fake.other_property: int\n",
    ],
    ids=["annotation in the class body", "annotation of an attribute"],
)
def test_a_bare_annotation_supplies_nothing(supply: str) -> None:
    assert missing_bindings(DOUBLE + supply, METHODS, MEMBERS) == FINDING


@pytest.mark.parametrize(
    "supply",
    [
        "    other = None\n    other_property = None\n",
        "    other: object = None\n    other_property: int = 1\n",
        ASSIGNED_LITERALS,
        "\ndef test_a():\n    _Fake(other=1, other_property=2)\n",
        "\ndef test_a(fake):\n    setattr(fake, 'other', 3)\n    setattr(fake, 'other_property', 2)\n",
        "    other = []\n    other_property = None\n",
        "\ndef test_a(fake):\n    fake.other = {}\n    fake.other_property = 1\n",
        "\ndef test_a(fake):\n    fake.other = -1\n    fake.other_property = 1\n",
        "\ndef test_a(fake):\n    patch.object(fake, 'other', new=None, create=True)\n"
        "    patch.object(fake, 'other_property', 2, create=True)\n",
        "    other = [helper_mock]\n    other_property = None\n",
        "\ndef test_a(fake, x):\n    fake.other = x + 1\n    fake.other_property = 1\n",
        "\ndef test_a(fake, x):\n    fake.other = f'{x}'\n    fake.other_property = 1\n",
        "\ndef test_a(fake, x):\n    fake.other = 1 if x else 2\n    fake.other_property = 1\n",
        "    other = AsyncMock()\n    other = None\n    other_property = None\n",
    ],
    ids=[
        "class body",
        "annotated",
        "assigned",
        "keyword",
        "setattr",
        "list",
        "dict",
        "negative number",
        "new keyword",
        "list of names",
        "sum",
        "f-string",
        "conditional of two literals",
        "literal after a callable in the class body",
    ],
)
def test_a_literal_does_not_supply_a_name_the_method_calls_but_does_one_it_only_reads(supply: str) -> None:
    assert missing_bindings(DOUBLE + supply, METHODS, MEMBERS) == CALLED


def test_a_literal_hides_no_callable_whichever_comes_first() -> None:
    # In the class body and later a literal in a test; and a literal in one test, a callable in another.
    method_then_literal = (
        DOUBLE + "    def other(self): ...\n    other_property = None\n\ndef test_a(fake):\n    fake.other = None\n"
    )
    literal_then_callable = DOUBLE + ASSIGNED_LITERALS + "\ndef test_b():\n    _coordinator(api, other=AsyncMock())\n"
    callable_in_a_branch = (
        DOUBLE + "\ndef test_a(fake, x):\n    fake.other = AsyncMock() if x else None\n    fake.other_property = 1\n"
    )
    literal_then_method = DOUBLE + "    other = None\n    def other(self): ...\n    other_property = None\n"

    assert missing_bindings(method_then_literal, METHODS, MEMBERS) == {}
    assert missing_bindings(literal_then_callable, METHODS, MEMBERS) == {}
    assert missing_bindings(callable_in_a_branch, METHODS, MEMBERS) == {}
    assert missing_bindings(literal_then_method, METHODS, MEMBERS) == {}


def test_a_keyword_at_the_call_of_one_double_does_not_supply_another() -> None:
    twin = DOUBLE.replace(FAKE, "_Twin")
    both = DOUBLE + twin + "\ndef test_a():\n    _Fake(other=AsyncMock(), other_property=2)\n"

    assert missing_bindings(both, METHODS, MEMBERS) == {("_Twin", RUN, OTHER): RUN, ("_Twin", HELPER, PROPERTY): HELPER}


def test_detector_follows_a_binding_through_an_alias_of_the_class() -> None:
    aliased = (
        "Alias = ComexioCoordinator\nclass _Fake(SimpleNamespace):\n    run = Alias.run\n    helper = Alias.helper\n"
    )
    imported = "from x import ComexioCoordinator as Real\nclass _Fake:\n    run = Real.run\n    helper = Real.helper\n"

    assert missing_bindings(aliased, METHODS, MEMBERS) == FINDING
    assert missing_bindings(imported, METHODS, MEMBERS) == FINDING


def test_detector_follows_a_binding_through_a_module_of_the_class() -> None:
    qualified = (
        "import x.coordinator as coordinator_module\nclass _Fake:\n"
        "    run = coordinator_module.ComexioCoordinator.run\n"
        "    helper = coordinator_module.ComexioCoordinator.helper\n"
    )
    aliased = (
        "import x.coordinator as coordinator_module\nReal = coordinator_module.ComexioCoordinator\nclass _Fake:\n"
        "    run = Real.run\n    helper = Real.helper\n"
    )

    assert missing_bindings(qualified, METHODS, MEMBERS) == FINDING
    assert missing_bindings(aliased, METHODS, MEMBERS) == FINDING


def test_two_doubles_of_one_name_keep_their_own_bindings() -> None:
    # The second one binds nothing that needs a supply; it must not wipe out what the first one binds.
    twice = DOUBLE + "\nclass _Fake:\n    other = ComexioCoordinator.other\n"

    assert missing_bindings(twice, METHODS, MEMBERS) == FINDING


def test_detector_binds_an_annotated_assignment_and_skips_an_inherited_method() -> None:
    annotated = "class _Fake:\n    run: object = ComexioCoordinator.run\n    helper = ComexioCoordinator.helper\n"
    inherited = "class _Fake:\n    async_request_refresh = ComexioCoordinator.async_request_refresh\n"

    assert missing_bindings(annotated, METHODS, MEMBERS) == FINDING
    assert missing_bindings(inherited, METHODS, MEMBERS) == {}


def test_real_members_has_methods_classmethods_and_properties_but_no_data() -> None:
    members = _real_members()
    classmethods = {
        name
        for name in dir(ComexioCoordinator)
        if isinstance(inspect.getattr_static(ComexioCoordinator, name), classmethod)
    }

    assert "async_reload_entry" in members
    assert "plan_run_states_available" in members  # a property
    assert classmethods <= members
    assert "data" not in members


def test_the_scan_sees_the_real_double_of_test_plan_run_states() -> None:
    # Else a refactor of the scan (another way to spell the binding, a renamed class) could leave it with no double
    # to check, and the guard would pass on nothing.
    binding = "    webio_source_data = ComexioCoordinator.webio_source_data\n"
    source = (TESTS / "test_plan_run_states.py").read_text(encoding="utf-8")
    assert binding in source

    found = missing_bindings(source.replace(binding, ""), _coordinator_methods(), _real_members())

    assert ("_FakeCoordinator", "webhook_command_ids", "webio_source_data") in found


def test_the_scan_reaches_a_test_module_in_a_nested_folder(tmp_path: Path) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "test_nested.py").write_text(
        "class _Fake:\n    webhook_command_ids = ComexioCoordinator.webhook_command_ids\n", encoding="utf-8"
    )

    found = _findings(tmp_path)

    assert _scanned_files(tmp_path) == [nested / "test_nested.py"]
    assert ("sub/test_nested.py", "_Fake", "webhook_command_ids", "webio_source_data") in found


def test_the_scan_reaches_a_conftest_and_a_support_module(tmp_path: Path) -> None:
    double = "class _Fake:\n    webhook_command_ids = ComexioCoordinator.webhook_command_ids\n"
    for name in ("conftest.py", "support.py"):
        (tmp_path / name).write_text(double, encoding="utf-8")

    found = _findings(tmp_path)

    for file in ("conftest.py", "support.py"):
        assert (file, "_Fake", "webhook_command_ids", "webio_source_data") in found


def test_a_listed_finding_is_not_reported_and_a_stale_entry_is() -> None:
    key = ("test_x.py", FAKE, RUN, OTHER)

    assert problems({key: RUN}, {}) != []
    assert problems({key: RUN}, {key: NO_TEST}) == []
    assert len(problems({}, {key: NO_TEST})) == 1


def test_detector_ignores_classes_without_bound_methods() -> None:
    assert missing_bindings("class _Plain:\n    run = 1\n", METHODS, MEMBERS) == {}
